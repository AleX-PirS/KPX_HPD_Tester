"""Measured GAIN response and offline per-pixel gain equalization proposals.

No unmeasured GAIN code is interpolated or programmed.  A mixed-code matrix
must be measured again before its predicted equalization is called verified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from numbers import Integral
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from .models import AnalysisSettings, FRAMEWORK_VERSION
from .storage import atomic_write_json, atomic_write_table, atomic_write_text
from .storage import file_sha256, utc_now_text


CONTEXT = ["window", "measurement_fclk_mhz", "injection_pattern"]
PIXEL_KEY = CONTEXT + ["gain_sweep_code", "column", "row"]


@dataclass
class GainEqualizationSettings:
    target_statistic: str = "median"
    amplitude_weight: float = 1.0
    gain_weight: float = 1.0
    minimum_gain_fit_r2: float = 0.98
    include_poor_fits: bool = False
    allow_shared_noise_baseline: bool = True

    def validate(self) -> None:
        if self.target_statistic not in {"median", "mean"}:
            raise ValueError("target_statistic must be 'median' or 'mean'")
        for name in ("amplitude_weight", "gain_weight"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.amplitude_weight <= 0:
            raise ValueError("amplitude_weight must be positive")
        if not 0 <= self.minimum_gain_fit_r2 <= 1:
            raise ValueError("minimum_gain_fit_r2 must be in 0..1")


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    for column, default in (
        ("window", "unknown"), ("measurement_fclk_mhz", -1.0),
        ("injection_pattern", "all"), ("gain_sweep_code", np.nan),
    ):
        if column not in data:
            data[column] = default
    data["window"] = data["window"].astype(str).str.upper()
    data["measurement_fclk_mhz"] = pd.to_numeric(
        data["measurement_fclk_mhz"], errors="coerce"
    ).fillna(-1.0)
    data["gain_sweep_code"] = pd.to_numeric(
        data["gain_sweep_code"], errors="coerce"
    )
    return data


def _key(record: dict, columns: list[str]) -> tuple:
    return tuple(record.get(column) for column in columns)


def build_gain_sweep_metrics(
    scurve_results: pd.DataFrame,
    noise_fits: pd.DataFrame,
    gain_results: pd.DataFrame,
    *,
    settings: GainEqualizationSettings | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separate V50, effective baseline, pulse amplitude and nominal gain.

    A multi-step intercept is preferred for the baseline. A/Q is computed for
    every valid point, including a single injection step. A shared noise-center
    fallback explicitly assumes a GAIN-invariant baseline. A/Q is not the
    independent multi-charge slope; negative amplitudes are not abs'd.
    """

    selected = settings or GainEqualizationSettings()
    selected.validate()
    if scurve_results.empty:
        return pd.DataFrame(), pd.DataFrame()
    data = _normalize(scurve_results)
    data = data[data["gain_sweep_code"].notna()].copy()
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    gains = _normalize(gain_results) if not gain_results.empty else pd.DataFrame()
    if not gains.empty and "window" not in gain_results and data["window"].nunique() == 1:
        gains["window"] = data.iloc[0]["window"]
    gain_lookup = {
        _key(record, PIXEL_KEY): record
        for record in gains.to_dict("records")
    }
    noise_lookup: dict[tuple, list[dict]] = {}
    stage_rank = {
        "equalized_final": 0, "baseline_noise": 1, "trim_16": 2,
        "trim_00": 3, "trim_31": 4,
    }
    if not noise_fits.empty:
        noise = _normalize(noise_fits)
        noise["baseline_stage_rank"] = noise.get(
            "stage", pd.Series("unknown", index=noise.index)
        ).map(stage_rank).fillna(5)
        for record in noise.sort_values("baseline_stage_rank").to_dict("records"):
            if math.isfinite(_number(record.get("center_selected_v"))):
                key = _key(record, ["window", "column", "row"])
                noise_lookup.setdefault(key, []).append(record)
    rows: list[dict[str, Any]] = []
    acceptable = {"ok", "poor_quality"} if selected.include_poor_fits else {"ok"}
    for record in data.to_dict("records"):
        gain = gain_lookup.get(_key(record, PIXEL_KEY), {})
        slope_valid = (
            _number(gain.get("amplitude_point_count")) >= 3
            and _number(gain.get("fit_r2")) >= selected.minimum_gain_fit_r2
            and _number(gain.get("v50_slope_v_per_injection_step_v")) > 0
            and math.isfinite(_number(gain.get("v50_slope_v_per_injection_step_v")))
            and math.isfinite(_number(gain.get("v50_intercept_v")))
        )
        baseline = _number(gain.get("v50_intercept_v")) if slope_valid else np.nan
        baseline_source = "scurve_zero_charge_intercept" if slope_valid else "unavailable"
        if not math.isfinite(baseline):
            candidates = noise_lookup.get(
                _key(record, ["window", "column", "row"]), []
            )
            # Never borrow a center measured at a different comparator trim.
            trim = _number(record.get("local_trim_code"))
            # A gain-specific center has priority over any shared reference,
            # even when the latter is from a later equalization stage.
            candidates = sorted(candidates, key=lambda candidate: (
                not math.isfinite(_number(candidate.get("gain_sweep_code"))),
                candidate.get("baseline_stage_rank", 5),
            ))
            for candidate in candidates:
                noise_gain = _number(candidate.get("gain_sweep_code"))
                noise_trim = _number(candidate.get("local_trim_code"))
                noise_clock = _number(candidate.get("measurement_fclk_mhz"))
                if noise_clock >= 0 and noise_clock != record["measurement_fclk_mhz"]:
                    continue
                if math.isfinite(trim) and math.isfinite(noise_trim) and trim != noise_trim:
                    continue
                if math.isfinite(noise_gain):
                    if noise_gain != record["gain_sweep_code"]:
                        continue
                    source = "gain_matched_noise_center"
                elif selected.allow_shared_noise_baseline:
                    source = "shared_noise_center_assumed_gain_invariant"
                else:
                    continue
                baseline = _number(candidate.get("center_selected_v"))
                baseline_source = source
                break
        v50 = _number(record.get("v50_v"))
        step = _number(record.get("injection_voltage_step_v"))
        charge = _number(record.get("injection_charge_electrons"))
        amplitude = v50 - baseline
        fit_ok = str(record.get("fit_status")) in acceptable
        valid_step = math.isfinite(step) and step > 0
        valid_charge = math.isfinite(charge) and charge > 0
        valid = fit_ok and math.isfinite(amplitude) and amplitude > 0 and valid_step
        secant_voltage_gain = amplitude / step if valid else np.nan
        secant_charge_gain = amplitude * 1e6 / charge if valid and valid_charge else np.nan
        slope_response_gain = (
            _number(gain.get("v50_slope_v_per_injection_step_v"))
            if slope_valid else np.nan
        )
        slope_nominal_gain = (
            _number(gain.get("nominal_gain_mv_per_ke"))
            if slope_valid else np.nan
        )
        reason = (
            "ok" if valid else "scurve_fit_not_accepted" if not fit_ok
            else "baseline_unavailable" if not math.isfinite(baseline)
            else "injection_step_unavailable" if not valid_step
            else "nonpositive_amplitude_check_baseline_and_polarity"
        )
        rows.append({
            **{column: record.get(column) for column in PIXEL_KEY},
            "local_trim_code": record.get("local_trim_code"),
            "stage": record.get("stage"),
            "injection_voltage_step_v": step,
            "injection_charge_electrons": charge,
            "injection_charge_status": record.get("injection_charge_status", "unknown"),
            "charge_gain_available": valid and valid_charge,
            "v50_v": v50, "baseline_v": baseline,
            "baseline_source": baseline_source,
            "effective_amplitude_v": amplitude,
            "amplitude_mv": 1000.0 * amplitude,
            "secant_gain_v_per_injection_step_v": secant_voltage_gain,
            "secant_nominal_gain_mv_per_ke": secant_charge_gain,
            "gain_v_per_injection_step_v": secant_voltage_gain,
            "nominal_gain_mv_per_ke": secant_charge_gain,
            "gain_method": "amplitude_over_charge",
            "slope_gain_available": slope_valid,
            "slope_gain_v_per_injection_step_v": slope_response_gain,
            "slope_nominal_gain_mv_per_ke": slope_nominal_gain,
            "gain_fit_r2": _number(gain.get("fit_r2")),
            "scurve_fit_status": record.get("fit_status"),
            "amplitude_valid": valid, "metric_status": reason,
            "baseline_gain_invariance_assumed": baseline_source.startswith("shared_"),
        })
    pixels = pd.DataFrame(rows)
    summaries = []
    for keys, group in pixels.groupby(
        CONTEXT + ["gain_sweep_code", "injection_voltage_step_v"],
        dropna=False, sort=True,
    ):
        summary = dict(zip(CONTEXT + ["gain_sweep_code", "injection_voltage_step_v"], keys))
        valid = group[group["amplitude_valid"]]
        summary.update(pixel_count=len(group), valid_pixel_count=len(valid))
        summary["shared_baseline_pixel_count"] = int(
            valid["baseline_gain_invariance_assumed"].sum()
        )
        for column in ("baseline_v", "amplitude_mv", "gain_v_per_injection_step_v", "nominal_gain_mv_per_ke"):
            values = pd.to_numeric(valid[column], errors="coerce").dropna()
            for statistic in ("mean", "median", "std"):
                summary[f"{column}_{statistic}"] = getattr(values, statistic)() if len(values) else np.nan
            summary[f"{column}_q10"] = values.quantile(.10) if len(values) else np.nan
            summary[f"{column}_q90"] = values.quantile(.90) if len(values) else np.nan
        summaries.append(summary)
    return pixels, pd.DataFrame(summaries)


