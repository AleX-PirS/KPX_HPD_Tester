from __future__ import annotations

from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class AcquisitionOutcome:
    newly_saved: bool
    any_nonzero_count: bool
    all_pixels_valid_and_zero: bool
    selected_counts: tuple[int, ...] = ()
    selected_counts_by_pixel: tuple[tuple[int, int, int], ...] = ()


@dataclass(frozen=True)
class ScurveScanRun:
    stage: str
    scan_phase: str
    planned_codes: tuple[int, ...]
    acquired_codes: tuple[int, ...]
    new_acquisitions: int
    baseline_stop_event: dict[str, Any] | None = None


def _inclusive_codes(start: int, stop: int, step: int) -> tuple[int, ...]:
    values = list(range(int(start), int(stop) + 1, int(step)))
    if not values or values[-1] != int(stop):
        values.append(int(stop))
    return tuple(dict.fromkeys(values))


def _valid_boolean(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin(("true", "1", "yes"))


def _acquisition_outcome(
    samples: Sequence[Mapping[str, Any]], *, newly_saved: bool
) -> AcquisitionOutcome:
    def is_valid(sample: Mapping[str, Any]) -> bool:
        value = sample.get("measurement_valid", False)
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        return str(value).strip().lower() in {"true", "1", "yes"}

    valid_samples = [
        sample for sample in samples if is_valid(sample)
    ]
    selected_counts = tuple(
        int(sample["selected_count"])
        for sample in valid_samples
        if sample.get("selected_count") not in (None, "")
    )
    selected_counts_by_pixel = tuple(
        (int(sample["column"]), int(sample["row"]), int(sample["selected_count"]))
        for sample in valid_samples
        if sample.get("selected_count") not in (None, "")
    )
    return AcquisitionOutcome(
        newly_saved=newly_saved,
        any_nonzero_count=any(value > 0 for value in selected_counts),
        all_pixels_valid_and_zero=(
            bool(samples)
            and len(valid_samples) == len(samples)
            and bool(selected_counts)
            and all(value == 0 for value in selected_counts)
        ),
        selected_counts=selected_counts,
        selected_counts_by_pixel=selected_counts_by_pixel,
    )


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

    positive_pixels = per_pixel[per_pixel["selected_count_numeric"] > 0]
    if positive_pixels.empty:
        return (), {
            "method": "no_coarse_activity_skip_uninformed_fine_scan",
            "warning": "No measured coarse activity; a narrow peak between coarse points is not excluded.",
            "fine_start": None, "fine_stop": None, "fine_step": settings.fine_step,
        }

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

    # The matrix q95 envelope can hide an entire minority population. Include
    # each observed pixel maximum, but do not fill long empty gaps between peaks.
    peak_indices = positive_pixels.groupby(["column", "row"])["selected_count_numeric"].idxmax()
    pixel_peak_codes = sorted(set(positive_pixels.loc[peak_indices, "threshold_dac_code"].astype(int)))
    margin = max(settings.fine_margin_codes, settings.coarse_step)
    fine_set = set(_inclusive_codes(lower, upper, settings.fine_step))
    for code in pixel_peak_codes:
        fine_set.update(_inclusive_codes(
            max(settings.coarse_start, code - margin),
            min(settings.coarse_stop, code + margin), settings.fine_step,
        ))
    fine_codes = tuple(sorted(fine_set))
    return fine_codes, {
        "method": method + "_plus_every_observed_pixel_peak",
        "coarse_envelope_quantile": 0.95,
        "baseline_count": baseline,
        "peak_count": peak,
        "active_coarse_codes": active_codes,
        "pixel_peak_coarse_codes": pixel_peak_codes,
        "last_observed_peak_code": max(pixel_peak_codes),
        "fine_start": min(fine_codes),
        "fine_stop": max(fine_codes),
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
    injection_capacitance_f: float = 15e-15,
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
                "scurve_background_mode": descriptor.get(
                    "scurve_background_mode", "paired"
                ),
                "scurve_tile_mode": descriptor.get(
                    "scurve_tile_mode", "tile_measurement"
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
                "main_fclk_mhz": shot_result.details.get("main_fclk_mhz"),
                "measurement_fclk_mhz": shot_result.details.get(
                    "measurement_fclk_mhz"
                ),
                "main_fclk_restored_before_get_pixel": shot_result.details.get(
                    "main_fclk_restored_before_get_pixel", False
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
    injection_capacitance_f: float = 15e-15,
    injection_capacitance_relative_uncertainty: float = 0.20,
    pulse_amplitude_configuration: Mapping[str, Any] | None = None,
) -> AcquisitionOutcome:
    if store.is_complete(descriptor):
        if descriptor.get("measurement_kind") == "scurve":
            saved = store.load_complete_acquisition(descriptor)
            return _acquisition_outcome(
                saved.to_dict(orient="records"), newly_saved=False
            )
        return AcquisitionOutcome(
            newly_saved=False,
            any_nonzero_count=False,
            all_pixels_valid_and_zero=False,
        )
    try:
        samples, shot_result = backend.acquire(pixels, request)
        recovery_events = shot_result.details.get("upo_recovery_events", [])
        if recovery_events:
            store.log_status(
                f"УПО восстановлено, acquisition повторен; попыток: "
                f"{shot_result.details.get('upo_acquisition_attempt_count', 1)}"
            )
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
        store.record_error({
            "timestamp_utc": utc_now_text(), "scope": "acquisition_transport_context",
            "descriptor": dict(descriptor),
            "error_type": type(error).__name__, "error": str(error),
            "last_upo_command": getattr(backend.client, "last_command_trace", {}),
            "pixel_cleanup_safe": backend.safe_for_pixel_cleanup,
        })
        raise
    return _acquisition_outcome(samples, newly_saved=True)


def _saved_noise_outcomes(
    store: ExperimentStore,
    *,
    stage: str,
    phase: str,
    pixels: Sequence[tuple[int, int]],
) -> dict[tuple[int, int], AcquisitionOutcome]:
    """Summarize already saved repeats so resume preserves early-stop behavior."""

    raw = store.load_raw("noise", stages=(stage,))
    if raw.empty:
        return {}
    raw = raw[raw["scan_phase"].astype(str) == phase].copy()
    if raw.empty:
        return {}
    raw["selected_count_numeric"] = pd.to_numeric(
        raw["selected_count"], errors="coerce"
    )
    raw["valid_numeric"] = _valid_boolean(raw["measurement_valid"])
    expected_coordinates = set(pixels)
    outcomes: dict[tuple[int, int], AcquisitionOutcome] = {}
    for (code, repeat), frame in raw.groupby(
        ["threshold_dac_code", "repeat_index"], sort=False
    ):
        coordinates = {
            (int(row["column"]), int(row["row"]))
            for _, row in frame.iterrows()
        }
        valid = frame["valid_numeric"] & frame["selected_count_numeric"].notna()
        any_nonzero = bool(
            (frame.loc[valid, "selected_count_numeric"] > 0).any()
        )
        all_valid_and_zero = bool(
            coordinates == expected_coordinates
            and len(frame) == len(expected_coordinates)
            and valid.all()
            and (frame["selected_count_numeric"] == 0).all()
        )
        outcomes[(int(code), int(repeat))] = AcquisitionOutcome(
            newly_saved=False,
            any_nonzero_count=any_nonzero,
            all_pixels_valid_and_zero=all_valid_and_zero,
        )
    return outcomes


def _run_noise_phase(
    *,
    backend: MGPDMeasurementBackend,
    store: ExperimentStore,
    calibration: ThresholdDacCalibration,
    spec: WindowSpec,
    pixels: Sequence[tuple[int, int]],
    trim_map: Mapping[tuple[int, int], int],
    stage: str,
    phase: str,
    codes: Sequence[int],
    upper_non_limiting_code: int,
    settings: NoiseScanSettings,
    overall_progress_start: float | None = None,
    overall_progress_end: float | None = None,
) -> tuple[int, tuple[int, ...], tuple[dict[str, Any], ...]]:
    planned_codes = tuple(dict.fromkeys(int(code) for code in codes))
    saved_outcomes = _saved_noise_outcomes(
        store,
        stage=stage,
        phase=phase,
        pixels=pixels,
    )
    completed = 0
    executed_codes: list[int] = []
    repeat_shortcuts: list[dict[str, Any]] = []
    last_logged_bucket = -1

    def overall_at(stage_percent: float) -> float | None:
        if overall_progress_start is None or overall_progress_end is None:
            return None
        fraction = min(100.0, max(0.0, stage_percent)) / 100.0
        return overall_progress_start + (
            overall_progress_end - overall_progress_start
        ) * fraction

    store.log_status(
        f"Noise scan {stage}/{phase}: {len(planned_codes)} DAC-точек, "
        f"{settings.noise_repeats} повторов на точку",
        stage_percent=0.0,
        overall_percent_estimate=overall_at(0.0),
    )

    for code_index, code in enumerate(planned_codes):
        calibration.lookup(code)
        backend.set_threshold(spec, code)
        if settings.settling_time_s:
            time.sleep(settings.settling_time_s)
        repeat_outcomes: list[AcquisitionOutcome] = []
        consecutive_empty_repeats = 0
        for repeat in range(settings.noise_repeats):
            descriptor = {
                "measurement_kind": "noise",
                "stage": stage,
                "scan_phase": phase,
                "acquisition_type": "background",
                "threshold_dac_code": code,
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
            if store.is_complete(descriptor):
                outcome = saved_outcomes.get(
                    (code, repeat),
                    AcquisitionOutcome(False, False, False),
                )
            else:
                outcome = _acquire_point(
                    backend=backend,
                    store=store,
                    calibration=calibration,
                    spec=spec,
                    pixels=pixels,
                    trim_map=trim_map,
                    upper_non_limiting_code=upper_non_limiting_code,
                    descriptor=descriptor,
                    request=request,
                )
            repeat_outcomes.append(outcome)
            if outcome.newly_saved:
                completed += 1
                store.update_metadata(
                    last_completed_acquisition={
                        "measurement_kind": "noise",
                        "stage": stage,
                        "scan_phase": phase,
                        "threshold_dac_code": code,
                        "repeat_index": repeat,
                        "timestamp_utc": utc_now_text(),
                    }
                )

            if outcome.all_pixels_valid_and_zero:
                consecutive_empty_repeats += 1
            else:
                # Invalid/incomplete readout and any non-zero count both reset
                # the sequence. Neither can be used as proof of an empty matrix.
                consecutive_empty_repeats = 0
            shortcut_count = settings.empty_matrix_repeats_to_skip_remaining
            if (
                shortcut_count is not None
                and consecutive_empty_repeats >= shortcut_count
                and repeat + 1 < settings.noise_repeats
            ):
                event = {
                    "timestamp_utc": utc_now_text(),
                    "stage": stage,
                    "scan_phase": phase,
                    "threshold_dac_code": int(code),
                    "reason": "consecutive_fully_valid_all_pixel_zero_repeats",
                    "configured_empty_repeat_count": int(shortcut_count),
                    "last_completed_repeat_index": int(repeat),
                    "completed_repeat_indices": list(range(repeat + 1)),
                    "skipped_repeat_indices": list(
                        range(repeat + 1, settings.noise_repeats)
                    ),
                    "dac_code_was_not_skipped": True,
                    "remaining_dac_codes_will_still_be_measured": True,
                    "invalid_reads_never_count_as_empty": True,
                }
                repeat_shortcuts.append(event)
                store.log_status(
                    f"Noise {stage}/{phase}: DAC={code}, матрица полностью "
                    f"пуста {shortcut_count} раза подряд; пропущено "
                    f"{len(event['skipped_repeat_indices'])} повторов только "
                    "этой точки"
                )
                break

        executed_codes.append(code)

        stage_percent = 100.0 * (code_index + 1) / max(len(planned_codes), 1)
        bucket = int(stage_percent // 5)
        if bucket > last_logged_bucket or code_index + 1 == len(planned_codes):
            last_logged_bucket = bucket
            store.log_status(
                f"Noise {stage}/{phase}: DAC={code}, точка "
                f"{code_index + 1}/{len(planned_codes)}, осталось "
                f"{len(planned_codes) - code_index - 1}",
                stage_percent=stage_percent,
                overall_percent_estimate=overall_at(stage_percent),
            )

    return completed, tuple(executed_codes), tuple(repeat_shortcuts)


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
    overall_progress_start: float | None = None,
    overall_progress_end: float | None = None,
) -> NoiseScanRun:
    """Acquire every requested DAC code and shorten only proven-empty repeats."""

    pixels = backend.active_pixels(pixels)
    settings.validate()
    if (overall_progress_start is None) != (overall_progress_end is None):
        raise ValueError(
            "overall_progress_start and overall_progress_end must be supplied together"
        )
    if (
        overall_progress_start is not None
        and overall_progress_end is not None
        and overall_progress_start > overall_progress_end
    ):
        raise ValueError("overall progress range must be non-decreasing")
    store.log_status(f"Noise scan {stage}: программируется trim-карта")
    # Build the final noise PX state in UPO memory first, then perform one
    # explicit full-matrix commit after the trim map has been staged.
    backend.program_noise_pixel_configuration(pixels, commit=False)
    programmed_trim_map = backend.program_trim_map(spec, pixels, trim_map)
    store.log_status(
        f"Noise scan {stage}: trim-карта явно загружена в ASIC через WRITE_TO_CHIP"
    )
    manual_codes = (
        tuple(int(code) for code in scan_codes)
        if scan_codes is not None
        else settings.manual_codes()
    )
    completed_acquisitions = 0
    coarse_planned: tuple[int, ...] = ()
    coarse_codes: tuple[int, ...] = ()
    fine_planned: tuple[int, ...] = ()
    fine_codes: tuple[int, ...] = ()
    repeat_shortcuts: list[dict[str, Any]] = []

    if manual_codes is not None:
        if not manual_codes:
            raise ValueError("scan code sequence is empty")
        auto_fine = False
        fine_planned = tuple(dict.fromkeys(manual_codes))
        completed, fine_codes, shortcuts = _run_noise_phase(
            backend=backend,
            store=store,
            calibration=calibration,
            spec=spec,
            pixels=pixels,
            trim_map=programmed_trim_map,
            stage=stage,
            phase="manual",
            codes=fine_planned,
            upper_non_limiting_code=upper_non_limiting_code,
            settings=settings,
            overall_progress_start=overall_progress_start,
            overall_progress_end=overall_progress_end,
        )
        completed_acquisitions += completed
        repeat_shortcuts.extend(shortcuts)
        fine_diagnostics: dict[str, Any] = {
            "method": "explicit_scan_codes",
            "fine_start": min(fine_planned),
            "fine_stop": max(fine_planned),
        }
    else:
        coarse_planned = _inclusive_codes(
            settings.coarse_start,
            settings.coarse_stop,
            settings.coarse_step,
        )
        coarse_progress_end = (
            (overall_progress_start + overall_progress_end) / 2.0
            if auto_fine
            and overall_progress_start is not None
            and overall_progress_end is not None
            else overall_progress_end
        )
        completed, coarse_codes, shortcuts = _run_noise_phase(
            backend=backend,
            store=store,
            calibration=calibration,
            spec=spec,
            pixels=pixels,
            trim_map=programmed_trim_map,
            stage=stage,
            phase="coarse",
            codes=coarse_planned,
            upper_non_limiting_code=upper_non_limiting_code,
            settings=settings,
            overall_progress_start=overall_progress_start,
            overall_progress_end=coarse_progress_end,
        )
        completed_acquisitions += completed
        repeat_shortcuts.extend(shortcuts)
        fine_diagnostics = {}

    if auto_fine:
        fine_planned, fine_diagnostics = _suggest_fine_codes(
            store,
            stage=stage,
            settings=settings,
        )
        completed, fine_codes, shortcuts = _run_noise_phase(
            backend=backend,
            store=store,
            calibration=calibration,
            spec=spec,
            pixels=pixels,
            trim_map=programmed_trim_map,
            stage=stage,
            phase="fine",
            codes=fine_planned,
            upper_non_limiting_code=upper_non_limiting_code,
            settings=settings,
            overall_progress_start=(
                (overall_progress_start + overall_progress_end) / 2.0
                if overall_progress_start is not None
                and overall_progress_end is not None
                else None
            ),
            overall_progress_end=overall_progress_end,
        )
        completed_acquisitions += completed
        repeat_shortcuts.extend(shortcuts)

    fine_diagnostics = dict(fine_diagnostics)
    fine_diagnostics["repeat_shortcut_events"] = repeat_shortcuts
    store.update_metadata(
        noise_scan_progress={
            "stage": stage,
            "new_acquisitions": completed_acquisitions,
            "coarse_codes_planned": list(coarse_planned),
            "coarse_codes_acquired": list(coarse_codes),
            "fine_codes_planned": list(fine_planned),
            "fine_codes_acquired": list(fine_codes),
            "all_planned_dac_codes_visited": (
                tuple(coarse_planned) == tuple(coarse_codes)
                and tuple(fine_planned) == tuple(fine_codes)
            ),
            "empty_repeat_shortcut_enabled": (
                settings.empty_matrix_repeats_to_skip_remaining is not None
            ),
            "empty_repeat_shortcut_events": repeat_shortcuts,
            "empty_repeat_shortcut_event_count": len(repeat_shortcuts),
            "legacy_dac_tail_early_stop_disabled": True,
            "fine_range_diagnostics": fine_diagnostics,
        }
    )
    return NoiseScanRun(
        stage=stage,
        trim_map=dict(programmed_trim_map),
        coarse_codes=coarse_codes,
        fine_codes=fine_codes,
        fine_range_diagnostics=fine_diagnostics,
    )


def _scurve_baseline_noise_diagnostic(
    selected_counts: Sequence[int],
    *,
    expected_observations: int,
    settings: ScurveSettings,
    dynamic_count_threshold: float | None = None,
    count_source: str = "background",
) -> dict[str, Any]:
    counts = np.asarray(tuple(selected_counts), dtype=float)
    finite = counts[np.isfinite(counts)]
    nominal_threshold = float(
        settings.n_injections * settings.baseline_noise_count_multiplier
    )
    threshold = max(
        nominal_threshold,
        float(dynamic_count_threshold or nominal_threshold),
    )
    coverage = (
        float(len(finite) / expected_observations)
        if expected_observations > 0
        else 0.0
    )
    above_fraction = float(np.mean(finite > threshold)) if len(finite) else 0.0
    enough_valid_pixels = coverage >= 0.80
    detected = bool(
        settings.baseline_noise_stop_enabled
        and enough_valid_pixels
        and above_fraction >= settings.baseline_noise_pixel_fraction
    )
    return {
        "criterion": "pixel_fraction_strictly_above_dynamic_scaled_N",
        "count_source": count_source,
        "detected": detected,
        "n_injections": int(settings.n_injections),
        "count_multiplier": float(settings.baseline_noise_count_multiplier),
        "count_threshold_strictly_greater_than": threshold,
        "nominal_count_threshold": nominal_threshold,
        "required_pixel_fraction": float(settings.baseline_noise_pixel_fraction),
        "observed_pixel_fraction": above_fraction,
        "valid_observation_count": int(len(finite)),
        "expected_observation_count": int(expected_observations),
        "valid_observation_coverage": coverage,
        "minimum_valid_observation_coverage": 0.80,
        "background_count_median": (
            float(np.median(finite)) if len(finite) else None
        ),
        "background_count_q90": (
            float(np.quantile(finite, 0.90)) if len(finite) else None
        ),
        "background_count_q95": (
            float(np.quantile(finite, 0.95)) if len(finite) else None
        ),
        "background_count_maximum": (
            float(np.max(finite)) if len(finite) else None
        ),
    }


def _outcome_counts_for_pixels(
    outcome: AcquisitionOutcome,
    pixels: set[tuple[int, int]],
) -> tuple[int, ...]:
    return tuple(
        count
        for column, row, count in outcome.selected_counts_by_pixel
        if (column, row) in pixels
    )


def _scurve_signal_state(
    selected_counts: Sequence[int],
    settings: ScurveSettings,
) -> dict[str, Any]:
    counts = np.asarray(tuple(selected_counts), dtype=float)
    finite = counts[np.isfinite(counts)]
    if not len(finite):
        return {
            "transition_detected": False,
            "signal_detected": False,
            "median_count": None,
            "transition_pixel_fraction": 0.0,
            "signal_pixel_fraction": 0.0,
        }
    nominal = float(settings.n_injections)
    transition = (
        (finite >= nominal * settings.transition_repeat_low_fraction)
        & (finite <= nominal * settings.transition_repeat_high_fraction)
    )
    signal_threshold = max(1.0, nominal * settings.signal_detection_fraction_of_n)
    signal = finite >= signal_threshold
    transition_fraction = float(np.mean(transition))
    signal_fraction = float(np.mean(signal))
    return {
        "transition_detected": bool(
            transition_fraction >= settings.signal_detection_pixel_fraction
        ),
        "signal_detected": bool(
            signal_fraction >= settings.signal_detection_pixel_fraction
        ),
        "median_count": float(np.median(finite)),
        "q90_count": float(np.quantile(finite, 0.90)),
        "transition_pixel_fraction": transition_fraction,
        "signal_pixel_fraction": signal_fraction,
        "signal_threshold": signal_threshold,
    }


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
    measurement_fclk_mhz: int | None = None,
    noise_statistics: pd.DataFrame | None = None,
) -> ScurveScanRun:
    from .adaptive_scurve import acquire_adaptive_scurve
    return acquire_adaptive_scurve(
        backend=backend,
        store=store,
        calibration=calibration,
        spec=spec,
        pixels=pixels,
        trim_map=trim_map,
        stage=stage,
        scan_phase=scan_phase,
        codes=codes,
        pulse_amplitude=pulse_amplitude,
        pulse_amplitude_configuration=pulse_amplitude_configuration,
        gain_map=gain_map,
        injection_group=injection_group,
        upper_non_limiting_code=upper_non_limiting_code,
        noise_settings=noise_settings,
        scurve_settings=scurve_settings,
        measurement_fclk_mhz=measurement_fclk_mhz,
        noise_statistics=noise_statistics,
    )
