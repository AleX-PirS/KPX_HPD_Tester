from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from mgpd import MGPDClient
from pixel_matrix import OWNED_COLUMNS

from .analysis import (
    _paired_scurve_efficiency,
    analyze_saved_experiment,
    calculate_noise_statistics,
    choose_measured_trim_map,
    fit_noise_statistics,
    select_equalization_target,
)
from .calibration import (
    ReferenceDacCalibration,
    ThresholdDacCalibration,
    load_reference_dac_calibrations,
    load_threshold_dac_calibrations,
    select_reference_dac_pairs,
)
from .hardware import (
    KeysightBurstGenerator,
    KeysightBurstSettings,
    KeysightBurstShotExecutor,
    MGPDMeasurementBackend,
    STANDARD_CHARACTERIZATION_FCLK_MHZ,
    ShotExecutor,
    UpoPwmShotExecutor,
    build_standard_characterization_pixel_configs,
    load_base_pixel_configs,
)
from .injection import build_injection_groups, resolve_gain_map
from .pixel_masks import (
    BadPixelMapInput, bad_pixel_document, noise_baseline_pixel_word,
    normalize_bad_pixel_map,
)
from .recommendations import save_noise_recommendations
from .parameters import validate_eo_overrides
from .measurement import (
    ScurveScanRun,
    run_noise_scan as acquire_noise_scan,
    run_scurve_points,
)
from .models import (
    AnalysisSettings,
    CharacterizationSettings,
    FRAMEWORK_VERSION,
    ScurveSettings,
    get_window_spec,
    resolve_pixels,
)
from .storage import ExperimentStore, file_sha256, utc_now_text


@dataclass(frozen=True)
class CharacterizationResult:
    experiment_path: Path
    analysis_path: Path | None
    target_voltage_v: float | None
    trim_map: dict[tuple[int, int], int]
    status: str


@dataclass(frozen=True)
class ManualExposureChange:
    """Information shown before the user changes the external UPO exposure."""

    experiment_path: Path
    noise_shutter_duration_s: float | None
    scurve_shutter_duration_s: float
    n_injections: int
    injection_count_source: str
    burst_duration_s_at_100khz: float | None = None
    upo_pwm_frequency_khz: float | None = None
    upo_pwm_high_time_ns: int | None = None


def interactive_exposure_pause(change: ManualExposureChange) -> None:
    """Wait for explicit confirmation after manual UPO exposure adjustment."""

    message = (
        "Установите в УПО экспозицию "
        f"{change.scurve_shutter_duration_s:g} с для S-кривой и нажмите Enter. "
        f"Номинальное число инжекций для анализа: {change.n_injections} "
        f"({change.injection_count_source}). "
        f"Эксперимент уже сохранен в {change.experiment_path}."
    )
    try:
        input(message + "\n")
    except EOFError as error:
        raise RuntimeError(
            "требуется подтверждение ручной смены экспозиции УПО; для "
            "неинтерактивного запуска передайте callback before_scurve"
        ) from error


def _project_revision(project_root: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        return {"git_commit": commit, "git_worktree_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_worktree_dirty": None}


def _normalized_document(value: Any) -> Any:
    def convert(item: Any) -> Any:
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, Mapping):
            return {str(key): convert(entry) for key, entry in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(entry) for entry in item]
        if hasattr(item, "item"):
            try:
                return item.item()
            except (TypeError, ValueError):
                pass
        return item

    return json.loads(json.dumps(convert(value), sort_keys=True, ensure_ascii=True))


# These fields first appeared after experiments made by older framework
# versions had already been saved.  A missing field is not an operator change:
# it only means that the old metadata could not record the new default.  An
# existing stored value is never replaced here, so a real settings mismatch is
# still rejected by the strict comparison below.
_RESUME_DEFAULT_MIGRATION_PATHS: tuple[tuple[str, ...], ...] = (
    ("scurve", "coarse_baseline_noise_consecutive_codes"),
    ("scurve", "reference_common_mode_step_error_slack_v"),
    ("analysis", "infer_upo_pwm_plateau_denominator"),
    ("analysis", "scurve_plateau_min_codes"),
    ("analysis", "scurve_fit_core_low_fraction"),
    ("analysis", "scurve_fit_core_high_fraction"),
    ("analysis", "scurve_plot_zero_tail_points"),
    ("analysis", "scurve_plot_code_margin"),
    ("analysis", "scurve_plot_noise_peak_search_codes"),
    ("analysis", "scurve_plot_noise_peak_support_fraction"),
)


def _backfill_new_resume_defaults(
    stored: dict[str, Any], requested: Mapping[str, Any]
) -> None:
    for path in _RESUME_DEFAULT_MIGRATION_PATHS:
        stored_cursor: dict[str, Any] = stored
        requested_cursor: Mapping[str, Any] = requested
        for name in path[:-1]:
            requested_child = requested_cursor.get(name)
            if not isinstance(requested_child, Mapping):
                break
            stored_child = stored_cursor.get(name)
            if stored_child is None:
                stored_child = {}
                stored_cursor[name] = stored_child
            if not isinstance(stored_child, dict):
                break
            stored_cursor = stored_child
            requested_cursor = requested_child
        else:
            leaf = path[-1]
            if leaf not in stored_cursor and leaf in requested_cursor:
                stored_cursor[leaf] = copy.deepcopy(requested_cursor[leaf])


def _validate_resume_inputs(
    store: ExperimentStore,
    *,
    settings: CharacterizationSettings,
    counter_key: str,
    upper_non_limiting_code: int,
    calibrations: Mapping[str, ThresholdDacCalibration],
    reference_calibrations: Mapping[str, ReferenceDacCalibration],
    base_configs: Mapping[tuple[int, int], int],
    pixels: Sequence[tuple[int, int]],
    bad_pixels: Sequence[tuple[int, int]] = (),
) -> None:
    stored_bad_pixels = normalize_bad_pixel_map(store.metadata.get("bad_pixel_mask"))
    if set(stored_bad_pixels) != set(bad_pixels):
        raise ValueError("resume bad_pixel_map differs; start a new physical experiment")
    stored_settings = copy.deepcopy(store.metadata.get("settings", {}))
    requested_settings = settings.to_dict()
    _backfill_new_resume_defaults(stored_settings, requested_settings)
    # Analysis and plotting settings never alter physical acquisition. They may
    # therefore change between framework versions without blocking a hardware
    # resume; every completed analysis records its own settings separately.
    for document in (stored_settings, requested_settings):
        document.pop("analysis", None)
    if _normalized_document(stored_settings) != _normalized_document(requested_settings):
        raise ValueError(
            "resume settings differ from metadata; re-analyze offline for analysis-only changes "
            "or start a new physical experiment"
        )
    if store.metadata.get("counter_key") != counter_key:
        raise ValueError("resume counter_key differs from the original experiment")
    stored_upper = (
        store.metadata.get("upper_non_limiting_selection", {}).get("selected_code")
    )
    if stored_upper is None or int(stored_upper) != int(upper_non_limiting_code):
        raise ValueError("resume upper non-limiting DAC code differs from the original experiment")
    spec = get_window_spec(store.metadata["window"])
    if store.metadata.get("fixed_threshold_codes") != spec.fixed_threshold_codes(upper_non_limiting_code):
        raise ValueError(
            "resume fixed comparator thresholds differ or were not recorded; "
            "start a new experiment with the inactive DACs at 1023. "
            "Old measurements remain available for offline analysis."
        )

    stored_calibrations = store.metadata.get("threshold_dac_calibrations", {})
    for name, calibration in calibrations.items():
        stored = stored_calibrations.get(name)
        if not stored:
            raise ValueError(f"resume metadata has no calibration record for {name}")
        current_digest = calibration.to_metadata()["curve_sha256"]
        if stored.get("curve_sha256") != current_digest:
            raise ValueError(f"resume calibration curve differs for {name}")

    stored_reference_calibrations = store.metadata.get(
        "reference_dac_calibrations", {}
    )
    for name, calibration in reference_calibrations.items():
        stored = stored_reference_calibrations.get(name)
        if not stored:
            raise ValueError(f"resume metadata has no reference calibration record for {name}")
        current_digest = calibration.to_metadata()["curve_sha256"]
        if stored.get("curve_sha256") != current_digest:
            raise ValueError(f"resume reference calibration curve differs for {name}")

    base_record = store.metadata.get("base_pixel_configuration", {})
    relative = base_record.get("normalized_selected_pixels_csv")
    if not relative:
        raise ValueError("resume metadata has no normalized base pixel configuration")
    stored_base = pd.read_csv(store.root / relative)
    stored_lookup = {
        (int(row["column"]), int(row["row"])): int(
            str(row["raw_pixel_config_hex"]), 16
        )
        for _, row in stored_base.iterrows()
    }
    for coordinate in pixels:
        if stored_lookup.get(coordinate) != int(base_configs[coordinate]):
            raise ValueError(
                f"resume base pixel configuration differs at Col={coordinate[0]} "
                f"Row={coordinate[1]}"
            )


def _save_calibrations(
    store: ExperimentStore,
    calibrations: Mapping[
        str, ThresholdDacCalibration | ReferenceDacCalibration
    ],
    *,
    destination_directory: str = "inputs/calibrations",
) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for name, calibration in calibrations.items():
        if calibration.source_path is not None:
            record = store.copy_input_file(
                calibration.source_path, destination_directory
            )
        else:
            destination = store.root / destination_directory / f"{name}.csv"
            frame = pd.DataFrame(
                {
                    "dac_code": calibration.codes,
                    "threshold_voltage_v": calibration.voltages,
                }
            )
            store.write_table(destination, frame)
            record = {
                "original_path": None,
                "experiment_copy": destination.relative_to(store.root).as_posix(),
                "sha256": file_sha256(destination),
                "size_bytes": destination.stat().st_size,
            }
        record.update(calibration.to_metadata())
        # The copied file has the same original column names when it came from CSV.
        record["code_column"] = calibration.code_column or "dac_code"
        record["voltage_column"] = calibration.voltage_column or "threshold_voltage_v"
        records[name] = record
    return records