def _target_codes(values: Sequence[int]) -> tuple[int, ...]:
    codes = []
    for value in values:
        if not isinstance(value, Integral) or isinstance(value, bool) or not 0 <= value <= 31:
            raise ValueError("TARGET_GAIN items must be integers in 0..31")
        if int(value) not in codes:
            codes.append(int(value))
    if not codes:
        raise ValueError("TARGET_GAIN must not be empty")
    return tuple(codes)


def validate_gain_targets(results: pd.DataFrame, target_gains: Sequence[int]) -> tuple[int, ...]:
    """Preflight every requested context before any derived files are created."""

    targets = _target_codes(target_gains)
    data = _normalize(results)
    if data.empty or data["gain_sweep_code"].notna().sum() == 0:
        raise ValueError("source has no measured uniform GAIN sweep")
    measured_codes = data["gain_sweep_code"].dropna()
    if not ((measured_codes >= 0) & (measured_codes <= 31)
            & (measured_codes == measured_codes.round())).all():
        raise ValueError("measured GAIN codes must be integers in 0..31")
    failures = []
    for context, group in data.groupby(CONTEXT, dropna=False, sort=True):
        measured = set(group["gain_sweep_code"].dropna().astype(int))
        missing = sorted(set(targets) - measured)
        if missing:
            failures.append(f"{context}: missing TARGET_GAIN={missing}; measured={sorted(measured)}")
    if failures:
        raise ValueError("gain equalization not started: " + "; ".join(failures))
    return targets


