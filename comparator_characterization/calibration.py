from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


_CODE_HINTS = ("daccode", "code", "dac")
_VOLTAGE_HINTS = (
    "measuredvaluev",
    "measuredvaluemv",
    "measuredvalueuv",
    "voltagev",
    "voltagemv",
    "voltageuv",
    "voltage",
    "valuev",
    "valuemv",
    "valueuv",
    "volt",
)


def _normalized_column(name: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def _detect_column(columns: Iterable[object], hints: tuple[str, ...], label: str) -> object:
    normalized = [(column, _normalized_column(column)) for column in columns]
    for hint in hints:
        matches = [column for column, name in normalized if hint in name]
        if len(matches) == 1:
            return matches[0]
    raise ValueError(
        f"Cannot identify the {label} column. Available columns: "
        + ", ".join(map(str, columns))
    )


@dataclass(frozen=True)
class ThresholdLookup:
    code: int
    voltage: float
    exact_calibration_point: bool


class ThresholdDacCalibration:
    """Measured threshold-DAC transfer characteristic.

    Exact measured values are returned unchanged. Missing integer codes inside
    the measured range are evaluated by piecewise-linear interpolation between
    adjacent measured points. Extrapolation is never performed.
    """

    def __init__(
        self,
        dac_name: str,
        codes: Iterable[int],
        voltages: Iterable[float],
        *,
        source_path: str | Path | None = None,
        code_column: str | None = None,
        voltage_column: str | None = None,
    ):
        self.dac_name = str(dac_name).strip().upper()
        self.source_path = Path(source_path).resolve() if source_path is not None else None
        self.code_column = code_column
        self.voltage_column = voltage_column

        code_array = np.asarray(list(codes), dtype=float)
        voltage_array = np.asarray(list(voltages), dtype=float)
        if code_array.ndim != 1 or voltage_array.ndim != 1:
            raise ValueError("calibration data must be one-dimensional")
        if len(code_array) != len(voltage_array) or len(code_array) < 2:
            raise ValueError("calibration requires at least two code-voltage points")
        if not np.all(np.isfinite(code_array)) or not np.all(np.isfinite(voltage_array)):
            raise ValueError("calibration contains NaN or infinite values")
        rounded = np.rint(code_array).astype(int)
        if not np.allclose(code_array, rounded, rtol=0, atol=1e-9):
            raise ValueError("DAC codes in calibration must be integers")
        if np.any((rounded < 0) | (rounded > 1023)):
            raise ValueError("DAC calibration codes must be in 0..1023")

        order = np.argsort(rounded)
        rounded = rounded[order]
        voltage_array = voltage_array[order]
        duplicates = rounded[pd.Series(rounded).duplicated(keep=False).to_numpy()]
        if len(duplicates):
            duplicate_text = ", ".join(map(str, sorted(set(duplicates.tolist()))[:10]))
            raise ValueError(f"duplicate DAC codes in calibration: {duplicate_text}")

        self.codes = rounded
        self.voltages = voltage_array
        self._exact = {int(code): float(voltage) for code, voltage in zip(rounded, voltage_array)}
        self.direction = self._determine_direction()

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        dac_name: str,
        *,
        code_column: str | None = None,
        voltage_column: str | None = None,
    ) -> "ThresholdDacCalibration":
        source = Path(path)
        frame = pd.read_csv(source)
        if frame.empty:
            raise ValueError(f"threshold calibration is empty: {source}")
        selected_code = code_column or _detect_column(frame.columns, _CODE_HINTS, "DAC code")
        selected_voltage = voltage_column or _detect_column(
            frame.columns, _VOLTAGE_HINTS, "threshold voltage"
        )
        if selected_code == selected_voltage:
            raise ValueError("DAC code and voltage columns resolve to the same CSV column")
        try:
            codes = pd.to_numeric(frame[selected_code], errors="raise")
            voltages = pd.to_numeric(frame[selected_voltage], errors="raise")
        except Exception as error:
            raise ValueError(f"non-numeric threshold calibration data in {source}: {error}") from error
        return cls(
            dac_name=dac_name,
            codes=codes,
            voltages=voltages,
            source_path=source,
            code_column=str(selected_code),
            voltage_column=str(selected_voltage),
        )

    def _determine_direction(self) -> str:
        differences = np.diff(self.voltages)
        tolerance = max(float(np.ptp(self.voltages)) * 1e-9, 1e-12)
        significant = differences[np.abs(differences) > tolerance]
        if not len(significant):
            return "flat"
        positive_fraction = float(np.mean(significant > 0))
        negative_fraction = float(np.mean(significant < 0))
        if positive_fraction >= 0.9:
            return "increasing"
        if negative_fraction >= 0.9:
            return "decreasing"
        return "non_monotonic"

    @property
    def min_code(self) -> int:
        return int(self.codes[0])

    @property
    def max_code(self) -> int:
        return int(self.codes[-1])

    def lookup(self, code: int) -> ThresholdLookup:
        if not isinstance(code, int) or isinstance(code, bool):
            raise TypeError("DAC code must be int")
        if not 0 <= code <= 1023:
            raise ValueError("DAC code must be in 0..1023")
        exact = self._exact.get(code)
        if exact is not None:
            return ThresholdLookup(code=code, voltage=exact, exact_calibration_point=True)
        if not self.min_code <= code <= self.max_code:
            raise ValueError(
                f"DAC code {code} is outside calibrated range "
                f"{self.min_code}..{self.max_code} for {self.dac_name}; extrapolation is disabled"
            )
        voltage = float(np.interp(code, self.codes, self.voltages))
        return ThresholdLookup(code=code, voltage=voltage, exact_calibration_point=False)

    def dac_code_to_voltage(self, code: int) -> float:
        return self.lookup(code).voltage

    def voltage_to_nearest_dac_code(self, voltage: float) -> int:
        voltage = float(voltage)
        if not np.isfinite(voltage):
            raise ValueError("voltage must be finite")
        candidates = np.arange(self.min_code, self.max_code + 1, dtype=int)
        candidate_voltages = np.interp(candidates, self.codes, self.voltages)
        return int(candidates[int(np.argmin(np.abs(candidate_voltages - voltage)))])

    def local_volts_per_code(self, code: int) -> float:
        code = int(code)
        lower = max(self.min_code, code - 1)
        upper = min(self.max_code, code + 1)
        if lower == upper:
            return float("nan")
        return (
            self.dac_code_to_voltage(upper) - self.dac_code_to_voltage(lower)
        ) / (upper - lower)

    def select_upper_non_limiting_endpoint(self) -> tuple[int, dict[str, object]]:
        """Select code 0 or 1023 from measured endpoint voltages.

        For the standard A>B>C>D physical threshold ordering, the upper
        comparator is non-limiting at the endpoint with the greater measured
        threshold voltage. Both physical endpoint measurements are required so
        code polarity is never inferred from a register value alone.
        """

        missing = [code for code in (0, 1023) if code not in self._exact]
        if missing:
            raise ValueError(
                f"{self.dac_name} calibration must contain measured endpoint(s) "
                f"{missing} to select a non-limiting upper threshold automatically; "
                "supply upper_non_limiting_code explicitly after physical verification"
            )
        endpoint_voltages = {code: self._exact[code] for code in (0, 1023)}
        selected = max(endpoint_voltages, key=endpoint_voltages.get)
        return selected, {
            "selection_method": "measured_endpoint_with_maximum_threshold_voltage",
            "calibration_direction": self.direction,
            "endpoint_voltages_v": endpoint_voltages,
            "selected_code": selected,
            "selected_voltage_v": endpoint_voltages[selected],
        }

    def to_metadata(self) -> dict[str, object]:
        digest = hashlib.sha256()
        digest.update(self.codes.astype("<i8", copy=False).tobytes())
        digest.update(self.voltages.astype("<f8", copy=False).tobytes())
        return {
            "calibration_kind": "threshold_dac",
            "dac_name": self.dac_name,
            "source_path": str(self.source_path) if self.source_path is not None else None,
            "code_column": self.code_column,
            "voltage_column": self.voltage_column,
            "measured_points": int(len(self.codes)),
            "minimum_measured_code": self.min_code,
            "maximum_measured_code": self.max_code,
            "direction": self.direction,
            "curve_sha256": digest.hexdigest(),
            "interpolation": "piecewise_linear_between_measured_points",
            "extrapolation": False,
        }


