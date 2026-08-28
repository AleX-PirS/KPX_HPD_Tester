"""Сравнение all, tile_2x2, tile_4x4 и tile_8x8."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import characterize_injection_crosstalk

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    config.require_hardware_run_enabled()
    settings = config.build_settings(
        injection_patterns=("all", "tile_2x2", "tile_4x4", "tile_8x8")
    )
    with config.build_generator() as generator, config.build_upo_client() as client:
        result = characterize_injection_crosstalk(
            client,
            config.threshold_calibration_files(),
            noise_reference_experiment=config.noise_reference_path(),
            window=config.WINDOW,
            pixels=config.PIXELS,
            base_pixel_config=config.base_pixel_config_path(),
            results_root=config.RESULTS_ROOT,
            settings=settings,
            reference_calibration_files=config.reference_calibration_files(),
            injection_voltage_steps_v=config.injection_voltage_steps_v(),
            reference_calibration_voltage_unit=config.REFERENCE_LUT_VOLTAGE_UNIT,
            gain_map=config.gain_map(),
            keysight_generator=generator,
            keysight_burst_settings=config.build_burst_settings(),
        )
    print(f"Тест наводок завершен: {result.experiment_path}")


if __name__ == "__main__":
    main()
