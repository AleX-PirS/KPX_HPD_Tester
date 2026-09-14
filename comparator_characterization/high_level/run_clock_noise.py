"""Быстрый автономный тест шума пикселей при разных FCLK измерения."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import characterize_measurement_clock_noise
from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    config.configure_runtime_logging()
    config.validate_configuration("clock_noise", require_hardware=True)
    settings = config.build_settings(
        injection_patterns=(config.CLOCK_NOISE.pattern,)
    )
    with (
        config.build_generator() as generator,
        config.build_oscilloscope() as oscilloscope,
        config.build_upo_client() as client,
    ):
        result = characterize_measurement_clock_noise(
            client,
            config.threshold_calibration_files(),
            measurement_fclk_values_mhz=(
                config.CLOCK_NOISE.fclk_mhz
            ),
            injection_pattern=config.CLOCK_NOISE.pattern,
            trim_reference_experiment=(
                config.PATHS.clock_trim_reference
            ),
            window=config.RUN.window,
            pixels=config.RUN.pixels,
            bad_pixel_map=config.PATHS.bad_pixel_mask,
            base_pixel_config=config.base_pixel_config(),
            results_root=config.PATHS.results_dir,
            settings=settings,
            generate_analysis_plots=config.PLOTS.generate,
            additional_metadata={"analysis_configuration_v2": config.configuration_snapshot("clock_noise")},
            initialization_fclk_mhz=config.ACQUISITION.main_fclk_mhz,
            measurement_fclk_mhz=config.ACQUISITION.measurement_fclk_mhz,
            eo_overrides=config.RUN.eo_overrides,
            resume_experiment=config.resume_experiment_path(),
            **config.reference_hardware_arguments(
                oscilloscope,
                required_for_scurve=True,
                injection_steps_mv=(config.CLOCK_NOISE.step_mv,),
            ),
            **config.gain_hardware_arguments(),
            **config.injection_hardware_arguments(generator),
        )
    print(f"FCLK noise test завершен: {result.experiment_path}")
    config.print_result_paths(result)


if __name__ == "__main__":
    main()