def _reference_voltage_unit(
    requested_unit: str,
    voltage_column: object,
) -> tuple[str, float]:
    unit = str(requested_unit).strip().lower().replace("µ", "u").replace("μ", "u")
    if unit == "auto":
        normalized = _normalized_column(voltage_column)
        if normalized.endswith("mv") or "millivolt" in normalized:
            unit = "mv"
        elif normalized.endswith("uv") or "microvolt" in normalized:
            unit = "uv"
        else:
            # A unitless or generic voltage heading is interpreted as volts.
            # Magnitude-based guessing would silently corrupt a physical LUT.
            unit = "v"
    scales = {"v": 1.0, "mv": 1e-3, "uv": 1e-6}
    if unit not in scales:
        raise ValueError("reference calibration voltage_unit must be auto, V, mV or uV")
    labels = {"v": "V", "mv": "mV", "uv": "uV"}
    return labels[unit], scales[unit]


class ReferenceDacCalibration(ThresholdDacCalibration):
    """Measured REF1 or REF2 transfer characteristic normalized to volts.

    Pair selection uses only measured rows from this table. It never chooses an
    interpolated or extrapolated reference-DAC code.
    """

    input_voltage_unit: str = "V"

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        dac_name: str,
        *,
        code_column: str | None = None,
        voltage_column: str | None = None,
        voltage_unit: str = "auto",
    ) -> "ReferenceDacCalibration":
        source = Path(path)
        frame = pd.read_csv(source)
        if frame.empty:
            raise ValueError(f"reference calibration is empty: {source}")
        selected_code = code_column or _detect_column(frame.columns, _CODE_HINTS, "DAC code")
        selected_voltage = voltage_column or _detect_column(
            frame.columns, _VOLTAGE_HINTS, "reference voltage"
        )
        if selected_code == selected_voltage:
            raise ValueError("DAC code and voltage columns resolve to the same CSV column")
        try:
            codes = pd.to_numeric(frame[selected_code], errors="raise")
            input_voltages = pd.to_numeric(frame[selected_voltage], errors="raise")
        except Exception as error:
            raise ValueError(f"non-numeric reference calibration data in {source}: {error}") from error
        selected_unit, scale = _reference_voltage_unit(voltage_unit, selected_voltage)
        calibration = cls(
            dac_name=dac_name,
            codes=codes,
            voltages=input_voltages * scale,
            source_path=source,
            code_column=str(selected_code),
            voltage_column=str(selected_voltage),
        )
        calibration.input_voltage_unit = selected_unit
        return calibration

    def to_metadata(self) -> dict[str, object]:
        metadata = super().to_metadata()
        metadata.update(
            {
                "calibration_kind": "reference_dac",
                "input_voltage_unit": self.input_voltage_unit,
                "normalized_voltage_unit": "V",
                "pair_selection_points": "measured_rows_only",
            }
        )
        return metadata


