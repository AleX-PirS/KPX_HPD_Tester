"""Per-method trim proposals and conservative masking advice from measured curves.

No function here programs hardware. Missing data, failed fits and an unreachable
target are distinguished from a physically confirmed dead pixel.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .pixel_masks import BadPixelMapInput, bad_pixel_document, normalize_bad_pixel_map
from .storage import atomic_write_json, atomic_write_table


METHOD_COLUMNS = {
    "fit": "center_fit_v",
    "centroid": "center_centroid_v",
    "maximum": "center_max_v",
}


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _true(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "1.0")


def noise_curve_diagnostics(statistics: pd.DataFrame) -> pd.DataFrame:
    """One row per measured pixel/trim stage, using repeat statistics only."""

    rows = []
    if statistics.empty:
        return pd.DataFrame()
    for (stage, column, row), group in statistics.groupby(["stage", "column", "row"], sort=True):
        counts = pd.to_numeric(group["mean_count"], errors="coerce")
        valid = group.loc[counts.notna() & (group["repeat_count_valid"] > 0)].copy()
        entry: dict[str, Any] = {
            "stage": stage, "column": int(column), "row": int(row),
            "dac_point_count": len(group), "valid_dac_point_count": len(valid),
            "valid_repeat_count": int(group["repeat_count_valid"].sum()),
            "invalid_repeat_count": int(group["repeat_count_invalid"].sum()),
            "saturated_repeat_count": int(group["repeat_count_saturated"].sum()),
            "min_repeats_per_code": int(group["repeat_count_total"].min()),
            "max_repeats_per_code": int(group["repeat_count_total"].max()),
            "no_response_in_measured_range": bool(not valid.empty and (valid["mean_count"] == 0).all()),
        }
        if not valid.empty:
            peak = valid.loc[valid["mean_count"].idxmax()]
            peak_count = float(peak["mean_count"])
            vmin = float(valid["threshold_voltage_v"].min())
            vmax = float(valid["threshold_voltage_v"].max())
            shutter = _number(peak.get("shutter_duration_s"))
            entry.update(
                scan_min_voltage_v=vmin, scan_max_voltage_v=vmax,
                peak_mean_count=peak_count,
                peak_dac_code=int(peak["threshold_dac_code"]),
                peak_voltage_v=float(peak["threshold_voltage_v"]),
                peak_rate_hz=peak_count / shutter if shutter > 0 else np.nan,
                peak_fano_factor=(float(peak["std_count"]) ** 2 / peak_count if peak_count > 0 else np.nan),
                peak_on_scan_boundary=bool(peak_count > 0 and peak["threshold_voltage_v"] in (vmin, vmax)),
                nonzero_dac_point_count=int((valid["mean_count"] > 0).sum()),
                positive_mean_zero_median_point_count=int(((valid["mean_count"] > 0) & (valid["median_count"] == 0)).sum()),
            )
        rows.append(entry)
    return pd.DataFrame(rows)


def _method_center(row: pd.Series, method: str) -> float:
    value = _number(row.get(METHOD_COLUMNS[method]))
    if not math.isfinite(value):
        return np.nan
    if method == "fit" and (row.get("fit_status") != "ok" or not str(row.get("fit_model", "")).startswith("gaussian")):
        return np.nan
    if method == "centroid" and not _true(row.get("centroid_applicable")):
        return np.nan
    if method == "maximum" and not _true(row.get("maximum_estimator_applicable")):
        return np.nan
    if str(row.get("curve_shape", "")) in ("flat", "edge_or_clipped", "unknown"):
        return np.nan
    lower, upper = _number(row.get("scan_min_voltage_v")), _number(row.get("scan_max_voltage_v"))
    if math.isfinite(lower) and math.isfinite(upper) and not lower <= value <= upper:
        return np.nan
    return value


def _choose_target(pairs: list[tuple[float, float, int, int]], requested: float | None) -> tuple[float | None, str]:
    if requested is not None:
        if not math.isfinite(float(requested)):
            raise ValueError("target_voltage must be finite")
        return float(requested), "explicit_common_target"
    if not pairs:
        return None, "no_usable_two_endpoint_pixels"
    values = np.asarray(pairs, dtype=float)
    a, b, t0, t1 = values.T
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    endpoints = np.unique(np.concatenate((lo, hi)))
    candidates = np.unique(np.concatenate((endpoints, (endpoints[:-1] + endpoints[1:]) / 2)))
    reference = float(np.median((a + b) / 2))
    best: tuple[float, float, float] | None = None
    target = reference
    for candidate in candidates:
        reachable = (lo <= candidate) & (candidate <= hi)
        trim = np.clip(np.rint(t0 + (candidate - a) * (t1 - t0) / (b - a)), t0, t1)
        headroom = np.minimum(trim - t0, t1 - trim)
        score = (-int(reachable.sum()), -float(np.median(headroom[reachable])), abs(float(candidate) - reference))
        if best is None or score < best:
            best, target = score, float(candidate)
    return target, "maximum_endpoint_overlap_then_trim_headroom"


def propose_noise_trim_maps(
    noise_fits: pd.DataFrame,
    noise_statistics: pd.DataFrame,
    *,
    target_voltage: float | None = None,
    bad_pixel_map: BadPixelMapInput = None,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    """Return fit/centroid/maximum proposals, summaries, and curve diagnostics.

    Two measured endpoints permit only a linear prediction. With interior trim
    measurements, the best measured setting is preferred. A final verification
    requires a real ``equalized_final`` acquisition at the proposed code.
    """

    bad = set(normalize_bad_pixel_map(bad_pixel_map))
    diagnostics = noise_curve_diagnostics(noise_statistics)
    coordinates = set(bad)
    if not noise_fits.empty:
        coordinates.update(zip(noise_fits["column"].astype(int), noise_fits["row"].astype(int)))
    if not diagnostics.empty:
        coordinates.update(zip(diagnostics["column"].astype(int), diagnostics["row"].astype(int)))
    fit_groups = {
        (int(c), int(r)): group.copy()
        for (c, r), group in noise_fits.groupby(["column", "row"])
    } if not noise_fits.empty else {}
    diagnostic_groups = {
        (int(c), int(r)): group
        for (c, r), group in diagnostics.groupby(["column", "row"])
    } if not diagnostics.empty else {}
    proposals, summaries = {}, []
    for method in METHOD_COLUMNS:
        valid_groups, endpoint_pairs = {}, []
        for coordinate, group in fit_groups.items():
            if coordinate in bad:
                continue
            group["method_center_v"] = group.apply(lambda row: _method_center(row, method), axis=1)
            usable = group[np.isfinite(group["method_center_v"])].sort_values("local_trim_code")
            valid_groups[coordinate] = usable
            if usable["local_trim_code"].nunique() >= 2:
                a, b = usable.iloc[0], usable.iloc[-1]
                if abs(float(b.method_center_v) - float(a.method_center_v)) > 1e-12:
                    endpoint_pairs.append((float(a.method_center_v), float(b.method_center_v), int(a.local_trim_code), int(b.local_trim_code)))
        target, target_method = _choose_target(endpoint_pairs, target_voltage)
        rows = []
        for column, row in sorted(coordinates):
            coordinate = column, row
            measured = fit_groups.get(coordinate, pd.DataFrame())
            usable = valid_groups.get(coordinate, pd.DataFrame())
            diag = diagnostic_groups.get(coordinate, pd.DataFrame())
            reasons: list[str] = []
            always_bad = coordinate in bad
            # No response at just ONE endpoint never produces a dead-pixel claim.
            no_response = bool(
                len(diag) >= 2 and diag["no_response_in_measured_range"].all()
                and not measured.empty and measured["local_trim_code"].nunique() >= 2
            )
            invalid_readout = bool(not diag.empty and diag["valid_repeat_count"].sum() == 0)
            mask_candidate = always_bad or no_response or invalid_readout
            record: dict[str, Any] = {
                "column": column, "row": row, "method": method,
                "comparator_under_test": str(measured.iloc[0].get("comparator_under_test", "")) if not measured.empty else "",
                "local_trim_field": str(measured.iloc[0].get("local_trim_field", "")) if not measured.empty else "",
                "target_voltage_v": target, "recommended_trim_code": None,
                "predicted_or_measured_center_v": np.nan, "residual_to_target_v": np.nan,
                "proposal_source": "unresolved", "target_reachable": False,
                "verified_in_equalized_final": False,
                "usable_trim_setting_count": int(usable["local_trim_code"].nunique()) if not usable.empty else 0,
                "permanent_bad_pixel": always_bad, "mask_recommended": mask_candidate,
                "trim_range_limited": False, "review_required": False,
            }
            if always_bad:
                reasons.append("user_permanent_bad_pixel")
            elif no_response:
                reasons.append("no_response_at_multiple_trims_in_measured_ranges_check_with_injection")
            elif invalid_readout:
                reasons.append("no_valid_readout_check_transport_before_masking")
            elif target is None or usable.empty or usable["local_trim_code"].nunique() < 2:
                reasons.append("insufficient_usable_trim_settings_for_this_method")
            else:
                a, b = usable.iloc[0], usable.iloc[-1]
                v0, v1 = float(a.method_center_v), float(b.method_center_v)
                t0, t1 = int(a.local_trim_code), int(b.local_trim_code)
                if abs(v1 - v0) <= 1e-12:
                    reasons.append("no_resolved_trim_response")
                else:
                    reachable = min(v0, v1) <= target <= max(v0, v1)
                    if usable["local_trim_code"].nunique() > 2:
                        best = usable.iloc[int(np.argmin(abs(usable.method_center_v.to_numpy(float) - target)))]
                        code, center = int(best.local_trim_code), float(best.method_center_v)
                        source = "best_measured_setting"
                    else:
                        code = int(np.clip(np.rint(t0 + (target - v0) * (t1 - t0) / (v1 - v0)), t0, t1))
                        center = v0 + (code - t0) * (v1 - v0) / (t1 - t0)
                        source = "linear_endpoint_prediction_NOT_MEASURED"
                    verified = bool(((usable["stage"] == "equalized_final") & (usable["local_trim_code"] == code)).any())
                    record.update(
                        recommended_trim_code=code, predicted_or_measured_center_v=center,
                        residual_to_target_v=center - target, proposal_source=source,
                        target_reachable=reachable, verified_in_equalized_final=verified,
                    )
                    if not reachable:
                        # A healthy responding pixel can simply have insufficient
                        # trim range. Keep it enabled and expose this as a separate
                        # review class instead of calling it a dead-pixel candidate.
                        record["trim_range_limited"] = True
                        reasons.append("target_outside_measured_trim_reach_not_a_dead_pixel_claim")
                    if source.startswith("linear"):
                        reasons.append("verify_prediction_with_local_trim_scan")
                    if not verified:
                        reasons.append("no_final_matrix_verification_at_proposed_setting")
            if not diag.empty:
                if diag["invalid_repeat_count"].sum() > 0:
                    reasons.append("invalid_readouts_present")
                if diag["saturated_repeat_count"].sum() > 0:
                    reasons.append("counter_saturation_present")
                if (diag["min_repeats_per_code"] != diag["max_repeats_per_code"]).any():
                    reasons.append("unequal_repeat_coverage")
                if "peak_on_scan_boundary" in diag and diag["peak_on_scan_boundary"].fillna(False).any():
                    reasons.append("peak_at_scan_boundary_extend_scan")
            record["review_required"] = bool(reasons)
            record["reason_codes"] = ";".join(reasons)
            # Explicit register semantics, to avoid inverting a boolean bad map.
            record["suggested_PX_MASK"] = int(not record["mask_recommended"])
            record["suggested_PX_TST_EN_when_excluded"] = 0 if record["mask_recommended"] else None
            rows.append(record)
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame["recommended_trim_code"] = pd.array(frame["recommended_trim_code"], dtype="Int64")
        proposals[method] = frame
        summaries.append({
            "method": method, "target_voltage_v": target, "target_method": target_method,
            "pixel_count": len(frame), "usable_two_endpoint_pixels": len(endpoint_pairs),
            "proposed_trim_count": int(frame["recommended_trim_code"].notna().sum()) if len(frame) else 0,
            "mask_candidate_count_including_user_mask": int(frame["mask_recommended"].sum()) if len(frame) else 0,
            "verified_final_pixel_count": int(frame["verified_in_equalized_final"].sum()) if len(frame) else 0,
        })
    return proposals, pd.DataFrame(summaries), diagnostics


def save_noise_recommendations(
    directory: Path,
    noise_fits: pd.DataFrame,
    noise_statistics: pd.DataFrame,
    *,
    target_voltage: float | None = None,
    bad_pixel_map: BadPixelMapInput = None,
) -> dict[str, Path]:
    """Write reusable proposal CSVs and optional mask JSONs. Never auto-apply."""

    proposals, summary, diagnostics = propose_noise_trim_maps(
        noise_fits, noise_statistics, target_voltage=target_voltage, bad_pixel_map=bad_pixel_map,
    )
    outputs = {
        "recommendation_summary": atomic_write_table(directory / "recommendation_summary.csv", summary),
        "noise_curve_diagnostics": atomic_write_table(directory / "noise_curve_diagnostics.csv", diagnostics),
    }
    for method, frame in proposals.items():
        outputs[f"trim_recommendations_{method}"] = atomic_write_table(directory / f"trim_recommendations_{method}.csv", frame)
        candidates = frame[frame["mask_recommended"]] if not frame.empty else frame
        coordinates = list(zip(candidates["column"], candidates["row"])) if len(candidates) else []
        document = bad_pixel_document(coordinates)
        document.update(
            method=method, proposal_only=True, automatically_applied=False,
            warning_ru="Это предложение для данного режима/метода, а не диагноз поломки. Проверьте причины в CSV перед применением.",
        )
        path = directory / f"bad_pixels_suggested_{method}.json"
        atomic_write_json(path, document)
        outputs[f"bad_pixels_suggested_{method}"] = path
    return outputs


def load_recommended_trim_map(path: str | Path) -> dict[tuple[int, int], int]:
    """Load resolved, unmasked proposals; unresolved pixels remain absent.

    This does NOT write to the chip. Before programming, the caller must decide
    how to handle all missing pixels and must explicitly select one method.
    """

    frame = pd.read_csv(path)
    required = {"column", "row", "recommended_trim_code", "mask_recommended"}
    if not required.issubset(frame):
        raise ValueError("not a per-method trim recommendation CSV")
    result = {}
    for _, row in frame.iterrows():
        if pd.isna(row.recommended_trim_code) or _true(row.mask_recommended):
            continue
        code = _number(row.recommended_trim_code)
        if not code.is_integer() or not 0 <= code <= 31:
            raise ValueError("recommended trim must be an integer in 0..31")
        coordinate_values = (_number(row.column), _number(row.row))
        if not all(value.is_integer() for value in coordinate_values):
            raise ValueError("trim-map coordinates must be integers")
        coordinate = tuple(int(value) for value in coordinate_values)
        normalize_bad_pixel_map([coordinate])  # Reuse physical-coordinate validation.
        if coordinate in result:
            raise ValueError(f"duplicate trim-map coordinate {coordinate}")
        result[coordinate] = int(code)
    return result
