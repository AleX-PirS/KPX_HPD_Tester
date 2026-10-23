"""Independent measured edges, without reflecting or joining missing branches."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from .models import AnalysisSettings
from .parallel import map_analysis_groups
from .storage import atomic_write_table


CONTEXT = ("stage", "window", "column", "row", "pulse_amplitude_native",
           "injection_pattern", "injection_group_id", "measurement_fclk_mhz",
           "gain_sweep_code", "local_gain_code", "local_trim_code")


def _boolean(values):
    return values.astype(str).str.strip().str.lower().isin(("true", "1", "yes"))


def _number(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return math.nan


def _fit_job(job):
    from .analysis import _fit_scurve_group
    group, baseline, calibration, settings, operator_validated = job
    row = {key: group.iloc[0][key] for key in CONTEXT}
    row.update(baseline_off_v=baseline, uncertainty_kind="descriptive_fit_covariance",
               counter_operator_validated=operator_validated)
    both = True
    for branch, sign in (("negative", -1), ("positive", 1)):
        side = group[(group.threshold_voltage_v-baseline)*sign > 0].copy()
        fit_input = side.copy()
        fit_input["background_corrected_detected"] = side.signal_count
        fit_input["injections_for_analysis"] = side.nominal_injections
        result = _fit_scurve_group(fit_input, calibration, settings)
        expected = "ascending" if sign < 0 else "descending"
        if result["fit_status"] == "ok" and result["transition_direction"] != expected:
            result["fit_status"] = "unexpected_edge_direction"
        row.update({branch+"_"+key: value for key, value in result.items()})
        both &= result["fit_status"] == "ok"
    row["two_edges_status"] = "resolved" if both else "not_resolved"
    row["physical_interpretation"] = (
        "conditional_on_verified_counter_operator" if operator_validated and both
        else "count_response_only")
    plus = _number(row.get("positive_v50_v"))
    minus = _number(row.get("negative_v50_v"))
    half = (plus-minus)/2 if both and plus > minus else math.nan
    row["threshold_half_span_v"] = half
    row["positive_amplitude_relative_off_v"] = plus-baseline if both else math.nan
    row["negative_amplitude_relative_off_v"] = baseline-minus if both else math.nan
    row["asymmetry_relative_off"] = (plus+minus-2*baseline)/(2*half) if half > 0 else math.nan
    charge = pd.to_numeric(group.injection_charge_electrons, errors="coerce").dropna().unique()
    row["injection_charge_electrons"] = float(charge[0]) if len(charge) == 1 else math.nan
    # This is a declared nominal A/Q calculation, not a claim about measured
    # equality of the two edge charges or an independently measured active base.
    q = row["injection_charge_electrons"]
    row["nominal_half_span_gain_mv_per_ke"] = 1e6*half/q if q > 0 else math.nan
    row["gain_interpretation"] = "nominal_equal_charge_magnitudes; active_base_not_measured"
    return row


def analyze_bipolar(raw, noise_fits, *, calibration, settings=None,
                    max_background_fraction=.01, counter_operator_validated=False):
    """Return all measured points and separately fitted outer transitions.

    A paired background is required for fit eligibility. Missing background,
    invalid counter reads and saturation never become zero-count observations.
    Central noisy points are retained in the output and plots, outside the fits.
    """
    from .analysis import _as_bool
    style = settings or AnalysisSettings()
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    frame = raw.copy()
    for key in CONTEXT:
        if key not in frame:
            frame[key] = "" if key not in {"column", "row", "local_trim_code"} else np.nan
    for key in ("selected_count", "threshold_voltage_v", "threshold_dac_code",
                "injections_for_analysis", "injection_charge_electrons"):
        if key not in frame:
            frame[key] = np.nan
        frame[key] = pd.to_numeric(frame[key], errors="coerce")
    valid = np.isfinite(frame.selected_count) & frame.selected_count.ge(0)
    for key in ("measurement_valid", "counter_decode_valid"):
        if key in frame:
            valid &= _as_bool(frame[key])
    if "counter_saturated" in frame:
        valid &= ~_as_bool(frame.counter_saturated)
    frame["read_valid"] = valid
    if "active_injection_pixel" in frame:
        frame = frame[_as_bool(frame.active_injection_pixel)].copy()
    if "pair_id" not in frame:
        frame["pair_id"] = ""
    signals = frame[frame.acquisition_type.eq("signal")].copy()
    backgrounds = frame[frame.acquisition_type.eq("background")].copy()
    pair_keys = ["pair_id", "column", "row"]
    backgrounds = backgrounds[backgrounds.pair_id.astype(str).ne("")]
    if backgrounds.duplicated(pair_keys).any():
        raise ValueError("duplicate background pair identity in bipolar raw")
    background_table = backgrounds[pair_keys+["selected_count", "read_valid"]].rename(
        columns={"selected_count": "background_count", "read_valid": "background_read_valid"})
    signals = signals.merge(background_table, on=pair_keys, how="left", validate="many_to_one")
    signals["fit_valid"] = (signals.read_valid
        & signals.background_read_valid.eq(True)
        & signals.injections_for_analysis.gt(0)
        & signals.background_count.le(max_background_fraction*signals.injections_for_analysis))
    # Do not subtract off-state noise as if it were the on-state process.
    signals["signal_count"] = signals.selected_count
    signals["nominal_injections"] = signals.injections_for_analysis
    signals["count_over_nominal_n"] = signals.signal_count/signals.nominal_injections
    signals["valid_signal_count"] = signals.signal_count.where(signals.read_valid)
    point_keys = list(CONTEXT)+["threshold_dac_code", "threshold_voltage_v"]
    points = signals.groupby(point_keys, dropna=False, sort=False).agg(
        signal_count=("valid_signal_count", "mean"),
        background_count=("background_count", "mean"),
        nominal_injections=("nominal_injections", "mean"),
        fit_eligible_repeats=("fit_valid", "sum"), repeats=("read_valid", "sum"),
        attempted_repeats=("read_valid", "size"),
        injection_charge_electrons=("injection_charge_electrons", "first"),
    ).reset_index()
    points["count_over_nominal_n"] = points.signal_count/points.nominal_injections
    points["fit_valid"] = points.fit_eligible_repeats.eq(points.attempted_repeats) & points.repeats.gt(0)
    groups = signals.groupby(list(CONTEXT), dropna=False, sort=False)
    def jobs():
        for _, group in groups:
            baseline = math.nan
            if not noise_fits.empty:
                n = noise_fits[(noise_fits.column == group.iloc[0].column)
                              & (noise_fits.row == group.iloc[0].row)]
                if "fit_status" in n:
                    n = n[n.fit_status.eq("ok")]
                for key in ("gain_sweep_code", "measurement_fclk_mhz"):
                    if key in n and key in group and pd.notna(group.iloc[0][key]) and str(group.iloc[0][key]) != "":
                        n = n[pd.to_numeric(n[key],errors="coerce").eq(_number(group.iloc[0][key]))]
                if "local_trim_code" in n:
                    n = n[pd.to_numeric(n.local_trim_code, errors="coerce").eq(
                        _number(group.iloc[0].local_trim_code))]
                for stage in ("equalized_final", "baseline_noise", "trim_16"):
                    chosen = n[n.stage.eq(stage)] if "stage" in n else n
                    if not chosen.empty:
                        baseline = _number(chosen.iloc[0].get("center_selected_v"))
                        break
            yield (group, baseline, calibration, style, counter_operator_validated)
    rows = map_analysis_groups(_fit_job, jobs, total=groups.ngroups,
                               settings=style, label="Bipolar edges")
    return points, pd.DataFrame(rows)


def save_bipolar_plots(points, metrics, directory, settings):
    """One complete measured response per pixel/context, three windows in a row."""
    import matplotlib.pyplot as plt
    if points.empty:
        return {}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ru = settings.plot_language == "ru"
    pixels = list(settings.plot_pixels) or list(
        dict.fromkeys(zip(points.column.astype(int), points.row.astype(int)))
    )[:settings.representative_pixels]
    # Do not average different injections, gains, phases or clocks in one curve.
    contexts = [key for key in CONTEXT if key not in {"window", "column", "row", "local_trim_code"}]
    outputs = {}
    for index, (_, subset) in enumerate(points.groupby(contexts, dropna=False, sort=False)):
        windows = sorted(subset.window.unique())
        for column, row in pixels:
            pixel = subset[(subset.column == column) & (subset.row == row)]
            if pixel.empty:
                continue
            fig, axes = plt.subplots(2, len(windows), squeeze=False,
                                     figsize=(4.5*len(windows), 6.2))
            for j, window in enumerate(windows):
                data = pixel[pixel.window.eq(window)].sort_values("threshold_voltage_v")
                top, bottom = axes[0,j], axes[1,j]
                x = data.threshold_voltage_v.to_numpy(float)
                top.plot(x, data.signal_count, color="#0072B2", label="Сигнал" if ru else "Signal")
                top.plot(x, data.background_count, color="#D55E00", label="Фон" if ru else "Background")
                top.set_title(window)
                top.set_ylabel("Счет" if ru else "Counts")
                top.legend()
                good = data[data.fit_valid]
                bottom.plot(good.threshold_voltage_v, good.count_over_nominal_n,
                            ".", color="#0072B2", ms=3)
                bottom.set(xlabel="Порог, В" if ru else "Threshold, V",
                           ylabel="Счет / номинальное N" if ru else "Counts / nominal N")
                # All measured points remain on the top panel, including the
                # noise lobe and unresolved tails. No branch reflection occurs.
                bottom.axhline(.5, color="#999999", lw=.7)
                if not metrics.empty and not data.empty:
                    matched = metrics[(metrics.column==column)&(metrics.row==row)&(metrics.window==window)]
                    for key in contexts:
                        value = data.iloc[0][key]
                        matched = matched[matched[key].isna() if pd.isna(value) else matched[key].eq(value)]
                    if len(matched)==1:
                        fit = matched.iloc[0]
                        for branch, color in (("negative","#009E73"),("positive","#CC79A7")):
                            if fit.get(branch+"_fit_status")=="ok":
                                bottom.axvline(fit[branch+"_v50_v"], color=color, ls="--", lw=.9)
                        label = ({"resolved":"Оба края разрешены", "not_resolved":"Переходы не разрешены"}
                                 .get(fit.two_edges_status,fit.two_edges_status) if ru else fit.two_edges_status)
                        bottom.text(.02,.97,str(label),ha="left",va="top",
                                    transform=bottom.transAxes,fontsize=8)
                for ax in (top, bottom):
                    ax.grid(alpha=.18)
            fig.suptitle(("Полный измеренный отклик" if ru else "Complete measured response")
                         + f": C{column} R{row}")
            fig.tight_layout()
            stem = f"full_response_{index:03d}_c{column:02d}_r{row:02d}"
            paths = []
            for extension in ("png", "pdf") if settings.save_pdf_plots else ("png",):
                dest = directory/(stem+"."+extension)
                fig.savefig(dest, dpi=settings.plot_dpi, bbox_inches="tight")
                paths.append(dest)
            plt.close(fig)
            outputs[stem] = paths
    return outputs