def propose_gain_equalization(
    pixel_metrics: pd.DataFrame,
    target_gains: Sequence[int],
    *,
    settings: GainEqualizationSettings | None = None,
    pixel_roster: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Joint measured-amplitude/slope objective, independently per context.

    Every target step must be available for a candidate.  A/Q is redundant
    with amplitude at fixed Q, so it is not counted as an independent slope
    term.  Missing pixels keep TARGET_GAIN and are explicitly unresolved.
    """

    selected = settings or GainEqualizationSettings()
    selected.validate()
    targets = validate_gain_targets(pixel_metrics, target_gains)
    data = _normalize(pixel_metrics)
    for column in ("amplitude_valid", "slope_gain_available"):
        data[column] = data[column].astype(str).str.lower().isin(("true", "1"))
    if data.duplicated(PIXEL_KEY + ["injection_voltage_step_v"]).any():
        raise ValueError("duplicate gain/pixel/step rows; do not mix distinct acquisition conditions")
    all_maps, all_predictions, all_targets, all_summaries = [], [], [], []
    for context, group in data.groupby(CONTEXT, dropna=False, sort=True):
        context_record = dict(zip(CONTEXT, context))
        roster = group[["column", "row"]].drop_duplicates()
        if pixel_roster is not None and not pixel_roster.empty:
            extra = pixel_roster
            if "window" in extra:
                extra = extra[extra["window"].astype(str).str.upper() == context_record["window"]]
            roster = pd.concat([roster, extra[["column", "row"]]]).drop_duplicates()
        for target_code in targets:
            uniform = group[group["gain_sweep_code"] == target_code]
            target_rows = []
            for step, at_step in uniform.groupby("injection_voltage_step_v", sort=True):
                usable = at_step[at_step["amplitude_valid"]]
                if usable.empty:
                    raise ValueError(f"no accepted target amplitudes: {context}, TARGET_GAIN={target_code}, step={step}")
                amplitude = getattr(usable["effective_amplitude_v"], selected.target_statistic)()
                charge_values = usable["injection_charge_electrons"]
                charge_values = charge_values[np.isfinite(charge_values) & (charge_values > 0)]
                if len(charge_values) and not np.allclose(charge_values, charge_values.iloc[0], rtol=1e-9, atol=1e-6):
                    raise ValueError("inconsistent injected charge within a target step")
                target_rows.append({
                    **context_record, "target_gain_code": target_code,
                    "injection_voltage_step_v": step,
                    "target_amplitude_v": float(amplitude),
                    "target_secant_gain_v_per_injection_step_v": float(amplitude) / float(step),
                    "target_nominal_gain_mv_per_ke": (
                        float(getattr(usable["nominal_gain_mv_per_ke"].dropna(), selected.target_statistic)())
                        if usable["nominal_gain_mv_per_ke"].notna().any() else np.nan
                    ),
                    "target_charge_electrons": float(charge_values.iloc[0]) if len(charge_values) else np.nan,
                    "target_population": int(len(usable)),
                    "target_statistic": selected.target_statistic,
                })
            if not target_rows:
                raise ValueError(f"no calibrated injection steps for TARGET_GAIN={target_code}")
            target_table = pd.DataFrame(target_rows).set_index("injection_voltage_step_v")
            slope_population = uniform[
                uniform["amplitude_valid"] & uniform["slope_gain_available"]
            ].drop_duplicates(["column", "row"])
            target_slope = (
                float(getattr(slope_population["slope_gain_v_per_injection_step_v"], selected.target_statistic)())
                if not slope_population.empty else np.nan
            )
            use_slope = math.isfinite(target_slope) and selected.gain_weight > 0
            for record in target_rows:
                record["target_slope_v_per_injection_step_v"] = target_slope
                record["objective"] = "amplitude_and_slope" if use_slope else "amplitude_secant_equivalent"
            all_targets.extend(target_rows)
            maps, predictions = [], []
            for column, row in roster.sort_values(["column", "row"]).itertuples(index=False, name=None):
                pixel = group[(group["column"] == column) & (group["row"] == row)]
                candidates = []
                for code, at_code in pixel.groupby("gain_sweep_code", sort=True):
                    measured = at_code.set_index("injection_voltage_step_v")
                    if not set(target_table.index).issubset(measured.index):
                        continue
                    measured = measured.loc[target_table.index]
                    if not measured["amplitude_valid"].all():
                        continue
                    charge = measured["injection_charge_electrons"].to_numpy(dtype=float)
                    target_charge = target_table["target_charge_electrons"].to_numpy(dtype=float)
                    known_charge = np.isfinite(target_charge)
                    if known_charge.any() and not np.allclose(charge[known_charge], target_charge[known_charge], rtol=1e-9, atol=1e-6):
                        continue
                    target_amplitudes = target_table["target_amplitude_v"].to_numpy(dtype=float)
                    measured_amplitudes = measured["effective_amplitude_v"].to_numpy(dtype=float)
                    residual = (measured_amplitudes - target_amplitudes) / target_amplitudes
                    amplitude_score = float(np.mean(residual**2))
                    slope_error = 0.0
                    if use_slope:
                        if not measured["slope_gain_available"].all():
                            continue
                        slope_error = (float(measured.iloc[0]["slope_gain_v_per_injection_step_v"]) - target_slope) / target_slope
                    score = selected.amplitude_weight * amplitude_score + selected.gain_weight * slope_error**2
                    candidates.append((score, abs(int(code) - target_code), int(code), measured))
                winner = None
                if candidates:
                    best_score = min(item[0] for item in candidates)
                    tied = [item for item in candidates if math.isclose(item[0], best_score, rel_tol=1e-9, abs_tol=1e-15)]
                    winner = min(tied, key=lambda item: item[1:3])
                chosen_code = winner[2] if winner else target_code
                maps.append({
                    **context_record, "target_gain_code": target_code,
                    "column": int(column), "row": int(row), "gain": chosen_code,
                    "changed": chosen_code != target_code,
                    "recommendation_status": "measured_code_proposal" if winner else "unresolved_preserve_target",
                    "objective_score": winner[0] if winner else np.nan,
                    "candidate_code_count": len(candidates),
                    "verified_in_mixed_gain_matrix": False,
                })
                if winner:
                    before = pixel[pixel["gain_sweep_code"] == target_code].set_index("injection_voltage_step_v")
                    for step, measured in winner[3].iterrows():
                        before_row = before.loc[step] if step in before.index else pd.Series(dtype=object)
                        before_ok = str(before_row.get("amplitude_valid")).lower() in ("true", "1")
                        target_amplitude = float(target_table.loc[step, "target_amplitude_v"])
                        predictions.append({
                            **context_record, "target_gain_code": target_code,
                            "column": int(column), "row": int(row), "gain": chosen_code,
                            "injection_voltage_step_v": step,
                            "target_amplitude_v": target_amplitude,
                            "target_gain_v_per_injection_step_v": target_table.loc[step, "target_secant_gain_v_per_injection_step_v"],
                            "before_amplitude_v": _number(before_row.get("effective_amplitude_v")) if before_ok else np.nan,
                            "predicted_amplitude_v": measured["effective_amplitude_v"],
                            "predicted_amplitude_residual_v": measured["effective_amplitude_v"] - target_amplitude,
                            "before_baseline_v": _number(before_row.get("baseline_v")),
                            "predicted_baseline_v": measured["baseline_v"],
                            "before_gain_v_per_injection_step_v": _number(before_row.get("gain_v_per_injection_step_v")) if before_ok else np.nan,
                            "before_nominal_gain_mv_per_ke": _number(before_row.get("nominal_gain_mv_per_ke")) if before_ok else np.nan,
                            "predicted_gain_v_per_injection_step_v": measured["gain_v_per_injection_step_v"],
                            "predicted_nominal_gain_mv_per_ke": measured["nominal_gain_mv_per_ke"],
                            "target_nominal_gain_mv_per_ke": target_table.loc[step, "target_nominal_gain_mv_per_ke"],
                            "baseline_source": measured["baseline_source"],
                            "gain_method": measured["gain_method"],
                            "predicted_slope_gain_v_per_injection_step_v": measured["slope_gain_v_per_injection_step_v"],
                            "predicted_slope_nominal_gain_mv_per_ke": measured["slope_nominal_gain_mv_per_ke"],
                            "target_slope_v_per_injection_step_v": target_slope,
                        })
            all_maps.extend(maps)
            all_predictions.extend(predictions)
            for step, at_step in pd.DataFrame(predictions).groupby("injection_voltage_step_v") if predictions else []:
                paired = at_step.dropna(subset=["before_amplitude_v", "predicted_amplitude_v"])
                summary = {
                    **context_record, "target_gain_code": target_code,
                    "injection_voltage_step_v": step, "pixel_count": len(maps),
                    "resolved_pixel_count": len(at_step), "paired_pixel_count": len(paired),
                    "changed_pixel_count": sum(item["changed"] for item in maps),
                    "unresolved_pixel_count": sum(item["recommendation_status"].startswith("unresolved") for item in maps),
                    "target_amplitude_v": float(target_table.loc[step, "target_amplitude_v"]),
                    "target_slope_v_per_injection_step_v": target_slope,
                }
                for prefix, name in (("before", "before_amplitude_v"), ("predicted", "predicted_amplitude_v")):
                    values = paired[name]
                    summary[f"{prefix}_paired_std_v"] = values.std() if len(values) >= 2 else np.nan
                    summary[f"{prefix}_paired_rms_to_target_v"] = float(np.sqrt(np.mean((values - summary["target_amplitude_v"])**2))) if len(values) else np.nan
                    summary[f"{prefix}_paired_abs_residual_q95_v"] = abs(values - summary["target_amplitude_v"]).quantile(.95) if len(values) else np.nan
                for prefix in ("before", "predicted"):
                    values = paired[f"{prefix}_nominal_gain_mv_per_ke"].dropna()
                    summary[f"{prefix}_paired_gain_mean_mv_per_ke"] = values.mean() if len(values) else np.nan
                    summary[f"{prefix}_paired_gain_std_mv_per_ke"] = values.std() if len(values) >= 2 else np.nan
                all_summaries.append(summary)
    return tuple(pd.DataFrame(rows) for rows in (all_maps, all_predictions, all_targets, all_summaries))


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _source_analysis(path: Path) -> tuple[Path, Path, dict]:
    source = path.resolve()
    if (source / "scurve_results.csv").is_file():
        analysis = source
        roots = [source] + list(source.parents)
        root = next((candidate for candidate in roots if (candidate / "metadata.json").is_file()), None)
        if root is None:
            raise FileNotFoundError("source analysis must belong to an experiment with metadata.json")
    else:
        root = source
        analyses = sorted((root / "analysis").glob("v[0-9]*"))
        analysis = next((candidate for candidate in reversed(analyses) if (candidate / "scurve_results.csv").is_file()), None)
        if analysis is None:
            raise FileNotFoundError(f"no saved S-curve analysis in {root}; run offline S-analysis first")
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    return root, analysis, metadata


def analyze_gain_sweep(
    source_experiment: str | Path,
    *,
    target_gains: Sequence[int],
    window: str = "ALL",
    equalization_settings: GainEqualizationSettings | None = None,
    settings: AnalysisSettings | None = None,
    output_root: str | Path | None = None,
    generate_plots: bool = True,
) -> dict[str, Any]:
    """Analyze an existing GAIN sweep, no hardware connection or acquisition.

    Accepts one experiment, one analysis/vNNN or a parent WINDOW=ALL experiment.
    Missing TARGET_GAIN anywhere aborts before output directories are created.
    """

    selected = equalization_settings or GainEqualizationSettings()
    selected.validate()
    plot_settings = settings or AnalysisSettings()
    plot_settings.validate()
    targets = _target_codes(target_gains)
    source = Path(source_experiment).resolve()
    parent_metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8")) if (source / "metadata.json").is_file() else {}
    sources = []
    requested_window = window.upper()
    if requested_window not in {"AB", "BC", "CD", "ALL"}:
        raise ValueError("window must be AB, BC, CD or ALL")
    if str(parent_metadata.get("window")).upper() == "ALL":
        for child_window, record in sorted(parent_metadata.get("window_runs", {}).items()):
            if requested_window not in {"ALL", child_window.upper()}:
                continue
            child = Path(record["experiment_path"])
            if not child.is_absolute():
                child = source / child
            if not child.exists():
                # Copied Windows experiment paths may be stale; only resolve
                # an exact saved folder basename beneath this parent.
                basename = str(record["experiment_path"]).replace("\\", "/").split("/")[-1]
                matches = list((source / "windows").glob(basename))
                if len(matches) == 1:
                    child = matches[0]
            sources.append(_source_analysis(child))
    else:
        sources.append(_source_analysis(source))
    if not sources:
        raise ValueError("no source windows match the requested window")
    loaded, roster_rows, lineage = [], [], []
    for root, analysis, metadata in sources:
        source_window = str(metadata.get("window", "unknown")).upper()
        if requested_window not in {"ALL", source_window}:
            raise ValueError(f"source window {source_window} does not match {requested_window}")
        results = _read_csv(analysis / "scurve_results.csv")
        if results.empty:
            raise ValueError(f"empty S-curve results: {analysis}")
        from .pixel_masks import exclude_bad_pixel_rows, normalize_bad_pixel_map
        excluded = set(normalize_bad_pixel_map(metadata.get("bad_pixel_mask")))
        results = exclude_bad_pixel_rows(results, tuple(excluded))
        results["window"] = source_window
        validate_gain_targets(results, targets)
        loaded.append((root, analysis, metadata, results))
        for pixel in metadata.get("pixel_selection", []):
            if (pixel["column"], pixel["row"]) not in excluded:
                roster_rows.append({"window": source_window, "column": pixel["column"], "row": pixel["row"]})
    pixel_tables, summaries = [], []
    for root, analysis, metadata, results in loaded:
        noise = _read_csv(analysis / "noise_fit_results.csv")
        reference = metadata.get("noise_reference", {}).get("statistics_copy")
        if noise.empty and reference:
            from .analysis import fit_noise_statistics
            noise = fit_noise_statistics(_read_csv(root / reference), settings=plot_settings)
        noise["window"] = str(metadata.get("window")).upper()
        gain_path = analysis / "scurve_pixel_gain_results.csv"
        gains = _read_csv(gain_path)
        if selected.include_poor_fits is False or gains.empty:
            from .analysis import fit_scurve_gain_results
            gains = fit_scurve_gain_results(results[results["fit_status"].eq("ok")] if not selected.include_poor_fits else results)
        gains["window"] = str(metadata.get("window")).upper()
        pixels, summary = build_gain_sweep_metrics(results, noise, gains, settings=selected)
        pixel_tables.append(pixels)
        summaries.append(summary)
        files = [analysis / "scurve_results.csv", analysis / "noise_fit_results.csv", gain_path, root / "metadata.json"]
        base_configuration = metadata.get("base_pixel_configuration", {}).get("normalized_selected_pixels_csv")
        if base_configuration:
            files.append(root / base_configuration)
        if reference:
            files.append(root / reference)
        lineage.extend({"path": str(file), "sha256": file_sha256(file)} for file in files if file.is_file())
    pixels = pd.concat(pixel_tables, ignore_index=True)
    summary = pd.concat(summaries, ignore_index=True)
    roster = pd.DataFrame(roster_rows)
    maps, predictions, target_table, equalization_summary = propose_gain_equalization(
        pixels, targets, settings=selected, pixel_roster=roster
    )
    parent = Path(output_root).resolve() if output_root is not None else source / "gain_equalization"
    version = 1
    while (parent / f"v{version:03d}").exists():
        version += 1
    directory = parent / f"v{version:03d}"
    outputs: dict[str, Any] = {"analysis_directory": directory, "gain_maps": {}}
    for name, table in (("gain_sweep_pixel_metrics", pixels), ("gain_sweep_summary", summary), ("gain_equalization_targets", target_table), ("gain_equalization_summary", equalization_summary), ("gain_equalization_predictions", predictions), ("gain_equalization_maps", maps)):
        outputs[name] = atomic_write_table(directory / f"{name}.csv", table)
    for keys, table in maps.groupby(CONTEXT + ["target_gain_code"], sort=True):
        context_record = dict(zip(CONTEXT, keys[:3]))
        target = int(keys[3])
        context_name = gain_context_name(context_record)
        path = directory / context_name / f"target_gain_{target:02d}" / "gain_map.csv"
        columns = ["column", "row", "gain"] + [name for name in table if name not in {"column", "row", "gain"}]
        outputs["gain_maps"][f"{context_name}/target_gain_{target:02d}"] = atomic_write_table(path, table[columns])
    atomic_write_json(directory / "gain_equalization_settings.json", {
        "created_utc": utc_now_text(), "framework_version": FRAMEWORK_VERSION,
        "source": str(source), "source_files": lineage, "target_gains": list(targets),
        "settings": asdict(selected), "plot_settings": asdict(plot_settings),
        "verified_in_mixed_gain_matrix": False,
        "unmeasured_codes_interpolated": False,
    })
    report = (
        "# Анализ и предложения эквализации GAIN\n\n"
        f"Источник: `{source}`. Статистика цели: `{selected.target_statistic}`.\n\n"
        "Цели взяты из измерений равномерного целевого GAIN-кода. "
        "Коды выбираются только среди измеренных, без предположения монотонности. "
        "Сравнение до/прогноз выполняется на одинаковых физических пикселях, "
        "раздельно для каждого окна, FCLK, режима инжекции и ступеньки.\n\n"
        "Карты являются предложениями, не проверенной смешанной конфигурацией ASIC. "
        "После их применения нужна повторная S-кривая: нагрузка и наводки могут измениться. "
        "У неопределенных пикселей сохраняется целевой код с явным статусом unresolved.\n\n"
        "Усиление вычисляется и при одной ступеньке: A=V50-baseline, "
        "G[мВ/кэ]=A[В]*10^6/Q[электроны]. A/Q и наклон V50(Q) сохраняются "
        "раздельно. При постоянном Q равенство амплитуд означает равенство A/Q, "
        "поэтому эти два эквивалентных остатка не учитываются дважды в критерии. "
        "При наличии пригодной многоточечной регрессии ее независимый наклон "
        "добавляется в критерий с настраиваемым весом.\n\n"
        "Общая noise-база предполагает независимость базы от GAIN и не доказывает ее. "
        "Это явно отмечено в baseline_source и baseline_gain_invariance_assumed. "
        "Измеренная noise-база при том же GAIN предпочтительна. При >=3 ступеньках "
        "с R2 выше установленного допуска база берется из свободного члена "
        "регрессии текущего GAIN. Номинальность Cinj/REF остается ограничением "
        "абсолютной калибровки заряда.\n\n"
        "## Разброс амплитуды на одинаковых пикселях\n\n"
        "| Окно | FCLK, МГц | Режим | TARGET_GAIN | REF, мВ | N | "
        "Изменены / неопределены | RMS до, мВ | RMS прогноз, мВ | "
        "q95 до, мВ | q95 прогноз, мВ |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )
    for row in equalization_summary.to_dict("records"):
        report += (
            f"| {row['window']} | {row['measurement_fclk_mhz']:g} | {row['injection_pattern']} "
            f"| {row['target_gain_code']} | {1000 * row['injection_voltage_step_v']:g} "
            f"| {row['paired_pixel_count']} | {row['changed_pixel_count']} / {row['unresolved_pixel_count']} "
            f"| {1000 * row['before_paired_rms_to_target_v']:.4g} "
            f"| {1000 * row['predicted_paired_rms_to_target_v']:.4g} "
            f"| {1000 * row['before_paired_abs_residual_q95_v']:.4g} "
            f"| {1000 * row['predicted_paired_abs_residual_q95_v']:.4g} |\n"
        )
    report += (
        "\nRMS=sqrt(mean((A-A_target)^2)); q95=95-й процентиль модуля остатка. "
        "Число неопределенных пикселей приводится отдельно, без их исключения из карт.\n\n"
        "## Выходные данные\n\n"
        "- [Покодовые метрики каждого пикселя](gain_sweep_pixel_metrics.csv).\n"
        "- [Матричные цели](gain_equalization_targets.csv).\n"
        "- [Амплитуда, база и усиление до/прогноз](gain_equalization_predictions.csv).\n"
        "- [Сводка разброса амплитуды и усиления](gain_equalization_summary.csv).\n"
        "- [Все предложенные коды и статусы](gain_equalization_maps.csv).\n\n"
        "Индивидуальные CSV для загрузки:\n\n"
    )
    for name, path in outputs["gain_maps"].items():
        report += f"- [{name}]({path.relative_to(directory).as_posix()}).\n"
    outputs["report"] = atomic_write_text(directory / "REPORT.md", report)
    if generate_plots:
        from .gain_sweep_plots import plot_gain_equalization, plot_gain_sweep
        outputs["plots"] = plot_gain_sweep(pixels, directory=directory / "plots" / "gain_sweep", settings=plot_settings)
        outputs["plots"].update(plot_gain_equalization(maps, predictions, directory=directory / "plots" / "gain_equalization", settings=plot_settings))
    return outputs


def gain_context_name(context: dict) -> str:
    clock = _number(context.get("measurement_fclk_mhz"))
    clock_name = f"{clock:g}" if math.isfinite(clock) and clock >= 0 else "unknown"
    pattern = str(context.get("injection_pattern", "all"))
    if pattern not in {"all", "tile_2x2", "tile_4x4", "tile_8x8"}:
        raise ValueError(f"unsupported injection pattern in GAIN analysis: {pattern}")
    return f"{context['window']}/fclk_{clock_name}_pattern_{pattern}"
