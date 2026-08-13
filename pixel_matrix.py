from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from mgpd import MGPDClient


MATRIX_ROWS = 32
MATRIX_COLS = 32
OWNED_COLUMN_START = 16
OWNED_COLUMN_STOP = 32
OWNED_COLUMNS = tuple(range(OWNED_COLUMN_START, OWNED_COLUMN_STOP))

PIXEL_CONFIG_BITS = 32
PIXEL_CONFIG_MAX = (1 << PIXEL_CONFIG_BITS) - 1


@dataclass(frozen=True)
class PixelField:
    """One independent field of the 32-bit matrix-pixel configuration word.

    This layout is deliberately independent from EO_cfg and from the SPI
    TEST_CONF registers at 0x8038..0x803B. Those registers configure the
    standalone test pixel and are not part of the matrix protocol.
    """

    offset: int
    width: int
    default: int = 0


# 32-bit per-matrix-pixel configuration word.
#
# IMPORTANT:
#   - These are PX fields, not TEST_CONF registers.
#   - There is no SPI address associated with a matrix pixel configuration.
#   - The complete 32-bit word is sent only through SET_PIXEL_CFG.
#   - The positions follow the pixel-configuration layout supplied for the
#     project. The new MGPDLab example independently confirms, among others,
#     bit 8, bit 10, SHAPER_TIME offset 5 and CMPA_TR offset 27.
#
# Defaults below are software/editor defaults only. They are intentionally
# duplicated here rather than read from EO_cfg.TEST_CONF_* so the matrix code
# can never become coupled to the standalone test-pixel registers.
PX_FIELDS: dict[str, PixelField] = {
    "PX_GAIN": PixelField(offset=0, width=5, default=4),
    "PX_SHT": PixelField(offset=5, width=3, default=2),
    "PX_REG": PixelField(offset=8, width=1, default=0),
    "PX_SH_EN": PixelField(offset=9, width=1, default=0),
    "PX_TST_EN": PixelField(offset=10, width=1, default=0),
    "PX_BUF_NEN": PixelField(offset=11, width=1, default=1),
    "PX_CMPD_TR": PixelField(offset=12, width=5, default=15),
    "PX_CMPC_TR": PixelField(offset=17, width=5, default=15),
    "PX_CMPB_TR": PixelField(offset=22, width=5, default=12),
    "PX_CMPA_TR": PixelField(offset=27, width=5, default=13),
}

PIXEL_FIELD_NAMES = tuple(PX_FIELDS)
PX_DEFAULT_FIELD_VALUES = {
    name: spec.default
    for name, spec in PX_FIELDS.items()
}


class PixelConfigCodec:
    """Pack/unpack the independent 32-bit PX matrix-pixel word."""

    def __init__(self):
        self.field_names = PIXEL_FIELD_NAMES
        self.field_widths = {
            name: spec.width
            for name, spec in PX_FIELDS.items()
        }
        self._validate_layout()

    @staticmethod
    def _validate_layout():
        used_bits: set[int] = set()

        for name, spec in PX_FIELDS.items():
            if spec.offset < 0 or spec.width <= 0:
                raise ValueError(f"Invalid PX field layout for {name}")

            if spec.offset + spec.width > PIXEL_CONFIG_BITS:
                raise ValueError(
                    f"{name} exceeds the {PIXEL_CONFIG_BITS}-bit pixel word"
                )

            max_value = (1 << spec.width) - 1
            if not 0 <= spec.default <= max_value:
                raise ValueError(
                    f"Default {name}={spec.default} is outside 0..{max_value}"
                )

            for bit in range(spec.offset, spec.offset + spec.width):
                if bit in used_bits:
                    raise ValueError(f"Overlapping PX bit {bit} at {name}")
                used_bits.add(bit)

        expected = set(range(PIXEL_CONFIG_BITS))
        if used_bits != expected:
            missing = sorted(expected - used_bits)
            raise ValueError(
                "PX fields do not cover the complete 32-bit pixel word. "
                f"Missing bits: {missing}"
            )

    def width(self, name: str) -> int:
        if name not in self.field_widths:
            raise KeyError(f"Unknown pixel field: {name}")
        return self.field_widths[name]

    def default_fields(self) -> dict[str, int]:
        return dict(PX_DEFAULT_FIELD_VALUES)

    def pack(self, values: Mapping[str, int] | None = None) -> int:
        fields = self.default_fields()

        if values is not None:
            unknown = set(values) - set(self.field_names)
            if unknown:
                raise KeyError(
                    f"Unknown pixel field(s): {', '.join(sorted(unknown))}"
                )
            fields.update({name: int(value) for name, value in values.items()})

        raw = 0
        for name, spec in PX_FIELDS.items():
            value = fields[name]
            max_value = (1 << spec.width) - 1
            if not 0 <= value <= max_value:
                raise ValueError(
                    f"{name}={value} is outside {spec.width}-bit range "
                    f"0..{max_value}"
                )
            raw |= value << spec.offset

        return raw & PIXEL_CONFIG_MAX

    def unpack(self, raw: int) -> dict[str, int]:
        self.validate_raw(raw)
        return {
            name: (raw >> spec.offset) & ((1 << spec.width) - 1)
            for name, spec in PX_FIELDS.items()
        }

    @staticmethod
    def validate_raw(raw: int):
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise TypeError("pixel configuration must be int")
        if not 0 <= raw <= PIXEL_CONFIG_MAX:
            raise ValueError(
                "pixel configuration must be in range "
                "0x00000000..0xFFFFFFFF"
            )


