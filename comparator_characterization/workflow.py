from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
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
    ShotExecutor,
    load_base_pixel_configs,
)
from .injection import build_injection_groups, resolve_gain_map
from .measurement import run_noise_scan as acquire_noise_scan, run_scurve_points
from .models import (
    CharacterizationSettings,
    FRAMEWORK_VERSION,
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
    burst_duration_s_at_100khz: float


def interactive_exposure_pause(change: ManualExposureChange) -> None:
    """Wait for explicit confirmation after manual UPO exposure adjustment."""

    message = (
        "Установите в УПО экспозицию "
        f"{change.scurve_shutter_duration_s:g} с для S-кривой и нажмите Enter. "
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
) -> None:
    if _normalized_document(store.metadata.get("settings")) != _normalized_document(
        settings.to_dict()
    ):
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
    raw = store.load_raw("noise")
    statistics = calculate_noise_statistics(raw)
    fits = fit_noise_statistics(
        statistics,
        settings=settings.analysis,
        calibration=calibration,
    )
    directory = store.root / "analysis" / "online_checkpoint"
    directory.mkdir(parents=True, exist_ok=True)
    store.write_table(directory / "noise_statistics.csv", statistics)
    store.write_table(directory / "noise_fit_results.csv", fits)
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
) -> dict[tuple[int, int], int]:
    return {
        coordinate: max(0, min(31, int(trim) + int(offset)))
        for coordinate, trim in estimate.items()
    }


