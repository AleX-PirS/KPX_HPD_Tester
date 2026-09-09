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
    config.require_hardware_run_enabled()
    if config.EO_PARAMETER_GRID or config.RESUME_SWEEP is not None:
        raise ValueError(
            "run_clock_noise.py выполняет один быстрый FCLK-sweep; "
            "EO_PARAMETER_GRID и RESUME_SWEEP должны быть None"
        )
    settings = config.build_settings(
        injection_patterns=(config.CLOCK_NOISE_INJECTION_PATTERN,)
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
                config.CLOCK_NOISE_MEASUREMENT_FCLK_MHZ
            ),
            injection_pattern=config.CLOCK_NOISE_INJECTION_PATTERN,
            trim_reference_experiment=(
                config.CLOCK_NOISE_TRIM_REFERENCE_EXPERIMENT
            ),
            window=config.WINDOW,
            pixels=config.PIXELS,
            bad_pixel_map=config.BAD_PIXEL_MAP,
            base_pixel_config=config.base_pixel_config(),
            results_root=config.RESULTS_ROOT,
            settings=settings,
            gain_map=config.gain_map(),
            initialization_fclk_mhz=config.ASIC_MAIN_FCLK_MHZ,
            measurement_fclk_mhz=config.ASIC_MEASUREMENT_FCLK_MHZ,
            eo_overrides=config.EO_OVERRIDES,
            resume_experiment=config.RESUME_EXPERIMENT,
            **config.reference_hardware_arguments(
                oscilloscope,
                required_for_scurve=True,
                injection_steps_mv=(config.CLOCK_NOISE_INJECTION_STEP_MV,),
            ),
            **config.injection_hardware_arguments(generator),
        )
    print(f"FCLK noise test завершен: {result.experiment_path}")
    config.print_result_paths(result)


if __name__ == "__main__":
    main()