@dataclass(frozen=True)
class ReferencePairSelection:
    requested_voltage_step_v: float
    actual_voltage_step_v: float
    voltage_step_error_v: float
    absolute_voltage_step_error_v: float
    ref1_code: int
    ref2_code: int
    ref1_voltage_v: float
    ref2_voltage_v: float
    reference_common_mode_v: float
    selection_method: str
    minimum_reference_code: int
    minimum_reference_voltage_v: float | None
    maximum_reference_code: int = 1023
    selected_common_mode_target_v: float | None = None
    common_mode_step_error_slack_v: float = 0.0
    minimum_achievable_step_error_v: float = 0.0
    fixed_ref1_voltage_v: float | None = None
    ref1_shared_across_amplitudes: bool = False

    def to_pulse_amplitude(self) -> dict[str, Any]:
        return {
            "DAC_TST_REF1": self.ref1_code,
            "DAC_TST_REF2": self.ref2_code,
            "requested_voltage_step_v": self.requested_voltage_step_v,
            "voltage_step_v": self.actual_voltage_step_v,
            "voltage_step_error_v": self.voltage_step_error_v,
            "absolute_voltage_step_error_v": self.absolute_voltage_step_error_v,
            "ref1_voltage_v": self.ref1_voltage_v,
            "ref2_voltage_v": self.ref2_voltage_v,
            "reference_common_mode_v": self.reference_common_mode_v,
            "selected_common_mode_target_v": self.selected_common_mode_target_v,
            "common_mode_step_error_slack_v": self.common_mode_step_error_slack_v,
            "minimum_achievable_step_error_v": self.minimum_achievable_step_error_v,
            "fixed_ref1_voltage_v": self.fixed_ref1_voltage_v,
            "ref1_shared_across_amplitudes": self.ref1_shared_across_amplitudes,
            "ref1_voltage_above_ref2": True,
            "reference_pair_selection_method": self.selection_method,
            "minimum_reference_code": self.minimum_reference_code,
            "maximum_reference_code": self.maximum_reference_code,
            "minimum_reference_voltage_v": self.minimum_reference_voltage_v,
        }


