from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any, Iterable, Mapping, Sequence

from pixel_matrix import MATRIX_ROWS, OWNED_COLUMNS


FRAMEWORK_VERSION = "0.3.0"


@dataclass(frozen=True)
class WindowSpec:
    """Physical definition of one independently characterized window."""

    name: str
    upper_comparator: str
    comparator: str
    threshold_dac: str
    upper_threshold_dac: str
    pixel_trim_field: str
    inferred_counter_key: str


WINDOW_SPECS: dict[str, WindowSpec] = {
    "AB": WindowSpec(
        name="AB",
        upper_comparator="CMP_A",
        comparator="CMP_B",
        threshold_dac="DAC_CMP_B",
        upper_threshold_dac="DAC_CMP_A",
        pixel_trim_field="PX_CMPB_TR",
        inferred_counter_key="high",
    ),
    "BC": WindowSpec(
        name="BC",
        upper_comparator="CMP_B",
        comparator="CMP_C",
        threshold_dac="DAC_CMP_C",
        upper_threshold_dac="DAC_CMP_B",
        pixel_trim_field="PX_CMPC_TR",
        inferred_counter_key="mid",
    ),
    "CD": WindowSpec(
        name="CD",
        upper_comparator="CMP_C",
        comparator="CMP_D",
        threshold_dac="DAC_CMP_D",
        upper_threshold_dac="DAC_CMP_C",
        pixel_trim_field="PX_CMPD_TR",
        inferred_counter_key="low",
    ),
}


def get_window_spec(window: str) -> WindowSpec:
    key = str(window).strip().upper()
    try:
        return WINDOW_SPECS[key]
    except KeyError as error:
        raise ValueError("window must be one of AB, BC or CD") from error


def resolve_pixels(
    pixels: str | Sequence[tuple[int, int]] | None,
    owned_columns: Iterable[int] = OWNED_COLUMNS,
) -> tuple[tuple[int, int], ...]:
    """Return unique physical ``(column, row)`` coordinates.

    The public characterization API always uses the coordinate order printed on
    the ASIC maps: column first, row second. The existing low-level MGPD API is
    adapted internally because it accepts ``row, col``.
    """

    columns = tuple(int(column) for column in owned_columns)
    if not columns:
        raise ValueError("owned matrix column selection is empty")

    if pixels is None or (isinstance(pixels, str) and pixels.lower() == "all"):
        return tuple(
            (column, row)
            for column in columns
            for row in range(MATRIX_ROWS)
        )
    if isinstance(pixels, str):
        raise ValueError("pixels must be 'all' or a sequence of (column, row) tuples")

    result: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for item in pixels:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise TypeError("every pixel must be a (column, row) pair")
        column, row = item
        if not isinstance(column, int) or isinstance(column, bool):
            raise TypeError("pixel column must be int")
        if not isinstance(row, int) or isinstance(row, bool):
            raise TypeError("pixel row must be int")
        if column not in columns:
            raise ValueError(
                f"Col={column} is outside the project-owned columns "
                f"{min(columns)}..{max(columns)}"
            )
        if not 0 <= row < MATRIX_ROWS:
            raise ValueError(f"Row={row} is outside 0..{MATRIX_ROWS - 1}")
        coordinate = (column, row)
        if coordinate not in seen:
            seen.add(coordinate)
            result.append(coordinate)

    if not result:
        raise ValueError("pixel selection is empty")
    return tuple(result)


@dataclass
class NoiseScanSettings:
    noise_repeats: int = 10
    settling_time_s: float = 0.1
    # MGPDLab/UPO controls the real exposure. This value is metadata and must
    # match the value set manually in UPO before acquisition.
    shutter_duration_s: float | None = 0.001
    coarse_start: int = 0
    coarse_stop: int = 1023
    coarse_step: int = 16
    fine_step: int = 1
    fine_margin_codes: int = 16
    dac_start: int | None = None
    dac_stop: int | None = None
    dac_step: int | None = None
    counter_mode_bits: int = 16
    decode_lfsr: bool = True
    lfsr_direction: str = "left"
    configure_get_shot_omr: bool = False
    mode_read: int = 0b010
    crw_mode: int = 0
    continue_after_pixel_read_error: bool = True

    def validate(self) -> None:
        if self.noise_repeats < 2:
            raise ValueError("noise_repeats must be at least 2")
        if self.settling_time_s < 0:
            raise ValueError("settling_time_s must be >= 0")
        if self.shutter_duration_s is not None and self.shutter_duration_s <= 0:
            raise ValueError("shutter_duration_s must be positive when supplied")
        for name, code in (
            ("coarse_start", self.coarse_start),
            ("coarse_stop", self.coarse_stop),
        ):
            if not 0 <= code <= 1023:
                raise ValueError(f"{name} must be in 0..1023")
        if self.coarse_start > self.coarse_stop:
            raise ValueError("coarse_start must not exceed coarse_stop")
        if self.coarse_step <= 0 or self.fine_step <= 0:
            raise ValueError("DAC steps must be positive")
        if self.fine_margin_codes < 0:
            raise ValueError("fine_margin_codes must be >= 0")
        manual = (self.dac_start, self.dac_stop, self.dac_step)
        if any(value is not None for value in manual):
            if not all(value is not None for value in manual):
                raise ValueError("dac_start, dac_stop and dac_step must be supplied together")
            assert self.dac_start is not None
            assert self.dac_stop is not None
            assert self.dac_step is not None
            if not 0 <= self.dac_start <= self.dac_stop <= 1023:
                raise ValueError("manual DAC range must satisfy 0 <= start <= stop <= 1023")
            if self.dac_step <= 0:
                raise ValueError("dac_step must be positive")
        if self.counter_mode_bits not in (8, 16):
            raise ValueError("counter_mode_bits must be 8 or 16")
        if self.lfsr_direction not in ("left", "right", "auto"):
            raise ValueError("lfsr_direction must be left, right or auto")
        if not 0 <= self.mode_read <= 0b111:
            raise ValueError("mode_read must be in 0..7")
        if self.crw_mode not in (0, 1):
            raise ValueError("crw_mode must be 0 or 1")

    def manual_codes(self) -> tuple[int, ...] | None:
        if self.dac_start is None:
            return None
        assert self.dac_stop is not None and self.dac_step is not None
        codes = list(range(self.dac_start, self.dac_stop + 1, self.dac_step))
        if codes[-1] != self.dac_stop:
            codes.append(self.dac_stop)
        return tuple(codes)