PIXEL_CODEC = PixelConfigCodec()
DEFAULT_PIXEL_CONFIG = PIXEL_CODEC.pack()


class PixelMatrixConfiguration:
    """Project helper for the 32x32 physical pixel matrix.

    Only project-owned columns Col=16..31 are exposed by the normal high-level
    API. Matrix values are not mapped to chip SPI addresses: they are sent as
    complete 32-bit words through MGPDLab SET_PIXEL_CFG.
    """

    def __init__(
        self,
        client: MGPDClient,
        owned_columns: tuple[int, ...] = OWNED_COLUMNS,
    ):
        self.client = client
        self.owned_columns = tuple(owned_columns)
        if not self.owned_columns:
            raise ValueError("owned_columns must not be empty")
        for col in self.owned_columns:
            self._validate_col(col)

    @staticmethod
    def _validate_row(row: int):
        if not isinstance(row, int) or isinstance(row, bool):
            raise TypeError("row must be int")
        if not 0 <= row < MATRIX_ROWS:
            raise ValueError(f"row must be in range 0..{MATRIX_ROWS - 1}")

    @staticmethod
    def _validate_col(col: int):
        if not isinstance(col, int) or isinstance(col, bool):
            raise TypeError("col must be int")
        if not 0 <= col < MATRIX_COLS:
            raise ValueError(f"col must be in range 0..{MATRIX_COLS - 1}")

    def validate_owned_pixel(self, row: int, col: int):
        self._validate_row(row)
        self._validate_col(col)
        if col not in self.owned_columns:
            raise ValueError(
                f"Col={col} is outside the project-owned matrix half. "
                f"Allowed columns: "
                f"{min(self.owned_columns)}..{max(self.owned_columns)}"
            )

    def set_pixel(self, row: int, col: int, raw_config: int) -> bool:
        """Update one owned matrix pixel in MGPDLab virtual memory only."""
        self.validate_owned_pixel(row, col)
        PIXEL_CODEC.validate_raw(raw_config)
        return self.client.set_pixel_cfg(row=row, col=col, value=raw_config)

    def set_owned_half(
        self,
        raw_config: int,
        progress_callback: Callable[[int, int, int, int], None] | None = None,
    ) -> int:
        """Load one PX config into all owned matrix pixels in UPO memory."""
        PIXEL_CODEC.validate_raw(raw_config)
        total = MATRIX_ROWS * len(self.owned_columns)
        current = 0

        for row in range(MATRIX_ROWS):
            for col in self.owned_columns:
                current += 1
                if not self.client.set_pixel_cfg(
                    row=row,
                    col=col,
                    value=raw_config,
                ):
                    raise RuntimeError(
                        f"SET_PIXEL_CFG failed at Col={col} Row={row} "
                        f"after {current - 1}/{total} successful pixel updates"
                    )
                if progress_callback is not None:
                    progress_callback(current, total, row, col)

        return total

    def write_to_chip(self) -> bool:
        """Ask MGPDLab to write its complete virtual matrix to the chip."""
        return self.client.write_pixel_cfg_to_chip()
