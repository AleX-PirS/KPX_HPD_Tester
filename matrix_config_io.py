from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import tempfile

from pixel_matrix import MATRIX_ROWS, OWNED_COLUMNS, PIXEL_CODEC


MATRIX_CONFIG_FORMAT = "kpx_hpd_pixel_matrix"
MATRIX_CONFIG_VERSION = 1
DEFAULT_CONFIG_DIRECTORY = Path(__file__).resolve().parent / "configs"

_JSON_FIELD_NAMES = {
    name: name[3:] if name.startswith("PX_") else name
    for name in PIXEL_CODEC.field_names
}
_PX_FIELD_NAMES = {
    json_name: px_name
    for px_name, json_name in _JSON_FIELD_NAMES.items()
}
_EXPECTED_COORDINATES = tuple(
    (row, col)
    for row in range(MATRIX_ROWS)
    for col in OWNED_COLUMNS
)
_EXPECTED_COORDINATE_SET = set(_EXPECTED_COORDINATES)


def _coordinate_text(coordinates: set[tuple[int, int]]) -> str:
    preview = sorted(coordinates)[:5]
    text = ", ".join(f"Col={col} Row={row}" for row, col in preview)
    if len(coordinates) > len(preview):
        text += f", and {len(coordinates) - len(preview)} more"
    return text


def resolve_matrix_config(
    local_values: Mapping[tuple[int, int], int],
    upo_values: Mapping[tuple[int, int], int],
    chip_values: Mapping[tuple[int, int], int],
) -> dict[tuple[int, int], int]:
    """Resolve each owned pixel using local -> UPO -> chip priority."""
    local = dict(local_values)
    upo = dict(upo_values)
    chip = dict(chip_values)
    values: dict[tuple[int, int], int] = {}
    unknown: list[tuple[int, int]] = []

    for coord in _EXPECTED_COORDINATES:
        if coord in local:
            value = local[coord]
        elif coord in upo:
            value = upo[coord]
        elif coord in chip:
            value = chip[coord]
        else:
            unknown.append(coord)
            continue
        PIXEL_CODEC.validate_raw(value)
        values[coord] = value

    if unknown:
        row, col = unknown[0]
        raise ValueError(
            "Matrix configuration cannot be saved because "
            f"{len(unknown)} of {len(_EXPECTED_COORDINATES)} pixel values are unknown. "
            f"First unknown pixel: Col={col} Row={row}. "
            "Load a configuration or establish every pixel locally/in UPO/on chip first."
        )
    return values


def build_matrix_config_document(
    pixel_values: Mapping[tuple[int, int], int],
) -> dict:
    """Create the human-readable versioned JSON document for all owned pixels."""
    values = dict(pixel_values)
    value_coordinates = set(values)

    missing_values = _EXPECTED_COORDINATE_SET - value_coordinates
    extra_values = value_coordinates - _EXPECTED_COORDINATE_SET
    if missing_values:
        raise ValueError(
            "Matrix configuration is missing pixel values: "
            + _coordinate_text(missing_values)
        )
    if extra_values:
        raise ValueError(
            "Matrix configuration contains pixels outside Col=16..31, Row=0..31: "
            + _coordinate_text(extra_values)
        )

    pixels = []
    for row, col in _EXPECTED_COORDINATES:
        raw = values[(row, col)]
        PIXEL_CODEC.validate_raw(raw)
        unpacked = PIXEL_CODEC.unpack(raw)
        pixels.append(
            {
                "row": row,
                "col": col,
                "fields": {
                    _JSON_FIELD_NAMES[name]: unpacked[name]
                    for name in PIXEL_CODEC.field_names
                },
            }
        )

    return {
        "format": MATRIX_CONFIG_FORMAT,
        "format_version": MATRIX_CONFIG_VERSION,
        "matrix": {
            "rows": MATRIX_ROWS,
            "columns": list(OWNED_COLUMNS),
            "pixel_count": len(_EXPECTED_COORDINATES),
        },
        "pixels": pixels,
    }


