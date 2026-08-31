from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import pandas as pd

from pixel_matrix import MATRIX_ROWS, OWNED_COLUMNS
from .pixel_masks import BadPixelMapInput, normalize_bad_pixel_map


ELEMENTARY_CHARGE_C = 1.602176634e-19
INJECTION_PATTERN_TILE_SIZE: dict[str, tuple[int, int] | None] = {
    "all": None,
    "tile_2x2": (2, 2),
    "tile_4x4": (4, 4),
    "tile_8x8": (8, 8),
}
CROSSTALK_INJECTION_PATTERNS = (
    "all",
    "tile_2x2",
    "tile_4x4",
    "tile_8x8",
)


@dataclass(frozen=True)
class InjectionGroup:
    """One simultaneously enabled set of physical ASIC pixels."""

    pattern: str
    group_id: str
    phase_column: int | None
    phase_row: int | None
    tile_width: int | None
    tile_height: int | None
    active_pixels: tuple[tuple[int, int], ...]

    def to_metadata(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "group_id": self.group_id,
            "phase_column": self.phase_column,
            "phase_row": self.phase_row,
            "tile_width": self.tile_width,
            "tile_height": self.tile_height,
            "active_pixel_count": len(self.active_pixels),
            "active_pixels": [
                {"column": column, "row": row}
                for column, row in self.active_pixels
            ],
        }


def _gain_value(value: Any, *, coordinate: tuple[int, int]) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(
            f"GAIN at Col={coordinate[0]} Row={coordinate[1]} must be int"
        )
    if not 0 <= value <= 31:
        raise ValueError(
            f"GAIN at Col={coordinate[0]} Row={coordinate[1]} is outside 0..31"
        )
    return int(value)


def resolve_gain_map(
    gain_map: Mapping[tuple[int, int], int] | Sequence[Any],
    *,
    required_pixels: Sequence[tuple[int, int]],
    owned_columns: Sequence[int] = OWNED_COLUMNS,
) -> dict[tuple[int, int], int]:
    """Normalize a per-pixel GAIN map to physical ``(column, row)`` keys.

    Accepted forms:

    * ``{(physical_column, physical_row): gain}``;
    * ``gain_map[row][owned_column_index]`` with 32 rows, where index zero
      maps to the first physical owned column, currently Col=16;
    * flat row-major data with index
      ``row * len(owned_columns) + owned_column_index``.
    """

    columns = tuple(int(value) for value in owned_columns)
    if not columns:
        raise ValueError("owned_columns must not be empty")
    required = tuple((int(column), int(row)) for column, row in required_pixels)

    if isinstance(gain_map, Mapping):
        normalized_source: dict[tuple[int, int], int] = {}
        for key, value in gain_map.items():
            if not isinstance(key, (tuple, list)) or len(key) != 2:
                raise TypeError("GAIN mapping keys must be (physical_column, physical_row)")
            coordinate = (int(key[0]), int(key[1]))
            normalized_source[coordinate] = _gain_value(value, coordinate=coordinate)
    else:
        if isinstance(gain_map, (str, bytes)):
            raise TypeError("GAIN map must not be a string")
        outer = list(gain_map)
        width = len(columns)
        if len(outer) == MATRIX_ROWS and all(
            isinstance(row_values, Sequence)
            and not isinstance(row_values, (str, bytes))
            for row_values in outer
        ):
            normalized_source = {}
            for row, row_values in enumerate(outer):
                values = list(row_values)
                if len(values) != width:
                    raise ValueError(
                        f"GAIN row {row} has {len(values)} values; expected {width}"
                    )
                for owned_column_index, value in enumerate(values):
                    coordinate = (columns[owned_column_index], row)
                    normalized_source[coordinate] = _gain_value(
                        value, coordinate=coordinate
                    )
        elif len(outer) == MATRIX_ROWS * width and not any(
            isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            for value in outer
        ):
            normalized_source = {}
            for index, value in enumerate(outer):
                row, owned_column_index = divmod(index, width)
                coordinate = (columns[owned_column_index], row)
                normalized_source[coordinate] = _gain_value(
                    value, coordinate=coordinate
                )
        else:
            raise ValueError(
                "GAIN sequence must be [row][owned_column_index] with shape "
                f"{MATRIX_ROWS}x{width}, or a flat row-major list of "
                f"{MATRIX_ROWS * width} values"
            )

    missing = [coordinate for coordinate in required if coordinate not in normalized_source]
    if missing:
        column, row = missing[0]
        raise ValueError(
            f"GAIN map is missing {len(missing)} selected pixel(s); first is "
            f"Col={column} Row={row}"
        )
    return {coordinate: normalized_source[coordinate] for coordinate in required}