def load_reference_dac_calibrations(
    files: Mapping[str, str | Path | ReferenceDacCalibration],
    *,
    voltage_unit: str = "auto",
) -> dict[str, ReferenceDacCalibration]:
    """Load the independent measured REF1 and REF2 characteristics."""

    aliases = {
        "REF1": "DAC_TST_REF1",
        "REF2": "DAC_TST_REF2",
        "DAC_TST_REF1": "DAC_TST_REF1",
        "DAC_TST_REF2": "DAC_TST_REF2",
    }
    result: dict[str, ReferenceDacCalibration] = {}
    for name, source in files.items():
        raw_key = str(name).strip().upper()
        if raw_key not in aliases:
            raise ValueError(f"unsupported reference DAC calibration key: {name}")
        key = aliases[raw_key]
        if key in result:
            raise ValueError(f"duplicate reference DAC calibration key: {key}")
        calibration = (
            source
            if isinstance(source, ReferenceDacCalibration)
            else ReferenceDacCalibration.from_csv(
                source,
                key,
                voltage_unit=voltage_unit,
            )
        )
        if calibration.dac_name != key:
            raise ValueError(
                f"calibration object is for {calibration.dac_name}, not requested key {key}"
            )
        result[key] = calibration
    missing = {"DAC_TST_REF1", "DAC_TST_REF2"} - set(result)
    if missing:
        raise ValueError(
            "missing required reference-DAC calibration(s): "
            + ", ".join(sorted(missing))
        )
    return result