def parse_matrix_config_document(document: object) -> dict[tuple[int, int], int]:
    """Validate one configuration document and return packed per-pixel values."""
    if not isinstance(document, dict):
        raise ValueError("Matrix configuration root must be a JSON object")

    required_root_keys = {"format", "format_version", "matrix", "pixels"}
    missing_root_keys = required_root_keys - set(document)
    if missing_root_keys:
        raise ValueError(
            "Matrix configuration is missing top-level key(s): "
            + ", ".join(sorted(missing_root_keys))
        )

    if document["format"] != MATRIX_CONFIG_FORMAT:
        raise ValueError(
            f"Unsupported matrix configuration format: {document['format']!r}"
        )
    version = document["format_version"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != MATRIX_CONFIG_VERSION
    ):
        raise ValueError(
            "Unsupported matrix configuration version: "
            f"{version!r}; expected {MATRIX_CONFIG_VERSION}"
        )

    matrix = document["matrix"]
    if not isinstance(matrix, dict):
        raise ValueError("matrix must be a JSON object")
    if matrix.get("rows") != MATRIX_ROWS:
        raise ValueError(f"matrix.rows must be {MATRIX_ROWS}")
    if matrix.get("columns") != list(OWNED_COLUMNS):
        raise ValueError(
            f"matrix.columns must be {min(OWNED_COLUMNS)}..{max(OWNED_COLUMNS)}"
        )
    if matrix.get("pixel_count") != len(_EXPECTED_COORDINATES):
        raise ValueError(
            f"matrix.pixel_count must be {len(_EXPECTED_COORDINATES)}"
        )

    pixels = document["pixels"]
    if not isinstance(pixels, list):
        raise ValueError("pixels must be a JSON array")
    if len(pixels) != len(_EXPECTED_COORDINATES):
        raise ValueError(
            f"pixels must contain exactly {len(_EXPECTED_COORDINATES)} entries; "
            f"found {len(pixels)}"
        )

    expected_fields = set(_PX_FIELD_NAMES)
    values: dict[tuple[int, int], int] = {}
    for index, item in enumerate(pixels):
        location = f"pixels[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{location} must be a JSON object")

        for key in ("row", "col", "fields"):
            if key not in item:
                raise ValueError(f"{location} is missing {key!r}")
        extra_item_keys = set(item) - {"row", "col", "fields"}
        if extra_item_keys:
            raise ValueError(
                f"{location} contains unknown key(s): "
                + ", ".join(sorted(extra_item_keys))
            )

        row = item["row"]
        col = item["col"]
        if not isinstance(row, int) or isinstance(row, bool):
            raise ValueError(f"{location}.row must be an integer")
        if not isinstance(col, int) or isinstance(col, bool):
            raise ValueError(f"{location}.col must be an integer")
        coord = (row, col)
        if coord not in _EXPECTED_COORDINATE_SET:
            raise ValueError(
                f"{location} coordinate Col={col} Row={row} is outside "
                "Col=16..31, Row=0..31"
            )
        if coord in values:
            raise ValueError(f"Duplicate pixel entry for Col={col} Row={row}")

        fields = item["fields"]
        if not isinstance(fields, dict):
            raise ValueError(f"{location}.fields must be a JSON object")
        field_names = set(fields)
        missing_fields = expected_fields - field_names
        extra_fields = field_names - expected_fields
        if missing_fields:
            raise ValueError(
                f"{location}.fields is missing: "
                + ", ".join(sorted(missing_fields))
            )
        if extra_fields:
            raise ValueError(
                f"{location}.fields contains unknown field(s): "
                + ", ".join(sorted(extra_fields))
            )

        px_values = {}
        for json_name, px_name in _PX_FIELD_NAMES.items():
            value = fields[json_name]
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"{location}.fields.{json_name} must be an integer"
                )
            px_values[px_name] = value

        try:
            values[coord] = PIXEL_CODEC.pack(px_values)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid fields for Col={col} Row={row}: {error}") from error

    missing_coordinates = _EXPECTED_COORDINATE_SET - set(values)
    if missing_coordinates:
        raise ValueError(
            "Matrix configuration is missing pixel entries: "
            + _coordinate_text(missing_coordinates)
        )
    return values


def save_matrix_config(path: str | Path, document: Mapping) -> Path:
    """Write a configuration atomically as indented UTF-8 JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, target)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return target


def load_matrix_config(path: str | Path) -> dict[tuple[int, int], int]:
    """Read and validate a matrix configuration JSON file."""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8-sig") as stream:
            document = json.load(stream)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    return parse_matrix_config_document(document)