def load_gain_map_csv(path: str | Path) -> dict[tuple[int, int], int]:
    """Load a long-form physical GAIN map with column, row and gain fields."""

    source = Path(path)
    frame = pd.read_csv(source)
    normalized = {
        "".join(character for character in str(name).lower() if character.isalnum()): name
        for name in frame.columns
    }

    def select(*aliases: str) -> Any:
        matches = [normalized[alias] for alias in aliases if alias in normalized]
        if len(matches) != 1:
            raise ValueError(
                f"GAIN CSV {source} must contain one of {aliases}; columns are "
                + ", ".join(map(str, frame.columns))
            )
        return matches[0]

    column_field = select("column", "col", "physicalcolumn")
    row_field = select("row", "physicalrow")
    gain_field = select("gain", "pxgain")
    result: dict[tuple[int, int], int] = {}
    for index, record in frame.iterrows():
        try:
            numeric_values = {
                "column": float(record[column_field]),
                "row": float(record[row_field]),
                "gain": float(record[gain_field]),
            }
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid GAIN CSV row {index}: {error}") from error
        if any(
            not math.isfinite(value) or not value.is_integer()
            for value in numeric_values.values()
        ):
            raise ValueError(
                f"GAIN CSV row {index} must contain finite integer column, row and gain"
            )
        column = int(numeric_values["column"])
        row = int(numeric_values["row"])
        gain = int(numeric_values["gain"])
        if column not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
            raise ValueError(
                f"GAIN CSV row {index} has coordinate outside owned matrix: "
                f"Col={column} Row={row}"
            )
        coordinate = (column, row)
        if coordinate in result:
            raise ValueError(
                f"duplicate GAIN CSV coordinate Col={column} Row={row}"
            )
        result[coordinate] = _gain_value(gain, coordinate=coordinate)
    if not result:
        raise ValueError(f"GAIN CSV is empty: {source}")
    return result


def build_injection_groups(
    pixels: Sequence[tuple[int, int]],
    pattern: str,
    *,
    owned_columns: Sequence[int] = OWNED_COLUMNS,
    bad_pixel_map: BadPixelMapInput = None,
) -> tuple[InjectionGroup, ...]:
    """Build simultaneous groups without renumbering physical coordinates.

    A tiled phase enables the same relative pixel in every tile. Phase order is
    row-major inside the tile. Empty phases are omitted for partial selections.
    """

    normalized = str(pattern).strip().lower()
    if normalized not in INJECTION_PATTERN_TILE_SIZE:
        raise ValueError(f"unknown injection pattern: {pattern}")
    selected = tuple(dict.fromkeys((int(column), int(row)) for column, row in pixels))
    bad_pixels = set(normalize_bad_pixel_map(bad_pixel_map))
    selected = tuple(pixel for pixel in selected if pixel not in bad_pixels)
    if not selected:
        raise ValueError("pixel selection is empty")

    tile_size = INJECTION_PATTERN_TILE_SIZE[normalized]
    if tile_size is None:
        return (
            InjectionGroup(
                pattern="all",
                group_id="all",
                phase_column=None,
                phase_row=None,
                tile_width=None,
                tile_height=None,
                active_pixels=selected,
            ),
        )

    tile_width, tile_height = tile_size
    column_origin = min(int(value) for value in owned_columns)
    groups: list[InjectionGroup] = []
    for phase_row in range(tile_height):
        for phase_column in range(tile_width):
            active = tuple(
                (column, row)
                for column, row in selected
                if (column - column_origin) % tile_width == phase_column
                and row % tile_height == phase_row
            )
            if not active:
                continue
            phase_index = phase_row * tile_width + phase_column
            groups.append(
                InjectionGroup(
                    pattern=normalized,
                    group_id=f"phase_{phase_index:03d}",
                    phase_column=phase_column,
                    phase_row=phase_row,
                    tile_width=tile_width,
                    tile_height=tile_height,
                    active_pixels=active,
                )
            )
    return tuple(groups)


