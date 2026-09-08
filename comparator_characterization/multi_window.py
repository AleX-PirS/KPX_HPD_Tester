from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from mgpd import MGPDClient
from pixel_matrix import OWNED_COLUMNS

from .analysis import analyze_saved_experiment
from .calibration import (
    ReferenceDacCalibration,
    ThresholdDacCalibration,
    load_reference_dac_calibrations,
    load_threshold_dac_calibrations,
    select_reference_dac_pairs,
)
from .hardware import (
    KeysightBurstSettings,
    KeysightBurstShotExecutor,
    MGPDMeasurementBackend,
    ShotExecutor,
    ShotRequest,
    UpoPwmShotExecutor,
)
from .injection import build_injection_groups, resolve_gain_map
from .models import (
    AnalysisSettings,
    CharacterizationSettings,
    FRAMEWORK_VERSION,
    get_window_spec,
    resolve_pixels,
)
from .pixel_masks import BadPixelMapInput, normalize_bad_pixel_map
from .storage import (
    ExperimentStore,
    atomic_write_json,
    atomic_write_table,
    atomic_write_text,
    utc_now_text,
)
from .workflow import CharacterizationResult, characterize_comparator


_WINDOWS = ("AB", "BC", "CD")
_COUNTER_COLUMNS = {
    "AB": ("counter_high_count", "counter_high_decode_valid", "counter_high_saturated"),
    "BC": ("counter_mid_count", "counter_mid_decode_valid", "counter_mid_saturated"),
    "CD": ("counter_low_count", "counter_low_decode_valid", "counter_low_saturated"),
}
_ELECTRON_CHARGE_C = 1.602176634e-19


@dataclass
class AllWindowSettings:
    """Settings used only by the AB+BC+CD combined characterization."""

    final_ref_sweep_enabled: bool = True
    final_ref_step_count: int = 100
    final_ref_repeats: int = 5
    final_ref_injection_pattern: str = "all"
    common_shift_z_threshold: float = 3.0
    common_shift_max_differential_z: float = 1.0
    comparator_outlier_z_threshold: float = 3.0
    good_fit_r2: float = 0.80

    def validate(self) -> None:
        if not isinstance(self.final_ref_sweep_enabled, bool):
            raise TypeError("final_ref_sweep_enabled must be bool")
        if (
            not isinstance(self.final_ref_step_count, int)
            or isinstance(self.final_ref_step_count, bool)
            or self.final_ref_step_count < 2
        ):
            raise ValueError("final_ref_step_count must be an integer >= 2")
        if (
            not isinstance(self.final_ref_repeats, int)
            or isinstance(self.final_ref_repeats, bool)
            or self.final_ref_repeats < 1
        ):
            raise ValueError("final_ref_repeats must be a positive integer")
        if self.final_ref_injection_pattern not in {
            "all", "tile_2x2", "tile_4x4", "tile_8x8"
        }:
            raise ValueError("unknown final_ref_injection_pattern")
        for name in (
            "common_shift_z_threshold",
            "common_shift_max_differential_z",
            "comparator_outlier_z_threshold",
        ):
            if not math.isfinite(float(getattr(self, name))) or float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 < float(self.good_fit_r2) <= 1:
            raise ValueError("good_fit_r2 must be in (0, 1]")


@dataclass(frozen=True)
class AllWindowExposureChange:
    parent_experiment_path: Path
    next_window: str
    noise_shutter_duration_s: float | None
    completed_windows: tuple[str, ...]


@dataclass(frozen=True)
class AllWindowCharacterizationResult:
    experiment_path: Path
    analysis_path: Path | None
    window_results: Mapping[str, CharacterizationResult]
    status: str


def interactive_all_window_noise_pause(change: AllWindowExposureChange) -> None:
    duration = (
        f"{change.noise_shutter_duration_s:g} с"
        if change.noise_shutter_duration_s is not None
        else "значение Noise scan из конфигурации"
    )
    message = (
        f"Перед окном {change.next_window} установите в УПО экспозицию "
        f"Noise scan {duration} и нажмите Enter. Общий эксперимент: "
        f"{change.parent_experiment_path}."
    )
    try:
        input(message + "\n")
    except EOFError as error:
        raise RuntimeError(
            "для WINDOW='ALL' требуется подтверждение экспозиции перед каждым окном"
        ) from error


def _latest_analysis(experiment: Path) -> Path:
    candidates = sorted((experiment / "analysis").glob("v[0-9][0-9][0-9]"))
    if not candidates:
        raise FileNotFoundError(f"analysis/vNNN not found in {experiment}")
    return candidates[-1]


def _result_from_saved_experiment(path: Path) -> CharacterizationResult:
    store = ExperimentStore(path)
    trim_map: dict[tuple[int, int], int] = {}
    trim_relative = store.metadata.get("final_trim_map")
    trim_path = store.root / str(trim_relative) if trim_relative else store.root / "final_trim_map.csv"
    if trim_path.exists():
        frame = pd.read_csv(trim_path)
        trim_column = "trim_code" if "trim_code" in frame else "local_trim_code"
        if trim_column in frame:
            trim_map = {
                (int(row["column"]), int(row["row"])): int(row[trim_column])
                for _, row in frame.iterrows()
            }
    target = None
    target_path = _latest_analysis(store.root) / "equalization_target.json"
    if target_path.exists():
        target = json.loads(target_path.read_text(encoding="utf-8")).get(
            "target_voltage_v"
        )
    return CharacterizationResult(
        experiment_path=store.root,
        analysis_path=_latest_analysis(store.root),
        target_voltage_v=float(target) if target is not None else None,
        trim_map=trim_map,
        status=str(store.metadata.get("status", "unknown")),
    )


def _window_records(parent: ExperimentStore) -> dict[str, dict[str, Any]]:
    records = parent.metadata.get("window_runs", {})
    return {str(key).upper(): dict(value) for key, value in records.items()}


def _discover_incomplete_child(parent: ExperimentStore, window: str) -> Path | None:
    directory = parent.root / "windows"
    if not directory.exists():
        return None
    candidates: list[Path] = []
    for path in sorted(directory.glob(f"*_{window}*")):
        metadata = path / "metadata.json"
        if not metadata.exists():
            continue
        try:
            document = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(document.get("window", "")).upper() == window:
            candidates.append(path)
    return candidates[-1].resolve() if candidates else None


def _resolve_child_path(parent: ExperimentStore, record: Mapping[str, Any]) -> Path:
    value = Path(str(record["experiment_path"]))
    return value.resolve() if value.is_absolute() else (parent.root / value).resolve()


def _save_figure(
    figure: plt.Figure,
    directory: Path,
    stem: str,
    settings: AnalysisSettings,
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / f"{stem}.png"]
    figure.savefig(paths[0], dpi=settings.plot_dpi, bbox_inches="tight")
    if settings.save_pdf_plots:
        paths.append(directory / f"{stem}.pdf")
        figure.savefig(paths[-1], bbox_inches="tight")
    plt.close(figure)
    return paths


def _aspect(settings: AnalysisSettings) -> str:
    return "equal" if settings.square_physical_pixels else "auto"


def _matrix(frame: pd.DataFrame, value: str) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if frame.empty or value not in frame:
        return None
    data = frame[["column", "row", value]].copy()
    data[value] = pd.to_numeric(data[value], errors="coerce")
    pivot = data.pivot_table(index="row", columns="column", values=value, aggfunc="median")
    columns = np.asarray(OWNED_COLUMNS, dtype=int)
    rows = np.arange(32, dtype=int)
    array = pivot.reindex(index=rows, columns=columns).to_numpy(dtype=float)
    if not np.isfinite(array).any():
        return None
    return array, columns, rows


def _robust_center_scale(values: pd.Series) -> tuple[float, float]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return float("nan"), float("nan")
    center = float(numeric.median())
    mad = float((numeric - center).abs().median())
    scale = 1.4826 * mad
    if not math.isfinite(scale) or scale <= 0:
        scale = float(numeric.std(ddof=1)) if len(numeric) > 1 else float("nan")
    return center, scale


def _safe_correlation(pair: pd.DataFrame, method: str) -> float:
    if len(pair) < 2:
        return float("nan")
    if pair.iloc[:, 0].nunique() < 2 or pair.iloc[:, 1].nunique() < 2:
        return float("nan")
    return float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method=method))