@dataclass
class EqualizationSettings:
    trim_min: int = 0
    trim_max: int = 31
    local_search_radius: int = 1
    expanded_search_radius: int = 3
    verification_margin_codes: int = 24
    full_trim_fallback: bool = False
    # Expensive publication/diagnostic mode: acquire complete threshold curves
    # for every uniform trim code 0..31 and use them for the final trim choice.
    scan_all_trim_codes: bool = False
    target_voltage: float | None = None

    def validate(self) -> None:
        if not 0 <= self.trim_min < self.trim_max <= 31:
            raise ValueError("trim endpoints must satisfy 0 <= min < max <= 31")
        if self.local_search_radius < 0:
            raise ValueError("local_search_radius must be >= 0")
        if self.expanded_search_radius < self.local_search_radius:
            raise ValueError("expanded_search_radius must be >= local_search_radius")
        if self.verification_margin_codes < 1:
            raise ValueError("verification_margin_codes must be >= 1")
        if not isinstance(self.scan_all_trim_codes, bool):
            raise TypeError("scan_all_trim_codes must be bool")


@dataclass
class ScurveSettings:
    # One SQU period produces one falling CTRL edge and one requested event.
    n_injections: int = 1000
    repeats: int = 1
    max_background_fraction: float = 0.01
    pulse_amplitudes: tuple[Any, ...] = field(default_factory=tuple)
    shutter_duration_s: float | None = None
    injection_patterns: tuple[str, ...] = ("all",)
    injection_capacitance_f: float = 10e-15
    injection_capacitance_relative_uncertainty: float = 0.20
    # Code 401 implements the requested strict condition REF1/REF2 code > 400.
    # A physical-voltage floor can be enabled independently when required.
    minimum_reference_code: int = 401
    minimum_reference_voltage_v: float | None = None
    preferred_reference_common_mode_v: float | None = None
    maximum_reference_step_error_v: float | None = None
    coarse_step: int = 8
    fine_step: int = 1
    fine_margin_codes: int = 8
    expand_codes: int = 32
    max_expand_rounds: int = 4
    paired_background: bool = True

    def validate(self) -> None:
        if self.n_injections <= 0:
            raise ValueError("n_injections must be positive")
        if self.repeats <= 0:
            raise ValueError("S-curve repeats must be positive")
        if self.shutter_duration_s is not None and self.shutter_duration_s <= 0:
            raise ValueError("S-curve shutter_duration_s must be positive when supplied")
        if not 0 <= self.max_background_fraction < 1:
            raise ValueError("max_background_fraction must be in [0, 1)")
        allowed_patterns = {"all", "tile_2x2", "tile_4x4", "tile_8x8"}
        if not self.injection_patterns:
            raise ValueError("at least one S-curve injection pattern is required")
        normalized_patterns = tuple(
            str(value).strip().lower() for value in self.injection_patterns
        )
        unknown_patterns = set(normalized_patterns) - allowed_patterns
        if unknown_patterns:
            raise ValueError(
                "unknown injection pattern(s): " + ", ".join(sorted(unknown_patterns))
            )
        if len(set(normalized_patterns)) != len(normalized_patterns):
            raise ValueError("S-curve injection_patterns must not contain duplicates")
        self.injection_patterns = normalized_patterns
        if self.injection_capacitance_f <= 0:
            raise ValueError("injection_capacitance_f must be positive")
        if not 0 <= self.injection_capacitance_relative_uncertainty < 1:
            raise ValueError(
                "injection_capacitance_relative_uncertainty must be in [0, 1)"
            )
        if (
            not isinstance(self.minimum_reference_code, int)
            or isinstance(self.minimum_reference_code, bool)
            or not 0 <= self.minimum_reference_code <= 1023
        ):
            raise ValueError("minimum_reference_code must be an integer in 0..1023")
        for name, value in (
            ("minimum_reference_voltage_v", self.minimum_reference_voltage_v),
            ("preferred_reference_common_mode_v", self.preferred_reference_common_mode_v),
        ):
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite when supplied")
        if self.maximum_reference_step_error_v is not None and (
            not math.isfinite(float(self.maximum_reference_step_error_v))
            or float(self.maximum_reference_step_error_v) < 0
        ):
            raise ValueError(
                "maximum_reference_step_error_v must be finite and >= 0 when supplied"
            )
        if self.coarse_step <= 0 or self.fine_step <= 0:
            raise ValueError("S-curve DAC steps must be positive")
        if self.fine_margin_codes < 0 or self.expand_codes < 1:
            raise ValueError("S-curve margins must be non-negative")
        if self.max_expand_rounds < 0:
            raise ValueError("max_expand_rounds must be >= 0")