def _select_final_trim_map(
    pixels: Sequence[tuple[int, int]],
    measured: pd.DataFrame,
    estimate: Mapping[tuple[int, int], int],
) -> tuple[dict[tuple[int, int], int], pd.DataFrame]:
    measured_lookup = {
        (int(row["column"]), int(row["row"])): int(row["selected_trim_code"])
        for _, row in measured.iterrows()
        if pd.notna(row.get("selected_trim_code"))
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


def _subsample_codes(codes: Sequence[int], step: int) -> tuple[int, ...]:
    if not codes:
        return ()
    available = set(int(code) for code in codes)
    lower, upper = min(available), max(available)
    selected = [code for code in range(lower, upper + 1, step) if code in available]
    if upper in available and upper not in selected:
        selected.append(upper)
    return tuple(selected)


def _scurve_transition_codes(
    store: ExperimentStore,
    noise_statistics: pd.DataFrame,
    maximum_background_fraction: float,
    *,
    stage: str,
) -> tuple[int, ...]:
    raw = store.load_raw("scurve")
    if raw.empty:
        return ()
    raw = raw[raw["stage"] == stage].copy()
    if raw.empty:
        return ()
    efficiency = _paired_scurve_efficiency(
        raw,
        noise_statistics=noise_statistics,
        max_background_fraction=maximum_background_fraction,
    )
    valid = efficiency[efficiency["fit_valid"].astype(str).str.lower().isin(("true", "1"))]
    if valid.empty:
        return ()
    envelope = valid.groupby("threshold_dac_code")["efficiency"].median()
    transition = envelope[(envelope >= 0.05) & (envelope <= 0.95)]
    return tuple(int(code) for code in transition.index)


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
    base_pixel_config: str | Path | Mapping[tuple[int, int], int] | None = None,
    results_root: str | Path = "results",
    run_noise_scan: bool = True,
    run_equalization: bool = True,
    run_scurve: bool = True,
    settings: CharacterizationSettings | None = None,
    n_injections: int | None = None,
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
) -> CharacterizationResult:
    """Characterize one AB, BC or CD counting window.

    Public pixel coordinates are always ``(column, row)``. Real hardware runs
    require a saved baseline pixel configuration so the selected comparator trim
    can be modified without changing any of the other 27 pixel bits. Noise,
    equalization and S-curve stages can be selected independently. ``n_injections``
    overrides the S-curve default of 1000 for this call. When measured REF1 and
    REF2 LUT paths plus ``injection_voltage_steps_v`` are supplied, native codes
    are selected automatically with the mandatory physical order
    ``V_REF1 > V_REF2``. S-curve-only runs may freeze and reuse a previous noise
    experiment through ``noise_reference_experiment``.
    """

    selected_settings = (
        copy.deepcopy(settings) if settings is not None else CharacterizationSettings()
    )
    if n_injections is not None:
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
            minimum_reference_voltage_v=(
                selected_settings.scurve.minimum_reference_voltage_v
            ),
            preferred_reference_common_mode_v=(
                selected_settings.scurve.preferred_reference_common_mode_v
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
    selected_settings.validate()
    spec = get_window_spec(window)
    selected_pixels = resolve_pixels(pixels, OWNED_COLUMNS)
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
        raise ValueError(
            "base_pixel_config is required because SET_PIXEL_CFG has no per-pixel readback; "
            "provide a Matrix-page JSON or a (column, row) -> raw word mapping"
        )
    base_configs = load_base_pixel_configs(base_pixel_config)

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

    if resume_experiment is not None:
        store = ExperimentStore(resume_experiment)
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
            "counter_key": counter_key,
            "counter_mapping_source": counter_mapping_source,
            "pixel_selection": [
                {"column": column, "row": row} for column, row in selected_pixels
            ],
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
            },
            "hardware_capability_notes": {
                "noise_acquisition": "MGPDLab GET_SHOT then GET_PIXEL",
                "shutter_duration_control": (
                    "externally configured in MGPDLab; no Python protocol command found"
                ),
                "exact_test_pulse_sequence": (
                    "caller ShotExecutor or Keysight 81150A/81160A MAN burst triggered by *TRG"
                ),
                "shutter_state_readback": "not_available",
                "counter_overflow": "counter_stops_at_maximum_and_does_not_wrap",
                "counter_mapping": "AB=High, BC=Mid, CD=Low, confirmed by user",
                "lfsr_direction": "existing project direction retained",
                "asic_polarity": "recorded from OMR and never modified by this pipeline",
            },
            "test_injection_configuration": {
                "ctrl_low_v": 0.0,
                "ctrl_high_v": 3.3,
                "event_edge": "falling",
                "events_per_square_period": 1,
                "generator_load_setting_ohm": 1_000_000.0,
                "default_frequency_hz": 100_000.0,
                "default_duty_cycle_percent": 50.0,
                "pixel_fields": {
                    "PX_TST_EN": "1 for current active group, 0 otherwise",
                    "PX_SH_EN": 0,
                    "PX_BUF_NEN": 1,
                    "PX_MASK": 1,
                    "PX_SHT": 2,
                    "PX_GAIN": "per-pixel gain_map",
                },
                "injection_capacitance_f": selected_settings.scurve.injection_capacitance_f,
                "injection_capacitance_relative_uncertainty": (
                    selected_settings.scurve.injection_capacitance_relative_uncertainty
                ),
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
                    "minimum_reference_voltage_v": (
                        selected_settings.scurve.minimum_reference_voltage_v
                    ),
                    "preferred_reference_common_mode_v": (
                        selected_settings.scurve.preferred_reference_common_mode_v
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
        store = ExperimentStore.create(results_root, window=spec.name, metadata=metadata)

    backend = MGPDMeasurementBackend(
        client,
        base_pixel_configs=base_configs,
        counter_key=counter_key,
        noise_settings=selected_settings.noise,
        shot_executor=shot_executor,
    )
    backend.validate_pixels(selected_pixels)

    analysis_path: Path | None = None
    target_voltage: float | None = None
    final_trim_map = backend.current_trim_map(spec, selected_pixels)
    try:
        if resume_experiment is None:
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
                },
                initial_asic_configuration=backend.initial_configuration_snapshot(),
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
                                "selection_method": item.selection_method,
                                "minimum_reference_code": item.minimum_reference_code,
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
            if run_noise_scan:
                for trim in (
                    selected_settings.equalization.trim_min,
                    selected_settings.equalization.trim_max,
                ):
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
                        )

            noise_statistics, noise_fits = _online_noise_analysis(
                store, threshold_calibration, selected_settings
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
            if missing_estimates:
                first = sorted(missing_estimates)[0]
                raise RuntimeError(
                    f"trim endpoint analysis failed for {len(missing_estimates)} pixel(s); "
                    f"first is Col={first[0]} Row={first[1]}"
                )
            verification_codes = _verification_codes(
                threshold_calibration,
                target_voltage,
                selected_settings.equalization.verification_margin_codes,
            )

            radius = selected_settings.equalization.local_search_radius
            for offset in range(-radius, radius + 1):
                acquire_noise_scan(
                    backend=backend,
                    store=store,
                    calibration=threshold_calibration,
                    spec=spec,
                    pixels=selected_pixels,
                    trim_map=_map_with_offset(estimate, offset),
                    stage=_candidate_stage("trim_candidate", offset),
                    upper_non_limiting_code=upper_non_limiting_code,
                    settings=selected_settings.noise,
                    scan_codes=verification_codes,
                    auto_fine=False,
                )

            noise_statistics, noise_fits = _online_noise_analysis(
                store, threshold_calibration, selected_settings
            )
            measured = choose_measured_trim_map(noise_fits, target_voltage=target_voltage)
            measured_coordinates = {
                (int(row["column"]), int(row["row"])) for _, row in measured.iterrows()
            }
            expansion_needed = set(selected_pixels) - measured_coordinates
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
                for offset in offsets:
                    acquire_noise_scan(
                        backend=backend,
                        store=store,
                        calibration=threshold_calibration,
                        spec=spec,
                        pixels=selected_pixels,
                        trim_map=_map_with_offset(estimate, offset),
                        stage=_candidate_stage("trim_expand", offset),
                        upper_non_limiting_code=upper_non_limiting_code,
                        settings=selected_settings.noise,
                        scan_codes=verification_codes,
                        auto_fine=False,
                    )
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
                        )
                    noise_statistics, noise_fits = _online_noise_analysis(
                        store, threshold_calibration, selected_settings
                    )
                    measured = choose_measured_trim_map(
                        noise_fits, target_voltage=target_voltage
                    )

            final_trim_map, trim_table = _select_final_trim_map(
                selected_pixels, measured, estimate
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
            )
        elif run_noise_scan:
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
            )

        if run_scurve:
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
            safe_codes = _safe_background_codes(
                noise_statistics,
                n_injections=selected_settings.scurve.n_injections,
                maximum_fraction=selected_settings.scurve.max_background_fraction,
                scurve_shutter_duration_s=(
                    selected_settings.scurve.shutter_duration_s
                ),
            )
            coarse_codes = _subsample_codes(
                safe_codes, selected_settings.scurve.coarse_step
            )
            if not coarse_codes:
                raise RuntimeError("automatic S-curve coarse range is empty")

            if run_noise_scan or run_equalization:
                change = ManualExposureChange(
                    experiment_path=store.root,
                    noise_shutter_duration_s=selected_settings.noise.shutter_duration_s,
                    scurve_shutter_duration_s=(
                        selected_settings.scurve.shutter_duration_s
                    ),
                    n_injections=selected_settings.scurve.n_injections,
                    burst_duration_s_at_100khz=(
                        selected_settings.scurve.n_injections / 100_000.0
                    ),
                )
                store.update_metadata(
                    status="awaiting_manual_scurve_exposure_confirmation",
                    manual_exposure_change={
                        "noise_shutter_duration_s": change.noise_shutter_duration_s,
                        "scurve_shutter_duration_s": change.scurve_shutter_duration_s,
                        "n_injections": change.n_injections,
                        "burst_duration_s_at_100khz": (
                            change.burst_duration_s_at_100khz
                        ),
                    },
                )
                (before_scurve or interactive_exposure_pause)(change)
                store.update_metadata(
                    status="in_progress",
                    manual_scurve_exposure_confirmed_utc=utc_now_text(),
                )

            pixel_snapshot = backend.snapshot_pixel_configs(selected_pixels)
            if isinstance(shot_executor, KeysightBurstShotExecutor):
                shot_executor.prepare_injections(
                    selected_settings.scurve.n_injections
                )

            def acquire_groups(
                groups: Sequence[Any],
                *,
                stage: str,
                phase: str,
                codes: Sequence[int],
                amplitude: Any,
                amplitude_configuration: Mapping[str, Any],
            ) -> None:
                for group in groups:
                    run_scurve_points(
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
                    )

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
                        acquire_groups(
                            groups,
                            stage=stage,
                            phase="coarse",
                            codes=coarse_codes,
                            amplitude=amplitude,
                            amplitude_configuration=amplitude_configuration,
                        )
                        transition_codes = _scurve_transition_codes(
                            store,
                            noise_statistics,
                            selected_settings.scurve.max_background_fraction,
                            stage=stage,
                        )
                        if not transition_codes:
                            current_lower = min(coarse_codes)
                            current_upper = max(coarse_codes)
                            for round_index in range(
                                1, selected_settings.scurve.max_expand_rounds + 1
                            ):
                                next_lower = max(
                                    threshold_calibration.min_code,
                                    current_lower
                                    - selected_settings.scurve.expand_codes,
                                )
                                next_upper = min(
                                    threshold_calibration.max_code,
                                    current_upper
                                    + selected_settings.scurve.expand_codes,
                                )
                                expansion = tuple(
                                    code
                                    for code in range(
                                        next_lower,
                                        next_upper + 1,
                                        selected_settings.scurve.coarse_step,
                                    )
                                    if code < current_lower or code > current_upper
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
                                current_lower, current_upper = next_lower, next_upper
                                transition_codes = _scurve_transition_codes(
                                    store,
                                    noise_statistics,
                                    selected_settings.scurve.max_background_fraction,
                                    stage=stage,
                                )
                                if transition_codes:
                                    break

                        if transition_codes:
                            lower = max(
                                threshold_calibration.min_code,
                                min(transition_codes)
                                - selected_settings.scurve.fine_margin_codes,
                            )
                            upper = min(
                                threshold_calibration.max_code,
                                max(transition_codes)
                                + selected_settings.scurve.fine_margin_codes,
                            )
                            fine_codes = tuple(
                                range(
                                    lower,
                                    upper + 1,
                                    selected_settings.scurve.fine_step,
                                )
                            )
                            acquire_groups(
                                groups,
                                stage=stage,
                                phase="fine",
                                codes=fine_codes,
                                amplitude=amplitude,
                                amplitude_configuration=amplitude_configuration,
                            )
            finally:
                try:
                    if isinstance(shot_executor, KeysightBurstShotExecutor):
                        shot_executor.return_to_idle()
                finally:
                    if (
                        not isinstance(shot_executor, KeysightBurstShotExecutor)
                        or not shot_executor.upo_command_in_flight
                    ):
                        backend.restore_pixel_configs(pixel_snapshot)
                    else:
                        store.record_error(
                            {
                                "timestamp_utc": utc_now_text(),
                                "scope": "scurve_cleanup",
                                "error": (
                                    "pixel configuration restoration was skipped because "
                                    "the GET_SHOT worker still owns the UPO connection"
                                ),
                                "error_type": "UnsafeUpoCleanupPrevented",
                            }
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
        return CharacterizationResult(
            experiment_path=store.root,
            analysis_path=analysis_path,
            target_voltage_v=target_voltage,
            trim_map=dict(final_trim_map),
            status="complete",
        )
    except KeyboardInterrupt:
        store.set_status("interrupted", error="user interruption")
        raise
    except BaseException as error:
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
        **characterization_arguments,
    )
