from __future__ import annotations

from dataclasses import asdict
import json
import logging
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .calibration import ThresholdDacCalibration
from .models import AnalysisSettings, FRAMEWORK_VERSION
from .parallel import map_analysis_groups
from .pixel_masks import (
    BadPixelMapInput, bad_pixel_document, exclude_bad_pixel_rows, normalize_bad_pixel_map,
)
from .recommendations import save_noise_recommendations
from .storage import ExperimentStore, atomic_write_json, atomic_write_table, file_sha256, utc_now_text


_NORMAL = NormalDist()
logger = logging.getLogger(__name__)


def _as_bool(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin(("true", "1", "yes"))


def _finite_or_nan(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if math.isfinite(result) else float("nan")


def _mad(values: pd.Series) -> float:
    array = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if not len(array):
        return float("nan")
    median = float(np.median(array))
    return float(np.median(np.abs(array - median)))


def _finite_median(frame: pd.DataFrame, column: str, *, absolute: bool = False) -> float:
    if frame.empty or column not in frame:
        return float("nan")
    values = pd.to_numeric(frame[column], errors="coerce")
    values = values[np.isfinite(values)]
    if absolute:
        values = values.abs()
    return float(values.median()) if len(values) else float("nan")


def _prepare_noise_raw(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return raw.copy()
    frame = raw.copy()
    numeric_columns = (
        "threshold_dac_code",
        "threshold_voltage_v",
        "local_trim_code",
        "repeat_index",
        "column",
        "row",
        "selected_count",
        "shutter_duration_s",
    )
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["measurement_valid_bool"] = _as_bool(frame["measurement_valid"])
    frame["counter_saturated_bool"] = (
        _as_bool(frame["counter_saturated"])
        if "counter_saturated" in frame
        else False
    )
    frame["phase_priority"] = frame["scan_phase"].map(
        {"coarse": 1, "fine": 2, "manual": 3}
    ).fillna(0)

    # If a two-stage scan measured the same DAC point in coarse and fine phases,
    # use the fine repetitions for statistical fitting. The coarse raw files
    # remain untouched and available for alternative offline analysis.
    identity = ["stage", "threshold_dac_code", "column", "row", "local_trim_code"]
    preferred = frame.groupby(identity, dropna=False)["phase_priority"].transform("max")
    frame["preferred_for_default_analysis"] = frame["phase_priority"] == preferred
    return frame


def calculate_noise_statistics(raw: pd.DataFrame) -> pd.DataFrame:
    """Calculate repeat statistics without modifying or replacing raw data."""

    frame = _prepare_noise_raw(raw)
    if frame.empty:
        return pd.DataFrame()
    frame = frame[frame["preferred_for_default_analysis"]].copy()
    group_columns = [
        "stage",
        "window",
        "comparator_under_test",
        "threshold_dac",
        "threshold_dac_code",
        "threshold_voltage_v",
        "local_trim_field",
        "local_trim_code",
        "shutter_duration_s",
        "column",
        "row",
    ]
    # Native grouped reductions avoid a Python DataFrame allocation/filter for
    # every (pixel, DAC code). Invalid readings remain NaN, never zero counts.
    frame["valid_count"] = frame["selected_count"].astype(float).where(
        frame["measurement_valid_bool"] & frame["selected_count"].notna()
    )
    grouped = frame.groupby(group_columns, dropna=False, sort=True, observed=True)
    frame["absolute_deviation"] = (
        frame["valid_count"] - grouped["valid_count"].transform("median")
    ).abs()
    result = grouped.agg(
        repeat_count_total=("selected_count", "size"),
        repeat_count_valid=("valid_count", "count"),
        repeat_count_saturated=("counter_saturated_bool", "sum"),
        repeat_fraction_saturated=("counter_saturated_bool", "mean"),
        mean_count=("valid_count", "mean"),
        median_count=("valid_count", "median"),
        std_count=("valid_count", "std"),
        min_count=("valid_count", "min"),
        max_count=("valid_count", "max"),
        mad_count=("absolute_deviation", "median"),
    ).reset_index()
    result["repeat_count_invalid"] = result["repeat_count_total"] - result["repeat_count_valid"]
    result["sem_count"] = result["std_count"] / np.sqrt(result["repeat_count_valid"].where(
        result["repeat_count_valid"] >= 2
    ))
    return result[group_columns + [
        "repeat_count_total", "repeat_count_valid", "repeat_count_invalid",
        "repeat_count_saturated", "repeat_fraction_saturated", "mean_count",
        "median_count", "std_count", "sem_count", "min_count", "max_count", "mad_count",
    ]]


def _weighted_linear_fit(
    design: np.ndarray,
    values: np.ndarray,
    weights: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    sqrt_weights = np.sqrt(np.clip(weights, 1e-12, np.inf))
    weighted_design = design * sqrt_weights[:, None]
    weighted_values = values * sqrt_weights
    parameters, _, rank, _ = np.linalg.lstsq(weighted_design, weighted_values, rcond=None)
    if rank < design.shape[1]:
        raise np.linalg.LinAlgError("fit design matrix is rank deficient")
    residuals = values - design @ parameters
    degrees = max(len(values) - design.shape[1], 1)
    weighted_variance = float(np.sum(weights * residuals**2) / degrees)
    covariance = np.linalg.pinv(design.T @ (weights[:, None] * design)) * weighted_variance
    return parameters, covariance, residuals


def _gaussian_log_fit(
    voltage: np.ndarray,
    counts: np.ndarray,
    sem: np.ndarray,
) -> dict[str, float]:
    background = float(np.nanpercentile(counts, 10))
    adjusted = counts - background
    dynamic = float(np.nanmax(adjusted))
    use = np.isfinite(voltage) & np.isfinite(adjusted) & (adjusted > max(dynamic * 0.03, 1e-12))
    if int(np.sum(use)) < 5:
        raise ValueError("insufficient positive points for Gaussian fit")
    x = voltage[use]
    y = adjusted[use]
    sem_use = sem[use]
    center_scale = float(np.mean(x))
    voltage_scale = float(np.ptp(x) / 2)
    if voltage_scale <= 0:
        raise ValueError("zero voltage span")
    u = (x - center_scale) / voltage_scale
    log_y = np.log(y)
    sigma_log = np.divide(
        sem_use,
        y,
        out=np.full_like(y, np.nan),
        where=(np.isfinite(sem_use) & (sem_use > 0)),
    )
    finite_sigma = sigma_log[np.isfinite(sigma_log) & (sigma_log > 0)]
    fallback_sigma = float(np.nanmedian(finite_sigma)) if len(finite_sigma) else 1.0
    sigma_log = np.where(
        np.isfinite(sigma_log) & (sigma_log > 0), sigma_log, fallback_sigma
    )
    weights = np.clip(1.0 / sigma_log**2, 1e-6, 1e12)
    design = np.column_stack((u**2, u, np.ones_like(u)))
    parameters, covariance, _ = _weighted_linear_fit(design, log_y, weights)
    a, b, c = parameters
    if a >= 0:
        raise ValueError("Gaussian log-quadratic curvature is not negative")
    center_u = -b / (2 * a)
    sigma_u = math.sqrt(-1 / (2 * a))
    center = center_scale + center_u * voltage_scale
    sigma_v = sigma_u * voltage_scale
    amplitude = math.exp(c - (b * b) / (4 * a))
    prediction = background + amplitude * np.exp(-0.5 * ((voltage - center) / sigma_v) ** 2)
    residual = counts - prediction
    total_variance = float(np.sum((counts - np.mean(counts)) ** 2))
    r2 = 1 - float(np.sum(residual**2)) / total_variance if total_variance > 0 else float("nan")
    rmse = float(np.sqrt(np.mean(residual**2)))

    gradient_center = np.array([b / (2 * a * a), -1 / (2 * a), 0.0])
    center_u_variance = float(gradient_center @ covariance @ gradient_center)
    center_uncertainty = math.sqrt(max(center_u_variance, 0.0)) * voltage_scale
    derivative_sigma_a = -sigma_u / (2 * a)
    sigma_variance = derivative_sigma_a**2 * float(covariance[0, 0])
    sigma_uncertainty = math.sqrt(max(sigma_variance, 0.0)) * voltage_scale

    finite_sem = np.isfinite(sem) & (sem > 0)
    chi2 = (
        float(np.sum((residual[finite_sem] / sem[finite_sem]) ** 2))
        if np.any(finite_sem)
        else float("nan")
    )
    dof = max(int(np.sum(finite_sem)) - 4, 1)
    return {
        "center": center,
        "sigma": sigma_v,
        "center_uncertainty": center_uncertainty,
        "sigma_uncertainty": sigma_uncertainty,
        "amplitude": amplitude,
        "background": background,
        "r2": r2,
        "rmse": rmse,
        "reduced_chi2": chi2 / dof if math.isfinite(chi2) else float("nan"),
    }


def _edge_probit_fit(voltage: np.ndarray, counts: np.ndarray) -> dict[str, float | str]:
    low = float(np.nanpercentile(counts, 5))
    high = float(np.nanpercentile(counts, 95))
    if high <= low:
        raise ValueError("edge curve has zero dynamic range")
    probability = np.clip((counts - low) / (high - low), 1e-4, 1 - 1e-4)
    correlation = float(np.corrcoef(voltage, probability)[0, 1])
    direction = "ascending" if correlation >= 0 else "descending"
    if direction == "descending":
        probability = 1 - probability
    z = np.array([_NORMAL.inv_cdf(float(value)) for value in probability])
    use = np.isfinite(voltage) & np.isfinite(z) & (probability > 0.02) & (probability < 0.98)
    if int(np.sum(use)) < 4:
        raise ValueError("insufficient edge-transition points")
    design = np.column_stack((np.ones(int(np.sum(use))), z[use]))
    parameters, covariance, _ = _weighted_linear_fit(
        design,
        voltage[use],
        np.ones(int(np.sum(use))),
    )
    center, slope = parameters
    sigma = abs(float(slope))
    if sigma <= 0:
        raise ValueError("invalid edge width")
    signed = 1 if direction == "ascending" else -1
    predicted_probability = np.array(
        [_NORMAL.cdf(signed * (value - center) / sigma) for value in voltage]
    )
    predicted = low + (high - low) * predicted_probability
    total_variance = float(np.sum((counts - np.mean(counts)) ** 2))
    r2 = 1 - float(np.sum((counts - predicted) ** 2)) / total_variance if total_variance else float("nan")
    return {
        "center": float(center),
        "sigma": sigma,
        "center_uncertainty": math.sqrt(max(float(covariance[0, 0]), 0.0)),
        "sigma_uncertainty": math.sqrt(max(float(covariance[1, 1]), 0.0)),
        "r2": r2,
        "rmse": float(np.sqrt(np.mean((counts - predicted) ** 2))),
        "reduced_chi2": float("nan"),
        "direction": direction,
        "background": low,
        "amplitude": high - low,
    }


def fit_noise_curve(group: pd.DataFrame, settings: AnalysisSettings) -> dict[str, Any]:
    data = group.sort_values("threshold_voltage_v").copy()
    voltage = data["threshold_voltage_v"].to_numpy(dtype=float)
    codes = data["threshold_dac_code"].to_numpy(dtype=float)
    counts = data["mean_count"].to_numpy(dtype=float)
    sem = data["sem_count"].to_numpy(dtype=float)
    finite = np.isfinite(voltage) & np.isfinite(counts)
    voltage, codes, counts, sem = voltage[finite], codes[finite], counts[finite], sem[finite]
    result: dict[str, Any] = {
        "points": int(len(voltage)),
        "center_fit_v": float("nan"),
        "center_fit_uncertainty_v": float("nan"),
        "sigma_fit_v": float("nan"),
        "sigma_fit_uncertainty_v": float("nan"),
        "center_centroid_v": float("nan"),
        "center_max_v": float("nan"),
        "center_max_dac_code": float("nan"),
        "fit_model": "none",
        "fit_status": "insufficient_data",
        "fit_r2": float("nan"),
        "fit_rmse_count": float("nan"),
        "fit_reduced_chi2": float("nan"),
        "fit_amplitude_count": float("nan"),
        "fit_background_count": float("nan"),
        "fit_transition_direction": "",
        "curve_shape": "unknown",
        "centroid_applicable": False,
        "maximum_estimator_applicable": False,
        "center_selected_v": float("nan"),
        "center_selected_method": "none",
        "diagnostic_flags": "[]",
        "scan_min_voltage_v": float(np.min(voltage)) if len(voltage) else float("nan"),
        "scan_max_voltage_v": float(np.max(voltage)) if len(voltage) else float("nan"),
        "nonzero_points": int(np.sum(counts > 0)),
        "candidate_center_fit_v": float("nan"),
        "candidate_sigma_fit_v": float("nan"),
        "centroid_weighting": "trapezoidal_voltage_integral",
    }
    if len(voltage) < settings.noise_min_points:
        return result

    flags: list[str] = []
    if "repeat_count_saturated" in data and (
        pd.to_numeric(data["repeat_count_saturated"], errors="coerce").fillna(0) > 0
    ).any():
        flags.append("counter_saturation_present_curve_may_be_clipped")
    peak_index = int(np.nanargmax(counts))
    peak_count = float(counts[peak_index])
    baseline = float(np.nanpercentile(counts, 10))
    dynamic = peak_count - baseline
    if dynamic <= max(abs(peak_count), 1.0) * 1e-9:
        flags.append("flat_response")
        result["curve_shape"] = "flat"
        result["diagnostic_flags"] = json.dumps(flags)
        return result

    sem_clean = np.where(np.isfinite(sem) & (sem >= 0), sem, 0.0)
    indistinguishable = counts >= (
        peak_count
        - settings.plateau_sigma_factor
        * np.sqrt(sem_clean**2 + sem_clean[peak_index] ** 2)
    )
    plateau_indices = [peak_index]
    left = peak_index - 1
    while left >= 0 and indistinguishable[left]:
        plateau_indices.insert(0, left)
        left -= 1
    right = peak_index + 1
    while right < len(counts) and indistinguishable[right]:
        plateau_indices.append(right)
        right += 1
    plateau_weights = np.clip(counts[plateau_indices] - baseline, 0, np.inf)
    if float(np.sum(plateau_weights)) > 0:
        center_max = float(
            np.average(voltage[plateau_indices], weights=plateau_weights)
        )
        center_max_code = float(
            np.average(codes[plateau_indices], weights=plateau_weights)
        )
    else:
        center_max = float(np.mean(voltage[plateau_indices]))
        center_max_code = float(np.mean(codes[plateau_indices]))
    result["center_max_v"] = center_max
    result["center_max_dac_code"] = center_max_code
    result["maximum_estimator_applicable"] = True
    if len(plateau_indices) > 1:
        flags.append("statistical_maximum_plateau")

    centroid_weights = np.clip(counts - baseline, 0, np.inf)
    # Coarse and fine points have unequal voltage spacing. Summing counts per
    # sample would spuriously favor the densely scanned region.
    dv = np.diff(voltage)
    area = float(np.sum(0.5 * (centroid_weights[:-1] + centroid_weights[1:]) * dv))
    if area > 0:
        first_moment = float(np.sum(0.5 * (
            voltage[:-1] * centroid_weights[:-1] + voltage[1:] * centroid_weights[1:]
        ) * dv))
        result["center_centroid_v"] = float(
            first_moment / area
        )

    half = baseline + 0.5 * dynamic
    left_half = np.where(counts[:peak_index] <= half)[0]
    right_half = np.where(counts[peak_index + 1 :] <= half)[0]
    has_left = len(left_half) > 0
    has_right = len(right_half) > 0
    edge_like = peak_index in (0, len(counts) - 1) or not (has_left and has_right)
    asymmetry = float("nan")
    if has_left and has_right:
        left_voltage = voltage[left_half[-1]]
        right_voltage = voltage[peak_index + 1 + right_half[0]]
        left_width = abs(voltage[peak_index] - left_voltage)
        right_width = abs(right_voltage - voltage[peak_index])
        if min(left_width, right_width) > 0:
            asymmetry = max(left_width, right_width) / min(left_width, right_width)
            if asymmetry > settings.max_asymmetry_ratio:
                flags.append("strong_asymmetry")

    result["centroid_applicable"] = not edge_like
    if edge_like:
        result["center_centroid_v"] = float("nan")
        flags.append("weighted_centroid_not_applicable_to_edge_or_clipped_curve")

    fit: dict[str, Any] | None = None
    if edge_like:
        result["curve_shape"] = "edge_or_clipped"
        flags.append("missing_two_sided_half_maximum")
        try:
            fit = _edge_probit_fit(voltage, counts)
            result["fit_model"] = "edge_probit"
        except (ValueError, np.linalg.LinAlgError, OverflowError) as error:
            flags.append(f"edge_fit_failed:{error}")
    elif math.isfinite(asymmetry) and asymmetry > settings.max_asymmetry_ratio:
        result["curve_shape"] = "asymmetric_bell"
        result["fit_model"] = "plateau_center_fallback"
        result["fit_status"] = "unsupported_shape"
        result["center_fit_v"] = center_max
    else:
        result["curve_shape"] = "bell_like"
        try:
            fit = _gaussian_log_fit(voltage, counts, sem)
            result["fit_model"] = "gaussian_log_quadratic"
        except (ValueError, np.linalg.LinAlgError, OverflowError) as error:
            flags.append(f"gaussian_fit_failed:{error}")

    if fit is not None:
        physical_fit = (
            math.isfinite(float(fit["center"]))
            and math.isfinite(float(fit["sigma"]))
            and float(voltage.min()) <= float(fit["center"]) <= float(voltage.max())
            and 0 < float(fit["sigma"]) <= float(np.ptp(voltage))
        )
        result.update(
            {
                "candidate_center_fit_v": fit["center"],
                "candidate_sigma_fit_v": fit["sigma"],
                "center_fit_v": fit["center"] if physical_fit else float("nan"),
                "center_fit_uncertainty_v": fit["center_uncertainty"],
                "sigma_fit_v": fit["sigma"] if physical_fit else float("nan"),
                "sigma_fit_uncertainty_v": fit["sigma_uncertainty"],
                "fit_r2": fit["r2"],
                "fit_rmse_count": fit["rmse"],
                "fit_reduced_chi2": fit["reduced_chi2"],
                "fit_amplitude_count": fit.get("amplitude", float("nan")),
                "fit_background_count": fit.get("background", float("nan")),
                "fit_transition_direction": fit.get("direction", ""),
                "fit_status": (
                    "ok"
                    if math.isfinite(float(fit["r2"]))
                    and float(fit["r2"]) >= settings.gaussian_min_r2
                    else "poor_quality"
                ),
            }
        )
        if not physical_fit:
            result["fit_status"] = "unphysical_fit"
            flags.append("fit_center_outside_scan_or_invalid_sigma")
        if result["fit_status"] != "ok":
            if physical_fit:
                flags.append("fit_below_r2_threshold")

    if result["fit_status"] == "ok" and math.isfinite(result["center_fit_v"]):
        result["center_selected_v"] = result["center_fit_v"]
        result["center_selected_method"] = "fit"
    elif math.isfinite(result["center_centroid_v"]) and not edge_like:
        result["center_selected_v"] = result["center_centroid_v"]
        result["center_selected_method"] = "weighted_centroid"
    elif math.isfinite(result["center_max_v"]):
        result["center_selected_v"] = result["center_max_v"]
        result["center_selected_method"] = "maximum_plateau_center"
    result["diagnostic_flags"] = json.dumps(flags, ensure_ascii=False)
    return result


def _fit_noise_job(job: tuple) -> dict[str, Any]:
    row, group, settings, calibration = job
    row.update(fit_noise_curve(group, settings))
    if calibration is not None and math.isfinite(_finite_or_nan(row["center_selected_v"])):
        row["center_selected_nearest_dac_code"] = calibration.voltage_to_nearest_dac_code(
            row["center_selected_v"]
        )
        row["center_fit_nearest_dac_code"] = (
            calibration.voltage_to_nearest_dac_code(row["center_fit_v"])
            if math.isfinite(_finite_or_nan(row["center_fit_v"])) else float("nan")
        )
    return row


def fit_noise_statistics(
    statistics: pd.DataFrame,
    *,
    settings: AnalysisSettings,
    calibration: ThresholdDacCalibration | None = None,
) -> pd.DataFrame:
    if statistics.empty:
        return pd.DataFrame()
    group_columns = [
        "stage",
        "window",
        "comparator_under_test",
        "threshold_dac",
        "local_trim_field",
        "local_trim_code",
        "column",
        "row",
    ]
    settings.validate()
    # Workers need only the numeric curve, not all raw/metadata columns.
    needed = [name for name in (
        "threshold_voltage_v", "threshold_dac_code", "mean_count", "sem_count",
        "repeat_count_saturated",
    ) if name in statistics]
    groups = statistics[group_columns + needed].groupby(group_columns, dropna=False, sort=True)

    def jobs():
        for keys, group in groups:
            yield dict(zip(group_columns, keys)), group, settings, calibration

    rows = map_analysis_groups(_fit_noise_job, jobs, total=groups.ngroups,
                               settings=settings, label="Noise fit")
    return pd.DataFrame(rows)


def select_equalization_target(
    noise_fits: pd.DataFrame,
    *,
    trim0_stage: str = "trim_00",
    trim31_stage: str = "trim_31",
    requested_target_voltage: float | None = None,
) -> tuple[float, str, pd.DataFrame]:
    endpoint0 = noise_fits[noise_fits["stage"] == trim0_stage].copy()
    endpoint31 = noise_fits[noise_fits["stage"] == trim31_stage].copy()
    columns = ["column", "row", "center_selected_v", "fit_status"]
    merged = endpoint0[columns].merge(
        endpoint31[columns],
        on=["column", "row"],
        suffixes=("_trim0", "_trim31"),
        how="outer",
    )
    merged["reachable_min_v"] = merged[
        ["center_selected_v_trim0", "center_selected_v_trim31"]
    ].min(axis=1, skipna=False)
    merged["reachable_max_v"] = merged[
        ["center_selected_v_trim0", "center_selected_v_trim31"]
    ].max(axis=1, skipna=False)
    valid = merged[
        np.isfinite(merged["reachable_min_v"])
        & np.isfinite(merged["reachable_max_v"])
    ].copy()
    if valid.empty:
        raise RuntimeError("no pixels have valid centers at both trim endpoints")

    if requested_target_voltage is not None:
        target = float(requested_target_voltage)
        if not math.isfinite(target):
            raise ValueError("requested target voltage must be finite")
        method = "user_supplied_target_voltage"
    else:
        endpoints = np.unique(
            np.concatenate(
                [
                    valid["reachable_min_v"].to_numpy(dtype=float),
                    valid["reachable_max_v"].to_numpy(dtype=float),
                ]
            )
        )
        dense = np.linspace(float(endpoints.min()), float(endpoints.max()), 2001)
        candidates = np.unique(np.concatenate((endpoints, dense)))
        midpoint_reference = float(
            np.median(
                (
                    valid["center_selected_v_trim0"].to_numpy(dtype=float)
                    + valid["center_selected_v_trim31"].to_numpy(dtype=float)
                )
                / 2
            )
        )
        best_score: tuple[float, ...] | None = None
        target = midpoint_reference
        for candidate in candidates:
            reachable = (
                (valid["reachable_min_v"].to_numpy(dtype=float) <= candidate)
                & (candidate <= valid["reachable_max_v"].to_numpy(dtype=float))
            )
            v0 = valid["center_selected_v_trim0"].to_numpy(dtype=float)
            v31 = valid["center_selected_v_trim31"].to_numpy(dtype=float)
            slope = (v31 - v0) / 31.0
            with np.errstate(divide="ignore", invalid="ignore"):
                trim_float = (candidate - v0) / slope
            trim_integer = np.clip(np.rint(trim_float), 0, 31)
            predicted = v0 + trim_integer * slope
            residual = predicted - candidate
            saturation = (trim_integer == 0) | (trim_integer == 31)
            trim_headroom = np.minimum(trim_integer, 31 - trim_integer)
            score = (
                -float(np.sum(reachable)),
                float(np.sum(saturation & reachable)),
                -float(np.nanmedian(trim_headroom[reachable])) if np.any(reachable) else np.inf,
                float(np.sqrt(np.nanmean(residual[reachable] ** 2))) if np.any(reachable) else np.inf,
                abs(float(candidate) - midpoint_reference),
            )
            if best_score is None or score < best_score:
                best_score = score
                target = float(candidate)
        method = (
            "maximize_common_reach_then_minimize_predicted_saturation_"
            "maximize_trim_headroom_and_minimize_integer_trim_residual"
        )

    merged["target_voltage_v"] = target
    merged["target_reachable"] = (
        (merged["reachable_min_v"] <= target)
        & (target <= merged["reachable_max_v"])
    )
    slope = (
        merged["center_selected_v_trim31"] - merged["center_selected_v_trim0"]
    ) / 31.0
    merged["trim_slope_v_per_code"] = slope
    with np.errstate(divide="ignore", invalid="ignore"):
        estimated = (target - merged["center_selected_v_trim0"]) / slope
    endpoint_distance0 = abs(merged["center_selected_v_trim0"] - target)
    endpoint_distance31 = abs(merged["center_selected_v_trim31"] - target)
    valid_endpoints = (
        np.isfinite(merged["center_selected_v_trim0"])
        & np.isfinite(merged["center_selected_v_trim31"])
    )
    endpoint_fallback = pd.Series(
        np.where(
            valid_endpoints,
            np.where(endpoint_distance31 < endpoint_distance0, 31.0, 0.0),
            np.nan,
        ),
        index=merged.index,
    )
    estimated = estimated.where(np.isfinite(estimated), endpoint_fallback)
    merged["estimated_trim_float"] = estimated
    merged["estimated_trim_code"] = np.clip(np.rint(estimated), 0, 31).astype("Int64")
    merged["predicted_center_v"] = (
        merged["center_selected_v_trim0"]
        + merged["estimated_trim_code"].astype(float) * slope
    )
    merged["predicted_residual_v"] = merged["predicted_center_v"] - target
    return target, method, merged


def choose_measured_trim_map(
    noise_fits: pd.DataFrame,
    *,
    target_voltage: float,
    stage_prefixes: Iterable[str] = ("trim_candidate_", "trim_expand_", "trim_full_"),
    stage_names: Iterable[str] = ("trim_00", "trim_31"),
) -> pd.DataFrame:
    prefixes = tuple(stage_prefixes)
    exact_names = tuple(stage_names)
    stages = noise_fits["stage"].astype(str)
    candidates = noise_fits[
        stages.str.startswith(prefixes) | stages.isin(exact_names)
    ].copy()
    candidates = candidates[np.isfinite(pd.to_numeric(
        candidates["center_selected_v"], errors="coerce"
    ))].copy()
    if candidates.empty:
        return pd.DataFrame()
    candidates["absolute_residual_v"] = abs(
        candidates["center_selected_v"] - target_voltage
    )
    candidates["fit_rank"] = candidates["fit_status"].map(
        {"ok": 0, "poor_quality": 1, "unsupported_shape": 2}
    ).fillna(3)
    candidates = candidates.sort_values(
        ["column", "row", "absolute_residual_v", "fit_rank", "local_trim_code"]
    )
    best = candidates.drop_duplicates(["column", "row"], keep="first").copy()
    best = best.rename(
        columns={
            "local_trim_code": "selected_trim_code",
            "center_selected_v": "selected_measured_center_v",
            "center_selected_method": "selected_center_method",
            "stage": "selected_from_stage",
        }
    )
    best["selected_measured_residual_v"] = (
        best["selected_measured_center_v"] - target_voltage
    )
    return best


def _paired_scurve_efficiency(
    raw: pd.DataFrame,
    *,
    noise_statistics: pd.DataFrame,
    max_background_fraction: float,
) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    frame = raw.copy()
    defaults: dict[str, Any] = {
        "injection_pattern": "all",
        "injection_group_id": "all",
        "injection_phase_column": "",
        "injection_phase_row": "",
        "active_injection_pixel": True,
        "active_injection_pixel_count": np.nan,
        "programmed_injections": np.nan,
        "injections_for_analysis": np.nan,
        "injection_count_source": "legacy_actual_injections",
        "counter_saturated": False,
        "shutter_duration_s": np.nan,
        "injection_voltage_step_v": np.nan,
        "requested_injection_voltage_step_v": np.nan,
        "injection_voltage_step_error_v": np.nan,
        "absolute_injection_voltage_step_error_v": np.nan,
        "ref1_dac_code": np.nan,
        "ref2_dac_code": np.nan,
        "ref1_voltage_v": np.nan,
        "ref2_voltage_v": np.nan,
        "reference_common_mode_v": np.nan,
        "ref_voltage_order": "not_available",
        "reference_pair_selection_method": "not_available",
        "injection_charge_c": np.nan,
        "injection_charge_uncertainty_c": np.nan,
        "injection_charge_electrons": np.nan,
        "injection_capacitance_f": np.nan,
        "injection_capacitance_relative_uncertainty": np.nan,
        "injection_charge_status": "legacy_or_native_amplitude_only",
    }
    for column, default in defaults.items():
        if column not in frame:
            frame[column] = default
    for column in (
        "threshold_dac_code",
        "threshold_voltage_v",
        "column",
        "row",
        "local_trim_code",
        "selected_count",
        "actual_injections",
        "programmed_injections",
        "injections_for_analysis",
        "requested_injections",
        "repeat_index",
        "shutter_duration_s",
        "active_injection_pixel_count",
        "injection_voltage_step_v",
        "requested_injection_voltage_step_v",
        "injection_voltage_step_error_v",
        "absolute_injection_voltage_step_error_v",
        "ref1_dac_code",
        "ref2_dac_code",
        "ref1_voltage_v",
        "ref2_voltage_v",
        "reference_common_mode_v",
        "injection_charge_c",
        "injection_charge_uncertainty_c",
        "injection_charge_electrons",
        "injection_capacitance_f",
        "injection_capacitance_relative_uncertainty",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["injections_for_analysis"] = frame["injections_for_analysis"].fillna(
        frame["actual_injections"]
    )
    frame["injections_for_analysis"] = frame["injections_for_analysis"].fillna(
        frame["requested_injections"]
    )
    frame["valid_bool"] = _as_bool(frame["measurement_valid"])
    frame["counter_saturated_bool"] = _as_bool(frame["counter_saturated"])
    frame["active_injection_pixel_bool"] = _as_bool(
        frame["active_injection_pixel"]
    )
    keys = [
        "pair_id",
        "stage",
        "scan_phase",
        "threshold_dac_code",
        "threshold_voltage_v",
        "pulse_amplitude_native",
        "repeat_index",
        "column",
        "row",
        "local_trim_code",
        "injection_pattern",
        "injection_group_id",
        "injection_phase_column",
        "injection_phase_row",
        "active_injection_pixel_bool",
        "active_injection_pixel_count",
    ]
    background = frame[frame["acquisition_type"] == "background"][
        keys
        + [
            "selected_count",
            "valid_bool",
            "counter_saturated_bool",
            "shutter_duration_s",
        ]
    ].rename(
        columns={
            "selected_count": "background_count",
            "valid_bool": "background_valid",
            "counter_saturated_bool": "background_counter_saturated",
            "shutter_duration_s": "background_shutter_duration_s",
        }
    )
    signal = frame[frame["acquisition_type"] == "signal"][
        keys
        + [
            "selected_count",
            "valid_bool",
            "counter_saturated_bool",
            "actual_injections",
            "programmed_injections",
            "injections_for_analysis",
            "requested_injections",
            "injection_count_source",
            "shutter_duration_s",
            "injection_voltage_step_v",
            "requested_injection_voltage_step_v",
            "injection_voltage_step_error_v",
            "absolute_injection_voltage_step_error_v",
            "ref1_dac_code",
            "ref2_dac_code",
            "ref1_voltage_v",
            "ref2_voltage_v",
            "reference_common_mode_v",
            "ref_voltage_order",
            "reference_pair_selection_method",
            "injection_charge_c",
            "injection_charge_uncertainty_c",
            "injection_charge_electrons",
            "injection_capacitance_f",
            "injection_capacitance_relative_uncertainty",
            "injection_charge_status",
        ]
    ].rename(
        columns={
            "selected_count": "signal_count",
            "valid_bool": "signal_valid",
            "counter_saturated_bool": "signal_counter_saturated",
            "shutter_duration_s": "signal_shutter_duration_s",
        }
    )
    paired = signal.merge(background, on=keys, how="outer", validate="one_to_one")

    expected_columns = [
        "threshold_dac_code",
        "column",
        "row",
        "mean_count",
        "shutter_duration_s",
    ]
    if noise_statistics.empty or "stage" not in noise_statistics:
        final_noise = pd.DataFrame(columns=expected_columns)
    else:
        final_noise = noise_statistics[
            noise_statistics["stage"] == "equalized_final"
        ].copy()
        if final_noise.empty:
            final_noise = noise_statistics[
                noise_statistics["stage"].isin(("baseline_noise", "trim_00"))
            ].copy()
    expected = final_noise.reindex(columns=expected_columns).rename(
        columns={
            "mean_count": "noise_scan_mean_count",
            "shutter_duration_s": "noise_scan_shutter_duration_s",
        }
    )
    if not expected.empty:
        expected = expected.groupby(
            ["threshold_dac_code", "column", "row"], as_index=False
        ).agg(
            noise_scan_mean_count=("noise_scan_mean_count", "mean"),
            noise_scan_shutter_duration_s=(
                "noise_scan_shutter_duration_s",
                "median",
            ),
        )
    paired = paired.merge(
        expected,
        on=["threshold_dac_code", "column", "row"],
        how="left",
    )
    paired["expected_background_rate_hz_from_noise_scan"] = (
        paired["noise_scan_mean_count"]
        / paired["noise_scan_shutter_duration_s"]
    )
    durations_available = (
        (paired["noise_scan_shutter_duration_s"] > 0)
        & (paired["background_shutter_duration_s"] > 0)
    )
    paired["expected_background_count_from_noise_scan"] = np.where(
        durations_available,
        paired["expected_background_rate_hz_from_noise_scan"]
        * paired["background_shutter_duration_s"],
        paired["noise_scan_mean_count"],
    )
    paired["noise_background_duration_scaling_status"] = np.where(
        paired["noise_scan_mean_count"].isna(),
        "noise_reference_missing_at_threshold",
        np.where(
            durations_available,
            "scaled_by_shutter_duration",
            "duration_missing_unscaled_legacy_value",
        ),
    )
    paired["background_limit_count"] = (
        paired["injections_for_analysis"] * float(max_background_fraction)
    )
    paired["background_fraction"] = (
        paired["background_count"] / paired["injections_for_analysis"]
    )
    paired["expected_background_fraction"] = (
        paired["expected_background_count_from_noise_scan"]
        / paired["injections_for_analysis"]
    )
    paired["background_low"] = (
        paired["background_count"] < paired["background_limit_count"]
    ) & (
        paired["expected_background_count_from_noise_scan"].isna()
        | (
            paired["expected_background_count_from_noise_scan"]
            < paired["background_limit_count"]
        )
    )
    paired["background_corrected_detected"] = (
        paired["signal_count"] - paired["background_count"]
    )
    paired["efficiency"] = (
        paired["background_corrected_detected"]
        / paired["injections_for_analysis"]
    )
    paired["inactive_excess_hit_fraction"] = np.where(
        ~paired["active_injection_pixel_bool"],
        paired["background_corrected_detected"].clip(lower=0)
        / paired["injections_for_analysis"],
        np.nan,
    )
    clipped_efficiency = paired["efficiency"].clip(lower=0, upper=1)
    paired["efficiency_binomial_sem"] = np.sqrt(
        clipped_efficiency
        * (1 - clipped_efficiency)
        / paired["injections_for_analysis"]
    )
    paired["fit_valid"] = (
        paired["signal_valid"].fillna(False)
        & paired["background_valid"].fillna(False)
        & ~paired["signal_counter_saturated"].fillna(False)
        & ~paired["background_counter_saturated"].fillna(False)
        & paired["active_injection_pixel_bool"].fillna(False)
        & paired["background_low"].fillna(False)
        & (paired["injections_for_analysis"] > 0)
        & (paired["background_corrected_detected"] >= 0)
        & (
            paired["background_corrected_detected"]
            <= paired["injections_for_analysis"]
        )
    )
    # Identical flag ordering without iterrows over millions of acquisitions.
    reasons = pd.Series("", index=paired.index, dtype=object)
    for field, invert, label in (
        ("signal_valid", True, "invalid_signal_counter"),
        ("background_valid", True, "invalid_background_counter"),
        ("signal_counter_saturated", False, "signal_counter_saturated"),
        ("background_counter_saturated", False, "background_counter_saturated"),
        ("active_injection_pixel_bool", True, "inactive_pixel_not_used_for_scurve_fit"),
        ("background_low", True, "background_above_limit"),
    ):
        flag = paired[field].astype(bool)
        if invert:
            flag = ~flag
        reasons.loc[flag] += label + ";"
    corrected = pd.to_numeric(paired["background_corrected_detected"], errors="coerce")
    injections = pd.to_numeric(paired["injections_for_analysis"], errors="coerce")
    negative = np.isfinite(corrected) & (corrected < 0)
    excess = np.isfinite(corrected) & np.isfinite(injections) & (corrected > injections)
    reasons.loc[negative] += "negative_after_background_subtraction;"
    reasons.loc[excess] += "corrected_count_exceeds_injections;"
    paired["quality_flags"] = reasons.str.rstrip(";")
    return paired


def _fit_scurve_group(
    group: pd.DataFrame,
    calibration: ThresholdDacCalibration,
) -> dict[str, Any]:
    valid = group[_as_bool(group["fit_valid"])].copy()
    result = {
        "fit_status": "insufficient_data",
        "transition_direction": "unknown",
        "v50_v": float("nan"),
        "v50_uncertainty_v": float("nan"),
        "d50_code": float("nan"),
        "sigma_v": float("nan"),
        "sigma_uncertainty_v": float("nan"),
        "sigma_dac_codes": float("nan"),
        "fit_r2": float("nan"),
        "fit_rmse_efficiency": float("nan"),
        "fit_points": int(len(valid)),
        "minimum_efficiency": float("nan"),
        "maximum_efficiency": float("nan"),
    }
    if valid.empty:
        return result
    aggregated = valid.groupby(
        ["threshold_dac_code", "threshold_voltage_v"], as_index=False
    ).agg(
        detected=("background_corrected_detected", "sum"),
        injections=("injections_for_analysis", "sum"),
    )
    aggregated["efficiency"] = aggregated["detected"] / aggregated["injections"]
    result["fit_points"] = int(len(aggregated))
    result["minimum_efficiency"] = float(aggregated["efficiency"].min())
    result["maximum_efficiency"] = float(aggregated["efficiency"].max())
    if (
        len(aggregated) < 5
        or result["minimum_efficiency"] > 0.2
        or result["maximum_efficiency"] < 0.8
    ):
        result["fit_status"] = "transition_not_bracketed"
        return result

    voltage = aggregated["threshold_voltage_v"].to_numpy(dtype=float)
    code = aggregated["threshold_dac_code"].to_numpy(dtype=float)
    detected = aggregated["detected"].to_numpy(dtype=float)
    injections = aggregated["injections"].to_numpy(dtype=float)
    efficiency = detected / injections
    correlation = float(np.corrcoef(voltage, efficiency)[0, 1])
    direction = "ascending" if correlation >= 0 else "descending"
    result["transition_direction"] = direction

    adjusted_probability = (detected + 0.5) / (injections + 1.0)
    if direction == "descending":
        adjusted_probability = 1 - adjusted_probability
    z = np.array([_NORMAL.inv_cdf(float(value)) for value in adjusted_probability])
    use = np.isfinite(z) & np.isfinite(voltage)
    design = np.column_stack((np.ones(int(np.sum(use))), z[use]))
    weights = np.clip(injections[use] * efficiency[use] * (1 - efficiency[use]), 0.25, np.inf)
    try:
        parameters, covariance, _ = _weighted_linear_fit(
            design, voltage[use], weights
        )
        code_parameters, _, _ = _weighted_linear_fit(design, code[use], weights)
    except np.linalg.LinAlgError:
        result["fit_status"] = "singular_fit"
        return result
    v50, slope = float(parameters[0]), float(parameters[1])
    sigma_v = abs(slope)
    d50 = float(code_parameters[0])
    sigma_code = abs(float(code_parameters[1]))
    if sigma_v <= 0:
        result["fit_status"] = "invalid_width"
        return result

    sign = 1 if direction == "ascending" else -1
    predicted = np.array(
        [_NORMAL.cdf(sign * (value - v50) / sigma_v) for value in voltage]
    )
    variance = float(np.sum((efficiency - np.mean(efficiency)) ** 2))
    r2 = 1 - float(np.sum((efficiency - predicted) ** 2)) / variance if variance else float("nan")
    result.update(
        {
            "fit_status": "ok" if math.isfinite(r2) and r2 >= 0.8 else "poor_quality",
            "v50_v": v50,
            "v50_uncertainty_v": math.sqrt(max(float(covariance[0, 0]), 0.0)),
            "d50_code": d50,
            "d50_nearest_programmable_code": calibration.voltage_to_nearest_dac_code(v50),
            "sigma_v": sigma_v,
            "sigma_millivolts": sigma_v * 1000.0,
            "threshold_domain_noise_sigma_v": sigma_v,
            "threshold_domain_noise_sigma_millivolts": sigma_v * 1000.0,
            "sigma_uncertainty_v": math.sqrt(max(float(covariance[1, 1]), 0.0)),
            "sigma_dac_codes": sigma_code,
            "fit_r2": r2,
            "fit_rmse_efficiency": float(np.sqrt(np.mean((efficiency - predicted) ** 2))),
        }
    )
    return result


def _fit_scurve_job(job: tuple) -> dict[str, Any]:
    row, group, calibration = job
    row.update(_fit_scurve_group(group, calibration))
    return row


def fit_scurves(
    efficiency: pd.DataFrame,
    calibration: ThresholdDacCalibration,
    *,
    settings: AnalysisSettings | None = None,
) -> pd.DataFrame:
    if efficiency.empty:
        return pd.DataFrame()
    selected_settings = settings or AnalysisSettings()
    selected_settings.validate()
    group_columns = [
        "stage", "pulse_amplitude_native", "injection_pattern",
        "column", "row", "local_trim_code",
    ]
    metadata_columns = [column for column in (
            "injection_voltage_step_v",
            "requested_injection_voltage_step_v",
            "injection_voltage_step_error_v",
            "absolute_injection_voltage_step_error_v",
            "ref1_dac_code",
            "ref2_dac_code",
            "ref1_voltage_v",
            "ref2_voltage_v",
            "reference_common_mode_v",
            "ref_voltage_order",
            "reference_pair_selection_method",
            "injection_charge_c",
            "injection_charge_uncertainty_c",
            "injection_charge_electrons",
            "injection_capacitance_f",
            "injection_capacitance_relative_uncertainty",
            "injection_charge_status",
            "injection_count_source",
    ) if column in efficiency]
    numeric_columns = [
        "fit_valid", "threshold_dac_code", "threshold_voltage_v",
        "background_corrected_detected", "injections_for_analysis",
    ]
    groups = efficiency.groupby(group_columns, dropna=False, sort=True)

    def jobs():
        for keys, group in groups:
            row = dict(zip(group_columns, keys))
            row.update(group.iloc[0][metadata_columns].to_dict())
            yield row, group[numeric_columns], calibration

    rows = map_analysis_groups(_fit_scurve_job, jobs, total=groups.ngroups,
                               settings=selected_settings, label="S-curve fit")
    return pd.DataFrame(rows)


def calculate_injection_crosstalk_metrics(
    efficiency: pd.DataFrame,
    scurve_results: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compare S-curves versus simultaneous-injection density.

    The sparsest acquired pattern for each pulse amplitude is the reference.
    Pixel metrics report V50 and width shifts. Inactive pixels additionally
    provide excess hit fraction after paired-background subtraction. These are
    experimental coupling indicators, not a decomposition of a specific
    electrical crosstalk mechanism.
    """

    if efficiency.empty or "injection_pattern" not in efficiency:
        return pd.DataFrame(), pd.DataFrame()
    frame = efficiency.copy()
    frame["active_injection_pixel_count"] = pd.to_numeric(
        frame["active_injection_pixel_count"], errors="coerce"
    )
    frame["inactive_excess_hit_fraction"] = pd.to_numeric(
        frame["inactive_excess_hit_fraction"], errors="coerce"
    )
    density = (
        frame.groupby(
            ["pulse_amplitude_native", "injection_pattern"],
            as_index=False,
            dropna=False,
        )["active_injection_pixel_count"]
        .median()
        .rename(columns={"active_injection_pixel_count": "median_active_pixels_per_shot"})
    )
    reference_by_amplitude = (
        density.sort_values(
            ["pulse_amplitude_native", "median_active_pixels_per_shot", "injection_pattern"]
        )
        .drop_duplicates("pulse_amplitude_native", keep="first")
        .set_index("pulse_amplitude_native")["injection_pattern"]
        .to_dict()
    )

    pixel_metrics = pd.DataFrame()
    if not scurve_results.empty:
        results = scurve_results.copy()
        reference_rows = []
        for amplitude, reference_pattern in reference_by_amplitude.items():
            subset = results[
                (results["pulse_amplitude_native"] == amplitude)
                & (results["injection_pattern"] == reference_pattern)
            ].copy()
            subset["reference_injection_pattern"] = reference_pattern
            reference_rows.append(subset)
        reference = (
            pd.concat(reference_rows, ignore_index=True)
            if reference_rows
            else pd.DataFrame()
        )
        if not reference.empty:
            reference = reference[
                [
                    "pulse_amplitude_native",
                    "column",
                    "row",
                    "reference_injection_pattern",
                    "v50_v",
                    "sigma_v",
                ]
            ].rename(
                columns={
                    "v50_v": "reference_v50_v",
                    "sigma_v": "reference_sigma_v",
                }
            )
            pixel_metrics = results.merge(
                reference,
                on=["pulse_amplitude_native", "column", "row"],
                how="left",
                validate="many_to_one",
            )
            pixel_metrics["delta_v50_v"] = (
                pixel_metrics["v50_v"] - pixel_metrics["reference_v50_v"]
            )
            pixel_metrics["delta_sigma_v"] = (
                pixel_metrics["sigma_v"] - pixel_metrics["reference_sigma_v"]
            )
            pixel_metrics["sigma_ratio_to_reference"] = (
                pixel_metrics["sigma_v"] / pixel_metrics["reference_sigma_v"]
            )

    summary_rows: list[dict[str, Any]] = []
    for (amplitude, pattern), group in frame.groupby(
        ["pulse_amplitude_native", "injection_pattern"],
        dropna=False,
        sort=True,
    ):
        inactive = group[
            ~_as_bool(group["active_injection_pixel_bool"])
            & _as_bool(group["signal_valid"])
            & _as_bool(group["background_valid"])
            & _as_bool(group["background_low"])
        ]["inactive_excess_hit_fraction"].dropna()
        fitted = (
            scurve_results[
                (scurve_results["pulse_amplitude_native"] == amplitude)
                & (scurve_results["injection_pattern"] == pattern)
            ]
            if not scurve_results.empty
            else pd.DataFrame()
        )
        pixel_subset = (
            pixel_metrics[
                (pixel_metrics["pulse_amplitude_native"] == amplitude)
                & (pixel_metrics["injection_pattern"] == pattern)
            ]
            if not pixel_metrics.empty
            else pd.DataFrame()
        )
        summary_rows.append(
            {
                "pulse_amplitude_native": amplitude,
                "injection_pattern": pattern,
                "reference_injection_pattern": reference_by_amplitude.get(amplitude),
                "group_count": int(group["injection_group_id"].nunique()),
                "median_active_pixels_per_shot": float(
                    group["active_injection_pixel_count"].median()
                ),
                "successful_scurve_fit_count": int(
                    (fitted.get("fit_status", pd.Series(dtype=str)) == "ok").sum()
                ),
                "median_v50_v": _finite_median(fitted, "v50_v"),
                "median_sigma_v": _finite_median(fitted, "sigma_v"),
                "median_abs_delta_v50_v_to_reference": _finite_median(
                    pixel_subset, "delta_v50_v", absolute=True
                ),
                "median_sigma_ratio_to_reference": _finite_median(
                    pixel_subset, "sigma_ratio_to_reference"
                ),
                "inactive_sample_count": int(len(inactive)),
                "inactive_excess_hit_fraction_median": float(inactive.median())
                if len(inactive)
                else float("nan"),
                "inactive_excess_hit_fraction_p95": float(inactive.quantile(0.95))
                if len(inactive)
                else float("nan"),
                "inactive_excess_hit_fraction_max": float(inactive.max())
                if len(inactive)
                else float("nan"),
            }
        )
    return pixel_metrics, pd.DataFrame(summary_rows)


def _load_primary_calibration(
    store: ExperimentStore,
) -> ThresholdDacCalibration | None:
    calibration_records = store.metadata.get("threshold_dac_calibrations", {})
    threshold_dac = store.metadata.get("threshold_dac")
    if not threshold_dac or threshold_dac not in calibration_records:
        return None
    record = calibration_records[threshold_dac]
    relative = record.get("experiment_copy")
    if not relative:
        return None
    return ThresholdDacCalibration.from_csv(
        store.root / relative,
        threshold_dac,
        code_column=record.get("code_column"),
        voltage_column=record.get("voltage_column"),
    )


def _summary_from_final(
    final: pd.DataFrame,
    *,
    target_voltage: float | None,
) -> pd.DataFrame:
    if final.empty:
        return pd.DataFrame()
    centers = pd.to_numeric(final["center_selected_v"], errors="coerce").dropna()
    residual = centers - target_voltage if target_voltage is not None else pd.Series(dtype=float)
    trims = pd.to_numeric(final["local_trim_code"], errors="coerce")
    row = {
        "pixel_count": int(len(final)),
        "valid_center_count": int(len(centers)),
        "failed_fit_count": int((final["fit_status"] != "ok").sum()),
        "target_voltage_v": target_voltage,
        "center_mean_v": float(centers.mean()) if len(centers) else float("nan"),
        "center_median_v": float(centers.median()) if len(centers) else float("nan"),
        "center_std_v": float(centers.std(ddof=1)) if len(centers) >= 2 else float("nan"),
        "center_mad_v": _mad(centers),
        "center_peak_to_peak_v": float(centers.max() - centers.min()) if len(centers) else float("nan"),
        "center_min_v": float(centers.min()) if len(centers) else float("nan"),
        "center_max_v": float(centers.max()) if len(centers) else float("nan"),
        "residual_rms_v": float(np.sqrt(np.mean(residual**2))) if len(residual) else float("nan"),
        "residual_mean_v": float(residual.mean()) if len(residual) else float("nan"),
        "residual_median_v": float(residual.median()) if len(residual) else float("nan"),
        "residual_std_v": float(residual.std(ddof=1)) if len(residual) >= 2 else float("nan"),
        "residual_mad_v": _mad(residual),
        "residual_peak_to_peak_v": (
            float(residual.max() - residual.min()) if len(residual) else float("nan")
        ),
        "residual_min_v": float(residual.min()) if len(residual) else float("nan"),
        "residual_max_v": float(residual.max()) if len(residual) else float("nan"),
        "saturated_trim_count": int(trims.isin((0, 31)).sum()),
        "saturated_trim_fraction": float(trims.isin((0, 31)).mean()) if len(trims) else float("nan"),
    }
    return pd.DataFrame([row])


def uniform_trim_characterization(noise_fits: pd.DataFrame) -> pd.DataFrame:
    """Return one fitted baseline row per pixel and uniformly applied trim code."""

    if noise_fits.empty or "stage" not in noise_fits:
        return pd.DataFrame()
    stages = noise_fits["stage"].astype(str)
    mask = stages.isin(("trim_00", "trim_31")) | stages.str.startswith("trim_full_")
    frame = noise_fits[mask].copy()
    if frame.empty:
        return frame
    frame["trim_code"] = pd.to_numeric(frame["local_trim_code"], errors="coerce")
    frame = frame.dropna(subset=["trim_code", "column", "row"])
    frame["trim_code"] = frame["trim_code"].astype(int)
    frame["fit_rank"] = frame["fit_status"].map(
        {"ok": 0, "poor_quality": 1, "unsupported_shape": 2}
    ).fillna(3)
    frame = frame.sort_values(
        ["trim_code", "column", "row", "fit_rank", "points"],
        ascending=[True, True, True, True, False],
    )
    return frame.drop_duplicates(["trim_code", "column", "row"], keep="first")


def summarize_uniform_trim_characterization(
    trim_fits: pd.DataFrame,
) -> pd.DataFrame:
    if trim_fits.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for trim_code, group in trim_fits.groupby("trim_code", sort=True):
        centers = pd.to_numeric(group["center_selected_v"], errors="coerce").dropna()
        widths = pd.to_numeric(group["sigma_fit_v"], errors="coerce").dropna()
        rows.append(
            {
                "trim_code": int(trim_code),
                "pixel_count": int(len(group)),
                "valid_center_count": int(len(centers)),
                "successful_fit_count": int((group["fit_status"] == "ok").sum()),
                "successful_fit_fraction": float((group["fit_status"] == "ok").mean()),
                "center_mean_v": float(centers.mean()) if len(centers) else float("nan"),
                "center_median_v": float(centers.median()) if len(centers) else float("nan"),
                "center_std_v": float(centers.std(ddof=1)) if len(centers) >= 2 else float("nan"),
                "center_mad_v": _mad(centers),
                "center_q10_v": float(centers.quantile(0.10)) if len(centers) else float("nan"),
                "center_q90_v": float(centers.quantile(0.90)) if len(centers) else float("nan"),
                "noise_sigma_median_v": float(widths.median()) if len(widths) else float("nan"),
                "noise_sigma_q10_v": float(widths.quantile(0.10)) if len(widths) else float("nan"),
                "noise_sigma_q90_v": float(widths.quantile(0.90)) if len(widths) else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def summarize_scurve_amplitudes(scurve_results: pd.DataFrame) -> pd.DataFrame:
    if scurve_results.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    group_columns = ["pulse_amplitude_native", "injection_pattern"]
    for keys, group in scurve_results.groupby(group_columns, dropna=False, sort=True):
        amplitude, pattern = keys
        valid = group[group["fit_status"].isin(("ok", "poor_quality"))]
        v50 = pd.to_numeric(valid["v50_v"], errors="coerce").dropna()
        sigma = pd.to_numeric(valid["sigma_v"], errors="coerce").dropna()
        row: dict[str, Any] = {
            "pulse_amplitude_native": amplitude,
            "injection_pattern": pattern,
            "pixel_count": int(len(group)),
            "successful_fit_count": int((group["fit_status"] == "ok").sum()),
            "usable_fit_count": int(len(valid)),
            "v50_median_v": float(v50.median()) if len(v50) else float("nan"),
            "v50_mean_v": float(v50.mean()) if len(v50) else float("nan"),
            "v50_std_v": float(v50.std(ddof=1)) if len(v50) >= 2 else float("nan"),
            "v50_mad_v": _mad(v50),
            "v50_q10_v": float(v50.quantile(0.10)) if len(v50) else float("nan"),
            "v50_q90_v": float(v50.quantile(0.90)) if len(v50) else float("nan"),
            "sigma_median_v": float(sigma.median()) if len(sigma) else float("nan"),
            "sigma_q10_v": float(sigma.quantile(0.10)) if len(sigma) else float("nan"),
            "sigma_q90_v": float(sigma.quantile(0.90)) if len(sigma) else float("nan"),
        }
        for column in (
            "requested_injection_voltage_step_v",
            "injection_voltage_step_v",
            "injection_voltage_step_error_v",
            "absolute_injection_voltage_step_error_v",
            "injection_charge_electrons",
            "injection_charge_uncertainty_c",
            "ref1_dac_code",
            "ref2_dac_code",
            "ref1_voltage_v",
            "ref2_voltage_v",
            "reference_common_mode_v",
        ):
            if column in group:
                row[column] = group.iloc[0][column]
        rows.append(row)
    return pd.DataFrame(rows)


def fit_scurve_gain_results(scurve_results: pd.DataFrame) -> pd.DataFrame:
    """Fit V50 against calibrated injection step for each pixel and pattern."""

    if scurve_results.empty or "injection_voltage_step_v" not in scurve_results:
        return pd.DataFrame()
    frame = scurve_results[scurve_results["fit_status"].isin(("ok", "poor_quality"))].copy()
    frame["injection_voltage_step_v"] = pd.to_numeric(
        frame["injection_voltage_step_v"], errors="coerce"
    )
    frame["injection_charge_electrons"] = (
        pd.to_numeric(frame["injection_charge_electrons"], errors="coerce")
        if "injection_charge_electrons" in frame
        else np.nan
    )
    frame["v50_v"] = pd.to_numeric(frame["v50_v"], errors="coerce")
    frame = frame.dropna(subset=["injection_voltage_step_v", "v50_v"])
    rows: list[dict[str, Any]] = []
    for (pattern, column, row), group in frame.groupby(
        ["injection_pattern", "column", "row"], sort=True
    ):
        points = (
            group.groupby("injection_voltage_step_v", as_index=False)
            .agg(v50_v=("v50_v", "mean"), injection_charge_electrons=("injection_charge_electrons", "mean"))
            .sort_values("injection_voltage_step_v")
        )
        if len(points) < 3:
            continue
        x = points["injection_voltage_step_v"].to_numpy(dtype=float)
        y = points["v50_v"].to_numpy(dtype=float)
        design = np.column_stack((x, np.ones(len(x))))
        parameters, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
        slope, intercept = (float(parameters[0]), float(parameters[1]))
        predicted = slope * x + intercept
        variance = float(np.sum((y - np.mean(y)) ** 2))
        residual = float(np.sum((y - predicted) ** 2))
        r2 = 1.0 - residual / variance if variance > 0 else float("nan")
        charge = points["injection_charge_electrons"].to_numpy(dtype=float)
        gain_mv_per_ke = float("nan")
        if np.all(np.isfinite(charge)) and len(np.unique(charge)) >= 3:
            charge_design = np.column_stack((charge, np.ones(len(charge))))
            charge_parameters, _, _, _ = np.linalg.lstsq(
                charge_design, y, rcond=None
            )
            gain_mv_per_ke = float(charge_parameters[0]) * 1e6
        rows.append(
            {
                "injection_pattern": pattern,
                "column": int(column),
                "row": int(row),
                "amplitude_point_count": int(len(points)),
                "v50_intercept_v": intercept,
                "v50_slope_v_per_injection_step_v": slope,
                "nominal_gain_mv_per_ke": gain_mv_per_ke,
                "fit_r2": r2,
                "fit_rmse_v": float(np.sqrt(np.mean((y - predicted) ** 2))),
                "charge_axis_status": (
                    "nominal_from_Cinj_and_REF_LUT"
                    if math.isfinite(gain_mv_per_ke)
                    else "charge_axis_unavailable"
                ),
            }
        )
    return pd.DataFrame(rows)


def analyze_saved_experiment(
    path: str | Path,
    *,
    settings: AnalysisSettings | None = None,
    target_voltage: float | None = None,
    n_injections: int | None = None,
    generate_plots: bool = True,
    bad_pixel_map: BadPixelMapInput = None,
) -> dict[str, Any]:
    """Re-analyze a saved experiment without importing or accessing hardware.

    ``n_injections`` is a consistency check for modern raw data and may only
    supply a missing denominator for a legacy experiment. It never changes the
    physical count recorded by an acquisition.
    """

    selected_settings = settings or AnalysisSettings()
    selected_settings.validate()
    if n_injections is not None and (
        not isinstance(n_injections, int)
        or isinstance(n_injections, bool)
        or n_injections <= 0
    ):
        raise ValueError("n_injections must be a positive integer when supplied")
    store = ExperimentStore(path)
    # Additional offline exclusions cannot re-enable pixels excluded at acquisition.
    bad_pixels = tuple(sorted(set(normalize_bad_pixel_map(bad_pixel_map)) | set(
        normalize_bad_pixel_map(store.metadata.get("bad_pixel_mask"))
    )))
    analysis_dir = store.next_analysis_directory()
    calibration = _load_primary_calibration(store)
    outputs: dict[str, Any] = {"analysis_directory": analysis_dir}

    atomic_write_json(
        analysis_dir / "analysis_settings.json",
        {
            "created_utc": utc_now_text(),
            "analysis_framework_version": FRAMEWORK_VERSION,
            "settings": asdict(selected_settings),
            "target_voltage_override_v": target_voltage,
            "n_injections_consistency_check": n_injections,
            "source_experiment": str(store.root),
            "bad_pixel_mask": bad_pixel_document(bad_pixels),
        },
    )

    store.log_status("Офлайн-анализ: чтение noise raw")
    raw_noise = exclude_bad_pixel_rows(store.load_raw("noise", workers=selected_settings.read_workers), bad_pixels)
    noise_statistics = calculate_noise_statistics(raw_noise)
    del raw_noise
    store.log_status("Офлайн-анализ: статистика noise рассчитана")
    store.write_table(analysis_dir / "noise_statistics.csv", noise_statistics)
    outputs["noise_statistics"] = analysis_dir / "noise_statistics.csv"

    noise_fits = fit_noise_statistics(
        noise_statistics,
        settings=selected_settings,
        calibration=calibration,
    )
    store.write_table(analysis_dir / "noise_fit_results.csv", noise_fits)
    outputs["noise_fit_results"] = analysis_dir / "noise_fit_results.csv"
    trim_characterization = uniform_trim_characterization(noise_fits)
    trim_characterization_summary = summarize_uniform_trim_characterization(
        trim_characterization
    )
    if not trim_characterization.empty:
        store.write_table(
            analysis_dir / "uniform_trim_characterization.csv",
            trim_characterization,
        )
        store.write_table(
            analysis_dir / "uniform_trim_summary.csv",
            trim_characterization_summary,
        )
        outputs["uniform_trim_characterization"] = (
            analysis_dir / "uniform_trim_characterization.csv"
        )
        outputs["uniform_trim_summary"] = analysis_dir / "uniform_trim_summary.csv"

    selected_target = target_voltage
    target_method = None
    reachability = pd.DataFrame()
    stages = set(noise_fits.get("stage", pd.Series(dtype=str)).astype(str))
    if {"trim_00", "trim_31"}.issubset(stages):
        try:
            selected_target, target_method, reachability = select_equalization_target(
                noise_fits,
                requested_target_voltage=target_voltage,
            )
        except RuntimeError as error:
            # Still produce diagnostics/proposals for an unsuccessful or partial scan.
            selected_target, target_method = target_voltage, str(error)
        store.write_table(analysis_dir / "reachable_range_per_pixel.csv", reachability)
        atomic_write_json(
            analysis_dir / "equalization_target.json",
            {
                "target_voltage_v": selected_target,
                "target_selection_method": target_method,
                "pixels_total": int(len(reachability)),
                "pixels_not_reaching_target": int((~reachability["target_reachable"]).sum()) if not reachability.empty else None,
            },
        )
        outputs["equalization_target"] = analysis_dir / "equalization_target.json"

        for stage, filename in (
            ("trim_00", "threshold_trim0.csv"),
            ("trim_31", "threshold_trim31.csv"),
        ):
            table = noise_fits[noise_fits["stage"] == stage].copy()
            store.write_table(analysis_dir / filename, table)
            outputs[filename.removesuffix(".csv")] = analysis_dir / filename

    measured_trim = pd.DataFrame()
    if selected_target is not None:
        measured_trim = choose_measured_trim_map(
            noise_fits,
            target_voltage=float(selected_target),
        )
        if not measured_trim.empty:
            trim_columns = [
                "column",
                "row",
                "selected_trim_code",
                "selected_measured_center_v",
                "selected_measured_residual_v",
                "selected_from_stage",
                "selected_center_method",
                "fit_status",
            ]
            # A nearest measured endpoint is not an equalized map. Preserve it
            # as a diagnostic, clearly separate from per-method predictions.
            filename = "best_measured_trim_map.csv"
            store.write_table(analysis_dir / filename, measured_trim[trim_columns])
            outputs["best_measured_trim_map"] = analysis_dir / filename
            if "equalized_final" in stages:
                final_map = noise_fits[noise_fits["stage"] == "equalized_final"].rename(columns={
                    "local_trim_code": "selected_trim_code", "center_selected_v": "selected_measured_center_v",
                    "center_selected_method": "selected_center_method", "stage": "selected_from_stage",
                }).copy()
                final_map["selected_measured_residual_v"] = final_map["selected_measured_center_v"] - float(selected_target)
                store.write_table(analysis_dir / "trim_map.csv", final_map[trim_columns])
                outputs["trim_map"] = analysis_dir / "trim_map.csv"

    if noise_fits.empty or "stage" not in noise_fits:
        final = pd.DataFrame()
    else:
        final = noise_fits[noise_fits["stage"] == "equalized_final"].copy()
    if not final.empty:
        if selected_target is not None:
            final["target_voltage_v"] = float(selected_target)
            final["residual_v"] = final["center_selected_v"] - float(selected_target)
        store.write_table(analysis_dir / "threshold_equalized.csv", final)
        residual_columns = [
            column
            for column in (
                "column",
                "row",
                "local_trim_code",
                "center_selected_v",
                "target_voltage_v",
                "residual_v",
                "fit_status",
                "diagnostic_flags",
            )
            if column in final
        ]
        store.write_table(analysis_dir / "residual_map.csv", final[residual_columns])
        noise_map = final[
            [
                "column",
                "row",
                "local_trim_code",
                "sigma_fit_v",
                "fit_status",
            ]
        ].rename(columns={"sigma_fit_v": "threshold_domain_noise_sigma_v"})
        store.write_table(analysis_dir / "noise_map.csv", noise_map)
        outputs.update(
            {
                "threshold_equalized": analysis_dir / "threshold_equalized.csv",
                "residual_map": analysis_dir / "residual_map.csv",
                "noise_map": analysis_dir / "noise_map.csv",
            }
        )

    fit_quality_columns = [
        "stage",
        "column",
        "row",
        "local_trim_code",
        "fit_model",
        "fit_status",
        "fit_r2",
        "fit_rmse_count",
        "fit_reduced_chi2",
        "diagnostic_flags",
    ]
    fit_quality = noise_fits[
        [column for column in fit_quality_columns if column in noise_fits]
    ].copy()
    store.write_table(analysis_dir / "fit_quality.csv", fit_quality)
    outputs["fit_quality"] = analysis_dir / "fit_quality.csv"

    summary_source = final
    if summary_source.empty and not noise_fits.empty and "stage" in noise_fits:
        for fallback_stage in ("baseline_noise", "trim_00"):
            summary_source = noise_fits[noise_fits["stage"] == fallback_stage].copy()
            if not summary_source.empty:
                break
    summary = _summary_from_final(summary_source, target_voltage=selected_target)
    if not summary.empty:
        summary["summary_source_stage"] = str(summary_source.iloc[0]["stage"])
        summary["equalized_final_measured"] = not final.empty
        summary["equalization_improvement_verified"] = not final.empty
        if final.empty:
            # Endpoint trims of 0/31 are intentional, not saturated equalization.
            summary["saturated_trim_count"] = np.nan
            summary["saturated_trim_fraction"] = np.nan
    store.write_table(analysis_dir / "summary.csv", summary)
    outputs["summary"] = analysis_dir / "summary.csv"
    outputs.update(save_noise_recommendations(
        analysis_dir, noise_fits, noise_statistics,
        target_voltage=target_voltage, bad_pixel_map=bad_pixels,
    ))
    atomic_write_json(analysis_dir / "measurement_coverage.json", {
        "source_kind": "raw_acquisitions", "source_status": store.metadata.get("status"),
        "available_noise_stages": sorted(stages),
        "equalized_final_measured": not final.empty,
        "warning_ru": "При отсутствии equalized_final карты подстроек являются предложениями, а не результатом проверенной эквализации.",
    })
    outputs["measurement_coverage"] = analysis_dir / "measurement_coverage.json"

    raw_scurve = exclude_bad_pixel_rows(store.load_raw("scurve", workers=selected_settings.read_workers), bad_pixels)
    scurve_efficiency = pd.DataFrame()
    scurve_results = pd.DataFrame()
    scurve_amplitude_summary = pd.DataFrame()
    scurve_gain_results = pd.DataFrame()
    crosstalk_pixels = pd.DataFrame()
    crosstalk_summary = pd.DataFrame()
    if not raw_scurve.empty:
        if n_injections is not None:
            count_columns = [
                column
                for column in (
                    "injections_for_analysis",
                    "actual_injections",
                    "programmed_injections",
                    "requested_injections",
                )
                if column in raw_scurve
            ]
            stored_counts: set[int] = set()
            for column in count_columns:
                values = pd.to_numeric(raw_scurve[column], errors="coerce").dropna()
                stored_counts.update(int(value) for value in values if value > 0)
            if stored_counts and stored_counts != {n_injections}:
                raise ValueError(
                    "offline n_injections does not match stored acquisition data: "
                    f"stored={sorted(stored_counts)}, requested={n_injections}. "
                    "The physical injection count cannot be changed during re-analysis."
                )
            if not stored_counts:
                raw_scurve["injections_for_analysis"] = n_injections
                raw_scurve["injection_count_source"] = (
                    "offline_user_value_for_legacy_data_without_saved_count"
                )
        max_background_fraction = float(
            store.metadata.get("settings", {})
            .get("scurve", {})
            .get("max_background_fraction", 0.01)
        )
        scurve_noise_statistics = noise_statistics
        reference_record = store.metadata.get("noise_reference", {})
        reference_relative = reference_record.get("statistics_copy")
        if reference_relative:
            reference_path = store.root / str(reference_relative)
            if not reference_path.exists():
                raise FileNotFoundError(
                    f"saved noise-reference statistics are missing: {reference_path}"
                )
            reference_statistics = pd.read_csv(reference_path, keep_default_na=False)
            reference_statistics = exclude_bad_pixel_rows(reference_statistics, bad_pixels)
            if scurve_noise_statistics.empty:
                scurve_noise_statistics = reference_statistics
        scurve_efficiency = _paired_scurve_efficiency(
            raw_scurve,
            noise_statistics=scurve_noise_statistics,
            max_background_fraction=max_background_fraction,
        )
        del raw_scurve
        store.write_table(analysis_dir / "scurve_efficiency.csv", scurve_efficiency)
        if calibration is not None:
            scurve_results = fit_scurves(scurve_efficiency, calibration, settings=selected_settings)
        store.write_table(analysis_dir / "scurve_results.csv", scurve_results)
        scurve_amplitude_summary = summarize_scurve_amplitudes(scurve_results)
        scurve_gain_results = fit_scurve_gain_results(scurve_results)
        store.write_table(
            analysis_dir / "scurve_amplitude_summary.csv",
            scurve_amplitude_summary,
        )
        store.write_table(
            analysis_dir / "scurve_pixel_gain_results.csv",
            scurve_gain_results,
        )
        crosstalk_pixels, crosstalk_summary = calculate_injection_crosstalk_metrics(
            scurve_efficiency, scurve_results
        )
        store.write_table(
            analysis_dir / "injection_crosstalk_pixel_metrics.csv",
            crosstalk_pixels,
        )
        store.write_table(
            analysis_dir / "injection_crosstalk_summary.csv",
            crosstalk_summary,
        )
        outputs["scurve_efficiency"] = analysis_dir / "scurve_efficiency.csv"
        outputs["scurve_results"] = analysis_dir / "scurve_results.csv"
        outputs["scurve_amplitude_summary"] = (
            analysis_dir / "scurve_amplitude_summary.csv"
        )
        outputs["scurve_pixel_gain_results"] = (
            analysis_dir / "scurve_pixel_gain_results.csv"
        )
        outputs["injection_crosstalk_pixel_metrics"] = (
            analysis_dir / "injection_crosstalk_pixel_metrics.csv"
        )
        outputs["injection_crosstalk_summary"] = (
            analysis_dir / "injection_crosstalk_summary.csv"
        )

    if generate_plots:
        from .plots import generate_diagnostic_plots

        plot_paths = generate_diagnostic_plots(
            analysis_directory=analysis_dir,
            noise_statistics=noise_statistics,
            noise_fits=noise_fits,
            trim_characterization=trim_characterization,
            trim_characterization_summary=trim_characterization_summary,
            scurve_efficiency=scurve_efficiency,
            scurve_results=scurve_results,
            scurve_amplitude_summary=scurve_amplitude_summary,
            scurve_gain_results=scurve_gain_results,
            crosstalk_pixel_metrics=crosstalk_pixels,
            crosstalk_summary=crosstalk_summary,
            target_voltage=selected_target,
            settings=selected_settings,
        )
        outputs["plots"] = plot_paths
        from .plots import generate_recommendation_plots

        outputs["plots"].update(generate_recommendation_plots(analysis_dir, selected_settings))

    atomic_write_json(
        analysis_dir / "analysis_manifest.json",
        {
            "created_utc": utc_now_text(),
            "source_experiment": str(store.root),
            "target_voltage_v": selected_target,
            "target_selection_method": target_method,
            "files": {
                key: str(value.relative_to(analysis_dir))
                for key, value in outputs.items()
                if isinstance(value, Path) and value.is_file()
            },
        },
    )
    outputs["analysis_manifest"] = analysis_dir / "analysis_manifest.json"
    return outputs


def analyze_saved_noise_statistics(
    path: str | Path,
    *,
    settings: AnalysisSettings | None = None,
    target_voltage: float | None = None,
    bad_pixel_map: BadPixelMapInput = None,
    output_root: str | Path | None = None,
    generate_plots: bool = True,
) -> dict[str, Any]:
    """Re-fit a saved noise_statistics.csv when original raw data are unavailable.

    Input files are never overwritten. A new reanalysis/vNNN directory records
    that raw repetitions, calibration provenance and hardware state cannot be
    reconstructed from aggregates. This output is NOT a hardware noise reference.
    """

    source = Path(path).resolve()
    if source.is_dir():
        source = source / "noise_statistics.csv"
    statistics = pd.read_csv(source)
    required = {
        "stage", "window", "comparator_under_test", "threshold_dac",
        "local_trim_field", "local_trim_code", "column", "row",
        "threshold_dac_code", "threshold_voltage_v", "mean_count", "sem_count",
        "median_count", "std_count", "repeat_count_total", "repeat_count_valid",
        "repeat_count_invalid", "repeat_count_saturated",
    }
    if not required.issubset(statistics):
        raise ValueError("noise statistics CSV is missing columns: " + ", ".join(sorted(required - set(statistics))))
    if statistics["window"].nunique() > 1:
        raise ValueError("analyze one comparator window at a time")
    selected_settings = settings or AnalysisSettings()
    selected_settings.validate()
    bad_pixels = normalize_bad_pixel_map(bad_pixel_map)
    statistics = exclude_bad_pixel_rows(statistics, bad_pixels)
    parent = Path(output_root).resolve() if output_root is not None else source.parent / "reanalysis"
    parent.mkdir(parents=True, exist_ok=True)
    version = 1
    while (parent / f"v{version:03d}").exists():
        version += 1
    directory = parent / f"v{version:03d}"
    directory.mkdir()
    fits = fit_noise_statistics(statistics, settings=selected_settings)
    trim_data = uniform_trim_characterization(fits)
    trim_summary = summarize_uniform_trim_characterization(trim_data)
    outputs: dict[str, Any] = {"analysis_directory": directory}
    for stem, table in (
        ("noise_statistics", statistics), ("noise_fit_results", fits),
        ("uniform_trim_characterization", trim_data), ("uniform_trim_summary", trim_summary),
    ):
        outputs[stem] = atomic_write_table(directory / f"{stem}.csv", table)
    outputs.update(save_noise_recommendations(
        directory, fits, statistics, target_voltage=target_voltage, bad_pixel_map=bad_pixels,
    ))
    stages = sorted(set(statistics["stage"].astype(str)))
    coverage = {
        "source_kind": "processed_statistics_only_NO_RAW",
        "source_statistics_path": str(source), "source_statistics_sha256": file_sha256(source),
        "available_noise_stages": stages, "equalized_final_measured": "equalized_final" in stages,
        "raw_repetitions_available": False, "calibration_provenance_verified": False,
        "hardware_configuration_verified": False,
        "warning_ru": "Повторный анализ средних/дисперсий. Порядок отдельных shots, reset, OMR, реальные настройки УПО и насыщение исходных слов проверить нельзя.",
    }
    atomic_write_json(directory / "measurement_coverage.json", coverage)
    outputs["measurement_coverage"] = directory / "measurement_coverage.json"
    atomic_write_json(directory / "analysis_settings.json", {
        "created_utc": utc_now_text(), "analysis_framework_version": FRAMEWORK_VERSION,
        "settings": asdict(selected_settings),
        "target_voltage_override_v": target_voltage, "bad_pixel_mask": bad_pixel_document(bad_pixels),
    })
    outputs["analysis_settings"] = directory / "analysis_settings.json"
    if generate_plots:
        from .plots import generate_diagnostic_plots, generate_recommendation_plots

        outputs["plots"] = generate_diagnostic_plots(
            analysis_directory=directory, noise_statistics=statistics, noise_fits=fits,
            trim_characterization=trim_data, trim_characterization_summary=trim_summary,
            scurve_efficiency=pd.DataFrame(), scurve_results=pd.DataFrame(),
            scurve_amplitude_summary=pd.DataFrame(), scurve_gain_results=pd.DataFrame(),
            crosstalk_pixel_metrics=pd.DataFrame(), crosstalk_summary=pd.DataFrame(),
            target_voltage=target_voltage, settings=selected_settings,
        )
        outputs["plots"].update(generate_recommendation_plots(directory, selected_settings))
    atomic_write_json(directory / "analysis_manifest.json", {
        "created_utc": utc_now_text(), **coverage,
        "files": {key: value.relative_to(directory).as_posix() for key, value in outputs.items() if isinstance(value, Path) and value.is_file()},
    })
    outputs["analysis_manifest"] = directory / "analysis_manifest.json"
    return outputs