def _choose_one_condition(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    selected = frame.copy()
    if "injection_pattern" in selected:
        patterns = selected["injection_pattern"].astype(str)
        if (patterns == "all").any():
            selected = selected[patterns == "all"].copy()
    if "measurement_fclk_mhz" in selected:
        clocks = pd.to_numeric(selected["measurement_fclk_mhz"], errors="coerce")
        finite = sorted(clocks.dropna().unique())
        if finite:
            selected = selected[clocks == finite[0]].copy()
    return selected


def _extract_window_metrics(
    window: str,
    experiment: Path,
    analysis: Path,
    *,
    good_fit_r2: float,
) -> pd.DataFrame:
    gain = _choose_one_condition(
        pd.read_csv(analysis / "scurve_pixel_gain_results.csv")
        if (analysis / "scurve_pixel_gain_results.csv").exists()
        else pd.DataFrame()
    )
    compensated = _choose_one_condition(
        pd.read_csv(analysis / "scurve_pixel_gain_spatially_compensated.csv")
        if (analysis / "scurve_pixel_gain_spatially_compensated.csv").exists()
        else pd.DataFrame()
    )
    noise = (
        pd.read_csv(analysis / "noise_fit_results.csv")
        if (analysis / "noise_fit_results.csv").exists()
        else pd.DataFrame()
    )
    baseline = pd.DataFrame()
    if not gain.empty:
        quality = (
            pd.to_numeric(gain.get("fit_r2"), errors="coerce") >= good_fit_r2
        )
        baseline = gain.loc[quality, ["column", "row", "v50_intercept_v"]].rename(
            columns={"v50_intercept_v": "baseline_v"}
        )
        baseline["baseline_source"] = "scurve_zero_charge_intercept"
    if baseline.empty and not noise.empty:
        final = noise[noise["stage"].astype(str) == "equalized_final"].copy()
        if final.empty:
            final = noise[noise["stage"].astype(str).isin(("trim_00", "baseline_noise"))].copy()
        baseline = final[["column", "row", "center_selected_v"]].rename(
            columns={"center_selected_v": "baseline_v"}
        )
        baseline["baseline_source"] = "noise_effective_threshold"
    if baseline.empty:
        raise RuntimeError(f"window {window} has no usable baseline estimator")
    baseline = baseline.groupby(["column", "row"], as_index=False).agg(
        baseline_v=("baseline_v", "median"),
        baseline_source=("baseline_source", "first"),
    )
    if not noise.empty:
        final = noise[noise["stage"].astype(str) == "equalized_final"].copy()
        if final.empty:
            final = noise.copy()
        noise_metric = final.groupby(["column", "row"], as_index=False).agg(
            noise_sigma_v=("sigma_fit_v", "median")
        )
        baseline = baseline.merge(noise_metric, on=["column", "row"], how="left")
    if not gain.empty:
        gain_metric = gain.groupby(["column", "row"], as_index=False).agg(
            raw_gain_mv_per_ke=("nominal_gain_mv_per_ke", "median"),
            gain_fit_r2=("fit_r2", "median"),
            maximum_tested_linear_charge_electrons=(
                "maximum_tested_linear_charge_electrons", "median"
            ),
        )
        baseline = baseline.merge(gain_metric, on=["column", "row"], how="left")
    if not compensated.empty:
        comp_metric = compensated.groupby(["column", "row"], as_index=False).agg(
            compensated_gain_mv_per_ke=("nominal_gain_mv_per_ke", "median")
        )
        baseline = baseline.merge(comp_metric, on=["column", "row"], how="left")
    for method in ("fit", "centroid", "maximum"):
        path = analysis / f"trim_recommendations_{method}.csv"
        if not path.exists():
            continue
        trim = pd.read_csv(path)
        columns = ["column", "row", "recommended_trim_code", "mask_recommended"]
        if not set(columns).issubset(trim):
            continue
        trim = trim[columns].rename(
            columns={
                "recommended_trim_code": f"trim_{method}",
                "mask_recommended": f"mask_{method}",
            }
        )
        baseline = baseline.merge(trim, on=["column", "row"], how="left")
    baseline["window"] = window
    baseline["source_experiment"] = str(experiment)
    baseline["source_analysis"] = str(analysis)
    return baseline


def _combine_window_metrics(metrics: Mapping[str, pd.DataFrame], settings: AllWindowSettings) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    wide: pd.DataFrame | None = None
    for window in _WINDOWS:
        frame = metrics[window].copy()
        center, scale = _robust_center_scale(frame["baseline_v"])
        frame[f"baseline_offset_{window}_v"] = frame["baseline_v"] - center
        frame[f"baseline_robust_z_{window}"] = frame[f"baseline_offset_{window}_v"] / scale
        rename = {
            column: f"{column}_{window}"
            for column in frame.columns
            if column not in ("column", "row")
            and column not in (
                f"baseline_offset_{window}_v",
                f"baseline_robust_z_{window}",
            )
        }
        frame = frame.rename(columns=rename)
        wide = frame if wide is None else wide.merge(
            frame, on=["column", "row"], how="outer", validate="one_to_one"
        )
    assert wide is not None
    z_columns = [f"baseline_robust_z_{window}" for window in _WINDOWS]
    offset_columns = [f"baseline_offset_{window}_v" for window in _WINDOWS]
    z = wide[z_columns].apply(pd.to_numeric, errors="coerce")
    offsets = wide[offset_columns].apply(pd.to_numeric, errors="coerce")
    wide["common_baseline_shift_v"] = offsets.median(axis=1, skipna=False)
    wide["common_baseline_robust_z"] = z.median(axis=1, skipna=False)
    wide["comparator_differential_rms_z"] = np.sqrt(
        ((z.sub(z.median(axis=1), axis=0)) ** 2).mean(axis=1)
    )
    wide["window_offset_spread_v"] = offsets.std(axis=1, ddof=1)
    same_sign = ((z > 0).all(axis=1) | (z < 0).all(axis=1))
    common = (
        (wide["common_baseline_robust_z"].abs() >= settings.common_shift_z_threshold)
        & (wide["comparator_differential_rms_z"] <= settings.common_shift_max_differential_z)
        & same_sign
    )
    outlier_count = (z.abs() >= settings.comparator_outlier_z_threshold).sum(axis=1)
    quiet_count = (z.abs() < 2.0).sum(axis=1)
    classifications = np.full(len(wide), "within_population", dtype=object)
    classifications[(outlier_count == 1) & (quiet_count >= 2)] = (
        "single_comparator_offset_candidate"
    )
    classifications[(outlier_count >= 1) & ~common & ~((outlier_count == 1) & (quiet_count >= 2))] = (
        "mixed_or_differential_shift_candidate"
    )
    classifications[common] = "common_CSA_or_baseline_shift_candidate"
    wide["diagnostic_classification"] = classifications
    wide["cause_is_proven"] = False
    wide["interpretation"] = (
        "correlation_across_three_windows_is_diagnostic_not_a_circuit_fault_proof"
    )

    complete = z.dropna()
    loading_rows: list[dict[str, Any]] = []
    if len(complete) >= 3:
        standardized = complete.to_numpy(dtype=float)
        standardized -= standardized.mean(axis=0, keepdims=True)
        _, singular, vt = np.linalg.svd(standardized, full_matrices=False)
        variance = singular ** 2
        fraction = variance / variance.sum() if variance.sum() > 0 else np.full(3, np.nan)
        scores = standardized @ vt.T
        wide.loc[complete.index, "baseline_pc1_score"] = scores[:, 0]
        for component in range(min(3, len(vt))):
            row: dict[str, Any] = {
                "component": component + 1,
                "explained_variance_fraction": float(fraction[component]),
            }
            for index, window in enumerate(_WINDOWS):
                row[f"loading_{window}"] = float(vt[component, index])
            loading_rows.append(row)
    pca = pd.DataFrame(loading_rows)

    correlations: list[dict[str, Any]] = []
    metric_templates = (
        "baseline_offset_{window}_v",
        "noise_sigma_v_{window}",
        "raw_gain_mv_per_ke_{window}",
        "compensated_gain_mv_per_ke_{window}",
        "maximum_tested_linear_charge_electrons_{window}",
        "trim_fit_{window}",
        "trim_centroid_{window}",
        "trim_maximum_{window}",
    )
    for template in metric_templates:
        for left_index, left in enumerate(_WINDOWS):
            for right in _WINDOWS[left_index + 1 :]:
                left_column = template.format(window=left)
                right_column = template.format(window=right)
                if left_column not in wide or right_column not in wide:
                    continue
                pair = wide[[left_column, right_column]].apply(
                    pd.to_numeric, errors="coerce"
                ).dropna()
                correlations.append(
                    {
                        "metric": template.replace("{window}", "window"),
                        "window_left": left,
                        "window_right": right,
                        "pixel_count": len(pair),
                        "pearson_r": _safe_correlation(pair, "pearson"),
                        "spearman_rho": _safe_correlation(pair, "spearman"),
                    }
                )
    for window in _WINDOWS:
        for gain_kind in ("raw_gain_mv_per_ke", "compensated_gain_mv_per_ke"):
            gain_column = f"{gain_kind}_{window}"
            baseline_column = f"baseline_offset_{window}_v"
            if gain_column not in wide:
                continue
            pair = wide[[baseline_column, gain_column]].apply(
                pd.to_numeric, errors="coerce"
            ).dropna()
            correlations.append({
                "metric": f"within_{window}_baseline_offset_vs_{gain_kind}",
                "window_left": window,
                "window_right": window,
                "pixel_count": len(pair),
                "pearson_r": _safe_correlation(pair, "pearson"),
                "spearman_rho": _safe_correlation(pair, "spearman"),
            })
    return wide, pd.DataFrame(correlations), pca


def _combined_trim_tables(metrics: Mapping[str, pd.DataFrame], analysis: Path) -> dict[str, Path]:
    outputs: dict[str, Path] = {}
    comparator_field = {"AB": "PX_CMPB_TR", "BC": "PX_CMPC_TR", "CD": "PX_CMPD_TR"}
    for method in ("fit", "centroid", "maximum"):
        combined: pd.DataFrame | None = None
        for window in _WINDOWS:
            frame = metrics[window]
            trim = f"trim_{method}"
            mask = f"mask_{method}"
            if trim not in frame:
                continue
            item = frame[["column", "row", trim, mask]].rename(
                columns={trim: comparator_field[window], mask: f"mask_candidate_{window}"}
            )
            combined = item if combined is None else combined.merge(
                item, on=["column", "row"], how="outer", validate="one_to_one"
            )
        if combined is None:
            continue
        mask_columns = [column for column in combined if column.startswith("mask_candidate_")]
        combined["mask_candidate_any_window"] = combined[mask_columns].apply(
            lambda row: any(str(value).strip().lower() in {"true", "1", "yes"} for value in row),
            axis=1,
        )
        combined["suggested_PX_MASK"] = (~combined["mask_candidate_any_window"]).astype(int)
        path = analysis / f"combined_trim_map_{method}.csv"
        outputs[method] = atomic_write_table(path, combined)
    return outputs


def _plot_multi_window(
    pixels: pd.DataFrame,
    correlations: pd.DataFrame,
    pca: pd.DataFrame,
    joint_pixels: pd.DataFrame,
    joint_summary: pd.DataFrame,
    directory: Path,
    settings: AnalysisSettings,
) -> dict[str, list[Path]]:
    outputs: dict[str, list[Path]] = {}
    extent = (min(OWNED_COLUMNS) - 0.5, max(OWNED_COLUMNS) + 0.5, -0.5, 31.5)
    map_columns = [f"baseline_offset_{window}_v" for window in _WINDOWS] + ["common_baseline_shift_v"]
    maps = [_matrix(pixels, column) for column in map_columns]
    finite = np.concatenate([
        item[0][np.isfinite(item[0])] for item in maps if item is not None and np.isfinite(item[0]).any()
    ]) if any(item is not None and np.isfinite(item[0]).any() for item in maps) else np.array([1e-3])
    limit = float(np.nanpercentile(np.abs(finite), 98)) or 1e-3
    figure, axes = plt.subplots(2, 2, figsize=(10.5, 8.5))
    image = None
    for axis, item, label in zip(axes.flat, maps, ("AB", "BC", "CD", "Common component")):
        if item is None:
            axis.set_axis_off()
            continue
        image = axis.imshow(
            1000.0 * item[0], origin="lower", aspect=_aspect(settings),
            interpolation="nearest", extent=extent, cmap="coolwarm",
            vmin=-1000.0 * limit, vmax=1000.0 * limit,
        )
        axis.set_title(label)
        axis.set_xlabel("Physical column")
        axis.set_ylabel("Physical row")
    if image is not None:
        figure.colorbar(image, ax=list(axes.flat), pad=0.02, label="Offset from window median, mV")
    figure.suptitle("Cross-window baseline offsets and common per-pixel component")
    outputs["three_window_baseline_maps"] = _save_figure(
        figure, directory, "three_window_baseline_maps", settings
    )

    figure, axes = plt.subplots(1, 3, figsize=(13.0, 4.0))
    for axis_index, (left, right) in enumerate((("AB", "BC"), ("AB", "CD"), ("BC", "CD"))):
        x = pd.to_numeric(pixels[f"baseline_offset_{left}_v"], errors="coerce") * 1000.0
        y = pd.to_numeric(pixels[f"baseline_offset_{right}_v"], errors="coerce") * 1000.0
        valid = x.notna() & y.notna()
        axes[axis_index].scatter(x[valid], y[valid], s=9, alpha=0.5)
        axes[axis_index].axhline(0, color="black", linewidth=0.7)
        axes[axis_index].axvline(0, color="black", linewidth=0.7)
        axes[axis_index].set_xlabel(f"{left} offset, mV")
        axes[axis_index].set_ylabel(f"{right} offset, mV")
        row = correlations[
            (correlations["metric"] == "baseline_offset_window_v")
            &
            (correlations["window_left"] == left) & (correlations["window_right"] == right)
        ]
        if not row.empty:
            axes[axis_index].set_title(
                f"Pearson {float(row.iloc[0]['pearson_r']):.3f}; Spearman {float(row.iloc[0]['spearman_rho']):.3f}"
            )
    figure.suptitle("Same-pixel baseline correlation between comparator windows")
    outputs["three_window_baseline_pair_scatter"] = _save_figure(
        figure, directory, "three_window_baseline_pair_scatter", settings
    )

    correlation_plot = correlations[
        correlations["window_left"] != correlations["window_right"]
    ].copy()
    if not correlation_plot.empty:
        correlation_plot["pair"] = (
            correlation_plot["window_left"].astype(str)
            + "-"
            + correlation_plot["window_right"].astype(str)
        )
        table = correlation_plot.pivot_table(
            index="metric", columns="pair", values="pearson_r", aggfunc="first"
        )
        figure, axis = plt.subplots(
            figsize=(7.0, max(3.8, 0.42 * len(table) + 1.8))
        )
        image = axis.imshow(table.to_numpy(dtype=float), cmap="coolwarm", vmin=-1, vmax=1, aspect="auto")
        axis.set_xticks(range(len(table.columns)), table.columns)
        axis.set_yticks(range(len(table.index)), table.index)
        axis.set_title("Pearson correlation of same-pixel metrics")
        figure.colorbar(image, ax=axis, pad=0.02, label="Pearson r")
        outputs["three_window_metric_correlation_heatmap"] = _save_figure(
            figure, directory, "three_window_metric_correlation_heatmap", settings
        )

    figure, axes = plt.subplots(1, 3, figsize=(13.0, 4.0))
    plotted = False
    for axis, window in zip(axes, _WINDOWS):
        baseline = pd.to_numeric(
            pixels[f"baseline_offset_{window}_v"], errors="coerce"
        ) * 1000.0
        raw_column = f"raw_gain_mv_per_ke_{window}"
        comp_column = f"compensated_gain_mv_per_ke_{window}"
        if raw_column in pixels:
            raw = pd.to_numeric(pixels[raw_column], errors="coerce")
            valid = baseline.notna() & raw.notna()
            axis.scatter(baseline[valid], raw[valid], s=9, alpha=0.45, label="Raw")
            plotted |= bool(valid.any())
        if comp_column in pixels:
            comp = pd.to_numeric(pixels[comp_column], errors="coerce")
            valid = baseline.notna() & comp.notna()
            axis.scatter(baseline[valid], comp[valid], s=9, alpha=0.45, label="Compensated")
            plotted |= bool(valid.any())
        axis.set_title(window)
        axis.set_xlabel("Baseline offset, mV")
        axis.set_ylabel("Nominal gain, mV/ke")
        axis.legend()
    if plotted:
        figure.suptitle("Baseline offset versus raw and spatially compensated gain")
        outputs["three_window_baseline_vs_gain"] = _save_figure(
            figure, directory, "three_window_baseline_vs_gain", settings
        )
    else:
        plt.close(figure)

    if all(f"trim_fit_{window}" in pixels for window in _WINDOWS):
        figure, axes = plt.subplots(1, 3, figsize=(12.7, 4.0))
        image = None
        for axis, window in zip(axes, _WINDOWS):
            item = _matrix(pixels, f"trim_fit_{window}")
            if item is None:
                axis.set_axis_off()
                continue
            image = axis.imshow(
                item[0], origin="lower", aspect=_aspect(settings), interpolation="nearest",
                extent=extent, cmap="viridis", vmin=0, vmax=31,
            )
            axis.set_title(f"{window}: fit trim")
            axis.set_xlabel("Physical column")
            axis.set_ylabel("Physical row")
        if image is not None:
            figure.colorbar(image, ax=list(axes), pad=0.02, label="Trim code")
        figure.suptitle("Recommended comparator trims in the same physical pixels")
        outputs["three_window_trim_maps_fit"] = _save_figure(
            figure, directory, "three_window_trim_maps_fit", settings
        )

    class_codes = {
        "within_population": 0,
        "single_comparator_offset_candidate": 1,
        "mixed_or_differential_shift_candidate": 2,
        "common_CSA_or_baseline_shift_candidate": 3,
    }
    classified = pixels.copy()
    classified["classification_code"] = classified["diagnostic_classification"].map(class_codes)
    item = _matrix(classified, "classification_code")
    figure, axes = plt.subplots(1, 2, figsize=(10.5, 4.4))
    if item is not None:
        image = axes[0].imshow(
            item[0], origin="lower", aspect=_aspect(settings), interpolation="nearest",
            extent=extent, cmap="viridis", vmin=0, vmax=3,
        )
        figure.colorbar(image, ax=axes[0], ticks=range(4), pad=0.02, label="0 normal, 1 one comparator, 2 mixed, 3 common")
    counts = classified["diagnostic_classification"].value_counts()
    axes[1].barh(range(len(counts)), counts.values, color="#4e79a7")
    axes[1].set_yticks(range(len(counts)), [str(value) for value in counts.index])
    axes[1].set_xlabel("Pixel count")
    axes[0].set_xlabel("Physical column")
    axes[0].set_ylabel("Physical row")
    figure.suptitle("Diagnostic fault candidates; classification is not a circuit diagnosis")
    outputs["three_window_diagnostic_classification"] = _save_figure(
        figure, directory, "three_window_diagnostic_classification", settings
    )

    figure, axes = plt.subplots(1, 2, figsize=(10.5, 4.2))
    for window in _WINDOWS:
        profile = pixels.groupby("row")[f"baseline_offset_{window}_v"].median() * 1000.0
        axes[0].plot(profile.index, profile.values, marker="o", markersize=3, label=window)
        profile = pixels.groupby("column")[f"baseline_offset_{window}_v"].median() * 1000.0
        axes[1].plot(profile.index, profile.values, marker="o", markersize=3, label=window)
    axes[0].set_xlabel("Physical row")
    axes[1].set_xlabel("Physical column")
    for axis in axes:
        axis.set_ylabel("Median baseline offset, mV")
        axis.legend()
    figure.suptitle("Shared and differential spatial profiles")
    outputs["three_window_spatial_profiles"] = _save_figure(
        figure, directory, "three_window_spatial_profiles", settings
    )

    if not pca.empty:
        figure, axis = plt.subplots(figsize=(6.4, 4.2))
        x = np.arange(1, len(pca) + 1)
        axis.bar(x, 100.0 * pca["explained_variance_fraction"], color="#59a14f")
        axis.set_xticks(x)
        axis.set_xlabel("PCA component")
        axis.set_ylabel("Explained variance, %")
        axis.set_title("Unsupervised decomposition of AB/BC/CD baseline offsets")
        outputs["three_window_baseline_pca"] = _save_figure(
            figure, directory, "three_window_baseline_pca", settings
        )

    gain_columns = []
    for window in _WINDOWS:
        for kind in ("raw_gain_mv_per_ke", "compensated_gain_mv_per_ke"):
            column = f"{kind}_{window}"
            if column in pixels:
                gain_columns.append((window, kind, column))
    if gain_columns:
        figure, axes = plt.subplots(3, 2, figsize=(10.8, 13.6))
        for row_index, window in enumerate(_WINDOWS):
            for column_index, (kind, label) in enumerate((
                ("raw_gain_mv_per_ke", "Raw"),
                ("compensated_gain_mv_per_ke", "Spatially compensated"),
            )):
                column = f"{kind}_{window}"
                item = _matrix(pixels, column)
                axis = axes[row_index, column_index]
                if item is None:
                    axis.set_axis_off()
                    continue
                image = axis.imshow(
                    item[0], origin="lower", aspect=_aspect(settings), interpolation="nearest",
                    extent=extent, cmap="viridis",
                )
                figure.colorbar(image, ax=axis, pad=0.02, label="mV/ke")
                axis.set_title(f"{window}: {label}")
                axis.set_xlabel("Physical column")
                axis.set_ylabel("Physical row")
        figure.suptitle("Gain maps across all comparator windows")
        figure.tight_layout(rect=(0, 0, 1, 0.975), h_pad=2.8, w_pad=1.5)
        outputs["three_window_gain_maps_raw_and_compensated"] = _save_figure(
            figure, directory, "three_window_gain_maps_raw_and_compensated", settings
        )

    if not joint_pixels.empty:
        metrics = (
            ("joint_baseline_intercept_v", "Raw baseline intercept, V"),
            ("joint_gain_mv_per_ke", "Raw gain, mV/ke"),
            ("joint_transfer_r2", "Raw three-threshold R2"),
            (
                "joint_baseline_spatially_compensated_intercept_v",
                "Compensated baseline intercept, V",
            ),
            (
                "joint_gain_spatially_compensated_mv_per_ke",
                "Compensated gain, mV/ke",
            ),
            (
                "joint_spatially_compensated_transfer_r2",
                "Compensated three-threshold R2",
            ),
        )
        figure, axes = plt.subplots(2, 3, figsize=(14.2, 8.7))
        for axis, (column, label) in zip(axes.flat, metrics):
            item = _matrix(joint_pixels, column)
            if item is None:
                axis.set_axis_off()
                continue
            image = axis.imshow(
                item[0], origin="lower", aspect=_aspect(settings), interpolation="nearest",
                extent=extent, cmap="viridis",
            )
            figure.colorbar(image, ax=axis, pad=0.02, label=label)
            axis.set_xlabel("Physical column")
            axis.set_ylabel("Physical row")
            axis.set_title(label)
        figure.suptitle("Final fixed-threshold three-window REF sweep")
        figure.tight_layout(rect=(0, 0, 1, 0.965), h_pad=2.0, w_pad=1.0)
        outputs["joint_ref_sweep_transfer_maps"] = _save_figure(
            figure, directory, "joint_ref_sweep_transfer_maps", settings
        )

        figure, axes = plt.subplots(2, 3, figsize=(13.8, 8.5))
        for column_index, window in enumerate(_WINDOWS):
            for row_index, (suffix, title) in enumerate((
                ("electrons", "Raw"),
                ("spatially_compensated_electrons", "Compensated"),
            )):
                column = f"q50_{window}_{suffix}"
                item = _matrix(joint_pixels, column)
                axis = axes[row_index, column_index]
                if item is None:
                    axis.set_axis_off()
                    continue
                image = axis.imshow(
                    item[0], origin="lower", aspect=_aspect(settings),
                    interpolation="nearest", extent=extent, cmap="viridis",
                )
                figure.colorbar(image, ax=axis, pad=0.02, label="Q50, electrons")
                axis.set_title(f"{window}: {title}")
                axis.set_xlabel("Physical column")
                axis.set_ylabel("Physical row")
        figure.suptitle(
            "Charge at 50% response for three fixed thresholds; raw and diagnostic plane removal"
        )
        figure.tight_layout(rect=(0, 0, 1, 0.965), h_pad=2.0, w_pad=1.0)
        outputs["joint_ref_sweep_q50_maps"] = _save_figure(
            figure, directory, "joint_ref_sweep_q50_maps", settings
        )

        figure, axes = plt.subplots(1, 2, figsize=(10.5, 4.4))
        for axis, (column, title) in zip(axes, (
            (
                "maximum_tested_linear_charge_electrons",
                "Raw three-threshold consistency",
            ),
            (
                "maximum_tested_linear_charge_spatially_compensated_electrons",
                "After diagnostic plane removal",
            ),
        )):
            item = _matrix(joint_pixels, column)
            if item is None:
                axis.set_axis_off()
                continue
            image = axis.imshow(
                item[0], origin="lower", aspect=_aspect(settings),
                interpolation="nearest", extent=extent, cmap="viridis",
            )
            figure.colorbar(
                image, ax=axis, pad=0.02,
                label="Maximum tested linear charge, electrons",
            )
            axis.set_title(title)
            axis.set_xlabel("Physical column")
            axis.set_ylabel("Physical row")
        figure.suptitle(
            "Largest tested charge consistent with the three-threshold line model"
        )
        outputs["joint_ref_sweep_linearity_maps"] = _save_figure(
            figure, directory, "joint_ref_sweep_linearity_maps", settings
        )
    if not joint_summary.empty:
        figure, axes = plt.subplots(1, 2, figsize=(10.5, 4.2))
        for window, group in joint_summary.groupby("window", sort=True):
            axes[0].plot(
                group["injection_charge_electrons"] / 1000.0,
                group["matrix_median_efficiency"], marker="o", markersize=3,
                label=window,
            )
            axes[1].plot(
                group["injection_charge_electrons"] / 1000.0,
                group["matrix_median_signal_count"], marker="o", markersize=3,
                label=window,
            )
        axes[0].set_ylabel("Detection efficiency")
        axes[1].set_ylabel("Background-subtracted count")
        for axis in axes:
            axis.set_xlabel("Nominal injected charge, ke")
            axis.legend()
        figure.suptitle("Three windows at fixed evenly spaced physical thresholds")
        outputs["joint_ref_sweep_matrix_response"] = _save_figure(
            figure, directory, "joint_ref_sweep_matrix_response", settings
        )
    return outputs


def _interpolate_crossing(x: np.ndarray, y: np.ndarray, level: float) -> float:
    if len(x) < 2 or not np.isfinite(y).all() or y[-1] < level:
        return float("nan")
    index = int(np.searchsorted(y, level, side="left"))
    if index == 0:
        return float(x[0])
    x0, x1 = float(x[index - 1]), float(x[index])
    y0, y1 = float(y[index - 1]), float(y[index])
    if y1 <= y0:
        return x1
    return x0 + (level - y0) * (x1 - x0) / (y1 - y0)


def _add_joint_spatial_compensation(crossings: pd.DataFrame) -> pd.DataFrame:
    """Remove one robust spatial Q50 plane independently in each window.

    The operation is diagnostic. It can suppress a baseline/IR-drop-like
    common gradient, but it can also remove a genuine comparator or gain
    gradient. Raw Q50 remains unchanged and is always retained next to the
    compensated value.
    """

    result = crossings.copy()
    result["q50_spatial_plane_electrons"] = np.nan
    result["q50_spatial_plane_offset_electrons"] = np.nan
    result["q50_spatially_compensated_electrons"] = np.nan
    result["q50_spatial_compensation_status"] = "not_available"
    for window, group in result.groupby("window", sort=True):
        values = group[["column", "row", "q50_electrons"]].apply(
            pd.to_numeric, errors="coerce"
        )
        valid = values.notna().all(axis=1)
        if int(valid.sum()) < 6:
            continue
        clean = values.loc[valid]
        column_center = float(clean["column"].mean())
        row_center = float(clean["row"].mean())
        design = np.column_stack(
            (
                np.ones(len(clean)),
                clean["column"].to_numpy(dtype=float) - column_center,
                clean["row"].to_numpy(dtype=float) - row_center,
            )
        )
        observed = clean["q50_electrons"].to_numpy(dtype=float)
        inlier = np.ones(len(clean), dtype=bool)
        parameters = np.linalg.lstsq(design, observed, rcond=None)[0]
        for _ in range(2):
            residual = observed - design @ parameters
            center = float(np.median(residual[inlier]))
            mad = float(np.median(np.abs(residual[inlier] - center)))
            robust_sigma = 1.4826 * mad
            if not math.isfinite(robust_sigma) or robust_sigma <= 0:
                break
            candidate = np.abs(residual - center) <= 4.0 * robust_sigma
            if int(candidate.sum()) < 6:
                break
            inlier = candidate
            parameters = np.linalg.lstsq(
                design[inlier], observed[inlier], rcond=None
            )[0]
        predicted = design @ parameters
        plane_center = float(np.median(predicted))
        offsets = predicted - plane_center
        corrected = observed - offsets
        target_index = clean.index
        result.loc[target_index, "q50_spatial_plane_electrons"] = predicted
        result.loc[
            target_index, "q50_spatial_plane_offset_electrons"
        ] = offsets
        result.loc[
            target_index, "q50_spatially_compensated_electrons"
        ] = corrected
        result.loc[
            target_index, "q50_spatial_compensation_status"
        ] = "diagnostic_per_window_q50_plane_removed"
    return result


def _fit_three_threshold_transfer(
    group: pd.DataFrame,
    *,
    charge_column: str,
) -> dict[str, float] | None:
    clean = group.dropna(
        subset=[charge_column, "threshold_voltage_v"]
    ).sort_values(charge_column)
    if len(clean) != 3:
        return None
    x = clean[charge_column].to_numpy(dtype=float)
    y = clean["threshold_voltage_v"].to_numpy(dtype=float)
    if len(np.unique(x)) < 2:
        return None
    design = np.column_stack((x, np.ones(len(x))))
    parameters, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
    predicted = design @ parameters
    variance = float(np.sum((y - y.mean()) ** 2))
    residual = float(np.sum((y - predicted) ** 2))
    r2 = 1.0 - residual / variance if variance > 0 else float("nan")
    return {
        "gain_mv_per_ke": float(parameters[0]) * 1e6,
        "baseline_intercept_v": float(parameters[1]),
        "transfer_r2": r2,
    }


def _analyze_joint_ref_sweep(
    parent: ExperimentStore,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    raw = parent.load_raw("all_window_ref_sweep")
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    long_rows: list[pd.DataFrame] = []
    for window, (count_column, valid_column, saturated_column) in _COUNTER_COLUMNS.items():
        if count_column not in raw:
            continue
        item = raw.copy()
        item["window"] = window
        item["decoded_count"] = pd.to_numeric(item[count_column], errors="coerce")
        item["counter_valid_for_window"] = item[valid_column].astype(str).str.lower().isin(("true", "1", "yes"))
        item["counter_saturated_for_window"] = item[saturated_column].astype(str).str.lower().isin(("true", "1", "yes"))
        long_rows.append(item)
    if not long_rows:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    long = pd.concat(long_rows, ignore_index=True)
    keys = [
        "pair_id", "window", "column", "row", "repeat_index",
        "injection_voltage_step_v", "injection_charge_electrons",
    ]
    background = long[long["acquisition_type"].astype(str) == "background"].copy()
    signal = long[long["acquisition_type"].astype(str) == "signal"].copy()
    keep = keys + ["decoded_count", "counter_valid_for_window", "counter_saturated_for_window"]
    pairs = signal[keep].merge(
        background[keep], on=keys, suffixes=("_signal", "_background"),
        how="inner", validate="one_to_one",
    )
    valid = (
        pairs["counter_valid_for_window_signal"]
        & pairs["counter_valid_for_window_background"]
        & ~pairs["counter_saturated_for_window_signal"]
        & ~pairs["counter_saturated_for_window_background"]
    )
    pairs["valid_pair"] = valid
    pairs["background_subtracted_count"] = np.where(
        valid,
        np.maximum(0.0, pairs["decoded_count_signal"] - pairs["decoded_count_background"]),
        np.nan,
    )
    aggregate = pairs.groupby(
        ["window", "column", "row", "injection_voltage_step_v", "injection_charge_electrons"],
        as_index=False,
    ).agg(
        signal_count_median=("background_subtracted_count", "median"),
        signal_count_q10=("background_subtracted_count", lambda value: value.quantile(0.10)),
        signal_count_q90=("background_subtracted_count", lambda value: value.quantile(0.90)),
        valid_repeat_count=("valid_pair", "sum"),
        repeat_count=("valid_pair", "size"),
    )
    aggregate["plateau_count"] = aggregate.groupby(
        ["window", "column", "row"]
    )["signal_count_median"].transform(lambda value: value.quantile(0.90))
    aggregate["detection_efficiency"] = (
        aggregate["signal_count_median"] / aggregate["plateau_count"]
    ).clip(0.0, 1.2)
    pixel_rows: list[dict[str, Any]] = []
    crossing_rows: list[dict[str, Any]] = []
    for (window, column, row), group in aggregate.groupby(
        ["window", "column", "row"], sort=True
    ):
        group = group.sort_values("injection_charge_electrons")
        x = group["injection_charge_electrons"].to_numpy(dtype=float)
        y = group["detection_efficiency"].to_numpy(dtype=float)
        finite = np.isfinite(x) & np.isfinite(y)
        x, y = x[finite], y[finite]
        if len(x) < 2:
            continue
        monotonic = np.maximum.accumulate(np.clip(y, 0.0, 1.0))
        q16 = _interpolate_crossing(x, monotonic, 0.16)
        q50 = _interpolate_crossing(x, monotonic, 0.50)
        q84 = _interpolate_crossing(x, monotonic, 0.84)
        crossing_rows.append({
            "window": window, "column": int(column), "row": int(row),
            "q16_electrons": q16, "q50_electrons": q50, "q84_electrons": q84,
            "sigma_charge_electrons": 0.5 * (q84 - q16) if math.isfinite(q16) and math.isfinite(q84) else np.nan,
            "plateau_count": float(group["plateau_count"].median()),
            "amplitude_point_count": int(len(x)),
            "fit_model": "monotonic_empirical_crossing_16_50_84_percent",
        })
    crossings = pd.DataFrame(crossing_rows)
    threshold_record = parent.metadata.get("joint_ref_sweep", {}).get("fixed_thresholds", {})
    if not crossings.empty:
        crossings["threshold_voltage_v"] = crossings["window"].map(
            {
                "AB": threshold_record.get("DAC_CMP_B", {}).get("voltage_v"),
                "BC": threshold_record.get("DAC_CMP_C", {}).get("voltage_v"),
                "CD": threshold_record.get("DAC_CMP_D", {}).get("voltage_v"),
            }
        )
        crossings = _add_joint_spatial_compensation(crossings)
        for (column, row), group in crossings.groupby(["column", "row"]):
            raw_fit = _fit_three_threshold_transfer(
                group, charge_column="q50_electrons"
            )
            compensated_fit = _fit_three_threshold_transfer(
                group,
                charge_column="q50_spatially_compensated_electrons",
            )
            if raw_fit is None:
                continue
            maximum_charge = float(
                np.nanmax(
                    aggregate[
                        (aggregate["column"] == column)
                        & (aggregate["row"] == row)
                    ]["injection_charge_electrons"]
                )
            )
            raw_r2 = float(raw_fit["transfer_r2"])
            compensated_r2 = (
                float(compensated_fit["transfer_r2"])
                if compensated_fit is not None
                else float("nan")
            )
            pixel_rows.append({
                "column": int(column), "row": int(row),
                "joint_gain_mv_per_ke": raw_fit["gain_mv_per_ke"],
                "joint_baseline_intercept_v": raw_fit["baseline_intercept_v"],
                "joint_transfer_r2": raw_r2,
                "maximum_tested_linear_charge_electrons": (
                    maximum_charge
                    if math.isfinite(raw_r2) and raw_r2 >= 0.98
                    else np.nan
                ),
                "joint_gain_spatially_compensated_mv_per_ke": (
                    compensated_fit["gain_mv_per_ke"]
                    if compensated_fit is not None
                    else np.nan
                ),
                "joint_baseline_spatially_compensated_intercept_v": (
                    compensated_fit["baseline_intercept_v"]
                    if compensated_fit is not None
                    else np.nan
                ),
                "joint_spatially_compensated_transfer_r2": compensated_r2,
                "maximum_tested_linear_charge_spatially_compensated_electrons": (
                    maximum_charge
                    if math.isfinite(compensated_r2) and compensated_r2 >= 0.98
                    else np.nan
                ),
                "joint_gain_spatial_compensation_delta_mv_per_ke": (
                    compensated_fit["gain_mv_per_ke"]
                    - raw_fit["gain_mv_per_ke"]
                    if compensated_fit is not None
                    else np.nan
                ),
                "linearity_interpretation": (
                    "three_fixed_threshold_crossings_only_not_direct_CSA_output_linearity"
                ),
                "spatial_compensation_interpretation": (
                    "diagnostic_per_window_q50_plane_removal_raw_result_remains_primary"
                ),
            })
    joint_pixels = pd.DataFrame(pixel_rows)
    summary = aggregate.groupby(
        ["window", "injection_voltage_step_v", "injection_charge_electrons"], as_index=False
    ).agg(
        matrix_median_efficiency=("detection_efficiency", "median"),
        matrix_q10_efficiency=("detection_efficiency", lambda value: value.quantile(0.10)),
        matrix_q90_efficiency=("detection_efficiency", lambda value: value.quantile(0.90)),
        matrix_median_signal_count=("signal_count_median", "median"),
        pixel_count=("column", "size"),
    )
    if not joint_pixels.empty and not crossings.empty:
        raw_q50 = crossings.pivot(
            index=["column", "row"], columns="window", values="q50_electrons"
        ).rename(columns=lambda value: f"q50_{value}_electrons").reset_index()
        compensated_q50 = crossings.pivot(
            index=["column", "row"],
            columns="window",
            values="q50_spatially_compensated_electrons",
        ).rename(
            columns=lambda value: f"q50_{value}_spatially_compensated_electrons"
        ).reset_index()
        joint_pixels = joint_pixels.merge(
            raw_q50, on=["column", "row"], how="left"
        ).merge(
            compensated_q50, on=["column", "row"], how="left"
        )
    return aggregate, crossings, joint_pixels, summary


def _multi_window_report(
    analysis: Path,
    pixels: pd.DataFrame,
    correlations: pd.DataFrame,
    pca: pd.DataFrame,
    joint_pixels: pd.DataFrame,
    joint_summary: pd.DataFrame,
    window_paths: Mapping[str, Path],
) -> Path:
    counts = pixels["diagnostic_classification"].value_counts()
    lines = [
        "# Отчет совместной характеризации окон AB, BC и CD",
        "",
        f"Создано: {utc_now_text()}.",
        "",
        "## Что измерено",
        "",
        "Сначала каждое окно независимо прошло обычный noise/equalization/S-curve "
        "анализ. Затем результаты сопоставлены по одним и тем же физическим "
        "координатам пикселей. Общий сдвиг трех окон рассматривается как кандидат "
        "на изменение baseline/ЗЧУ, а не как доказанный дефект цепи.",
        "",
        "| Окно | Эксперимент |",
        "| --- | --- |",
        *[f"| {window} | `{path}` |" for window, path in window_paths.items()],
        "",
        "## Диагностические классы",
        "",
        "| Класс | Пикселей |",
        "| --- | ---: |",
        *[f"| {name} | {int(value)} |" for name, value in counts.items()],
        "",
        "`common_CSA_or_baseline_shift_candidate` требует одинакового знака и "
        "большого робастного сдвига во всех трех окнах при малой межоконной "
        "разности. Три компаратора все еще могут иметь коррелированный mismatch, "
        "поэтому для подтверждения ЗЧУ нужны повторения при изменении bias, FCLK и "
        "пространственной активности.",
    ]
    if not correlations.empty:
        lines.extend([
            "",
            "## Межоконные и внутриоконные корреляции",
            "",
            "| Метрика | Пара | Pearson | Spearman | N |",
            "| --- | --- | ---: | ---: | ---: |",
        ])
        for _, row in correlations.iterrows():
            lines.append(
                f"| {row['metric']} | {row['window_left']}-{row['window_right']} | "
                f"{float(row['pearson_r']):.3f} | {float(row['spearman_rho']):.3f} | {int(row['pixel_count'])} |"
            )
    if not pca.empty:
        first = pca.iloc[0]
        lines.extend([
            "", "## Общая компонента PCA", "",
            f"Первая компонента объясняет {100.0 * float(first['explained_variance_fraction']):.2f}% "
            "дисперсии стандартизованных сдвигов AB/BC/CD. Большая доля при "
            "сходных знаках трех loading поддерживает гипотезу общего источника, "
            "но не идентифицирует физическую причину.",
        ])
    if not joint_summary.empty:
        lines.extend([
            "", "## Финальный REF sweep при фиксированных трех порогах", "",
            f"Измерено уровней заряда: {joint_summary['injection_voltage_step_v'].nunique()}; "
            f"пикселей с тремя найденными Q50: {len(joint_pixels)}.",
            "",
            "Пороговые ЦАП B/C/D были распределены равномерно по измеренному "
            "напряжению, а не по коду DAC. DAC_A оставлен на 1023 и до съемки "
            "проверено условие физического порядка напряжений. Линейность по трем "
            "пороговым пересечениям является ограниченной диагностикой и не заменяет "
            "прямое измерение аналогового выхода ЗЧУ.",
        ])
        if not joint_pixels.empty:
            raw_gain = pd.to_numeric(
                joint_pixels.get("joint_gain_mv_per_ke"), errors="coerce"
            ).dropna()
            compensated_gain = pd.to_numeric(
                joint_pixels.get("joint_gain_spatially_compensated_mv_per_ke"),
                errors="coerce",
            ).dropna()
            if len(raw_gain) and len(compensated_gain):
                lines.extend([
                    "",
                    "Диагностическая компенсация отдельно удаляет пространственную "
                    "плоскость Q50 каждого окна и повторяет трехпороговый fit. "
                    "Она не заменяет raw-результат: различие может включать как "
                    "IRdrop/baseline, так и реальную пространственную неоднородность.",
                    "",
                    f"Разброс gain: raw std={raw_gain.std(ddof=1):.4g} мВ/ke; "
                    f"compensated std={compensated_gain.std(ddof=1):.4g} мВ/ke.",
                ])
    lines.extend([
        "", "## Основные файлы", "",
        "- `multi_window_pixel_metrics.csv`: общий и дифференциальный сдвиг каждого пикселя.",
        "- `multi_window_correlation_summary.csv`: межоконные корреляции.",
        "- `multi_window_pca_summary.csv`: loading и доля дисперсии PCA.",
        "- `combined_trim_map_*.csv`: совместные предложения trim B/C/D и mask.",
        "- `joint_ref_sweep_*.csv`: финальная трехпороговая съемка, raw и диагностически компенсированные метрики.",
        "",
        "## Методические источники",
        "",
        "Threshold scan, S-curve и сравнение точного/быстрого trimming: "
        "[HEPS-BPIX calibration](https://arxiv.org/abs/2011.01342). Локальный "
        "5-bit trim для компенсации pixel-to-pixel baseline: "
        "[Timepix4 front-end](https://arxiv.org/abs/2203.15912).",
    ])
    key_plot = analysis / "plots" / "three_window_baseline_maps.png"
    if key_plot.exists():
        lines.extend(["", "![Карты общего сдвига](plots/three_window_baseline_maps.png)"])
    return atomic_write_text(analysis / "REPORT.md", "\n".join(lines))


def analyze_all_windows(
    path: str | Path,
    *,
    settings: AnalysisSettings | None = None,
    reanalyze_children: bool = False,
    generate_plots: bool = True,
) -> dict[str, Any]:
    """Offline combined analysis for a parent WINDOW='ALL' experiment."""

    selected = copy.deepcopy(settings) if settings is not None else AnalysisSettings()
    selected.validate()
    parent = ExperimentStore(path)
    if str(parent.metadata.get("window", "")).upper() != "ALL":
        raise ValueError("analyze_all_windows requires a parent WINDOW='ALL' experiment")
    combined_settings = AllWindowSettings(
        **parent.metadata.get("all_window_settings", {})
    )
    combined_settings.validate()
    records = _window_records(parent)
    missing = set(_WINDOWS) - set(records)
    if missing:
        raise ValueError("ALL experiment has no child window(s): " + ", ".join(sorted(missing)))
    window_paths: dict[str, Path] = {}
    analysis_paths: dict[str, Path] = {}
    metrics: dict[str, pd.DataFrame] = {}
    for window in _WINDOWS:
        child = _resolve_child_path(parent, records[window])
        window_paths[window] = child
        if reanalyze_children:
            outputs = analyze_saved_experiment(
                child, settings=selected, generate_plots=generate_plots
            )
            analysis = Path(outputs["analysis_directory"])
        else:
            analysis = _latest_analysis(child)
        analysis_paths[window] = analysis
        metrics[window] = _extract_window_metrics(
            window,
            child,
            analysis,
            good_fit_r2=combined_settings.good_fit_r2,
        )
    analysis = parent.next_analysis_directory()
    pixels, correlations, pca = _combine_window_metrics(
        metrics, combined_settings
    )
    atomic_write_table(analysis / "multi_window_pixel_metrics.csv", pixels)
    atomic_write_table(analysis / "multi_window_correlation_summary.csv", correlations)
    atomic_write_table(analysis / "multi_window_pca_summary.csv", pca)
    trim_paths = _combined_trim_tables(metrics, analysis)
    joint_efficiency, joint_crossings, joint_pixels, joint_summary = (
        _analyze_joint_ref_sweep(parent)
    )
    for filename, frame in (
        ("joint_ref_sweep_efficiency.csv", joint_efficiency),
        ("joint_ref_sweep_threshold_crossings.csv", joint_crossings),
        ("joint_ref_sweep_pixel_transfer.csv", joint_pixels),
        ("joint_ref_sweep_summary.csv", joint_summary),
    ):
        atomic_write_table(analysis / filename, frame)
    plots = (
        _plot_multi_window(
            pixels, correlations, pca, joint_pixels, joint_summary,
            analysis / "plots", selected,
        )
        if generate_plots
        else {}
    )
    report = _multi_window_report(
        analysis, pixels, correlations, pca, joint_pixels, joint_summary,
        window_paths,
    )
    atomic_write_json(analysis / "analysis_settings.json", {
        "created_utc": utc_now_text(),
        "analysis_framework_version": FRAMEWORK_VERSION,
        "settings": asdict(selected),
        "reanalyzed_children": reanalyze_children,
        "generate_plots": generate_plots,
        "child_analysis_paths": {key: str(value) for key, value in analysis_paths.items()},
    })
    atomic_write_json(analysis / "analysis_manifest.json", {
        "created_utc": utc_now_text(),
        "source_experiment": str(parent.root),
        "window": "ALL",
        "files": sorted(path.relative_to(analysis).as_posix() for path in analysis.rglob("*") if path.is_file()),
    })
    return {
        "analysis_directory": analysis,
        "report": report,
        "trim_maps": trim_paths,
        "plots": plots,
    }


def _baseline_and_high_anchor(
    window_results: Mapping[str, CharacterizationResult],
    *,
    good_fit_r2: float,
) -> tuple[float, float, dict[str, Any]]:
    baselines: list[float] = []
    maxima: list[float] = []
    details: dict[str, Any] = {}
    for window in _WINDOWS:
        analysis = Path(window_results[window].analysis_path)
        gain_path = analysis / "scurve_pixel_gain_results.csv"
        scurve_path = analysis / "scurve_results.csv"
        gain = _choose_one_condition(pd.read_csv(gain_path)) if gain_path.exists() else pd.DataFrame()
        scurve = _choose_one_condition(pd.read_csv(scurve_path)) if scurve_path.exists() else pd.DataFrame()
        baseline = float("nan")
        if not gain.empty:
            good = gain[
                pd.to_numeric(gain["fit_r2"], errors="coerce") >= good_fit_r2
            ]
            baseline = float(pd.to_numeric(good["v50_intercept_v"], errors="coerce").median())
        if not math.isfinite(baseline):
            noise = pd.read_csv(analysis / "noise_fit_results.csv")
            noise = noise[noise["stage"].astype(str) == "equalized_final"]
            baseline = float(pd.to_numeric(noise["center_selected_v"], errors="coerce").median())
        if scurve.empty:
            raise RuntimeError(f"window {window} has no S-curve results for final REF sweep")
        step = pd.to_numeric(scurve["injection_voltage_step_v"], errors="coerce")
        maximum_step = float(step.max())
        maximum = float(pd.to_numeric(scurve.loc[step == maximum_step, "v50_v"], errors="coerce").median())
        if not math.isfinite(baseline) or not math.isfinite(maximum):
            raise RuntimeError(f"window {window} has no finite baseline/high-amplitude anchor")
        baselines.append(baseline)
        maxima.append(maximum)
        details[window] = {"baseline_v": baseline, "maximum_amplitude_v50_v": maximum}
    low = float(np.median(baselines))
    high = float(np.median(maxima))
    if not high > low:
        raise RuntimeError(
            "final all-window threshold range is not positive in physical voltage; "
            "check S-curve polarity and V50 calibration"
        )
    return low, high, details


def _select_fixed_thresholds(
    calibrations: Mapping[str, ThresholdDacCalibration],
    low: float,
    high: float,
) -> tuple[dict[str, int], dict[str, dict[str, float | int]]]:
    fractions = {"DAC_CMP_D": 0.25, "DAC_CMP_C": 0.50, "DAC_CMP_B": 0.75}
    codes = {"DAC_CMP_A": 1023}
    records: dict[str, dict[str, float | int]] = {}
    for name, fraction in fractions.items():
        target = low + fraction * (high - low)
        code = calibrations[name].voltage_to_nearest_dac_code(target)
        voltage = calibrations[name].dac_code_to_voltage(code)
        codes[name] = code
        records[name] = {"code": code, "target_voltage_v": target, "voltage_v": voltage}
    a_voltage = calibrations["DAC_CMP_A"].dac_code_to_voltage(1023)
    records["DAC_CMP_A"] = {"code": 1023, "target_voltage_v": a_voltage, "voltage_v": a_voltage}
    ordered = [records[name]["voltage_v"] for name in ("DAC_CMP_D", "DAC_CMP_C", "DAC_CMP_B", "DAC_CMP_A")]
    if not all(float(left) < float(right) for left, right in zip(ordered, ordered[1:])):
        raise RuntimeError(
            "nearest calibrated DAC codes do not satisfy V_D < V_C < V_B < V_A; "
            "final REF sweep was not started"
        )
    return codes, records


def _joint_raw_rows(
    parent: ExperimentStore,
    descriptor: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    shot_result: Any,
    amplitude: Mapping[str, Any],
    thresholds: Mapping[str, dict[str, float | int]],
    trim_maps: Mapping[str, Mapping[tuple[int, int], int]],
    active_pixels: set[tuple[int, int]],
    shutter_duration_s: float,
    capacitance_f: float,
    capacitance_relative_uncertainty: float,
    pair_id: str,
) -> list[dict[str, Any]]:
    step = float(amplitude["voltage_step_v"])
    charge_c = capacitance_f * step
    charge_e = charge_c / _ELECTRON_CHARGE_C
    rows = []
    for sample in samples:
        coordinate = (int(sample["column"]), int(sample["row"]))
        rows.append({
            "experiment_id": parent.metadata["experiment_id"],
            "acquisition_timestamp_utc": utc_now_text(),
            **dict(descriptor),
            "pair_id": pair_id,
            "column": coordinate[0], "row": coordinate[1],
            "active_injection_pixel": coordinate in active_pixels,
            "ref1_dac_code": int(amplitude["DAC_TST_REF1"]),
            "ref2_dac_code": int(amplitude["DAC_TST_REF2"]),
            "ref1_voltage_v": float(amplitude["ref1_voltage_v"]),
            "ref2_voltage_v": float(amplitude["ref2_voltage_v"]),
            "injection_voltage_step_v": step,
            "injection_charge_c": charge_c,
            "injection_charge_electrons": charge_e,
            "injection_charge_uncertainty_c": abs(charge_c) * capacitance_relative_uncertainty,
            "injection_capacitance_f": capacitance_f,
            "injection_capacitance_relative_uncertainty": capacitance_relative_uncertainty,
            "shutter_duration_s": shutter_duration_s,
            "requested_injections": shot_result.requested_injections,
            "programmed_injections": shot_result.programmed_injections,
            "actual_injections": shot_result.actual_injections,
            "injections_for_analysis": shot_result.injections_for_analysis,
            "injection_count_source": shot_result.injection_count_source,
            "shot_execution_details_json": json.dumps(dict(shot_result.details), ensure_ascii=False, sort_keys=True),
            "DAC_CMP_A_code": int(thresholds["DAC_CMP_A"]["code"]),
            "DAC_CMP_B_code": int(thresholds["DAC_CMP_B"]["code"]),
            "DAC_CMP_C_code": int(thresholds["DAC_CMP_C"]["code"]),
            "DAC_CMP_D_code": int(thresholds["DAC_CMP_D"]["code"]),
            "DAC_CMP_A_voltage_v": float(thresholds["DAC_CMP_A"]["voltage_v"]),
            "DAC_CMP_B_voltage_v": float(thresholds["DAC_CMP_B"]["voltage_v"]),
            "DAC_CMP_C_voltage_v": float(thresholds["DAC_CMP_C"]["voltage_v"]),
            "DAC_CMP_D_voltage_v": float(thresholds["DAC_CMP_D"]["voltage_v"]),
            "PX_CMPB_TR": int(trim_maps["AB"][coordinate]),
            "PX_CMPC_TR": int(trim_maps["BC"][coordinate]),
            "PX_CMPD_TR": int(trim_maps["CD"][coordinate]),
            **dict(sample),
        })
    return rows


def _acquire_joint_ref_sweep(
    parent: ExperimentStore,
    client: MGPDClient,
    threshold_calibration_files: Mapping[str, str | Path | ThresholdDacCalibration],
    reference_calibration_files: Mapping[str, str | Path | ReferenceDacCalibration],
    reference_calibration_voltage_unit: str,
    window_results: Mapping[str, CharacterizationResult],
    settings: CharacterizationSettings,
    all_settings: AllWindowSettings,
    pixels: str | Sequence[tuple[int, int]],
    bad_pixel_map: BadPixelMapInput,
    base_pixel_config: Any,
    gain_map: Any,
    shot_executor: ShotExecutor,
    initialization_fclk_mhz: int,
    measurement_fclk_mhz: int,
    eo_overrides: Mapping[str, int] | None,
    injection_voltage_steps_v: Sequence[float],
) -> None:
    calibrations = load_threshold_dac_calibrations(threshold_calibration_files)
    required = {"DAC_CMP_A", "DAC_CMP_B", "DAC_CMP_C", "DAC_CMP_D"}
    if required - set(calibrations):
        raise ValueError("final ALL REF sweep requires all four threshold DAC LUTs")
    low, high, anchors = _baseline_and_high_anchor(
        window_results,
        good_fit_r2=all_settings.good_fit_r2,
    )
    threshold_codes, threshold_records = _select_fixed_thresholds(calibrations, low, high)
    desired = np.linspace(
        float(min(injection_voltage_steps_v)),
        float(max(injection_voltage_steps_v)),
        all_settings.final_ref_step_count,
    )
    reference = load_reference_dac_calibrations(
        reference_calibration_files, voltage_unit=reference_calibration_voltage_unit
    )
    selections = select_reference_dac_pairs(
        reference["DAC_TST_REF1"], reference["DAC_TST_REF2"], desired,
        minimum_reference_code=settings.scurve.minimum_reference_code,
        maximum_reference_code=settings.scurve.maximum_reference_code,
        minimum_reference_voltage_v=settings.scurve.minimum_reference_voltage_v,
        maximum_reference_step_error_v=settings.scurve.maximum_reference_step_error_v,
    )
    amplitudes = []
    seen_codes = set()
    for selection in selections:
        amplitude = selection.to_pulse_amplitude()
        key = (amplitude["DAC_TST_REF1"], amplitude["DAC_TST_REF2"])
        if key in seen_codes:
            continue
        seen_codes.add(key)
        amplitudes.append(amplitude)
    if len(amplitudes) < 2:
        raise RuntimeError("REF LUT resolution produced fewer than two distinct final steps")
    requested_pixels = resolve_pixels(pixels, OWNED_COLUMNS)
    bad = normalize_bad_pixel_map(bad_pixel_map)
    selected_pixels = tuple(pixel for pixel in requested_pixels if pixel not in bad)
    normalized_gain = resolve_gain_map(
        gain_map, required_pixels=selected_pixels, owned_columns=OWNED_COLUMNS
    )
    trim_maps = {window: dict(window_results[window].trim_map) for window in _WINDOWS}
    for window, trim in trim_maps.items():
        missing = set(selected_pixels) - set(trim)
        if missing:
            raise RuntimeError(f"final trim map for {window} misses {len(missing)} pixels")
    joint_scurve = copy.deepcopy(settings.scurve)
    joint_scurve.repeats = all_settings.final_ref_repeats
    if isinstance(shot_executor, UpoPwmShotExecutor):
        joint_scurve.n_injections = shot_executor.nominal_injections(
            float(joint_scurve.shutter_duration_s),
            counter_mode_bits=settings.noise.counter_mode_bits,
        )[0]
    backend = MGPDMeasurementBackend(
        client, base_pixel_configs=base_pixel_config, counter_key="high",
        noise_settings=settings.noise, shot_executor=shot_executor,
        status_callback=parent.log_status, bad_pixel_map=bad,
    )
    backend.initialize_standard_configuration(
        selected_pixels, fclk_mhz=initialization_fclk_mhz,
        measurement_fclk_mhz=measurement_fclk_mhz, eo_overrides=eo_overrides,
    )
    backend.configure_threshold_codes(threshold_codes)
    for window in _WINDOWS:
        backend.program_trim_map(
            get_window_spec(window), selected_pixels, trim_maps[window], commit=False
        )
    parent.update_metadata(joint_ref_sweep={
        "status": "in_progress", "anchors": anchors,
        "low_anchor_v": low, "high_anchor_v": high,
        "fixed_thresholds": threshold_records,
        "threshold_spacing": "equal_physical_voltage_intervals",
        "DAC_CMP_A_policy": "explicit_code_1023_with_physical_order_validation",
        "requested_ref_step_count": all_settings.final_ref_step_count,
        "distinct_ref_step_count": len(amplitudes),
        "repeats_per_charge": all_settings.final_ref_repeats,
        "paired_background": True,
        "intermediate_ref_verification": (
            "selected_from_measured_LUT; standard S-curve endpoints may have scope verification; "
            "intermediate points are not independently scope-verified"
        ),
    })
    atomic_write_table(parent.root / "inputs" / "joint_ref_pair_selection.csv", pd.DataFrame(amplitudes))
    groups = build_injection_groups(selected_pixels, all_settings.final_ref_injection_pattern)
    total_pairs = len(groups) * len(amplitudes) * all_settings.final_ref_repeats
    completed_pairs = 0
    snapshot = backend.snapshot_pixel_configs(selected_pixels)
    try:
        for group in groups:
            backend.program_scurve_pixel_configuration(
                selected_pixels, gain_map=normalized_gain,
                active_injection_pixels=group.active_pixels, commit=True,
            )
            active = set(group.active_pixels) - set(bad)
            for amplitude_index, amplitude in enumerate(amplitudes):
                backend.configure_test_pulse_amplitude(amplitude)
                for repeat in range(all_settings.final_ref_repeats):
                    seed = json.dumps([
                        "joint", group.group_id, amplitude["DAC_TST_REF2"], repeat
                    ], separators=(",", ":"))
                    pair_id = ExperimentStore.acquisition_id(seed)
                    common = {
                        "measurement_kind": "all_window_ref_sweep",
                        "stage": f"ref_step_{amplitude_index:03d}",
                        "scan_phase": "linear_ref2_sweep",
                        "threshold_dac_code": int(amplitude["DAC_TST_REF2"]),
                        "repeat_index": repeat,
                        "pulse_amplitude": amplitude,
                        "injection_pattern": group.pattern,
                        "injection_group_id": group.group_id,
                    }
                    for acquisition_type, test_pulses in (("background", False), ("signal", True)):
                        descriptor = {**common, "acquisition_type": acquisition_type}
                        if parent.is_complete(descriptor):
                            continue
                        request = ShotRequest(
                            measurement_kind="all_window_ref_sweep",
                            acquisition_type=acquisition_type,
                            shutter_duration_s=joint_scurve.shutter_duration_s,
                            test_pulses=test_pulses,
                            n_injections=joint_scurve.n_injections if test_pulses else None,
                            pulse_amplitude=amplitude if test_pulses else None,
                            configure_get_shot_omr=settings.noise.configure_get_shot_omr,
                            counter_mode_bits=settings.noise.counter_mode_bits,
                            mode_read=settings.noise.mode_read,
                            crw_mode=settings.noise.crw_mode,
                            measurement_fclk_mhz=measurement_fclk_mhz,
                        )
                        samples, shot_result = backend.acquire(selected_pixels, request)
                        rows = _joint_raw_rows(
                            parent, descriptor, samples, shot_result, amplitude,
                            threshold_records, trim_maps, active,
                            float(joint_scurve.shutter_duration_s),
                            joint_scurve.injection_capacitance_f,
                            joint_scurve.injection_capacitance_relative_uncertainty,
                            pair_id,
                        )
                        parent.write_acquisition(descriptor, rows)
                    completed_pairs += 1
                    percent = 100.0 * completed_pairs / max(total_pairs, 1)
                    if completed_pairs == 1 or completed_pairs == total_pairs or completed_pairs % max(1, total_pairs // 20) == 0:
                        parent.log_status(
                            f"ALL final REF sweep: {completed_pairs}/{total_pairs} пар, "
                            f"{percent:.1f}%", stage_percent=percent,
                        )
        parent.update_metadata(joint_ref_sweep={
            **parent.metadata["joint_ref_sweep"], "status": "complete",
            "completed_utc": utc_now_text(),
        })
    finally:
        original_error = __import__("sys").exc_info()[1]
        try:
            if isinstance(shot_executor, UpoPwmShotExecutor):
                shot_executor.return_to_idle(client)
            elif isinstance(shot_executor, KeysightBurstShotExecutor):
                shot_executor.return_to_idle()
        finally:
            if original_error is None and backend.safe_for_pixel_cleanup:
                backend.restore_pixel_configs(
                    snapshot, commit=True,
                    commit_context="ALL final REF sweep cleanup restore",
                )


def characterize_all_windows(
    client: MGPDClient,
    threshold_calibration_files: Mapping[str, str | Path | ThresholdDacCalibration],
    *,
    all_window_settings: AllWindowSettings | None = None,
    resume_experiment: str | Path | None = None,
    before_window: Callable[[AllWindowExposureChange], None] | None = None,
    **kwargs: Any,
) -> AllWindowCharacterizationResult:
    """Run AB, BC and CD sequentially and then correlate the same pixels."""

    selected_all = copy.deepcopy(all_window_settings) if all_window_settings is not None else AllWindowSettings()
    selected_all.validate()
    settings = copy.deepcopy(kwargs.get("settings") or CharacterizationSettings())
    settings.validate()
    run_noise = bool(kwargs.get("run_noise_scan", True) or kwargs.get("run_equalization", True))
    run_scurve = bool(kwargs.get("run_scurve", True))
    results_root = kwargs.get("results_root", "results")
    if resume_experiment is None:
        parent = ExperimentStore.create(results_root, window="ALL", metadata={
            "comparator_characterization_version": FRAMEWORK_VERSION,
            "window": "ALL",
            "windows": list(_WINDOWS),
            "all_window_settings": asdict(selected_all),
            "window_runs": {},
            "run_options": {"run_noise_scan": run_noise, "run_scurve": run_scurve},
        })
    else:
        parent = ExperimentStore(resume_experiment)
        if str(parent.metadata.get("window", "")).upper() != "ALL":
            raise ValueError("resume experiment is not a WINDOW='ALL' parent")
        if parent.metadata.get("all_window_settings") != asdict(selected_all):
            raise ValueError("resume ALL-window settings differ from the original run")
        parent.update_metadata(status="in_progress", resumed_utc=utc_now_text())
    window_results: dict[str, CharacterizationResult] = {}
    records = _window_records(parent)
    completed: list[str] = []
    try:
        for window in _WINDOWS:
            record = records.get(window)
            child_resume = None
            if record is not None:
                child_path = _resolve_child_path(parent, record)
                child_store = ExperimentStore(child_path)
                if str(child_store.metadata.get("status")) == "complete":
                    window_results[window] = _result_from_saved_experiment(child_path)
                    completed.append(window)
                    parent.log_status(f"ALL: окно {window} уже завершено, пропуск")
                    continue
                child_resume = child_path
            elif resume_experiment is not None:
                child_resume = _discover_incomplete_child(parent, window)
                if child_resume is not None:
                    records[window] = {
                        "status": "discovered_for_resume",
                        "experiment_path": child_resume.relative_to(parent.root).as_posix(),
                    }
                    parent.update_metadata(window_runs=records)
                    parent.log_status(
                        f"ALL: найден незавершенный эксперимент окна {window}: {child_resume}"
                    )
            if run_noise:
                change = AllWindowExposureChange(
                    parent_experiment_path=parent.root,
                    next_window=window,
                    noise_shutter_duration_s=settings.noise.shutter_duration_s,
                    completed_windows=tuple(completed),
                )
                parent.update_metadata(
                    status="awaiting_manual_noise_exposure_confirmation",
                    next_window=window,
                )
                (before_window or interactive_all_window_noise_pause)(change)
                parent.update_metadata(status="in_progress")
            child_kwargs = dict(kwargs)
            child_kwargs["window"] = window
            child_kwargs["results_root"] = parent.root / "windows"
            child_kwargs["settings"] = settings
            result = characterize_comparator(
                client, threshold_calibration_files,
                resume_experiment=child_resume, **child_kwargs,
            )
            window_results[window] = result
            completed.append(window)
            records[window] = {
                "status": result.status,
                "experiment_path": result.experiment_path.relative_to(parent.root).as_posix(),
                "analysis_path": result.analysis_path.relative_to(parent.root).as_posix() if result.analysis_path else None,
                "completed_utc": utc_now_text(),
            }
            parent.update_metadata(window_runs=records)
            parent.log_status(f"ALL: окно {window} завершено ({len(completed)}/3)")
        if run_scurve and selected_all.final_ref_sweep_enabled:
            joint_executor = kwargs.get("shot_executor")
            if joint_executor is None and kwargs.get("keysight_generator") is not None:
                joint_executor = KeysightBurstShotExecutor(
                    kwargs["keysight_generator"],
                    settings=kwargs.get("keysight_burst_settings") or KeysightBurstSettings(),
                )
            required_arguments = (
                "reference_calibration_files", "injection_voltage_steps_v",
                "gain_map",
            )
            missing = [name for name in required_arguments if kwargs.get(name) is None]
            if joint_executor is None:
                missing.append("shot_executor or keysight_generator")
            if missing:
                raise ValueError(
                    "final ALL REF sweep requires: " + ", ".join(missing)
                )
            _acquire_joint_ref_sweep(
                parent, client, threshold_calibration_files,
                kwargs["reference_calibration_files"],
                kwargs.get("reference_calibration_voltage_unit", "auto"),
                window_results, settings, selected_all,
                kwargs.get("pixels", "all"), kwargs.get("bad_pixel_map"),
                kwargs.get("base_pixel_config"), kwargs.get("gain_map"),
                joint_executor,
                int(kwargs.get("initialization_fclk_mhz", 25)),
                int(kwargs.get("measurement_fclk_mhz") or kwargs.get("initialization_fclk_mhz", 25)),
                kwargs.get("eo_overrides"),
                tuple(float(value) for value in kwargs["injection_voltage_steps_v"]),
            )
        parent.update_metadata(status="analysis_in_progress")
        outputs = analyze_all_windows(parent.root, settings=settings.analysis)
        analysis_path = Path(outputs["analysis_directory"])
        parent.update_metadata(
            status="complete", completed_utc=utc_now_text(),
            latest_analysis=analysis_path.relative_to(parent.root).as_posix(),
        )
        parent.log_status(f"ALL: общий анализ завершен: {analysis_path}")
        return AllWindowCharacterizationResult(
            experiment_path=parent.root,
            analysis_path=analysis_path,
            window_results=window_results,
            status="complete",
        )
    except BaseException as error:
        parent.record_error({
            "timestamp_utc": utc_now_text(), "scope": "all_window_pipeline",
            "error_type": type(error).__name__, "error": str(error),
        })
        parent.update_metadata(status="failed", error=str(error))
        raise
