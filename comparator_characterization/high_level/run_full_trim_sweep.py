"""Полный noise scan для каждого равномерного trim-кода 0..31."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import characterize_comparator

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    config.configure_runtime_logging()
    config.validate_configuration("trim_sweep", require_hardware=True)
    settings = config.build_settings(scan_all_trim_codes=True)
    with (
        config.build_oscilloscope() as oscilloscope,
        config.build_upo_client() as client,
    ):
        result = config.run_characterization(
            client,
            config.threshold_calibration_files(),
            test_mode="trim_sweep",
            window=config.RUN.window,
            pixels=config.RUN.pixels,
            bad_pixel_map=config.PATHS.bad_pixel_mask,
            base_pixel_config=config.base_pixel_config(),
            results_root=config.PATHS.results_dir,
            settings=settings,
            run_noise_scan=True,
            run_equalization=True,
            run_scurve=False,
            initialization_fclk_mhz=config.ACQUISITION.main_fclk_mhz,
            measurement_fclk_mhz=config.ACQUISITION.measurement_fclk_mhz,
            **config.reference_hardware_arguments(
                oscilloscope, required_for_scurve=False
            ),
        )
    print(f"Полный trim-sweep завершен: {result.experiment_path}")
    config.print_result_paths(result)


if __name__ == "__main__":
    main()