def _load_and_freeze_noise_reference(
    reference_path: str | Path,
    *,
    destination_store: ExperimentStore,
    spec_name: str,
    threshold_dac: str,
    calibration: ThresholdDacCalibration,
    pixels: Sequence[tuple[int, int]],
    require_trim_map: bool,
) -> tuple[pd.DataFrame, dict[tuple[int, int], int] | None]:
    """Validate and copy a previous noise result into a new S-curve run."""

    reference = ExperimentStore(reference_path)
    if reference.metadata.get("window") != spec_name:
        raise ValueError("noise-reference window does not match the S-curve window")
    reference_pixels = {
        (int(item["column"]), int(item["row"]))
        for item in reference.metadata.get("pixel_selection", [])
    }
    missing_pixels = set(pixels) - reference_pixels
    if missing_pixels:
        first = sorted(missing_pixels)[0]
        raise ValueError(
            f"noise reference is missing {len(missing_pixels)} selected pixel(s); "
            f"first is Col={first[0]} Row={first[1]}"
        )
    reference_calibration = reference.metadata.get(
        "threshold_dac_calibrations", {}
    ).get(threshold_dac, {})
    current_digest = calibration.to_metadata()["curve_sha256"]
    if reference_calibration.get("curve_sha256") != current_digest:
        raise ValueError("noise-reference threshold-DAC calibration curve differs")

    raw = reference.load_raw("noise")
    statistics = calculate_noise_statistics(raw)
    if statistics.empty:
        raise RuntimeError("noise-reference experiment contains no usable raw noise data")
    available_stages = set(statistics["stage"].astype(str))
    if not ({"equalized_final", "baseline_noise", "trim_00"} & available_stages):
        raise RuntimeError(
            "noise reference has no equalized_final, baseline_noise or trim_00 stage"
        )

    reference_directory = destination_store.root / "inputs" / "noise_reference"
    statistics_path = reference_directory / "noise_statistics.csv"
    destination_store.write_table(statistics_path, statistics)
    metadata_copy = destination_store.copy_input_file(
        reference.metadata_path, "inputs/noise_reference"
    )

    trim_map: dict[tuple[int, int], int] | None = None
    trim_relative = reference.metadata.get("final_trim_map")
    if trim_relative:
        trim_source = reference.root / str(trim_relative)
        if trim_source.exists():
            trim_table = pd.read_csv(trim_source, keep_default_na=False)
            trim_column = (
                "trim_code"
                if "trim_code" in trim_table
                else "selected_trim_code"
            )
            trim_map = {
                (int(row["column"]), int(row["row"])): int(row[trim_column])
                for _, row in trim_table.iterrows()
            }
            missing_trims = set(pixels) - set(trim_map)
            if missing_trims:
                first = sorted(missing_trims)[0]
                raise ValueError(
                    f"reference trim map is missing Col={first[0]} Row={first[1]}"
                )
            trim_map = {coordinate: trim_map[coordinate] for coordinate in pixels}
            destination_store.write_table(
                reference_directory / "final_trim_map.csv",
                pd.DataFrame(
                    [
                        {"column": column, "row": row, "trim_code": trim}
                        for (column, row), trim in trim_map.items()
                    ]
                ),
            )
    if require_trim_map and trim_map is None:
        raise RuntimeError(
            "use_reference_trim_map=True but the noise reference has no final trim map"
        )

    destination_store.update_metadata(
        noise_reference={
            "source_experiment_path": str(reference.root),
            "source_experiment_id": reference.metadata.get("experiment_id"),
            "source_status": reference.metadata.get("status"),
            "source_metadata_copy": metadata_copy["experiment_copy"],
            "source_metadata_sha256": metadata_copy["sha256"],
            "statistics_copy": statistics_path.relative_to(
                destination_store.root
            ).as_posix(),
            "final_trim_map_copy": (
                (reference_directory / "final_trim_map.csv")
                .relative_to(destination_store.root)
                .as_posix()
                if trim_map is not None
                else None
            ),
            "threshold_dac_curve_sha256": current_digest,
        }
    )
    return statistics, trim_map


def _verification_codes(
    calibration: ThresholdDacCalibration,
    target_voltage: float,
    margin_codes: int,
) -> tuple[int, ...]:
    target_code = calibration.voltage_to_nearest_dac_code(target_voltage)
    lower = max(calibration.min_code, target_code - margin_codes)
    upper = min(calibration.max_code, target_code + margin_codes)
    return tuple(range(lower, upper + 1))


