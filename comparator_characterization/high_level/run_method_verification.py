"""Фиксированные trim-карты, новые noise-сканы и два измеренных сигнальных края."""
from __future__ import annotations

import copy
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization.method_verification import run_method_verification
from comparator_characterization.high_level import characterization_config as config


def main():
    config.configure_runtime_logging()
    config.validate_configuration("method_verification", require_hardware=True)
    settings = config.build_settings()
    options = copy.deepcopy(config.metadata.METHOD_VERIFICATION)
    options.repeat_scans = config.METHOD_VERIFICATION.repeat_scans
    windows = ("AB","BC","CD") if config.RUN.window.upper()=="ALL" else (config.RUN.window.upper(),)
    print(f"Верификация: {options.repeat_scans} новых полных скана, окна {', '.join(windows)}.")
    print("S-скан: шаг 1 во всем заданном диапазоне, paired background, без отсечения второго края.")
    with (config.build_generator() as generator, config.build_oscilloscope() as oscilloscope,
          config.build_upo_client() as client):
        hardware = {
            "initialization_fclk_mhz":config.ACQUISITION.main_fclk_mhz,
            "measurement_fclk_mhz":config.ACQUISITION.measurement_fclk_mhz,
            "eo_overrides":config.RUN.eo_overrides,
            **config.reference_hardware_arguments(oscilloscope,required_for_scurve=True),
            **config.gain_hardware_arguments(), **config.injection_hardware_arguments(generator)}
        result = run_method_verification(client,config.threshold_calibration_files(),
            settings=settings, method_settings=options, windows=windows, pixels=config.RUN.pixels,
            bad_pixel_map=config.PATHS.bad_pixel_mask, base_pixel_config=config.base_pixel_config(),
            results_root=config.PATHS.results_dir, trim_reference=config.PATHS.method_trim_reference,
            hardware_arguments=hardware,generate_plots=config.PLOTS.generate,
            configuration_metadata=config.configuration_snapshot("method_verification"))
    print(f"Эксперимент: {result['experiment_path']}")
    print(f"Верификация: {result['analysis_directory']}")
    return result


if __name__ == "__main__":
    main()
