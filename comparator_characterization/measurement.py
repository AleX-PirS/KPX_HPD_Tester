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
    injection_capacitance_f: float = 15e-15,
    injection_capacitance_relative_uncertainty: float = 0.20,
    pulse_amplitude_configuration: Mapping[str, Any] | None = None,
) -> AcquisitionOutcome:
    if store.is_complete(descriptor):
        if (
            descriptor.get("measurement_kind") == "scurve"
            and descriptor.get("acquisition_type") == "background"
        ):
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
    minimum_early_stop_code: int | None = None,
) -> tuple[int, tuple[int, ...], dict[str, Any] | None]:
    planned_codes = tuple(dict.fromkeys(int(code) for code in codes))
    saved_outcomes = _saved_noise_outcomes(
        store,
        stage=stage,
        phase=phase,
        pixels=pixels,
    )
    activity_seen = False
    empty_streak = 0
    completed = 0
    executed_codes: list[int] = []
    early_stop: dict[str, Any] | None = None
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

        executed_codes.append(code)
        point_has_activity = any(
            outcome.any_nonzero_count for outcome in repeat_outcomes
        )
        point_is_completely_empty = (
            len(repeat_outcomes) == settings.noise_repeats
            and all(
                outcome.all_pixels_valid_and_zero
                for outcome in repeat_outcomes
            )
        )
        if point_has_activity:
            activity_seen = True
            empty_streak = 0
        elif activity_seen and point_is_completely_empty:
            empty_streak += 1
        else:
            # Invalid/incomplete readout cannot be used as evidence of zero
            # activity and therefore breaks the empty-point sequence.
            empty_streak = 0

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

        stop_count = settings.stop_after_consecutive_empty_codes
        if (
            stop_count is not None
            and activity_seen
            and empty_streak >= stop_count
            and (minimum_early_stop_code is None or code >= minimum_early_stop_code)
        ):
            early_stop = {
                "timestamp_utc": utc_now_text(),
                "stage": stage,
                "scan_phase": phase,
                "reason": "consecutive_all_pixel_zero_DAC_points_after_activity",
                "consecutive_empty_codes": empty_streak,
                "configured_stop_count": stop_count,
                "last_acquired_code": code,
                "skipped_codes": list(planned_codes[code_index + 1 :]),
                "initial_empty_codes_do_not_stop_scan": True,
                "invalid_reads_never_count_as_empty": True,
            }
            store.update_metadata(last_noise_scan_early_stop=early_stop)
            store.log_status(
                f"Noise {stage}/{phase}: ранняя остановка после DAC={code}; "
                f"пропущено {len(early_stop['skipped_codes'])} НЕИЗМЕРЕННЫХ хвостовых точек",
                stage_percent=stage_percent,
                overall_percent_estimate=overall_at(stage_percent),
            )
            break

    return completed, tuple(executed_codes), early_stop


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
    """Acquire repeated raw noise counts with safe trailing-empty early stop."""

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
    backend.program_noise_pixel_configuration(pixels)
    programmed_trim_map = backend.program_trim_map(spec, pixels, trim_map)
    store.log_status(f"Noise scan {stage}: trim-карта записана")
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
    early_stops: list[dict[str, Any]] = []

    if manual_codes is not None:
        if not manual_codes:
            raise ValueError("scan code sequence is empty")
        auto_fine = False
        fine_planned = tuple(dict.fromkeys(manual_codes))
        completed, fine_codes, early_stop = _run_noise_phase(
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
        if early_stop is not None:
            early_stops.append(early_stop)
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
        completed, coarse_codes, early_stop = _run_noise_phase(
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
        if early_stop is not None:
            early_stops.append(early_stop)
        fine_diagnostics = {}

    if auto_fine:
        fine_planned, fine_diagnostics = _suggest_fine_codes(
            store,
            stage=stage,
            settings=settings,
        )
        completed, fine_codes, early_stop = _run_noise_phase(
            backend=backend,
            store=store,
            calibration=calibration,
            spec=spec,
            pixels=pixels,
            trim_map=programmed_trim_map,
            stage=stage,
            phase="fine",
            minimum_early_stop_code=fine_diagnostics.get("last_observed_peak_code"),
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
        if early_stop is not None:
            early_stops.append(early_stop)

    fine_diagnostics = dict(fine_diagnostics)
    fine_diagnostics["early_stop_events"] = early_stops
    store.update_metadata(
        noise_scan_progress={
            "stage": stage,
            "new_acquisitions": completed_acquisitions,
            "coarse_codes_planned": list(coarse_planned),
            "coarse_codes_acquired": list(coarse_codes),
            "fine_codes_planned": list(fine_planned),
            "fine_codes_acquired": list(fine_codes),
            "early_stop_enabled": (
                settings.stop_after_consecutive_empty_codes is not None
            ),
            "early_stop_events": early_stops,
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
) -> dict[str, Any]:
    counts = np.asarray(tuple(selected_counts), dtype=float)
    finite = counts[np.isfinite(counts)]
    threshold = float(
        settings.n_injections * settings.baseline_noise_count_multiplier
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
        "criterion": "background_pixel_fraction_strictly_above_scaled_N",
        "detected": detected,
        "n_injections": int(settings.n_injections),
        "count_multiplier": float(settings.baseline_noise_count_multiplier),
        "count_threshold_strictly_greater_than": threshold,
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
) -> ScurveScanRun:
    """Acquire paired S-curve points and retain a measured baseline boundary."""

    pixels = backend.active_pixels(pixels)
    active_pixels = tuple(pixel for pixel in injection_group.active_pixels if pixel not in backend.bad_pixels)
    if not active_pixels:
        raise ValueError("injection group has no unmasked pixels")
    injection_group = replace(injection_group, active_pixels=active_pixels)
    scurve_settings.validate()
    if not scurve_settings.paired_background:
        raise NotImplementedError(
            "Only paired S-curve background acquisition is implemented because it is "
            "the robust default requested for drift control"
        )
    store.log_status(
        f"S-curve {stage}/{scan_phase}/{injection_group.group_id}: "
        "программируется PX-конфигурация"
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
    planned_codes = tuple(dict.fromkeys(int(value) for value in codes))
    acquired_codes: list[int] = []
    noisy_streak: list[dict[str, Any]] = []
    baseline_stop_event: dict[str, Any] | None = None
    last_logged_bucket = -1
    store.log_status(
        f"S-curve {stage}/{scan_phase}/{injection_group.group_id}: "
        f"{len(planned_codes)} DAC-точек",
        stage_percent=0.0,
    )
    for code_index, code in enumerate(planned_codes):
        background_counts: list[int] = []
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
            background_outcome = _acquire_point(
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
            )
            if background_outcome.newly_saved:
                completed += 1
            background_counts.extend(background_outcome.selected_counts)

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
            signal_outcome = _acquire_point(
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
            )
            if signal_outcome.newly_saved:
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
        acquired_codes.append(code)
        diagnostic = _scurve_baseline_noise_diagnostic(
            background_counts,
            expected_observations=len(pixels) * scurve_settings.repeats,
            settings=scurve_settings,
        )
        diagnostic.update({
            "threshold_dac_code": int(code),
            "scan_phase": scan_phase,
        })
        if diagnostic["detected"]:
            noisy_streak.append(diagnostic)
            store.log_status(
                f"S-curve {stage}/{scan_phase}/{injection_group.group_id}: "
                f"сохранена шумовая точка {len(noisy_streak)}/"
                f"{scurve_settings.baseline_noise_consecutive_codes}, DAC={code}, "
                f"пикселей выше N*коэффициент: "
                f"{100.0 * diagnostic['observed_pixel_fraction']:.1f}%",
                stage_percent=(
                    100.0 * (code_index + 1) / max(len(planned_codes), 1)
                ),
            )
        else:
            noisy_streak.clear()

        if (
            scurve_settings.baseline_noise_stop_enabled
            and len(noisy_streak)
            >= scurve_settings.baseline_noise_consecutive_codes
        ):
            baseline_stop_event = {
                "timestamp_utc": utc_now_text(),
                "stage": stage,
                "scan_phase": scan_phase,
                "injection_pattern": injection_group.pattern,
                "injection_group_id": injection_group.group_id,
                "scan_direction": (
                    "descending" if scurve_settings.scan_descending else "ascending"
                ),
                "first_noise_code": int(noisy_streak[0]["threshold_dac_code"]),
                "stop_code": int(code),
                "retained_consecutive_noise_points": [dict(item) for item in noisy_streak],
                "retained_noise_point_count": len(noisy_streak),
                "background_and_signal_saved_before_decision": True,
                "skipped_unmeasured_codes": list(planned_codes[code_index + 1 :]),
            }
            previous_events = list(
                store.metadata.get("scurve_baseline_stop_events", [])
            )
            signature = (
                stage,
                scan_phase,
                injection_group.group_id,
                int(code),
            )
            if not any(
                (
                    item.get("stage"),
                    item.get("scan_phase"),
                    item.get("injection_group_id"),
                    int(item.get("stop_code", -1)),
                )
                == signature
                for item in previous_events
            ):
                previous_events.append(baseline_stop_event)
                store.update_metadata(
                    scurve_baseline_stop_events=previous_events
                )
            store.log_status(
                f"S-curve {stage}/{scan_phase}/{injection_group.group_id}: "
                f"после {len(noisy_streak)} последовательных шумовых точек "
                f"движение к "
                f"{'меньшим' if scurve_settings.scan_descending else 'большим'} "
                f"кодам остановлено на DAC={code}",
                stage_percent=100.0,
            )
            break
        stage_percent = 100.0 * (code_index + 1) / max(len(planned_codes), 1)
        bucket = int(stage_percent // 5)
        if bucket > last_logged_bucket or code_index + 1 == len(planned_codes):
            last_logged_bucket = bucket
            store.log_status(
                f"S-curve {stage}/{scan_phase}/{injection_group.group_id}: "
                f"DAC={code}, точка {code_index + 1}/{len(planned_codes)}, "
                f"осталось {len(planned_codes) - code_index - 1}",
                stage_percent=stage_percent,
            )
    store.update_metadata(
        scurve_progress={
            "stage": stage,
            "scan_phase": scan_phase,
            "injection_pattern": injection_group.pattern,
            "injection_group_id": injection_group.group_id,
            "active_injection_pixel_count": len(injection_group.active_pixels),
            "new_acquisitions": completed,
            "planned_codes": list(planned_codes),
            "acquired_codes": list(acquired_codes),
            "baseline_stop_event": baseline_stop_event,
        }
    )
    return ScurveScanRun(
        stage=stage,
        scan_phase=scan_phase,
        planned_codes=planned_codes,
        acquired_codes=tuple(acquired_codes),
        new_acquisitions=completed,
        baseline_stop_event=baseline_stop_event,
    )