def _online_noise_analysis(
    store: ExperimentStore,
    calibration: ThresholdDacCalibration,
    settings: CharacterizationSettings,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    store.log_status("Анализ noise: чтение raw и расчет статистики")
    raw = store.load_raw("noise", workers=settings.analysis.read_workers)
    statistics = calculate_noise_statistics(raw)
    del raw
    fits = fit_noise_statistics(
        statistics,
        settings=settings.analysis,
        calibration=calibration,
    )
    directory = store.root / "analysis" / "online_checkpoint"
    directory.mkdir(parents=True, exist_ok=True)
    store.write_table(directory / "noise_statistics.csv", statistics)
    store.write_table(directory / "noise_fit_results.csv", fits)
    save_noise_recommendations(
        directory, fits, statistics,
        target_voltage=settings.equalization.target_voltage,
        bad_pixel_map=store.metadata.get("bad_pixel_mask"),
    )
    return statistics, fits


def _candidate_stage(prefix: str, offset: int) -> str:
    sign = "p" if offset >= 0 else "m"
    return f"{prefix}_{sign}{abs(offset):02d}"


def _estimated_trim_map(
    reachability: pd.DataFrame,
) -> dict[tuple[int, int], int]:
    result: dict[tuple[int, int], int] = {}
    for _, row in reachability.iterrows():
        if pd.isna(row["estimated_trim_code"]):
            continue
        result[(int(row["column"]), int(row["row"]))] = int(row["estimated_trim_code"])
    return result


def _map_with_offset(
    estimate: Mapping[tuple[int, int], int],
    offset: int,
    *,
    unchanged: Mapping[tuple[int, int], int] | None = None,
) -> dict[tuple[int, int], int]:
    return {
        **dict(unchanged or {}),
        **{
        coordinate: max(0, min(31, int(trim) + int(offset)))
        for coordinate, trim in estimate.items()
        },
    }


def _select_final_trim_map(
    pixels: Sequence[tuple[int, int]],
    measured: pd.DataFrame,
    estimate: Mapping[tuple[int, int], int],
    *,
    unchanged: Mapping[tuple[int, int], int] | None = None,
) -> tuple[dict[tuple[int, int], int], pd.DataFrame]:
    measured_lookup = {
        (int(row["column"]), int(row["row"])): int(row["selected_trim_code"])
        for _, row in measured.iterrows()
        if pd.notna(row.get("selected_trim_code"))
        and (
            (int(row["column"]), int(row["row"])) in estimate
            or str(row.get("selected_from_stage", "")).startswith(("trim_candidate_", "trim_expand_", "trim_full_"))
        )
    }
    rows = []
    trim_map = {}
    for coordinate in pixels:
        if coordinate in measured_lookup:
            trim = measured_lookup[coordinate]
            source = "experimentally_measured_candidate"
        elif coordinate in estimate:
            trim = int(estimate[coordinate])
            source = "endpoint_interpolation_fallback"
        elif unchanged is not None and coordinate in unchanged:
            trim = int(unchanged[coordinate])
            source = "initial_trim_preserved_NO_RELIABLE_EQUALIZATION"
        else:
            raise RuntimeError(
                f"no trim estimate is available for Col={coordinate[0]} Row={coordinate[1]}"
            )
        trim_map[coordinate] = trim
        rows.append(
            {
                "column": coordinate[0],
                "row": coordinate[1],
                "trim_code": trim,
                "selection_source": source,
            }
        )
    return trim_map, pd.DataFrame(rows)


def _safe_background_codes(
    noise_statistics: pd.DataFrame,
    *,
    n_injections: int,
    maximum_fraction: float,
    scurve_shutter_duration_s: float,
    minimum_pixel_fraction: float = 0.95,
) -> tuple[int, ...]:
    if noise_statistics.empty:
        raise RuntimeError("noise statistics are required for automatic S-curve range selection")
    final = noise_statistics[noise_statistics["stage"] == "equalized_final"].copy()
    if final.empty:
        final = noise_statistics[noise_statistics["stage"] == "baseline_noise"].copy()
    if final.empty:
        final = noise_statistics[noise_statistics["stage"] == "trim_00"].copy()
    if final.empty:
        raise RuntimeError(
            "S-curve range selection requires an equalized_final or baseline_noise noise scan"
        )
    if "shutter_duration_s" not in final:
        raise RuntimeError(
            "noise statistics do not contain shutter duration; automatic S-curve "
            "background scaling is unsafe"
        )
    durations = pd.to_numeric(final["shutter_duration_s"], errors="coerce")
    if durations.isna().any() or (durations <= 0).any():
        raise RuntimeError(
            "noise shutter duration is missing or invalid; set it to the manual UPO value"
        )
    final["expected_scurve_background_count"] = (
        pd.to_numeric(final["mean_count"], errors="coerce")
        / durations
        * float(scurve_shutter_duration_s)
    )
    limit = n_injections * maximum_fraction
    safe_fraction = final.groupby("threshold_dac_code")[
        "expected_scurve_background_count"
    ].apply(lambda values: float(np.mean(pd.to_numeric(values, errors="coerce") < limit)))
    safe_codes = sorted(
        int(code) for code, fraction in safe_fraction.items() if fraction >= minimum_pixel_fraction
    )
    if not safe_codes:
        raise RuntimeError(
            "no threshold code meets the configured background criterion for at least "
            f"{minimum_pixel_fraction:.0%} of selected pixels"
        )
    return tuple(safe_codes)


def _ordered_scurve_codes(
    calibration: ThresholdDacCalibration,
    settings: ScurveSettings,
    *,
    high_code: int | None = None,
    low_code: int | None = None,
    step: int | None = None,
) -> tuple[int, ...]:
    high = int(
        calibration.max_code
        if high_code is None and settings.coarse_high_code is None
        else settings.coarse_high_code
        if high_code is None
        else high_code
    )
    low = int(
        calibration.min_code
        if low_code is None and settings.coarse_low_code is None
        else settings.coarse_low_code
        if low_code is None
        else low_code
    )
    if high < low:
        raise ValueError("S-curve high DAC code must be >= low DAC code")
    calibration.lookup(high)
    calibration.lookup(low)
    increment = int(step or settings.coarse_step)
    if settings.scan_descending:
        values = list(range(high, low - 1, -increment))
        if not values or values[-1] != low:
            values.append(low)
    else:
        values = list(range(low, high + 1, increment))
        if not values or values[-1] != high:
            values.append(high)
    return tuple(dict.fromkeys(values))


def _scurve_transition_brackets(
    store: ExperimentStore,
    noise_statistics: pd.DataFrame,
    maximum_background_fraction: float,
    *,
    stage: str,
    scurve_settings: ScurveSettings,
    analysis_settings: AnalysisSettings,
) -> tuple[tuple[int, int], ...]:
    """Locate per-pixel 50% crossings, including 0-to-1 coarse jumps."""

    raw = store.load_raw("scurve", stages=(stage,))
    if raw.empty:
        return ()
    efficiency = _paired_scurve_efficiency(
        raw,
        noise_statistics=noise_statistics,
        max_background_fraction=maximum_background_fraction,
        settings=analysis_settings,
        baseline_noise_count_multiplier=(
            scurve_settings.baseline_noise_count_multiplier
        ),
        baseline_noise_pixel_fraction=(
            scurve_settings.baseline_noise_pixel_fraction
        ),
    )
    valid = efficiency[
        efficiency["fit_valid"].astype(str).str.lower().isin(("true", "1"))
    ].copy()
    if valid.empty:
        return ()
    acquired_codes = sorted(
        int(code) for code in efficiency["threshold_dac_code"].dropna().unique()
    )
    code_rank = {code: index for index, code in enumerate(acquired_codes)}
    points = (
        valid.groupby(["column", "row", "threshold_dac_code"], as_index=False)
        ["efficiency"]
        .mean()
    )
    brackets: set[tuple[int, int]] = set()
    for _, pixel in points.groupby(["column", "row"], sort=False):
        pixel = pixel.sort_values("threshold_dac_code")
        codes = pixel["threshold_dac_code"].to_numpy(dtype=int)
        values = pixel["efficiency"].to_numpy(dtype=float)
        for code, value in zip(codes, values):
            if np.isfinite(value) and 0.05 <= value <= 0.95:
                brackets.add((int(code), int(code)))
        for index in range(len(codes) - 1):
            left_code = int(codes[index])
            right_code = int(codes[index + 1])
            # Do not bridge a DAC code whose paired background was rejected.
            if code_rank.get(right_code, -2) - code_rank.get(left_code, -1) != 1:
                continue
            left_value = float(values[index])
            right_value = float(values[index + 1])
            if not np.isfinite(left_value) or not np.isfinite(right_value):
                continue
            if left_value == right_value:
                continue
            if (left_value - 0.5) * (right_value - 0.5) <= 0:
                brackets.add((left_code, right_code))
    return tuple(sorted(brackets))


def _fine_codes_from_brackets(
    brackets: Sequence[tuple[int, int]],
    calibration: ThresholdDacCalibration,
    settings: ScurveSettings,
) -> tuple[tuple[int, ...], dict[str, Any]]:
    fine_set: set[int] = set()
    for first, second in brackets:
        lower = max(
            calibration.min_code,
            min(int(first), int(second)) - settings.fine_margin_codes,
        )
        upper = min(
            calibration.max_code,
            max(int(first), int(second)) + settings.fine_margin_codes,
        )
        fine_set.update(range(lower, upper + 1, settings.fine_step))
        fine_set.add(upper)
    ordered = tuple(sorted(fine_set, reverse=settings.scan_descending))
    ascending = sorted(fine_set)
    bands: list[dict[str, int]] = []
    if ascending:
        band_start = previous = ascending[0]
        for code in ascending[1:]:
            if code - previous > settings.fine_step:
                bands.append({"start": band_start, "stop": previous})
                band_start = code
            previous = code
        bands.append({"start": band_start, "stop": previous})
    return ordered, {
        "method": "per_pixel_adjacent_50_percent_brackets_plus_midlevel_points",
        "transition_bracket_count": len(brackets),
        "fine_step": settings.fine_step,
        "fine_margin_codes": settings.fine_margin_codes,
        "fine_code_count": len(ordered),
        "fine_bands": bands,
        "scan_direction": "descending" if settings.scan_descending else "ascending",
    }


def _preflight_scan_coverage(
    calibration: ThresholdDacCalibration,
    settings: CharacterizationSettings,
) -> None:
    manual = settings.noise.manual_codes()
    codes = manual or (
        settings.noise.coarse_start,
        settings.noise.coarse_stop,
    )
    for code in codes:
        calibration.lookup(int(code))


def characterize_comparator(
    client: MGPDClient,
    threshold_calibration_files: Mapping[
        str, str | Path | ThresholdDacCalibration
    ],
    *,
    window: str = "AB",
    pixels: str | Sequence[tuple[int, int]] = "all",
    bad_pixel_map: BadPixelMapInput = None,
    base_pixel_config: str | Path | Mapping[tuple[int, int], int] | None = None,
    results_root: str | Path = "results",
    run_noise_scan: bool = True,
    run_equalization: bool = True,
    run_scurve: bool = True,
    settings: CharacterizationSettings | None = None,
    n_injections: int | None = None,
    coarse_start: int | None = None,
    coarse_stop: int | None = None,
    minimum_reference_code: int | None = None,
    maximum_reference_code: int | None = None,
    reference_calibration_files: Mapping[
        str, str | Path | ReferenceDacCalibration
    ] | None = None,
    injection_voltage_steps_v: Sequence[float] | None = None,
    reference_calibration_voltage_unit: str = "auto",
    gain_map: Mapping[tuple[int, int], int] | Sequence[Any] | None = None,
    counter_key: str | None = None,
    confirm_inferred_counter_mapping: bool = False,
    upper_non_limiting_code: int | None = None,
    shot_executor: ShotExecutor | None = None,
    keysight_generator: KeysightBurstGenerator | None = None,
    keysight_burst_settings: KeysightBurstSettings | None = None,
    noise_reference_experiment: str | Path | None = None,
    use_reference_trim_map: bool = True,
    before_scurve: Callable[[ManualExposureChange], None] | None = None,
    resume_experiment: str | Path | None = None,
    additional_metadata: Mapping[str, Any] | None = None,
    initialization_fclk_mhz: int = STANDARD_CHARACTERIZATION_FCLK_MHZ,
    eo_overrides: Mapping[str, int] | None = None,
) -> CharacterizationResult:
    """Characterize one AB, BC or CD counting window.

    Public pixel coordinates are always ``(column, row)``. Real hardware runs
    use the built-in reproducible pixel baseline unless an explicit advanced
    baseline is supplied. Later trim changes preserve every other pixel bit. Noise,
    equalization and S-curve stages can be selected independently. ``n_injections``
    controls only finite-burst executors. It is ignored for ``UpoPwmShotExecutor``:
    that denominator is derived from real PWM frequency and the manually matched
    shutter exposure. When measured REF1 and REF2 LUT paths plus
    ``injection_voltage_steps_v`` are supplied, native codes are selected
    automatically with the mandatory physical order ``V_REF1 > V_REF2``.
    S-curve-only runs may freeze and reuse a previous noise experiment through
    ``noise_reference_experiment``.
    """

    eo_overrides = validate_eo_overrides(eo_overrides, run_scurve=run_scurve)
    selected_settings = (
        copy.deepcopy(settings) if settings is not None else CharacterizationSettings()
    )
    upo_pwm_mode = run_scurve and isinstance(shot_executor, UpoPwmShotExecutor)
    if upo_pwm_mode:
        # A continuous UPO PWM has no user-programmed burst count. Put a benign
        # placeholder in the copied settings before generic validation; the real
        # nominal denominator is calculated after shutter validation below.
        selected_settings.scurve.n_injections = 1
    for name, value in (("coarse_start", coarse_start), ("coarse_stop", coarse_stop)):
        if value is not None:
            setattr(selected_settings.noise, name, value)
    for name, value in (
        ("minimum_reference_code", minimum_reference_code),
        ("maximum_reference_code", maximum_reference_code),
    ):
        if value is not None:
            setattr(selected_settings.scurve, name, value)
    selected_settings.validate()
    if n_injections is not None and not upo_pwm_mode:
        if (
            not isinstance(n_injections, int)
            or isinstance(n_injections, bool)
            or n_injections <= 0
        ):
            raise ValueError("n_injections must be a positive integer")
        selected_settings.scurve.n_injections = n_injections
    reference_calibrations: dict[str, ReferenceDacCalibration] = {}
    reference_pair_selections: tuple[Any, ...] = ()
    if injection_voltage_steps_v is not None:
        if not run_scurve:
            raise ValueError(
                "injection_voltage_steps_v is only valid when run_scurve=True"
            )
        if selected_settings.scurve.pulse_amplitudes:
            raise ValueError(
                "supply either settings.scurve.pulse_amplitudes or "
                "injection_voltage_steps_v with REF LUTs, not both"
            )
        if reference_calibration_files is None:
            raise ValueError(
                "automatic REF selection requires reference_calibration_files for REF1 and REF2"
            )
        reference_calibrations = load_reference_dac_calibrations(
            reference_calibration_files,
            voltage_unit=reference_calibration_voltage_unit,
        )
        reference_pair_selections = select_reference_dac_pairs(
            reference_calibrations["DAC_TST_REF1"],
            reference_calibrations["DAC_TST_REF2"],
            injection_voltage_steps_v,
            minimum_reference_code=(
                selected_settings.scurve.minimum_reference_code
            ),
            maximum_reference_code=(
                selected_settings.scurve.maximum_reference_code
            ),
            minimum_reference_voltage_v=(
                selected_settings.scurve.minimum_reference_voltage_v
            ),
            preferred_reference_common_mode_v=(
                selected_settings.scurve.preferred_reference_common_mode_v
            ),
            common_mode_step_error_slack_v=(
                selected_settings.scurve.reference_common_mode_step_error_slack_v
            ),
            maximum_reference_step_error_v=(
                selected_settings.scurve.maximum_reference_step_error_v
            ),
        )
        selected_settings.scurve.pulse_amplitudes = tuple(
            selection.to_pulse_amplitude()
            for selection in reference_pair_selections
        )
    elif reference_calibration_files is not None:
        raise ValueError(
            "reference_calibration_files requires injection_voltage_steps_v; "
            "manual native-code amplitudes do not use a LUT implicitly"
        )
    bounded_amplitudes = []
    for amplitude in selected_settings.scurve.pulse_amplitudes:
        if isinstance(amplitude, Mapping) and any(
            name in amplitude for name in ("DAC_TST_REF1", "DAC_TST_REF2")
        ):
            amplitude = dict(amplitude)
            for name in ("DAC_TST_REF1", "DAC_TST_REF2"):
                code = amplitude.get(name)
                if not isinstance(code, int) or isinstance(code, bool) or not (
                    selected_settings.scurve.minimum_reference_code <= code <= selected_settings.scurve.maximum_reference_code
                ):
                    raise ValueError(f"{name} must be within the configured inclusive REF code bounds")
            amplitude["minimum_reference_code"] = selected_settings.scurve.minimum_reference_code
            amplitude["maximum_reference_code"] = selected_settings.scurve.maximum_reference_code
        bounded_amplitudes.append(amplitude)
    selected_settings.scurve.pulse_amplitudes = tuple(bounded_amplitudes)
    selected_settings.validate()
    spec = get_window_spec(window)
    requested_pixels = resolve_pixels(pixels, OWNED_COLUMNS)
    bad_pixels = normalize_bad_pixel_map(bad_pixel_map)
    selected_pixels = tuple(pixel for pixel in requested_pixels if pixel not in bad_pixels)
    if not selected_pixels:
        raise ValueError("all requested pixels are excluded by bad_pixel_map")
    calibrations = load_threshold_dac_calibrations(threshold_calibration_files)
    missing_calibrations = {
        spec.threshold_dac,
        spec.upper_threshold_dac,
    } - set(calibrations)
    if missing_calibrations:
        raise ValueError(
            "missing required threshold-DAC calibration(s): "
            + ", ".join(sorted(missing_calibrations))
        )
    threshold_calibration = calibrations[spec.threshold_dac]
    upper_calibration = calibrations[spec.upper_threshold_dac]
    _preflight_scan_coverage(threshold_calibration, selected_settings)

    if base_pixel_config is None:
        base_configs = build_standard_characterization_pixel_configs(
            digital_counting_enabled=True
        )
        base_config_source = "built_in_standard_counting_baseline"
    else:
        base_configs = load_base_pixel_configs(base_pixel_config)
        base_config_source = "explicit_base_pixel_config"
    base_configs = {
        coordinate: noise_baseline_pixel_word(raw, bad=coordinate in bad_pixels)
        for coordinate, raw in base_configs.items()
    }

    if counter_key is None:
        counter_key = spec.inferred_counter_key
        counter_mapping_source = "user_confirmed_AB_high_BC_mid_CD_low"
    else:
        counter_mapping_source = "explicit_user_argument"

    if run_scurve and shot_executor is not None and keysight_generator is not None:
        raise ValueError("supply either shot_executor or keysight_generator, not both")
    if run_scurve and shot_executor is None and keysight_generator is not None:
        shot_executor = KeysightBurstShotExecutor(
            keysight_generator,
            settings=keysight_burst_settings,
        )
    if run_scurve and shot_executor is None:
        raise ValueError(
            "run_scurve=True requires shot_executor or keysight_generator"
        )
    if run_scurve and not selected_settings.scurve.pulse_amplitudes:
        raise ValueError("run_scurve=True requires at least one pulse_amplitude")
    if run_scurve and selected_settings.scurve.shutter_duration_s is None:
        raise ValueError(
            "run_scurve=True requires settings.scurve.shutter_duration_s matching UPO"
        )
    upo_pwm_count_metadata: dict[str, Any] | None = None
    if run_scurve and isinstance(shot_executor, UpoPwmShotExecutor):
        # In continuous-PWM mode there is no finite burst count. The analysis
        # denominator is derived solely from the real UPO PWM frequency and the
        # one manual shutter exposure recorded in ScurveSettings. Any public
        # n_injections argument is intentionally irrelevant in this mode.
        derived_injections, upo_pwm_count_metadata = shot_executor.nominal_injections(
            selected_settings.scurve.shutter_duration_s,
            counter_mode_bits=selected_settings.noise.counter_mode_bits,
        )
        selected_settings.scurve.n_injections = derived_injections
        selected_settings.validate()
    normalized_gain_map: dict[tuple[int, int], int] | None = None
    if run_scurve:
        if gain_map is None:
            raise ValueError("run_scurve=True requires a per-pixel gain_map")
        normalized_gain_map = resolve_gain_map(
            gain_map,
            required_pixels=selected_pixels,
            owned_columns=OWNED_COLUMNS,
        )

    if upper_non_limiting_code is None:
        upper_non_limiting_code, upper_selection = (
            upper_calibration.select_upper_non_limiting_endpoint()
        )
    else:
        if not isinstance(upper_non_limiting_code, int) or isinstance(
            upper_non_limiting_code, bool
        ) or not 0 <= upper_non_limiting_code <= 1023:
            raise ValueError("upper_non_limiting_code must be an integer in 0..1023")
        upper_selection = {
            "selection_method": "explicit_user_argument_after_physical_verification",
            "selected_code": upper_non_limiting_code,
            "selected_voltage_v": upper_calibration.dac_code_to_voltage(
                upper_non_limiting_code
            ),
            "calibration_direction": upper_calibration.direction,
        }

    # Reject an incompatible external noise reference before creating a new
    # experiment or sending any command to UPO. The complete raw-data check and
    # immutable copy still happen later, once the destination store exists.
    if noise_reference_experiment is not None:
        reference_preflight = ExperimentStore(noise_reference_experiment).metadata
        if reference_preflight.get("eo_overrides", {}) != eo_overrides:
            raise ValueError(
                "noise reference EO overrides differ; each EO combination needs its own noise scan"
            )
        if reference_preflight.get("window") != spec.name:
            raise ValueError("noise-reference window does not match the S-curve window")
        reference_pixels = {
            (int(item["column"]), int(item["row"]))
            for item in reference_preflight.get("pixel_selection", [])
        }
        if not set(selected_pixels).issubset(reference_pixels):
            raise ValueError("noise reference does not cover every selected pixel")
        reference_curve = reference_preflight.get(
            "threshold_dac_calibrations", {}
        ).get(spec.threshold_dac, {})
        if reference_curve.get("curve_sha256") != threshold_calibration.to_metadata()[
            "curve_sha256"
        ]:
            raise ValueError("noise-reference threshold-DAC calibration curve differs")

    if resume_experiment is not None:
        store = ExperimentStore(resume_experiment)
        if run_scurve:
            expected_ctrl_source = (
                "MGPDLab_UPO_PWM"
                if isinstance(shot_executor, UpoPwmShotExecutor)
                else "Keysight_or_custom_executor"
            )
            stored_injection = store.metadata.get(
                "test_injection_configuration", {}
            )
            if stored_injection.get("ctrl_source") != expected_ctrl_source:
                raise ValueError(
                    "resume CTRL injection source differs or was not recorded; "
                    "reuse the old noise experiment as NOISE_REFERENCE_EXPERIMENT "
                    "and start a new S-curve experiment"
                )
            if isinstance(shot_executor, UpoPwmShotExecutor) and (
                stored_injection.get("upo_pwm_count_derivation")
                != upo_pwm_count_metadata
            ):
                raise ValueError(
                    "resume UPO PWM frequency, high time or manual shutter differs"
                )
        if store.metadata.get("eo_overrides", {}) != eo_overrides:
            raise ValueError("resume EO overrides differ from the original experiment")
        if store.metadata.get("window") != spec.name:
            raise ValueError("resume experiment window does not match requested window")
        stored_pixels = {
            (int(item["column"]), int(item["row"]))
            for item in store.metadata.get("pixel_selection", [])
        }
        if stored_pixels != set(selected_pixels):
            raise ValueError("resume experiment pixel selection does not match")
        _validate_resume_inputs(
            store,
            settings=selected_settings,
            counter_key=counter_key,
            upper_non_limiting_code=upper_non_limiting_code,
            calibrations=calibrations,
            reference_calibrations=reference_calibrations,
            base_configs=base_configs,
            pixels=selected_pixels,
            bad_pixels=bad_pixels,
        )
        if normalized_gain_map is not None:
            gain_relative = store.metadata.get("gain_map", {}).get("normalized_csv")
            if not gain_relative:
                raise ValueError("resume metadata has no normalized GAIN map")
            stored_gain_frame = pd.read_csv(store.root / gain_relative)
            stored_gain = {
                (int(row["column"]), int(row["row"])): int(row["gain"])
                for _, row in stored_gain_frame.iterrows()
            }
            for coordinate, value in normalized_gain_map.items():
                if stored_gain.get(coordinate) != value:
                    raise ValueError(
                        f"resume GAIN map differs at Col={coordinate[0]} Row={coordinate[1]}"
                    )
        store.update_metadata(status="in_progress", resumed_utc=utc_now_text())
    else:
        metadata = {
            "comparator_characterization_version": FRAMEWORK_VERSION,
            "project_revision": _project_revision(Path(__file__).resolve().parents[1]),
            "window": spec.name,
            "comparator_under_test": spec.comparator,
            "upper_comparator": spec.upper_comparator,
            "threshold_dac": spec.threshold_dac,
            "upper_threshold_dac": spec.upper_threshold_dac,
            "upper_non_limiting_selection": upper_selection,
            "fixed_threshold_codes": spec.fixed_threshold_codes(upper_non_limiting_code),
            "counter_key": counter_key,
            "counter_mapping_source": counter_mapping_source,
            "pixel_selection": [
                {"column": column, "row": row} for column, row in selected_pixels
            ],
            "requested_pixel_selection": [
                {"column": column, "row": row} for column, row in requested_pixels
            ],
            "bad_pixel_mask": bad_pixel_document(bad_pixels),
            "matrix_ownership": {
                "physical_rows": list(range(32)),
                "physical_columns": list(OWNED_COLUMNS),
                "source": "pixel_matrix.OWNED_COLUMNS",
            },
            "settings": selected_settings.to_dict(),
            "run_options": {
                "run_noise_scan": run_noise_scan,
                "run_equalization": run_equalization,
                "run_scurve": run_scurve,
                "noise_reference_experiment": (
                    str(Path(noise_reference_experiment).resolve())
                    if noise_reference_experiment is not None
                    else None
                ),
                "use_reference_trim_map": use_reference_trim_map,
                "initialization_fclk_mhz": int(initialization_fclk_mhz),
            },
            "hardware_capability_notes": {
                "noise_acquisition": "MGPDLab GET_SHOT then GET_PIXEL",
                "shutter_duration_control": (
                    "externally configured in MGPDLab; no Python protocol command found"
                ),
                "exact_test_pulse_sequence": (
                    "continuous MGPDLab/UPO PWM around blocking GET_SHOT"
                    if isinstance(shot_executor, UpoPwmShotExecutor)
                    else "caller ShotExecutor or Keysight 81150A/81160A MAN burst triggered by *TRG"
                ),
                "shutter_state_readback": "not_available",
                "counter_overflow": "counter_stops_at_maximum_and_does_not_wrap",
                "counter_mapping": "AB=High, BC=Mid, CD=Low, confirmed by user",
                "lfsr_direction": "existing project direction retained",
                "asic_polarity": "recorded from OMR and never modified by this pipeline",
            },
            "test_injection_configuration": {
                "ctrl_source": (
                    "MGPDLab_UPO_PWM"
                    if isinstance(shot_executor, UpoPwmShotExecutor)
                    else "Keysight_or_custom_executor"
                ),
                "ctrl_low_v": 0.0,
                "ctrl_high_v": 3.3,
                "event_edge": "falling",
                "events_per_square_period": 1,
                "finite_burst_n_injections_argument_used": (
                    not isinstance(shot_executor, UpoPwmShotExecutor)
                ),
                "generator_load_setting_ohm": (
                    None
                    if isinstance(shot_executor, UpoPwmShotExecutor)
                    else 1_000_000.0
                ),
                "default_frequency_hz": (
                    shot_executor.settings.real_frequency_khz * 1_000.0
                    if isinstance(shot_executor, UpoPwmShotExecutor)
                    else 100_000.0
                ),
                "default_duty_cycle_percent": (
                    shot_executor.settings.high_time_ns
                    * shot_executor.settings.real_frequency_khz
                    / 10_000.0
                    if isinstance(shot_executor, UpoPwmShotExecutor)
                    else 50.0
                ),
                "pixel_fields": {
                    "PX_TST_EN": "1 for current active group, 0 otherwise",
                    "PX_SH_EN": 0,
                    "PX_BUF_NEN": 1,
                    "PX_MASK": "1 for selected good pixels; 0 for permanent bad pixels",
                    "PX_SHT": 2,
                    "PX_GAIN": "per-pixel gain_map",
                },
                "injection_capacitance_f": selected_settings.scurve.injection_capacitance_f,
                "injection_capacitance_relative_uncertainty": (
                    selected_settings.scurve.injection_capacitance_relative_uncertainty
                ),
                "upo_pwm_count_derivation": upo_pwm_count_metadata,
                "reference_pair_selection": {
                    "source": (
                        "measured_REF1_REF2_LUTs"
                        if reference_pair_selections
                        else "manual_or_external_pulse_amplitudes"
                    ),
                    "requested_voltage_steps_v": [
                        selection.requested_voltage_step_v
                        for selection in reference_pair_selections
                    ],
                    "minimum_reference_code": (
                        selected_settings.scurve.minimum_reference_code
                    ),
                    "maximum_reference_code": (
                        selected_settings.scurve.maximum_reference_code
                    ),
                    "minimum_reference_voltage_v": (
                        selected_settings.scurve.minimum_reference_voltage_v
                    ),
                    "preferred_reference_common_mode_v": (
                        selected_settings.scurve.preferred_reference_common_mode_v
                    ),
                    "reference_common_mode_step_error_slack_v": (
                        selected_settings.scurve.reference_common_mode_step_error_slack_v
                    ),
                    "maximum_reference_step_error_v": (
                        selected_settings.scurve.maximum_reference_step_error_v
                    ),
                    "physical_order": "V_REF1 > V_REF2",
                    "candidate_codes": "measured_LUT_rows_only",
                },
            },
            **dict(additional_metadata or {}),
        }
        # Safety/reproducibility fields cannot be replaced by descriptive extras.
        metadata["bad_pixel_mask"] = bad_pixel_document(bad_pixels)
        metadata["eo_overrides"] = eo_overrides
        metadata["pixel_selection"] = [
            {"column": c, "row": r} for c, r in selected_pixels
        ]
        store = ExperimentStore.create(results_root, window=spec.name, metadata=metadata)

    backend = MGPDMeasurementBackend(
        client,
        base_pixel_configs=base_configs,
        counter_key=counter_key,
        noise_settings=selected_settings.noise,
        shot_executor=shot_executor,
        status_callback=store.log_status,
        bad_pixel_map=bad_pixels,
    )
    backend.validate_pixels(selected_pixels)

    analysis_path: Path | None = None
    target_voltage: float | None = None
    final_trim_map = backend.current_trim_map(spec, selected_pixels)
    try:
        store.log_status(
            f"Запуск теста окна {spec.name}, пикселей: {len(selected_pixels)}",
            overall_percent_estimate=0.0,
        )
        store.log_status(
            f"Постоянная маска: {len(bad_pixels)} пикселей; "
            f"coarse DAC: {selected_settings.noise.coarse_start}.."
            f"{selected_settings.noise.coarse_stop}, шаг {selected_settings.noise.coarse_step}"
        )
        if isinstance(shot_executor, UpoPwmShotExecutor):
            # A previous interrupted process could have left the standalone UPO
            # PWM generator running. Establish the safe idle state before any
            # EO or pixel initialization in every new or resumed test.
            shot_executor.recover_safe_state(client)
            store.log_status("CTRL через УПО установлен в 0 перед конфигурацией ASIC")
        initialization_record = backend.initialize_standard_configuration(
            selected_pixels,
            fclk_mhz=initialization_fclk_mhz,
            eo_overrides=eo_overrides,
            progress_callback=lambda message, percent: store.log_status(
                message,
                stage_percent=percent,
                overall_percent_estimate=3.0 * percent / 100.0,
            ),
        )
        initialized_snapshot = backend.initial_configuration_snapshot()
        initialization_history = list(
            store.metadata.get("asic_initialization_history", [])
        )
        initialization_history.append(
            {
                "timestamp_utc": utc_now_text(),
                "resume_run": resume_experiment is not None,
                **initialization_record,
            }
        )
        store.update_metadata(
            asic_initialization_history=initialization_history,
            initial_asic_configuration=initialized_snapshot,
            acquisition_sequence={
                "version": 3, "upo_execution": "calling_thread_only",
                "pixel_commit": "GET_SHOT_only; cleanup_stages_PX_for_next_shot",
                "ctrl_execution": (
                    "same_UPO_thread: PWM_before_GET_SHOT, CTRL0_after_response, then_GET_PIXEL"
                    if isinstance(shot_executor, UpoPwmShotExecutor)
                    else "generator_only_worker"
                ),
                "burst_settings": (dict(vars(shot_executor.settings))
                    if isinstance(shot_executor, KeysightBurstShotExecutor) else None),
                "upo_pwm_settings": (dict(vars(shot_executor.settings))
                    if isinstance(shot_executor, UpoPwmShotExecutor) else None),
                "upo_pwm_count_derivation": upo_pwm_count_metadata,
                "shutter_open_observed": False,
            },
        )
        store.log_status(
            "Global-конфигурация установлена, все PX подготовлены в кеше УПО; "
            "единственная загрузка матрицы в чип выполняется внутри GET_SHOT",
            overall_percent_estimate=3.0,
        )

        if resume_experiment is None:
            from .storage import atomic_write_json

            atomic_write_json(store.root / "inputs" / "bad_pixels.json", bad_pixel_document(bad_pixels))
            calibration_records = _save_calibrations(store, calibrations)
            reference_calibration_records = _save_calibrations(
                store,
                reference_calibrations,
                destination_directory="inputs/reference_calibrations",
            )
            base_config_path = store.root / "inputs" / "base_pixel_config.csv"
            store.write_table(
                base_config_path,
                pd.DataFrame(backend.base_pixel_rows(selected_pixels)),
            )
            base_source_record = (
                store.copy_input_file(base_pixel_config, "inputs/base_pixel_source")
                if isinstance(base_pixel_config, (str, Path))
                else None
            )
            store.update_metadata(
                threshold_dac_calibrations=calibration_records,
                reference_dac_calibrations=reference_calibration_records,
                base_pixel_configuration={
                    "normalized_selected_pixels_csv": base_config_path.relative_to(
                        store.root
                    ).as_posix(),
                    "source_file": base_source_record,
                    "source_policy": base_config_source,
                    "built_in_mask_semantics": (
                        "PX_MASK=1 enables digital counting for selected test pixels"
                    ),
                },
            )
            if reference_pair_selections:
                pair_path = store.root / "inputs" / "reference_pair_selection.csv"
                store.write_table(
                    pair_path,
                    pd.DataFrame(
                        [
                            {
                                "requested_voltage_step_v": item.requested_voltage_step_v,
                                "actual_voltage_step_v": item.actual_voltage_step_v,
                                "voltage_step_error_v": item.voltage_step_error_v,
                                "absolute_voltage_step_error_v": item.absolute_voltage_step_error_v,
                                "ref1_code": item.ref1_code,
                                "ref1_voltage_v": item.ref1_voltage_v,
                                "ref2_code": item.ref2_code,
                                "ref2_voltage_v": item.ref2_voltage_v,
                                "reference_common_mode_v": item.reference_common_mode_v,
                                "selected_common_mode_target_v": (
                                    item.selected_common_mode_target_v
                                ),
                                "common_mode_step_error_slack_v": (
                                    item.common_mode_step_error_slack_v
                                ),
                                "minimum_achievable_step_error_v": (
                                    item.minimum_achievable_step_error_v
                                ),
                                "selection_method": item.selection_method,
                                "minimum_reference_code": item.minimum_reference_code,
                                "maximum_reference_code": item.maximum_reference_code,
                                "minimum_reference_voltage_v": item.minimum_reference_voltage_v,
                            }
                            for item in reference_pair_selections
                        ]
                    ),
                )
                store.update_metadata(
                    reference_pair_selection_csv=pair_path.relative_to(
                        store.root
                    ).as_posix()
                )
            if normalized_gain_map is not None:
                gain_path = store.root / "inputs" / "gain_map.csv"
                store.write_table(
                    gain_path,
                    pd.DataFrame(
                        [
                            {"column": column, "row": row, "gain": gain}
                            for (column, row), gain in normalized_gain_map.items()
                        ]
                    ),
                )
                group_rows = []
                for pattern in selected_settings.scurve.injection_patterns:
                    for group in build_injection_groups(selected_pixels, pattern):
                        for column, row in group.active_pixels:
                            group_rows.append(
                                {
                                    "injection_pattern": group.pattern,
                                    "injection_group_id": group.group_id,
                                    "phase_column": group.phase_column,
                                    "phase_row": group.phase_row,
                                    "tile_width": group.tile_width,
                                    "tile_height": group.tile_height,
                                    "active_pixel_count": len(group.active_pixels),
                                    "column": column,
                                    "row": row,
                                }
                            )
                groups_path = store.root / "inputs" / "injection_groups.csv"
                store.write_table(groups_path, pd.DataFrame(group_rows))
                store.update_metadata(
                    gain_map={
                        "normalized_csv": gain_path.relative_to(store.root).as_posix(),
                        "coordinate_order": "physical (column, row)",
                        "sequence_order_if_used": (
                            "gain_map[row][owned_column_index], index 0 is first "
                            "pixel_matrix.OWNED_COLUMNS value; flat form is row-major"
                        ),
                    },
                    injection_groups=groups_path.relative_to(store.root).as_posix(),
                )

        backend.configure_window(spec, upper_non_limiting_code)
        store.log_status(
            f"Окно {spec.name} настроено, начинается измерительная часть",
            overall_percent_estimate=7.0,
        )

        reference_noise_statistics = pd.DataFrame()
        reference_trim_map: dict[tuple[int, int], int] | None = None
        frozen_reference = store.metadata.get("noise_reference", {})
        if frozen_reference.get("statistics_copy"):
            reference_noise_statistics = pd.read_csv(
                store.root / frozen_reference["statistics_copy"],
                keep_default_na=False,
            )
            trim_copy = frozen_reference.get("final_trim_map_copy")
            if trim_copy:
                frame = pd.read_csv(store.root / trim_copy, keep_default_na=False)
                reference_trim_map = {
                    (int(row["column"]), int(row["row"])): int(row["trim_code"])
                    for _, row in frame.iterrows()
                }
        elif noise_reference_experiment is not None:
            reference_noise_statistics, reference_trim_map = (
                _load_and_freeze_noise_reference(
                    noise_reference_experiment,
                    destination_store=store,
                    spec_name=spec.name,
                    threshold_dac=spec.threshold_dac,
                    calibration=threshold_calibration,
                    pixels=selected_pixels,
                    require_trim_map=use_reference_trim_map,
                )
            )
        if run_scurve and use_reference_trim_map and reference_trim_map is not None:
            final_trim_map = backend.program_trim_map(
                spec, selected_pixels, reference_trim_map
            )

        if run_equalization:
            primary_analysis_progress = (
                65.0
                if selected_settings.equalization.scan_all_trim_codes
                else 40.0
            )
            candidate_completion_progress = (
                80.0
                if selected_settings.equalization.scan_all_trim_codes
                else 60.0
            )
            final_noise_progress = (
                85.0
                if run_scurve
                and selected_settings.equalization.scan_all_trim_codes
                else 72.0
                if run_scurve
                else 92.0
            )
            progress_cursor = candidate_completion_progress
            store.log_status(
                "Начинается noise scan и эквализация trim-кодов",
                overall_percent_estimate=10.0,
            )
            if run_noise_scan:
                endpoint_trims = (
                    selected_settings.equalization.trim_min,
                    selected_settings.equalization.trim_max,
                )
                for endpoint_index, trim in enumerate(endpoint_trims):
                    stage = f"trim_{trim:02d}"
                    trim_map = {coordinate: trim for coordinate in selected_pixels}
                    acquire_noise_scan(
                        backend=backend,
                        store=store,
                        calibration=threshold_calibration,
                        spec=spec,
                        pixels=selected_pixels,
                        trim_map=trim_map,
                        stage=stage,
                        upper_non_limiting_code=upper_non_limiting_code,
                        settings=selected_settings.noise,
                        overall_progress_start=10.0 + 10.0 * endpoint_index,
                        overall_progress_end=20.0 + 10.0 * endpoint_index,
                    )
                    store.log_status(
                        f"Завершен endpoint noise scan при trim={trim}",
                        overall_percent_estimate=20.0 + 10.0 * endpoint_index,
                    )
                if selected_settings.equalization.scan_all_trim_codes:
                    store.update_metadata(
                        trim_scan_strategy={
                            "mode": "complete_uniform_trim_scan",
                            "trim_codes": list(
                                range(
                                    selected_settings.equalization.trim_min,
                                    selected_settings.equalization.trim_max + 1,
                                )
                            ),
                            "endpoint_stage_names": [
                                f"trim_{selected_settings.equalization.trim_min:02d}",
                                f"trim_{selected_settings.equalization.trim_max:02d}",
                            ],
                            "intermediate_stage_prefix": "trim_full_",
                        }
                    )
                    for trim in range(
                        selected_settings.equalization.trim_min + 1,
                        selected_settings.equalization.trim_max,
                    ):
                        acquire_noise_scan(
                            backend=backend,
                            store=store,
                            calibration=threshold_calibration,
                            spec=spec,
                            pixels=selected_pixels,
                            trim_map={
                                coordinate: trim for coordinate in selected_pixels
                            },
                            stage=f"trim_full_{trim:02d}",
                            upper_non_limiting_code=upper_non_limiting_code,
                            settings=selected_settings.noise,
                            overall_progress_start=30.0 + (trim - 1),
                            overall_progress_end=30.0 + trim,
                        )
                        store.log_status(
                            f"Полный trim-sweep: завершен trim={trim}/31",
                            overall_percent_estimate=30.0 + trim,
                        )

            noise_statistics, noise_fits = _online_noise_analysis(
                store, threshold_calibration, selected_settings
            )
            store.log_status(
                "Первичный анализ noise-кривых завершен",
                overall_percent_estimate=primary_analysis_progress,
            )
            target_voltage, target_method, reachability = select_equalization_target(
                noise_fits,
                trim0_stage=f"trim_{selected_settings.equalization.trim_min:02d}",
                trim31_stage=f"trim_{selected_settings.equalization.trim_max:02d}",
                requested_target_voltage=selected_settings.equalization.target_voltage,
            )
            online_directory = store.root / "analysis" / "online_checkpoint"
            store.write_table(
                online_directory / "reachable_range_per_pixel.csv", reachability
            )
            estimate = _estimated_trim_map(reachability)
            missing_estimates = set(selected_pixels) - set(estimate)
            unresolved_baseline = {
                coordinate: final_trim_map[coordinate] for coordinate in missing_estimates
            }
            if missing_estimates:
                store.log_status(
                    f"Для {len(missing_estimates)} пикселей нет двух надежных endpoint-центров. "
                    "Их исходный trim сохраняется; пиксели не маскируются автоматически. "
                    f"Рекомендации: {online_directory}"
                )
                store.update_metadata(unresolved_equalization_pixels=[
                    {"column": c, "row": r, "preserved_initial_trim": unresolved_baseline[(c, r)]}
                    for c, r in sorted(missing_estimates)
                ])
            verification_codes = _verification_codes(
                threshold_calibration,
                target_voltage,
                selected_settings.equalization.verification_margin_codes,
            )

            radius = selected_settings.equalization.local_search_radius
            candidate_offsets = tuple(range(-radius, radius + 1))
            for candidate_index, offset in enumerate(candidate_offsets, start=1):
                acquire_noise_scan(
                    backend=backend,
                    store=store,
                    calibration=threshold_calibration,
                    spec=spec,
                    pixels=selected_pixels,
                    trim_map=_map_with_offset(estimate, offset, unchanged=unresolved_baseline),
                    stage=_candidate_stage("trim_candidate", offset),
                    upper_non_limiting_code=upper_non_limiting_code,
                    settings=selected_settings.noise,
                    scan_codes=verification_codes,
                    auto_fine=False,
                    overall_progress_start=(
                        primary_analysis_progress
                        + (candidate_completion_progress - primary_analysis_progress)
                        * (candidate_index - 1)
                        / max(len(candidate_offsets), 1)
                    ),
                    overall_progress_end=(
                        primary_analysis_progress
                        + (candidate_completion_progress - primary_analysis_progress)
                        * candidate_index
                        / max(len(candidate_offsets), 1)
                    ),
                )
                store.log_status(
                    f"Локальная проверка trim: вариант {candidate_index}/"
                    f"{len(candidate_offsets)}",
                    overall_percent_estimate=(
                        primary_analysis_progress
                        + (candidate_completion_progress - primary_analysis_progress)
                        * candidate_index
                        / max(len(candidate_offsets), 1)
                    ),
                )

            noise_statistics, noise_fits = _online_noise_analysis(
                store, threshold_calibration, selected_settings
            )
            measured = choose_measured_trim_map(noise_fits, target_voltage=target_voltage)
            measured_coordinates = {
                (int(row["column"]), int(row["row"])) for _, row in measured.iterrows()
            }
            expansion_needed = set(estimate) - measured_coordinates
            if radius > 0 and not measured.empty:
                boundary_suffixes = (f"_m{radius:02d}", f"_p{radius:02d}")
                boundary = measured[
                    measured["selected_from_stage"].astype(str).str.endswith(
                        boundary_suffixes
                    )
                ]
                expansion_needed.update(
                    (int(row["column"]), int(row["row"]))
                    for _, row in boundary.iterrows()
                )
            if (
                expansion_needed
                and selected_settings.equalization.expanded_search_radius > radius
            ):
                expanded = selected_settings.equalization.expanded_search_radius
                offsets = [
                    offset
                    for offset in range(-expanded, expanded + 1)
                    if abs(offset) > radius
                ]
                expansion_progress_end = progress_cursor + 0.20 * (
                    final_noise_progress - progress_cursor
                )
                for offset_index, offset in enumerate(offsets, start=1):
                    acquire_noise_scan(
                        backend=backend,
                        store=store,
                        calibration=threshold_calibration,
                        spec=spec,
                        pixels=selected_pixels,
                        trim_map=_map_with_offset(estimate, offset, unchanged=unresolved_baseline),
                        stage=_candidate_stage("trim_expand", offset),
                        upper_non_limiting_code=upper_non_limiting_code,
                        settings=selected_settings.noise,
                        scan_codes=verification_codes,
                        auto_fine=False,
                        overall_progress_start=(
                            progress_cursor
                            + (expansion_progress_end - progress_cursor)
                            * (offset_index - 1)
                            / max(len(offsets), 1)
                        ),
                        overall_progress_end=(
                            progress_cursor
                            + (expansion_progress_end - progress_cursor)
                            * offset_index
                            / max(len(offsets), 1)
                        ),
                    )
                progress_cursor = expansion_progress_end
                noise_statistics, noise_fits = _online_noise_analysis(
                    store, threshold_calibration, selected_settings
                )
                measured = choose_measured_trim_map(
                    noise_fits, target_voltage=target_voltage
                )

            if selected_settings.equalization.full_trim_fallback:
                measured_coordinates = {
                    (int(row["column"]), int(row["row"])) for _, row in measured.iterrows()
                }
                if set(selected_pixels) - measured_coordinates:
                    fallback_progress_end = progress_cursor + 0.70 * (
                        final_noise_progress - progress_cursor
                    )
                    for trim in range(32):
                        acquire_noise_scan(
                            backend=backend,
                            store=store,
                            calibration=threshold_calibration,
                            spec=spec,
                            pixels=selected_pixels,
                            trim_map={coordinate: trim for coordinate in selected_pixels},
                            stage=f"trim_full_{trim:02d}",
                            upper_non_limiting_code=upper_non_limiting_code,
                            settings=selected_settings.noise,
                            scan_codes=verification_codes,
                            auto_fine=False,
                            overall_progress_start=(
                                progress_cursor
                                + (fallback_progress_end - progress_cursor)
                                * trim
                                / 32.0
                            ),
                            overall_progress_end=(
                                progress_cursor
                                + (fallback_progress_end - progress_cursor)
                                * (trim + 1)
                                / 32.0
                            ),
                        )
                    progress_cursor = fallback_progress_end
                    noise_statistics, noise_fits = _online_noise_analysis(
                        store, threshold_calibration, selected_settings
                    )
                    measured = choose_measured_trim_map(
                        noise_fits, target_voltage=target_voltage
                    )

            final_trim_map, trim_table = _select_final_trim_map(
                selected_pixels, measured, estimate, unchanged=unresolved_baseline,
            )
            final_trim_path = store.root / "final_trim_map.csv"
            store.write_table(final_trim_path, trim_table)
            store.update_metadata(
                equalization_target={
                    "target_voltage_v": target_voltage,
                    "target_selection_method": target_method,
                    "reachable_range_per_pixel": (
                        online_directory / "reachable_range_per_pixel.csv"
                    ).relative_to(store.root).as_posix(),
                    "pixels_not_reaching_target": int(
                        (~reachability["target_reachable"]).sum()
                    ),
                },
                final_trim_map=final_trim_path.relative_to(store.root).as_posix(),
            )
            acquire_noise_scan(
                backend=backend,
                store=store,
                calibration=threshold_calibration,
                spec=spec,
                pixels=selected_pixels,
                trim_map=final_trim_map,
                stage="equalized_final",
                upper_non_limiting_code=upper_non_limiting_code,
                settings=selected_settings.noise,
                overall_progress_start=progress_cursor,
                overall_progress_end=final_noise_progress,
            )
            store.log_status(
                "Финальная проверка эквализированной матрицы завершена",
                overall_percent_estimate=final_noise_progress,
            )
        elif run_noise_scan:
            store.log_status(
                "Начинается отдельный baseline noise scan",
                overall_percent_estimate=10.0,
            )
            final_trim_map = backend.current_trim_map(spec, selected_pixels)
            acquire_noise_scan(
                backend=backend,
                store=store,
                calibration=threshold_calibration,
                spec=spec,
                pixels=selected_pixels,
                trim_map=final_trim_map,
                stage="baseline_noise",
                upper_non_limiting_code=upper_non_limiting_code,
                settings=selected_settings.noise,
                overall_progress_start=10.0,
                overall_progress_end=(65.0 if run_scurve else 92.0),
            )
            store.log_status(
                "Baseline noise scan завершен",
                overall_percent_estimate=(65.0 if run_scurve else 92.0),
            )

        if run_scurve:
            scurve_progress_start = (
                85.0
                if run_equalization
                and selected_settings.equalization.scan_all_trim_codes
                else 72.0
                if run_noise_scan or run_equalization
                else 10.0
            )
            total_scurve_groups = len(selected_settings.scurve.pulse_amplitudes) * sum(
                len(build_injection_groups(selected_pixels, pattern))
                for pattern in selected_settings.scurve.injection_patterns
            )
            completed_scurve_groups = 0
            ctrl_sequence_text = (
                "CTRL PWM, GET_SHOT, CTRL=0 и GET_PIXEL выполняются последовательно "
                "в вызывающем потоке УПО. "
                f"Freal={shot_executor.settings.real_frequency_khz:g} кГц, "
                f"T={selected_settings.scurve.shutter_duration_s:g} с, "
                f"Nnom={selected_settings.scurve.n_injections}; внешний "
                "n_injections игнорируется"
                if isinstance(shot_executor, UpoPwmShotExecutor)
                else "GET_SHOT полностью завершается до GET_PIXEL; фоновый поток "
                "управляет только CTRL генератора"
            )
            store.log_status(
                f"Начинается S-curve: групп инжекции {total_scurve_groups}. "
                f"{ctrl_sequence_text}",
                overall_percent_estimate=scurve_progress_start,
            )
            if store.load_raw("noise").empty:
                noise_statistics = reference_noise_statistics
            else:
                noise_statistics, _ = _online_noise_analysis(
                    store, threshold_calibration, selected_settings
                )
            if noise_statistics.empty:
                raise RuntimeError(
                    "S-curve requires noise data from this experiment or "
                    "noise_reference_experiment"
                )
            assert normalized_gain_map is not None
            assert selected_settings.scurve.shutter_duration_s is not None
            predicted_safe_codes = _safe_background_codes(
                noise_statistics,
                n_injections=selected_settings.scurve.n_injections,
                maximum_fraction=selected_settings.scurve.max_background_fraction,
                scurve_shutter_duration_s=(
                    selected_settings.scurve.shutter_duration_s
                ),
            )
            coarse_codes = _ordered_scurve_codes(
                threshold_calibration, selected_settings.scurve
            )
            if not coarse_codes:
                raise RuntimeError("automatic S-curve coarse range is empty")
            store.update_metadata(
                scurve_scan_strategy={
                    "physical_target": (
                        "positive pulse from the falling CTRL edge above baseline"
                    ),
                    "opposite_polarity_rising_edge_branch_excluded": bool(
                        selected_settings.scurve.scan_descending
                    ),
                    "scan_direction": (
                        "descending"
                        if selected_settings.scurve.scan_descending
                        else "ascending"
                    ),
                    "coarse_high_code": max(coarse_codes),
                    "coarse_low_safety_limit_code": min(coarse_codes),
                    "coarse_step": selected_settings.scurve.coarse_step,
                    "fine_step": selected_settings.scurve.fine_step,
                    "runtime_baseline_stop_enabled": (
                        selected_settings.scurve.baseline_noise_stop_enabled
                    ),
                    "baseline_stop_count_threshold": (
                        "background_count > n_injections * count_multiplier"
                    ),
                    "baseline_noise_count_multiplier": (
                        selected_settings.scurve.baseline_noise_count_multiplier
                    ),
                    "baseline_noise_pixel_fraction": (
                        selected_settings.scurve.baseline_noise_pixel_fraction
                    ),
                    "coarse_retained_noise_codes": (
                        selected_settings.scurve.coarse_baseline_noise_consecutive_codes
                    ),
                    "fine_retained_consecutive_noise_codes": (
                        selected_settings.scurve.baseline_noise_consecutive_codes
                    ),
                    "predicted_safe_code_minimum_from_noise_reference": min(
                        predicted_safe_codes
                    ),
                    "predicted_safe_code_maximum_from_noise_reference": max(
                        predicted_safe_codes
                    ),
                    "note": (
                        "noise reference is used for prediction and fit filtering; "
                        "it does not sparsify the programmable S-curve DAC grid"
                    ),
                }
            )
            store.log_status(
                "S-curve threshold scan: "
                f"DAC {coarse_codes[0]} -> {coarse_codes[-1]}, "
                f"coarse шаг {selected_settings.scurve.coarse_step}; "
                f"coarse остановится после "
                f"{selected_settings.scurve.coarse_baseline_noise_consecutive_codes} "
                "полностью сохраненной шумовой точки, fine после "
                f"{selected_settings.scurve.baseline_noise_consecutive_codes} "
                "соседних точек с шагом 1"
            )

            if run_noise_scan or run_equalization:
                change = ManualExposureChange(
                    experiment_path=store.root,
                    noise_shutter_duration_s=selected_settings.noise.shutter_duration_s,
                    scurve_shutter_duration_s=(
                        selected_settings.scurve.shutter_duration_s
                    ),
                    n_injections=selected_settings.scurve.n_injections,
                    injection_count_source=(
                        "Freal * manual UPO shutter, actual count uncertainty recorded"
                        if isinstance(shot_executor, UpoPwmShotExecutor)
                        else "finite Keysight burst cycles"
                    ),
                    burst_duration_s_at_100khz=(
                        None
                        if isinstance(shot_executor, UpoPwmShotExecutor)
                        else selected_settings.scurve.n_injections / 100_000.0
                    ),
                    upo_pwm_frequency_khz=(
                        shot_executor.settings.real_frequency_khz
                        if isinstance(shot_executor, UpoPwmShotExecutor)
                        else None
                    ),
                    upo_pwm_high_time_ns=(
                        shot_executor.settings.high_time_ns
                        if isinstance(shot_executor, UpoPwmShotExecutor)
                        else None
                    ),
                )
                store.update_metadata(
                    status="awaiting_manual_scurve_exposure_confirmation",
                    manual_exposure_change={
                        "noise_shutter_duration_s": change.noise_shutter_duration_s,
                        "scurve_shutter_duration_s": change.scurve_shutter_duration_s,
                        "n_injections": change.n_injections,
                        "injection_count_source": change.injection_count_source,
                        "burst_duration_s_at_100khz": (
                            change.burst_duration_s_at_100khz
                        ),
                        "upo_pwm_frequency_khz": change.upo_pwm_frequency_khz,
                        "upo_pwm_high_time_ns": change.upo_pwm_high_time_ns,
                    },
                )
                store.log_status(
                    "Ожидание ручной установки S-curve экспозиции в УПО и подтверждения",
                    overall_percent_estimate=scurve_progress_start,
                )
                (before_scurve or interactive_exposure_pause)(change)
                store.update_metadata(
                    status="in_progress",
                    manual_scurve_exposure_confirmed_utc=utc_now_text(),
                )
                store.log_status(
                    "Изменение экспозиции подтверждено, S-curve продолжена",
                    overall_percent_estimate=scurve_progress_start,
                )

            pixel_snapshot = backend.snapshot_pixel_configs(selected_pixels)
            if isinstance(shot_executor, KeysightBurstShotExecutor):
                shot_executor.prepare_injections(
                    selected_settings.scurve.n_injections
                )
            elif isinstance(shot_executor, UpoPwmShotExecutor):
                nominal, _ = shot_executor.nominal_injections(
                    selected_settings.scurve.shutter_duration_s,
                    counter_mode_bits=selected_settings.noise.counter_mode_bits,
                )
                if nominal != selected_settings.scurve.n_injections:
                    raise RuntimeError(
                        "UPO PWM nominal injection denominator changed after preflight"
                    )

            def acquire_groups(
                groups: Sequence[Any],
                *,
                stage: str,
                phase: str,
                codes: Sequence[int],
                amplitude: Any,
                amplitude_configuration: Mapping[str, Any],
            ) -> tuple[ScurveScanRun, ...]:
                runs: list[ScurveScanRun] = []
                for group in groups:
                    runs.append(run_scurve_points(
                        backend=backend,
                        store=store,
                        calibration=threshold_calibration,
                        spec=spec,
                        pixels=selected_pixels,
                        trim_map=final_trim_map,
                        stage=stage,
                        scan_phase=phase,
                        codes=codes,
                        pulse_amplitude=amplitude,
                        pulse_amplitude_configuration=amplitude_configuration,
                        gain_map=normalized_gain_map,
                        injection_group=group,
                        upper_non_limiting_code=upper_non_limiting_code,
                        noise_settings=selected_settings.noise,
                        scurve_settings=selected_settings.scurve,
                    ))
                return tuple(runs)

            try:
                for amplitude_index, amplitude in enumerate(
                    selected_settings.scurve.pulse_amplitudes
                ):
                    amplitude_configuration = backend.configure_test_pulse_amplitude(
                        amplitude
                    )
                    for pattern in selected_settings.scurve.injection_patterns:
                        groups = build_injection_groups(selected_pixels, pattern)
                        stage = (
                            f"pulse_amplitude_{amplitude_index:03d}_"
                            f"pattern_{pattern}"
                        )
                        coarse_runs = acquire_groups(
                            groups,
                            stage=stage,
                            phase="coarse",
                            codes=coarse_codes,
                            amplitude=amplitude,
                            amplitude_configuration=amplitude_configuration,
                        )
                        transition_brackets = _scurve_transition_brackets(
                            store,
                            noise_statistics,
                            selected_settings.scurve.max_background_fraction,
                            stage=stage,
                            scurve_settings=selected_settings.scurve,
                            analysis_settings=selected_settings.analysis,
                        )
                        current_upper = max(coarse_codes)
                        if not transition_brackets:
                            for round_index in range(
                                1, selected_settings.scurve.max_expand_rounds + 1
                            ):
                                next_upper = min(
                                    threshold_calibration.max_code,
                                    current_upper
                                    + selected_settings.scurve.expand_codes,
                                )
                                if next_upper <= current_upper:
                                    break
                                expansion = _ordered_scurve_codes(
                                    threshold_calibration,
                                    selected_settings.scurve,
                                    high_code=next_upper,
                                    low_code=current_upper + 1,
                                    step=selected_settings.scurve.coarse_step,
                                )
                                if not expansion:
                                    break
                                acquire_groups(
                                    groups,
                                    stage=stage,
                                    phase=f"expand_{round_index:02d}",
                                    codes=expansion,
                                    amplitude=amplitude,
                                    amplitude_configuration=amplitude_configuration,
                                )
                                current_upper = next_upper
                                transition_brackets = _scurve_transition_brackets(
                                    store,
                                    noise_statistics,
                                    selected_settings.scurve.max_background_fraction,
                                    stage=stage,
                                    scurve_settings=selected_settings.scurve,
                                    analysis_settings=selected_settings.analysis,
                                )
                                if transition_brackets:
                                    break

                        fine_codes, fine_diagnostics = _fine_codes_from_brackets(
                            transition_brackets,
                            threshold_calibration,
                            selected_settings.scurve,
                        )
                        fine_diagnostics.update({
                            "stage": stage,
                            "injection_pattern": pattern,
                            "coarse_baseline_stop_codes": [
                                run.baseline_stop_event.get("stop_code")
                                for run in coarse_runs
                                if run.baseline_stop_event is not None
                            ],
                        })
                        previous_fine_diagnostics = list(
                            store.metadata.get("scurve_fine_range_diagnostics", [])
                        )
                        previous_fine_diagnostics = [
                            item
                            for item in previous_fine_diagnostics
                            if not (
                                item.get("stage") == stage
                                and item.get("injection_pattern") == pattern
                            )
                        ]
                        previous_fine_diagnostics.append(fine_diagnostics)
                        store.update_metadata(
                            scurve_fine_range_diagnostics=previous_fine_diagnostics
                        )
                        if fine_codes:
                            store.log_status(
                                f"S-curve {stage}/{pattern}: fine scan, "
                                f"{len(fine_codes)} кодов с шагом "
                                f"{selected_settings.scurve.fine_step}, "
                                f"диапазонов {len(fine_diagnostics['fine_bands'])}"
                            )
                            acquire_groups(
                                groups,
                                stage=stage,
                                phase="fine",
                                codes=fine_codes,
                                amplitude=amplitude,
                                amplitude_configuration=amplitude_configuration,
                            )
                        else:
                            store.log_status(
                                f"S-curve {stage}/{pattern}: положительный переход "
                                "не ограничен соседними coarse-точками; fine scan "
                                "не выполняется"
                            )
                        completed_scurve_groups += len(groups)
                        scurve_fraction = completed_scurve_groups / max(
                            total_scurve_groups, 1
                        )
                        store.log_status(
                            f"S-curve: завершено групп {completed_scurve_groups}/"
                            f"{total_scurve_groups}, амплитуда {amplitude_index + 1}/"
                            f"{len(selected_settings.scurve.pulse_amplitudes)}, "
                            f"режим {pattern}",
                            stage_percent=100.0 * scurve_fraction,
                            overall_percent_estimate=(
                                scurve_progress_start
                                + (96.0 - scurve_progress_start) * scurve_fraction
                            ),
                        )
            finally:
                original_error = sys.exc_info()[1]
                try:
                    if isinstance(shot_executor, KeysightBurstShotExecutor):
                        shot_executor.return_to_idle()
                    elif isinstance(shot_executor, UpoPwmShotExecutor):
                        shot_executor.return_to_idle(client)
                except Exception as cleanup_error:
                    store.record_error({"scope": "generator_cleanup", "error": str(cleanup_error)})
                    if original_error is None:
                        raise
                finally:
                    if (
                        original_error is None
                        and backend.safe_for_pixel_cleanup
                        and not bool(getattr(shot_executor, "upo_command_in_flight", False))
                    ):
                        backend.restore_pixel_configs(pixel_snapshot)
                        store.update_metadata(pixel_cleanup={
                            "staged_in_upo": True, "committed_to_chip": False,
                            "note": "next GET_SHOT commits staged pixels; CTRL output disabled",
                        })
                    else:
                        store.record_error(
                            {
                                "timestamp_utc": utc_now_text(),
                                "scope": "scurve_cleanup",
                                "error": (
                                    "pixel restoration skipped after failed/cancelled acquisition; "
                                    "no cleanup commands are sent to uncertain UPO state"
                                ),
                                "error_type": "UnsafeUpoCleanupPrevented",
                            }
                        )

        store.log_status(
            "Измерения завершены, начинается итоговый анализ и построение графиков",
            overall_percent_estimate=97.0,
        )
        store.set_status("complete")
        analysis_outputs = analyze_saved_experiment(
            store.root,
            settings=selected_settings.analysis,
            target_voltage=target_voltage,
            n_injections=(
                selected_settings.scurve.n_injections if run_scurve else None
            ),
            generate_plots=True,
        )
        analysis_path = Path(analysis_outputs["analysis_directory"])
        store.log_status(
            f"Тест и анализ завершены: {analysis_path}",
            overall_percent_estimate=100.0,
        )
        return CharacterizationResult(
            experiment_path=store.root,
            analysis_path=analysis_path,
            target_voltage_v=target_voltage,
            trim_map=dict(final_trim_map),
            status="complete",
        )
    except KeyboardInterrupt:
        store.log_status("Тест остановлен пользователем")
        store.set_status("interrupted", error="user interruption")
        raise
    except BaseException as error:
        store.log_status(
            f"Тест завершился ошибкой: {type(error).__name__}: {error}"
        )
        store.record_error(
            {
                "timestamp_utc": utc_now_text(),
                "scope": "characterization_pipeline",
                "error": str(error),
                "error_type": type(error).__name__,
            }
        )
        store.set_status("failed", error=str(error))
        raise


def characterize_injection_crosstalk(
    client: MGPDClient,
    threshold_calibration_files: Mapping[
        str, str | Path | ThresholdDacCalibration
    ],
    *,
    noise_reference_experiment: str | Path,
    settings: CharacterizationSettings | None = None,
    bad_pixel_map: BadPixelMapInput = None,
    **characterization_arguments: Any,
) -> CharacterizationResult:
    """Run S-curves for all, 2x2, 4x4 and 8x8 injection patterns.

    This convenience entry point reuses a saved noise experiment, applies its
    final trim map by default, and produces density/crosstalk comparison tables.
    All other hardware arguments, including ``gain_map``, ``keysight_generator``
    and ``n_injections``, are forwarded to :func:`characterize_comparator`.
    """

    forbidden = {
        "run_noise_scan",
        "run_equalization",
        "run_scurve",
        "noise_reference_experiment",
        "settings",
    } & set(characterization_arguments)
    if forbidden:
        raise TypeError(
            "characterize_injection_crosstalk fixes these argument(s): "
            + ", ".join(sorted(forbidden))
        )
    selected_settings = (
        copy.deepcopy(settings) if settings is not None else CharacterizationSettings()
    )
    selected_settings.scurve.injection_patterns = (
        "all",
        "tile_2x2",
        "tile_4x4",
        "tile_8x8",
    )
    return characterize_comparator(
        client,
        threshold_calibration_files,
        settings=selected_settings,
        run_noise_scan=False,
        run_equalization=False,
        run_scurve=True,
        noise_reference_experiment=noise_reference_experiment,
        bad_pixel_map=bad_pixel_map,
        **characterization_arguments,
    )
