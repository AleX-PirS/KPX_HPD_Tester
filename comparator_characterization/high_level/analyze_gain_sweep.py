"""GAIN-карты по завершенному свипу и опциональная аппаратная проверка."""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import GainEqualizationSettings, analyze_gain_sweep
from comparator_characterization.high_level import characterization_config as config
from comparator_characterization.storage import atomic_write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", nargs="?", type=Path)
    parser.add_argument("--targets", nargs="+", type=int)
    parser.add_argument("--window", choices=("AB", "BC", "CD", "ALL"), default=config.RUN.window)
    parser.add_argument("--no-plots", action="store_true")
    arguments = parser.parse_args()
    source = arguments.experiment or config.PATHS.gain_sweep_source
    if source is None:
        raise ValueError("Заполните GAIN_SWEEP_SOURCE в .env или укажите путь в командной строке")
    config.validate_configuration("gain_equalization", source_override=Path(source), require_hardware=True)
    config.configure_runtime_logging()
    result = analyze_gain_sweep(
        source,
        target_gains=arguments.targets if arguments.targets is not None else config.GAIN_EQUALIZATION.target_codes,
        window=arguments.window,
        equalization_settings=copy.deepcopy(config.metadata.GAIN_EQUALIZATION),
        settings=config.build_analysis_settings(),
        generate_plots=config.PLOTS.generate and not arguments.no_plots,
    )
    snapshot = config.configuration_snapshot("gain_equalization")
    snapshot["actual_source"] = str(Path(source).resolve())
    snapshot["actual_target_codes"] = arguments.targets if arguments.targets is not None else config.GAIN_EQUALIZATION.target_codes
    snapshot["actual_window"] = arguments.window
    atomic_write_json(Path(result["analysis_directory"]) / "configuration_v2.json", snapshot)
    print(f"GAIN-анализ: {result['analysis_directory']}")
    for target, path in result["gain_maps"].items():
        print(f"GAIN-карта {target}: {path}")
    print(f"Отчет: {result['report']}")
    if config.GAIN_EQUALIZATION.check_map:
        config.require_hardware_run_enabled()
        from comparator_characterization.gain_verification import prepare_gain_verification, verify_gain_equalization
        calibration_files = config.threshold_calibration_files()
        prepared = prepare_gain_verification(
            result, calibration_files, settings=config.build_settings(),
            all_windows=config.GAIN_EQUALIZATION.check_all_windows,
            reference_window=config.GAIN_EQUALIZATION.map_reference_window,
            allow_unresolved=config.metadata.GAIN_CHECK_ALLOW_UNRESOLVED,
            background_mode=config.metadata.GAIN_CHECK_BACKGROUND_MODE,
        )
        # Reject acquisition timing changes BEFORE any device is opened.
        for job in prepared[1]:
            source = job.metadata.get("test_injection_configuration", {})
            expected_ctrl = "MGPDLab_UPO_PWM" if config.normalized_ctrl_source() == "upo_pwm" else "Keysight_or_custom_executor"
            if source.get("ctrl_source") != expected_ctrl:
                raise ValueError("Источник CTRL проверки должен совпадать с исходным свипом")
            if job.metadata["run_options"]["initialization_fclk_mhz"] != config.ACQUISITION.main_fclk_mhz:
                raise ValueError("ACQUISITION.main_fclk_mhz проверки должен совпадать с исходным свипом")
            if config.normalized_ctrl_source() == "upo_pwm":
                saved = job.metadata.get("acquisition_sequence", {}).get("upo_pwm_settings", {})
                current = config.build_upo_pwm_settings()
                if saved.get("frequency_khz") != current.frequency_khz or saved.get("high_time_ns") != current.high_time_ns:
                    raise ValueError("Частота/длительность PWM проверки должны совпадать с исходным свипом")
        exposure = prepared[1][0].settings.scurve.shutter_duration_s
        print(f"Реальная проверка: {len(prepared[1])} проходов, УПО shutter={exposure:g} с для noise и S-кривых")
        with (config.build_generator() as generator, config.build_oscilloscope() as oscilloscope,
              config.build_upo_client() as client):
            hardware = config.injection_hardware_arguments(generator)
            if config.REFERENCE.verify_with_scope:
                hardware.update(
                    reference_step_oscilloscope=oscilloscope,
                    reference_step_verification_settings=config.build_reference_verification_settings(),
                    reference_verification_pwm_frequency_khz=config.ACQUISITION.pwm_frequency_khz,
                    reference_verification_pwm_high_time_ns=config.ACQUISITION.pwm_high_time_ns,
                )
            checked = verify_gain_equalization(
                client, calibration_files, prepared=prepared, hardware_arguments=hardware,
                generate_plots=config.PLOTS.generate and not arguments.no_plots,
                configuration_metadata=snapshot,
            )
        print(f"Реальная проверка GAIN: {checked['verification_directory']}")
        print(f"Отчет измерений: {checked['report']}")


if __name__ == "__main__":
    main()
