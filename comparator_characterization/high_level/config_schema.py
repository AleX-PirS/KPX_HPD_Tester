"""Typed user-facing configuration blocks. No legacy setting aliases."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunConfig:
    test: str
    window: str
    pixels: Any
    hardware_enabled: bool
    resume: bool
    eo_overrides: dict | None


@dataclass
class ConnectionConfig:
    host: str
    port: int
    timeout_s: float
    oscilloscope_address: str | None
    oscilloscope_idn: str
    generator_address: str | None


@dataclass
class AcquisitionConfig:
    main_fclk_mhz: int
    measurement_fclk_mhz: int
    noise_shutter_s: float
    scurve_shutter_s: float
    ctrl_source: str
    pwm_frequency_khz: int
    pwm_high_time_ns: int
    burst_injections: int


@dataclass
class NoiseConfig:
    repeats: int
    coarse_range: tuple[int, int, int]
    manual_range: tuple[int, int, int] | None


@dataclass
class ScurveConfig:
    gain: int | list[int] | tuple[int, ...]
    gain_source: str
    repeats: int
    patterns: tuple[str, ...]
    background_mode: str
    tile_mode: str
    dac_range: tuple[int | None, int | None]


@dataclass
class ReferenceConfig:
    mode: str
    steps_mv: tuple[float, ...]
    manual_ref1: int
    manual_ref2: int
    manual_step_mv: float
    code_limits: tuple[int, int]
    lut_voltage_unit: str
    verify_with_scope: bool


@dataclass
class ClockNoiseConfig:
    fclk_mhz: tuple[int, ...]
    step_mv: float
    pattern: str


@dataclass
class GainEqualizationConfig:
    target_codes: list[int] | tuple[int, ...]
    check_map: bool
    check_all_windows: bool
    map_reference_window: str | None


@dataclass
class AllWindowsConfig:
    final_q_sweep: bool
    q_steps: int
    repeats: int
    pattern: str


@dataclass
class EoSweepConfig:
    grid: dict | None


@dataclass
class PlotConfig:
    language: str
    geometry: str
    pixels: tuple[tuple[int, int], ...]
    patterns: tuple[str, ...]
    representative_pixels: int
    dpi: int
    save_pdf: bool
    generate: bool


@dataclass
class DashboardConfig:
    port: int
    open_browser: bool