@dataclass
class AnalysisSettings:
    noise_min_points: int = 5
    gaussian_min_r2: float = 0.70
    max_asymmetry_ratio: float = 2.5
    plateau_sigma_factor: float = 1.0
    representative_pixels: int = 6
    # Explicit physical (column, row) coordinates for dedicated noise/S-curve plots.
    plot_pixels: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    # Empty means all acquired patterns. This filters plots, not processed tables.
    plot_injection_patterns: tuple[str, ...] = field(default_factory=tuple)
    # Creating 32 full-resolution maps is intentionally opt-in.
    plot_all_trim_heatmaps: bool = False
    plot_dpi: int = 300
    save_pdf_plots: bool = True

    def validate(self) -> None:
        if self.noise_min_points < 4:
            raise ValueError("noise_min_points must be at least 4")
        if not -1 <= self.gaussian_min_r2 <= 1:
            raise ValueError("gaussian_min_r2 must be in [-1, 1]")
        if self.max_asymmetry_ratio <= 1:
            raise ValueError("max_asymmetry_ratio must exceed 1")
        if self.plateau_sigma_factor < 0:
            raise ValueError("plateau_sigma_factor must be >= 0")
        if self.representative_pixels < 1:
            raise ValueError("representative_pixels must be positive")
        normalized_pixels: list[tuple[int, int]] = []
        for coordinate in self.plot_pixels:
            if not isinstance(coordinate, (tuple, list)) or len(coordinate) != 2:
                raise TypeError("analysis plot_pixels entries must be (column, row)")
            column, row = coordinate
            if (
                not isinstance(column, int)
                or isinstance(column, bool)
                or column not in OWNED_COLUMNS
            ):
                raise ValueError("analysis plot pixel column is outside owned columns")
            if not isinstance(row, int) or isinstance(row, bool) or not 0 <= row < MATRIX_ROWS:
                raise ValueError("analysis plot pixel row must be in 0..31")
            pair = (int(column), int(row))
            if pair not in normalized_pixels:
                normalized_pixels.append(pair)
        self.plot_pixels = tuple(normalized_pixels)
        allowed_patterns = {"all", "tile_2x2", "tile_4x4", "tile_8x8"}
        normalized_patterns = tuple(
            str(value).strip().lower() for value in self.plot_injection_patterns
        )
        unknown_patterns = set(normalized_patterns) - allowed_patterns
        if unknown_patterns:
            raise ValueError(
                "unknown analysis plot injection pattern(s): "
                + ", ".join(sorted(unknown_patterns))
            )
        if len(set(normalized_patterns)) != len(normalized_patterns):
            raise ValueError("analysis plot_injection_patterns must not contain duplicates")
        self.plot_injection_patterns = normalized_patterns
        if self.plot_dpi < 72:
            raise ValueError("plot_dpi must be at least 72")
        if not isinstance(self.plot_all_trim_heatmaps, bool):
            raise TypeError("plot_all_trim_heatmaps must be bool")


@dataclass
class CharacterizationSettings:
    noise: NoiseScanSettings = field(default_factory=NoiseScanSettings)
    equalization: EqualizationSettings = field(default_factory=EqualizationSettings)
    scurve: ScurveSettings = field(default_factory=ScurveSettings)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)

    def validate(self) -> None:
        self.noise.validate()
        self.equalization.validate()
        self.scurve.validate()
        self.analysis.validate()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def merge_settings(
    base: CharacterizationSettings,
    updates: Mapping[str, Any] | None,
) -> CharacterizationSettings:
    """Small helper for callers loading settings from JSON-like mappings."""

    if not updates:
        base.validate()
        return base
    unknown = set(updates) - {"noise", "equalization", "scurve", "analysis"}
    if unknown:
        raise KeyError(f"unknown settings group(s): {', '.join(sorted(unknown))}")
    for group_name, group_updates in updates.items():
        group = getattr(base, group_name)
        if not isinstance(group_updates, Mapping):
            raise TypeError(f"settings.{group_name} must be an object")
        for name, value in group_updates.items():
            if not hasattr(group, name):
                raise KeyError(f"unknown setting {group_name}.{name}")
            setattr(group, name, value)
    base.validate()
    return base
