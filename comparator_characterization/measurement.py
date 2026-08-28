from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .calibration import ThresholdDacCalibration
from .hardware import MGPDMeasurementBackend, ShotExecutionResult, ShotRequest
from .injection import InjectionGroup, injection_charge_metadata
from .models import NoiseScanSettings, ScurveSettings, WindowSpec
from .storage import ExperimentStore, utc_now_text


@dataclass(frozen=True)
class NoiseScanRun:
    stage: str
    trim_map: dict[tuple[int, int], int]
    coarse_codes: tuple[int, ...]
    fine_codes: tuple[int, ...]
    fine_range_diagnostics: dict[str, Any]


def _inclusive_codes(start: int, stop: int, step: int) -> tuple[int, ...]:
    values = list(range(int(start), int(stop) + 1, int(step)))
    if not values or values[-1] != int(stop):
        values.append(int(stop))
    return tuple(dict.fromkeys(values))


def _valid_boolean(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin(("true", "1", "yes"))


def _suggest_fine_codes(
    store: ExperimentStore,
    *,
    stage: str,
    settings: NoiseScanSettings,
) -> tuple[tuple[int, ...], dict[str, Any]]:
    raw = store.load_raw("noise", stages=(stage,))
    if raw.empty:
        raise RuntimeError(f"no saved coarse data found for stage {stage}")
    raw = raw[raw["scan_phase"] == "coarse"].copy()
    raw["selected_count_numeric"] = pd.to_numeric(raw["selected_count"], errors="coerce")
    raw = raw[
        _valid_boolean(raw["measurement_valid"])
        & raw["selected_count_numeric"].notna()
    ]
    if raw.empty:
        raise RuntimeError(f"coarse stage {stage} has no valid decoded counter values")

    per_pixel = (
        raw.groupby(["threshold_dac_code", "column", "row"], as_index=False)
        ["selected_count_numeric"]
        .mean()
    )
    envelope = (
        per_pixel.groupby("threshold_dac_code")["selected_count_numeric"]
        .quantile(0.95)
        .sort_index()
    )
    codes = envelope.index.to_numpy(dtype=int)
    values = envelope.to_numpy(dtype=float)
    baseline = float(np.nanpercentile(values, 10))
    peak = float(np.nanmax(values))
    dynamic = peak - baseline

    if not np.isfinite(dynamic) or dynamic <= max(abs(peak), 1.0) * 1e-9:
        peak_code = int(codes[int(np.nanargmax(values))])
        lower = max(settings.coarse_start, peak_code - 2 * settings.coarse_step)
        upper = min(settings.coarse_stop, peak_code + 2 * settings.coarse_step)
        method = "flat_curve_fallback_around_observed_maximum"
        active_codes = [peak_code]
    else:
        activity_threshold = baseline + 0.05 * dynamic
        active_mask = values >= activity_threshold
        active_codes = codes[active_mask].tolist()
        if not active_codes:
            active_codes = [int(codes[int(np.nanargmax(values))])]
        lower = max(settings.coarse_start, min(active_codes) - settings.fine_margin_codes)
        upper = min(settings.coarse_stop, max(active_codes) + settings.fine_margin_codes)
        method = "q95_pixel_envelope_above_5_percent_dynamic_range"

    fine_codes = _inclusive_codes(lower, upper, settings.fine_step)
    return fine_codes, {
        "method": method,
        "coarse_envelope_quantile": 0.95,
        "baseline_count": baseline,
        "peak_count": peak,
        "active_coarse_codes": active_codes,
        "fine_start": lower,
        "fine_stop": upper,
        "fine_step": settings.fine_step,
    }


def _raw_rows(
    *,
    store: ExperimentStore,
    descriptor: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    spec: WindowSpec,
    calibration: ThresholdDacCalibration,
    trim_map: Mapping[tuple[int, int], int],
    threshold_code: int,
    upper_non_limiting_code: int,
    shutter_duration_s: float | None,
    shot_result: ShotExecutionResult,
    pair_id: str | None,
    injection_group: InjectionGroup | None = None,
    injection_capacitance_f: float = 10e-15,
    injection_capacitance_relative_uncertainty: float = 0.20,
    pulse_amplitude_configuration: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    lookup = calibration.lookup(threshold_code)
    timestamp = utc_now_text()
    key = store.acquisition_key(descriptor)
    acquisition_id = store.acquisition_id(key)
    active_pixels = (
        set(injection_group.active_pixels) if injection_group is not None else set()
    )
    charge = injection_charge_metadata(
        descriptor.get("pulse_amplitude"),
        capacitance_f=injection_capacitance_f,
        capacitance_relative_uncertainty=(
            injection_capacitance_relative_uncertainty
        ),
    )
    rows: list[dict[str, Any]] = []
    for sample in samples:
        column = int(sample["column"])
        row = int(sample["row"])
        rows.append(
            {
                "experiment_id": store.metadata["experiment_id"],
                "acquisition_id": acquisition_id,
                "acquisition_timestamp_utc": timestamp,
                "measurement_kind": descriptor["measurement_kind"],
                "stage": descriptor["stage"],
                "scan_phase": descriptor["scan_phase"],
                "acquisition_type": descriptor["acquisition_type"],
                "window": spec.name,
                "comparator_under_test": spec.comparator,
                "upper_comparator": spec.upper_comparator,
                "threshold_dac": spec.threshold_dac,
                "threshold_dac_code": threshold_code,
                "threshold_voltage_v": lookup.voltage,
                "threshold_voltage_exact_calibration_point": lookup.exact_calibration_point,
                "upper_threshold_dac": spec.upper_threshold_dac,
                "upper_non_limiting_dac_code": upper_non_limiting_code,
                "local_trim_field": spec.pixel_trim_field,
                "local_trim_code": int(trim_map[(column, row)]),
                "shutter_duration_s": shutter_duration_s,
                "repeat_index": descriptor["repeat_index"],
                "pair_id": pair_id or "",
                "pulse_amplitude_native": json.dumps(
                    descriptor.get("pulse_amplitude"), ensure_ascii=False
                ),
                "pulse_amplitude_configuration_json": json.dumps(
                    dict(pulse_amplitude_configuration or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "injection_pattern": (
                    injection_group.pattern if injection_group is not None else ""
                ),
                "injection_group_id": (
                    injection_group.group_id if injection_group is not None else ""
                ),
                "injection_phase_column": (
                    injection_group.phase_column if injection_group is not None else ""
                ),
                "injection_phase_row": (
                    injection_group.phase_row if injection_group is not None else ""
                ),
                "injection_tile_width": (
                    injection_group.tile_width if injection_group is not None else ""
                ),
                "injection_tile_height": (
                    injection_group.tile_height if injection_group is not None else ""
                ),
                "active_injection_pixel": (
                    (column, row) in active_pixels if injection_group is not None else False
                ),
                "active_injection_pixel_count": (
                    len(active_pixels) if injection_group is not None else 0
                ),
                "requested_injections": shot_result.requested_injections,
                "programmed_injections": shot_result.programmed_injections,
                "actual_injections": shot_result.actual_injections,
                "injections_for_analysis": shot_result.injections_for_analysis,
                "injection_count_source": shot_result.injection_count_source,
                "shot_execution_details_json": json.dumps(
                    dict(shot_result.details), ensure_ascii=False, sort_keys=True
                ),
                **charge,
                **sample,
            }
        )
    return rows


def _acquire_point(
    *,
    backend: MGPDMeasurementBackend,
    store: ExperimentStore,
    calibration: ThresholdDacCalibration,
    spec: WindowSpec,
    pixels: Sequence[tuple[int, int]],
    trim_map: Mapping[tuple[int, int], int],
    upper_non_limiting_code: int,
    descriptor: Mapping[str, Any],
    request: ShotRequest,
    pair_id: str | None = None,
    injection_group: InjectionGroup | None = None,
    injection_capacitance_f: float = 10e-15,
    injection_capacitance_relative_uncertainty: float = 0.20,
    pulse_amplitude_configuration: Mapping[str, Any] | None = None,
) -> bool:
    if store.is_complete(descriptor):
        return False
    try:
        samples, shot_result = backend.acquire(pixels, request)
        rows = _raw_rows(
            store=store,
            descriptor=descriptor,
            samples=samples,
            spec=spec,
            calibration=calibration,
            trim_map=trim_map,
            threshold_code=int(descriptor["threshold_dac_code"]),
            upper_non_limiting_code=upper_non_limiting_code,
            shutter_duration_s=request.shutter_duration_s,
            shot_result=shot_result,
            pair_id=pair_id,
            injection_group=injection_group,
            injection_capacitance_f=injection_capacitance_f,
            injection_capacitance_relative_uncertainty=(
                injection_capacitance_relative_uncertainty
            ),
            pulse_amplitude_configuration=pulse_amplitude_configuration,
        )
        store.write_acquisition(descriptor, rows)
    except BaseException as error:
        store.record_failed_acquisition(descriptor, error)
        raise
    return True


def run_noise_scan(
    *,
    backend: MGPDMeasurementBackend,
    store: ExperimentStore,
    calibration: ThresholdDacCalibration,
    spec: WindowSpec,
    pixels: Sequence[tuple[int, int]],
    trim_map: Mapping[tuple[int, int], int],
    stage: str,
    upper_non_limiting_code: int,
    settings: NoiseScanSettings,
    scan_codes: Sequence[int] | None = None,
    auto_fine: bool = True,
) -> NoiseScanRun:
    """Acquire repeated raw noise counts and save every shot incrementally."""

    settings.validate()
    programmed_trim_map = backend.program_trim_map(spec, pixels, trim_map)

    manual_codes = tuple(int(code) for code in scan_codes) if scan_codes is not None else settings.manual_codes()
    if manual_codes is not None:
        if not manual_codes:
            raise ValueError("scan code sequence is empty")
        phases = [("manual", tuple(dict.fromkeys(manual_codes)))]
        auto_fine = False
        coarse_codes: tuple[int, ...] = ()
        fine_codes = phases[0][1]
        fine_diagnostics = {
            "method": "explicit_scan_codes",
            "fine_start": min(fine_codes),
            "fine_stop": max(fine_codes),
        }
    else:
        coarse_codes = _inclusive_codes(
            settings.coarse_start,
            settings.coarse_stop,
            settings.coarse_step,
        )
        phases = [("coarse", coarse_codes)]
        fine_codes = ()
        fine_diagnostics: dict[str, Any] = {}

    completed_acquisitions = 0
    for phase, codes in phases:
        for code in codes:
            calibration.lookup(int(code))
            backend.set_threshold(spec, int(code))
            if settings.settling_time_s:
                time.sleep(settings.settling_time_s)
            for repeat in range(settings.noise_repeats):
                descriptor = {
                    "measurement_kind": "noise",
                    "stage": stage,
                    "scan_phase": phase,
                    "acquisition_type": "background",
                    "threshold_dac_code": int(code),
                    "repeat_index": repeat,
                    "pulse_amplitude": None,
                }
                request = ShotRequest(
                    measurement_kind="noise",
                    acquisition_type="background",
                    shutter_duration_s=settings.shutter_duration_s,
                    test_pulses=False,
                    configure_get_shot_omr=settings.configure_get_shot_omr,
                    counter_mode_bits=settings.counter_mode_bits,
                    mode_read=settings.mode_read,
                    crw_mode=settings.crw_mode,
                )
                if _acquire_point(
                    backend=backend,
                    store=store,
                    calibration=calibration,
                    spec=spec,
                    pixels=pixels,
                    trim_map=programmed_trim_map,
                    upper_non_limiting_code=upper_non_limiting_code,
                    descriptor=descriptor,
                    request=request,
                ):
                    completed_acquisitions += 1
                    store.update_metadata(
                        last_completed_acquisition={
                            "measurement_kind": "noise",
                            "stage": stage,
                            "scan_phase": phase,
                            "threshold_dac_code": int(code),
                            "repeat_index": repeat,
                            "timestamp_utc": utc_now_text(),
                        }
                    )

    if auto_fine:
        fine_codes, fine_diagnostics = _suggest_fine_codes(
            store,
            stage=stage,
            settings=settings,
        )
        for code in fine_codes:
            calibration.lookup(int(code))
            backend.set_threshold(spec, int(code))
            if settings.settling_time_s:
                time.sleep(settings.settling_time_s)
            for repeat in range(settings.noise_repeats):
                descriptor = {
                    "measurement_kind": "noise",
                    "stage": stage,
                    "scan_phase": "fine",
                    "acquisition_type": "background",
                    "threshold_dac_code": int(code),
                    "repeat_index": repeat,
                    "pulse_amplitude": None,
                }
                request = ShotRequest(
                    measurement_kind="noise",
                    acquisition_type="background",
                    shutter_duration_s=settings.shutter_duration_s,
                    test_pulses=False,
                    configure_get_shot_omr=settings.configure_get_shot_omr,
                    counter_mode_bits=settings.counter_mode_bits,
                    mode_read=settings.mode_read,
                    crw_mode=settings.crw_mode,
                )
                if _acquire_point(
                    backend=backend,
                    store=store,
                    calibration=calibration,
                    spec=spec,
                    pixels=pixels,
                    trim_map=programmed_trim_map,
                    upper_non_limiting_code=upper_non_limiting_code,
                    descriptor=descriptor,
                    request=request,
                ):
                    completed_acquisitions += 1
                    store.update_metadata(
                        last_completed_acquisition={
                            "measurement_kind": "noise",
                            "stage": stage,
                            "scan_phase": "fine",
                            "threshold_dac_code": int(code),
                            "repeat_index": repeat,
                            "timestamp_utc": utc_now_text(),
                        }
                    )

    store.update_metadata(
        noise_scan_progress={
            "stage": stage,
            "new_acquisitions": completed_acquisitions,
            "coarse_codes": list(coarse_codes),
            "fine_codes": list(fine_codes),
            "fine_range_diagnostics": fine_diagnostics,
        }
    )
    return NoiseScanRun(
        stage=stage,
        trim_map=dict(programmed_trim_map),
        coarse_codes=tuple(coarse_codes),
        fine_codes=tuple(fine_codes),
        fine_range_diagnostics=fine_diagnostics,
    )


def run_scurve_points(
    *,
    backend: MGPDMeasurementBackend,
    store: ExperimentStore,
    calibration: ThresholdDacCalibration,
    spec: WindowSpec,
    pixels: Sequence[tuple[int, int]],
    trim_map: Mapping[tuple[int, int], int],
    stage: str,
    scan_phase: str,
    codes: Sequence[int],
    pulse_amplitude: Any,
    pulse_amplitude_configuration: Mapping[str, Any],
    gain_map: Mapping[tuple[int, int], int],
    injection_group: InjectionGroup,
    upper_non_limiting_code: int,
    noise_settings: NoiseScanSettings,
    scurve_settings: ScurveSettings,
) -> int:
    """Acquire paired no-pulse and exact-N-pulse shots at supplied DAC codes."""

    scurve_settings.validate()
    if not scurve_settings.paired_background:
        raise NotImplementedError(
            "Only paired S-curve background acquisition is implemented because it is "
            "the robust default requested for drift control"
        )
    programmed_trim_map = backend.program_trim_map(spec, pixels, trim_map)
    pixel_config_rows = backend.program_scurve_pixel_configuration(
        pixels,
        gain_map=gain_map,
        active_injection_pixels=injection_group.active_pixels,
    )
    pixel_config_path = (
        store.root
        / "inputs"
        / "scurve_pixel_configuration"
        / f"{stage}_{injection_group.group_id}.csv"
    )
    store.write_table(pixel_config_path, pd.DataFrame(pixel_config_rows))
    completed = 0
    for code in tuple(dict.fromkeys(int(value) for value in codes)):
        calibration.lookup(code)
        backend.set_threshold(spec, code)
        if noise_settings.settling_time_s:
            time.sleep(noise_settings.settling_time_s)
        for repeat in range(scurve_settings.repeats):
            pair_seed = json.dumps(
                [
                    stage,
                    scan_phase,
                    code,
                    repeat,
                    pulse_amplitude,
                    injection_group.pattern,
                    injection_group.group_id,
                ],
                ensure_ascii=True,
                sort_keys=True,
            )
            pair_id = ExperimentStore.acquisition_id(pair_seed)
            common = {
                "measurement_kind": "scurve",
                "stage": stage,
                "scan_phase": scan_phase,
                "threshold_dac_code": code,
                "repeat_index": repeat,
                "pulse_amplitude": pulse_amplitude,
                "injection_pattern": injection_group.pattern,
                "injection_group_id": injection_group.group_id,
            }
            background_descriptor = {**common, "acquisition_type": "background"}
            background_request = ShotRequest(
                measurement_kind="scurve",
                acquisition_type="background",
                shutter_duration_s=scurve_settings.shutter_duration_s,
                test_pulses=False,
                configure_get_shot_omr=noise_settings.configure_get_shot_omr,
                counter_mode_bits=noise_settings.counter_mode_bits,
                mode_read=noise_settings.mode_read,
                crw_mode=noise_settings.crw_mode,
            )
            if _acquire_point(
                backend=backend,
                store=store,
                calibration=calibration,
                spec=spec,
                pixels=pixels,
                trim_map=programmed_trim_map,
                upper_non_limiting_code=upper_non_limiting_code,
                descriptor=background_descriptor,
                request=background_request,
                pair_id=pair_id,
                injection_group=injection_group,
                injection_capacitance_f=scurve_settings.injection_capacitance_f,
                injection_capacitance_relative_uncertainty=(
                    scurve_settings.injection_capacitance_relative_uncertainty
                ),
                pulse_amplitude_configuration=pulse_amplitude_configuration,
            ):
                completed += 1

            signal_descriptor = {**common, "acquisition_type": "signal"}
            signal_request = ShotRequest(
                measurement_kind="scurve",
                acquisition_type="signal",
                shutter_duration_s=scurve_settings.shutter_duration_s,
                test_pulses=True,
                n_injections=scurve_settings.n_injections,
                pulse_amplitude=pulse_amplitude,
                configure_get_shot_omr=noise_settings.configure_get_shot_omr,
                counter_mode_bits=noise_settings.counter_mode_bits,
                mode_read=noise_settings.mode_read,
                crw_mode=noise_settings.crw_mode,
            )
            if _acquire_point(
                backend=backend,
                store=store,
                calibration=calibration,
                spec=spec,
                pixels=pixels,
                trim_map=programmed_trim_map,
                upper_non_limiting_code=upper_non_limiting_code,
                descriptor=signal_descriptor,
                request=signal_request,
                pair_id=pair_id,
                injection_group=injection_group,
                injection_capacitance_f=scurve_settings.injection_capacitance_f,
                injection_capacitance_relative_uncertainty=(
                    scurve_settings.injection_capacitance_relative_uncertainty
                ),
                pulse_amplitude_configuration=pulse_amplitude_configuration,
            ):
                completed += 1
            store.update_metadata(
                last_completed_acquisition={
                    "measurement_kind": "scurve",
                    "stage": stage,
                    "scan_phase": scan_phase,
                    "threshold_dac_code": code,
                    "repeat_index": repeat,
                    "pulse_amplitude": pulse_amplitude,
                    "injection_pattern": injection_group.pattern,
                    "injection_group_id": injection_group.group_id,
                    "timestamp_utc": utc_now_text(),
                }
            )
    store.update_metadata(
        scurve_progress={
            "stage": stage,
            "scan_phase": scan_phase,
            "injection_pattern": injection_group.pattern,
            "injection_group_id": injection_group.group_id,
            "active_injection_pixel_count": len(injection_group.active_pixels),
            "new_acquisitions": completed,
        }
    )
    return completed
