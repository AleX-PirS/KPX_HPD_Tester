"""Постоянная маска: True/1 означает исключение физического (column, row).

MASK в регистре имеет ОБРАТНЫЙ смысл: исключенному пикселю всегда записываются
PX_MASK=0 и PX_TST_EN=0. Координаты не переставляются и не перенумеровываются.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
import json
from numbers import Integral
from pathlib import Path
from typing import Any

import pandas as pd

from pixel_matrix import MATRIX_ROWS, OWNED_COLUMNS, PIXEL_CODEC


BadPixelMapInput = str | Path | Mapping[Any, Any] | Sequence[Any] | None


def _bad_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral) and int(value) in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("0", "1", "true", "false"):
        return value.strip().lower() in ("1", "true")
    raise ValueError("bad must be True/False or 1/0; True/1 means EXCLUDE")


def _coordinate(column: Any, row: Any) -> tuple[int, int]:
    result = []
    for value in (column, row):
        if isinstance(value, str) and value.strip().lstrip("+").isdigit():
            value = int(value)
        if not isinstance(value, Integral) or isinstance(value, bool):
            raise ValueError("bad pixel coordinates must be integer (column, row)")
        result.append(int(value))
    column, row = result
    if column not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
        raise ValueError(
            f"bad pixel ({column}, {row}) is outside the owned matrix "
            f"Col={min(OWNED_COLUMNS)}..{max(OWNED_COLUMNS)}, Row=0..{MATRIX_ROWS - 1}"
        )
    return column, row


def normalize_bad_pixel_map(source: BadPixelMapInput = None) -> tuple[tuple[int, int], ...]:
    """Read a coordinate list, {(col,row): bool}, or CSV/JSON path.

    CSV: ``column,row,bad`` (``bad`` optional; a row without it means bad=True).
    JSON: ``{"bad_pixels": [{"column": 16, "row": 0}, ...]}`` or a list of pairs.
    An empty input excludes nothing. A 2D raster is intentionally NOT inferred.
    """

    if source is None:
        return ()
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.suffix.lower() == ".json":
            with path.open(encoding="utf-8-sig") as handle:
                return normalize_bad_pixel_map(json.load(handle))
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not {"column", "row"}.issubset(reader.fieldnames or ()):
                    raise ValueError("bad pixel CSV requires column,row[,bad]")
                return normalize_bad_pixel_map(list(reader))
        raise ValueError("bad pixel file must have .csv or .json extension")
    states: dict[tuple[int, int], bool] = {}

    def remember(coordinate: tuple[int, int], flag: bool) -> None:
        if coordinate in states and states[coordinate] != flag:
            raise ValueError(f"contradictory bad-pixel flags for {coordinate}")
        states[coordinate] = flag

    if isinstance(source, Mapping):
        if "bad_pixels" in source:
            return normalize_bad_pixel_map(source["bad_pixels"])
        for key, value in source.items():
            if not isinstance(key, (tuple, list)) or len(key) != 2:
                raise ValueError("bad pixel map keys must be physical (column, row) pairs")
            remember(_coordinate(*key), _bad_flag(value))
    else:
        if not isinstance(source, Sequence):
            raise TypeError("bad pixel map must be a list, mapping, CSV/JSON path or None")
        for item in source:
            if isinstance(item, Mapping):
                if not {"column", "row"}.issubset(item):
                    raise ValueError("bad pixel entries require column and row")
                remember(_coordinate(item["column"], item["row"]), _bad_flag(item.get("bad", True)))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                remember(_coordinate(*item), True)
            else:
                raise ValueError("bad pixel list entries must be (column, row) or records")
    return tuple(sorted(coordinate for coordinate, bad in states.items() if bad))


def bad_pixel_document(source: BadPixelMapInput = None) -> dict[str, Any]:
    return {
        "coordinate_order": "physical_column_row",
        "semantics": "bad=true -> PX_MASK=0 and PX_TST_EN=0",
        "bad_pixels": [
            {"column": column, "row": row, "bad": True}
            for column, row in normalize_bad_pixel_map(source)
        ],
    }


def enforce_disabled_pixel(raw: int) -> int:
    """Clear the two enable fields without changing gain, trims or other bits."""

    fields = PIXEL_CODEC.unpack(int(raw))
    fields["PX_MASK"] = 0
    fields["PX_TST_EN"] = 0
    return PIXEL_CODEC.pack(fields)


def noise_baseline_pixel_word(raw: int, *, bad: bool = False) -> int:
    """Use the explicit mask for counting and always turn injection off at startup."""

    fields = PIXEL_CODEC.unpack(int(raw))
    fields["PX_MASK"] = int(not bad)
    fields["PX_TST_EN"] = 0
    return PIXEL_CODEC.pack(fields)


def exclude_bad_pixel_rows(frame: pd.DataFrame, bad_pixels: Sequence[tuple[int, int]]) -> pd.DataFrame:
    """Filter a derived table; never modify its source/raw data on disk."""

    if frame.empty or not bad_pixels:
        return frame.copy()
    coordinates = pd.MultiIndex.from_frame(frame[["column", "row"]].astype(int))
    return frame.loc[~coordinates.isin(bad_pixels)].copy()
