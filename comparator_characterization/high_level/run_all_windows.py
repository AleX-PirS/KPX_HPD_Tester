"""Полная связанная характеризация AB, BC и CD с финальным REF sweep."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    """Запустить WINDOW=ALL независимо от текущего значения config.WINDOW."""

    config.configure_runtime_logging()
    config.require_hardware_run_enabled()
    settings = config.build_settings()
    with (
        config.build_generator() as generator,
        config.build_oscilloscope() as oscilloscope,
        config.build_upo_client() as client,
    ):
        result = config.run_characterization(
            client,
            config.threshold_calibration_files(),
            window="ALL",
            pixels=config.PIXELS,
            bad_pixel_map=config.BAD_PIXEL_MAP,
            base_pixel_config=config.base_pixel_config(),
            results_root=config.RESULTS_ROOT,
            settings=settings,
            gain_map=config.gain_map(),
            run_noise_scan=True,
            run_equalization=True,
            run_scurve=True,
            initialization_fclk_mhz=config.ASIC_MAIN_FCLK_MHZ,
            measurement_fclk_mhz=config.ASIC_MEASUREMENT_FCLK_MHZ,
            **config.reference_hardware_arguments(
                oscilloscope, required_for_scurve=True
            ),
            **config.injection_hardware_arguments(generator),
        )
    print(f"Связанная характеризация трех окон завершена: {result.experiment_path}")
    config.print_result_paths(result)


if __name__ == "__main__":
    main()
