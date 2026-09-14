"""Сравнение all, tile_2x2, tile_4x4 и tile_8x8."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import characterize_injection_crosstalk

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    config.configure_runtime_logging()
    config.validate_configuration("crosstalk", require_hardware=True)
    settings = config.build_settings(
        injection_patterns=("all", "tile_2x2", "tile_4x4", "tile_8x8")
    )
    with (
        config.build_generator() as generator,
        config.build_oscilloscope() as oscilloscope,
        config.build_upo_client() as client,
    ):
        result = characterize_injection_crosstalk(
            client,
            config.threshold_calibration_files(),
            noise_reference_experiment=config.noise_reference_path(),
            window=config.RUN.window,
            pixels=config.RUN.pixels,
            bad_pixel_map=config.PATHS.bad_pixel_mask,
            base_pixel_config=config.base_pixel_config(),
            results_root=config.PATHS.results_dir,
            settings=settings,
            generate_analysis_plots=config.PLOTS.generate,
            additional_metadata={"analysis_configuration_v2": config.configuration_snapshot("crosstalk")},
            initialization_fclk_mhz=config.ACQUISITION.main_fclk_mhz,
            measurement_fclk_mhz=config.ACQUISITION.measurement_fclk_mhz,
            eo_overrides=config.RUN.eo_overrides,
            resume_experiment=config.resume_experiment_path(),
            **config.reference_hardware_arguments(
                oscilloscope, required_for_scurve=True
            ),
            **config.gain_hardware_arguments(),
            **config.injection_hardware_arguments(generator),
        )
    print(f"Тест наводок завершен: {result.experiment_path}")
    config.print_recommendation_paths(result.analysis_path)


if __name__ == "__main__":
    main()