def select_reference_dac_pairs(
    ref1: ReferenceDacCalibration,
    ref2: ReferenceDacCalibration,
    requested_voltage_steps_v: Sequence[float],
    *,
    minimum_reference_code: int = 401,
    maximum_reference_code: int = 1023,
    minimum_reference_voltage_v: float | None = None,
    preferred_reference_common_mode_v: float | None = None,
    common_mode_step_error_slack_v: float = 0.0,
    maximum_reference_step_error_v: float | None = 1e-3,
) -> tuple[ReferencePairSelection, ...]:
    """Select one fixed low REF1 and a separate REF2 for every positive step.

    The selector uses measured LUT rows only and enforces ``V_REF1 > V_REF2``.
    With a finite ``maximum_reference_step_error_v`` it first finds every REF1
    level for which all requested steps can be represented within that error,
    then chooses the lowest *measured voltage* REF1. REF2 alone changes between
    amplitudes. Distinct requested steps receive distinct REF2 codes.

    ``preferred_reference_common_mode_v`` and
    ``common_mode_step_error_slack_v`` remain accepted for source compatibility,
    but common-mode optimization is deliberately not used by this policy.
    """

    if not isinstance(minimum_reference_code, int) or isinstance(
        minimum_reference_code, bool
    ) or not 0 <= minimum_reference_code <= 1023:
        raise ValueError("minimum_reference_code must be an integer in 0..1023")
    if not isinstance(maximum_reference_code, int) or isinstance(
        maximum_reference_code, bool
    ) or not 0 <= maximum_reference_code <= 1023:
        raise ValueError("maximum_reference_code must be an integer in 0..1023")
    if minimum_reference_code > maximum_reference_code:
        raise ValueError("minimum_reference_code must not exceed maximum_reference_code")
    if minimum_reference_voltage_v is not None:
        minimum_reference_voltage_v = float(minimum_reference_voltage_v)
        if not math.isfinite(minimum_reference_voltage_v):
            raise ValueError("minimum_reference_voltage_v must be finite")
    if preferred_reference_common_mode_v is not None:
        preferred_reference_common_mode_v = float(preferred_reference_common_mode_v)
        if not math.isfinite(preferred_reference_common_mode_v):
            raise ValueError("preferred_reference_common_mode_v must be finite")
    common_mode_step_error_slack_v = float(common_mode_step_error_slack_v)
    if (
        not math.isfinite(common_mode_step_error_slack_v)
        or common_mode_step_error_slack_v < 0
    ):
        raise ValueError(
            "common_mode_step_error_slack_v must be finite and >= 0"
        )
    if maximum_reference_step_error_v is not None:
        maximum_reference_step_error_v = float(maximum_reference_step_error_v)
        if not math.isfinite(maximum_reference_step_error_v) or maximum_reference_step_error_v < 0:
            raise ValueError("maximum_reference_step_error_v must be finite and >= 0")

    requested = tuple(float(value) for value in requested_voltage_steps_v)
    if not requested:
        raise ValueError("requested_voltage_steps_v must not be empty")
    if any(not math.isfinite(value) or value <= 0 for value in requested):
        raise ValueError("every requested REF voltage step must be finite and positive")
    if len(set(requested)) != len(requested):
        raise ValueError("requested REF voltage steps must not contain duplicates")

    ref1_mask = (ref1.codes >= minimum_reference_code) & (ref1.codes <= maximum_reference_code)
    ref2_mask = (ref2.codes >= minimum_reference_code) & (ref2.codes <= maximum_reference_code)
    if minimum_reference_voltage_v is not None:
        ref1_mask &= ref1.voltages >= minimum_reference_voltage_v
        ref2_mask &= ref2.voltages >= minimum_reference_voltage_v
    ref1_codes = ref1.codes[ref1_mask]
    ref1_voltages = ref1.voltages[ref1_mask]
    ref2_codes = ref2.codes[ref2_mask]
    ref2_voltages = ref2.voltages[ref2_mask]
    if not len(ref1_codes) or not len(ref2_codes):
        raise ValueError(
            "no measured REF codes satisfy the configured code bounds/voltage constraints"
        )

    delta = ref1_voltages[:, None] - ref2_voltages[None, :]
    valid_order = delta > 0
    if not bool(np.any(valid_order)):
        raise ValueError(
            "no measured REF pair satisfies the required physical order V_REF1 > V_REF2"
        )

    global_minimum_errors = tuple(
        float(np.min(np.where(valid_order, np.abs(delta - target), np.inf)))
        for target in requested
    )
    if maximum_reference_step_error_v is not None:
        for target, minimum_error in zip(requested, global_minimum_errors):
            if minimum_error > maximum_reference_step_error_v:
                raise ValueError(
                    f"nearest REF pair for requested step {target:g} V has error "
                    f"{minimum_error:g} V, above maximum_reference_step_error_v="
                    f"{maximum_reference_step_error_v:g} V"
                )

    def distinct_ref2_assignment(
        ref1_index: int,
        error_limit: float,
    ) -> tuple[dict[int, int], tuple[float, ...]] | None:
        options: dict[int, list[int]] = {}
        error_rows: dict[int, np.ndarray] = {}
        for target_index, target in enumerate(requested):
            errors = np.abs(delta[ref1_index] - target)
            permitted = np.flatnonzero(
                valid_order[ref1_index] & (errors <= error_limit + 1e-15)
            )
            if not len(permitted):
                return None
            options[target_index] = sorted(
                (int(index) for index in permitted),
                key=lambda index: (
                    float(errors[index]),
                    int(ref2_codes[index]),
                ),
            )
            error_rows[target_index] = errors

        ref2_to_target: dict[int, int] = {}

        def assign(target_index: int, visited: set[int]) -> bool:
            for ref2_index in options[target_index]:
                if ref2_index in visited:
                    continue
                visited.add(ref2_index)
                previous_target = ref2_to_target.get(ref2_index)
                if previous_target is None or assign(previous_target, visited):
                    ref2_to_target[ref2_index] = target_index
                    return True
            return False

        for target_index in sorted(options, key=lambda index: (len(options[index]), index)):
            if not assign(target_index, set()):
                return None
        target_to_ref2 = {
            target_index: ref2_index
            for ref2_index, target_index in ref2_to_target.items()
        }
        selected_errors = tuple(
            float(error_rows[index][target_to_ref2[index]])
            for index in range(len(requested))
        )
        return target_to_ref2, selected_errors

    ref1_order = sorted(
        range(len(ref1_codes)),
        key=lambda index: (float(ref1_voltages[index]), int(ref1_codes[index])),
    )
    selected_ref1_index: int | None = None
    selected_assignment: dict[int, int] | None = None
    selected_errors: tuple[float, ...] | None = None
    if maximum_reference_step_error_v is not None:
        for ref1_index in ref1_order:
            result = distinct_ref2_assignment(
                ref1_index, maximum_reference_step_error_v
            )
            if result is not None:
                selected_ref1_index = ref1_index
                selected_assignment, selected_errors = result
                break
    else:
        candidates: list[
            tuple[tuple[float, float, float, int], int, dict[int, int], tuple[float, ...]]
        ] = []
        for ref1_index in ref1_order:
            result = distinct_ref2_assignment(ref1_index, float("inf"))
            if result is None:
                continue
            assignment, errors = result
            candidates.append(
                (
                    (
                        max(errors),
                        sum(errors),
                        float(ref1_voltages[ref1_index]),
                        int(ref1_codes[ref1_index]),
                    ),
                    ref1_index,
                    assignment,
                    errors,
                )
            )
        if candidates:
            _, selected_ref1_index, selected_assignment, selected_errors = min(
                candidates, key=lambda item: item[0]
            )

    if selected_ref1_index is None or selected_assignment is None or selected_errors is None:
        limit_text = (
            f" within {maximum_reference_step_error_v:g} V"
            if maximum_reference_step_error_v is not None
            else ""
        )
        raise ValueError(
            "no single measured REF1 level can represent every requested step"
            f"{limit_text} with distinct REF2 codes and V_REF1 > V_REF2"
        )

    code1 = int(ref1_codes[selected_ref1_index])
    voltage1 = float(ref1_voltages[selected_ref1_index])
    method = (
        "fixed_lowest_measured_REF1_voltage_then_distinct_nearest_REF2_"
        "within_step_tolerance"
    )
    selections: list[ReferencePairSelection] = []
    for target_index, target in enumerate(requested):
        index2 = selected_assignment[target_index]
        code2 = int(ref2_codes[index2])
        voltage2 = float(ref2_voltages[index2])
        actual = voltage1 - voltage2
        signed_error = actual - target
        absolute_error = abs(signed_error)
        selections.append(
            ReferencePairSelection(
                requested_voltage_step_v=target,
                actual_voltage_step_v=actual,
                voltage_step_error_v=signed_error,
                absolute_voltage_step_error_v=absolute_error,
                ref1_code=code1,
                ref2_code=code2,
                ref1_voltage_v=voltage1,
                ref2_voltage_v=voltage2,
                reference_common_mode_v=0.5 * (voltage1 + voltage2),
                selection_method=method,
                minimum_reference_code=minimum_reference_code,
                maximum_reference_code=maximum_reference_code,
                minimum_reference_voltage_v=minimum_reference_voltage_v,
                selected_common_mode_target_v=None,
                common_mode_step_error_slack_v=0.0,
                minimum_achievable_step_error_v=global_minimum_errors[target_index],
                fixed_ref1_voltage_v=voltage1,
                ref1_shared_across_amplitudes=True,
            )
        )
    return tuple(selections)


def load_threshold_dac_calibrations(
    files: Mapping[str, str | Path | ThresholdDacCalibration],
) -> dict[str, ThresholdDacCalibration]:
    """Load independent measured calibrations keyed by ``DAC_CMP_A`` etc."""

    result: dict[str, ThresholdDacCalibration] = {}
    for name, source in files.items():
        key = str(name).strip().upper()
        if key not in {"DAC_CMP_A", "DAC_CMP_B", "DAC_CMP_C", "DAC_CMP_D"}:
            raise ValueError(f"unsupported threshold DAC calibration key: {name}")
        calibration = (
            source
            if isinstance(source, ThresholdDacCalibration)
            else ThresholdDacCalibration.from_csv(source, key)
        )
        if calibration.dac_name != key:
            raise ValueError(
                f"calibration object is for {calibration.dac_name}, not requested key {key}"
            )
        result[key] = calibration
    return result