def injection_charge_metadata(
    pulse_amplitude: Any,
    *,
    capacitance_f: float,
    capacitance_relative_uncertainty: float,
) -> dict[str, float | str]:
    """Calculate nominal charge only when a calibrated voltage step is given."""

    result: dict[str, float | str] = {
        "injection_capacitance_f": float(capacitance_f),
        "injection_capacitance_relative_uncertainty": float(
            capacitance_relative_uncertainty
        ),
        "injection_voltage_step_v": float("nan"),
        "requested_injection_voltage_step_v": float("nan"),
        "injection_voltage_step_error_v": float("nan"),
        "absolute_injection_voltage_step_error_v": float("nan"),
        "ref1_dac_code": float("nan"),
        "ref2_dac_code": float("nan"),
        "ref1_voltage_v": float("nan"),
        "ref2_voltage_v": float("nan"),
        "reference_common_mode_v": float("nan"),
        "ref_voltage_order": "not_available",
        "reference_pair_selection_method": "not_available",
        "injection_charge_c": float("nan"),
        "injection_charge_uncertainty_c": float("nan"),
        "injection_charge_electrons": float("nan"),
        "injection_charge_status": "native_amplitude_only_no_voltage_step_lut",
    }
    if not isinstance(pulse_amplitude, Mapping):
        return result
    numeric_fields = {
        "requested_injection_voltage_step_v": "requested_voltage_step_v",
        "injection_voltage_step_error_v": "voltage_step_error_v",
        "absolute_injection_voltage_step_error_v": "absolute_voltage_step_error_v",
        "ref1_dac_code": "DAC_TST_REF1",
        "ref2_dac_code": "DAC_TST_REF2",
        "ref1_voltage_v": "ref1_voltage_v",
        "ref2_voltage_v": "ref2_voltage_v",
        "reference_common_mode_v": "reference_common_mode_v",
    }
    for output_name, input_name in numeric_fields.items():
        if input_name in pulse_amplitude and pulse_amplitude[input_name] is not None:
            value = float(pulse_amplitude[input_name])
            if not math.isfinite(value):
                raise ValueError(f"{input_name} must be finite")
            result[output_name] = value
    if "reference_pair_selection_method" in pulse_amplitude:
        result["reference_pair_selection_method"] = str(
            pulse_amplitude["reference_pair_selection_method"]
        )
    ref1_voltage = float(result["ref1_voltage_v"])
    ref2_voltage = float(result["ref2_voltage_v"])
    if math.isfinite(ref1_voltage) and math.isfinite(ref2_voltage):
        if not ref1_voltage > ref2_voltage:
            raise ValueError("physical REF levels must satisfy V_REF1 > V_REF2")
        result["ref_voltage_order"] = "V_REF1_above_V_REF2"
    voltage = pulse_amplitude.get(
        "voltage_step_v", pulse_amplitude.get("injection_voltage_step_v")
    )
    if voltage is None:
        return result
    voltage_value = float(voltage)
    if not math.isfinite(voltage_value):
        raise ValueError("injection voltage step must be finite")
    if voltage_value <= 0:
        raise ValueError("injection voltage step must be positive for V_REF1 > V_REF2")
    if math.isfinite(ref1_voltage) and math.isfinite(ref2_voltage):
        lut_step = ref1_voltage - ref2_voltage
        if not math.isclose(voltage_value, lut_step, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError(
                "voltage_step_v does not match ref1_voltage_v - ref2_voltage_v"
            )
    charge = abs(voltage_value) * float(capacitance_f)
    result.update(
        {
            "injection_voltage_step_v": voltage_value,
            "injection_charge_c": charge,
            "injection_charge_uncertainty_c": charge
            * float(capacitance_relative_uncertainty),
            "injection_charge_electrons": charge / ELEMENTARY_CHARGE_C,
            "injection_charge_status": "nominal_c_times_delta_v",
        }
    )
    return result
