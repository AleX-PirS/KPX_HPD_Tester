"""GAIN-карты по завершенному свипу и опциональная аппаратная проверка."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import GainEqualizationSettings, analyze_gain_sweep
from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", nargs="?", type=Path)
    parser.add_argument("--targets", nargs="+", type=int)
    parser.add_argument("--window", choices=("AB", "BC", "CD", "ALL"), default="ALL")
    parser.add_argument("--no-plots", action="store_true")
    arguments = parser.parse_args()
    source = arguments.experiment or config.GAIN_SWEEP_ANALYSIS_EXPERIMENT
    if source is None:
        raise ValueError("Укажите GAIN_SWEEP_ANALYSIS_EXPERIMENT в конфиге или путь в командной строке")
    config.configure_runtime_logging()
    result = analyze_gain_sweep(
        source,
        target_gains=arguments.targets if arguments.targets is not None else config.TARGET_GAIN,
        window=arguments.window,
        equalization_settings=GainEqualizationSettings(
            target_statistic=config.GAIN_EQUALIZATION_TARGET_STATISTIC,
            amplitude_weight=config.GAIN_EQUALIZATION_AMPLITUDE_WEIGHT,
            gain_weight=config.GAIN_EQUALIZATION_GAIN_WEIGHT,
            minimum_gain_fit_r2=config.GAIN_EQUALIZATION_MINIMUM_GAIN_FIT_R2,
            include_poor_fits=config.GAIN_EQUALIZATION_INCLUDE_POOR_FITS,
            allow_shared_noise_baseline=config.GAIN_EQUALIZATION_ALLOW_SHARED_NOISE_BASELINE,
        ),
        settings=config.build_settings().analysis,
        generate_plots=not arguments.no_plots,
    )
    print(f"GAIN-анализ: {result['analysis_directory']}")
    for target, path in result["gain_maps"].items():
        print(f"GAIN-карта {target}: {path}")
    print(f"Отчет: {result['report']}")
    if config.CHECK_EQ_GAIN_MAP:
        config.require_hardware_run_enabled()
        from comparator_characterization.gain_verification import prepare_gain_verification, verify_gain_equalization
        calibration_files = config.threshold_calibration_files()
        prepared = prepare_gain_verification(
            result, calibration_files, settings=config.build_settings(),
            all_windows=config.CHECK_EQ_GAIN_MAP_ALL_WINDOWS,
            reference_window=config.CHECK_EQ_GAIN_MAP_REFERENCE_WINDOW,
            allow_unresolved=config.CHECK_EQ_GAIN_MAP_ALLOW_UNRESOLVED,
            background_mode=config.CHECK_EQ_GAIN_MAP_BACKGROUND_MODE,
        )
        # Reject acquisition timing changes BEFORE any device is opened.
        for job in prepared[1]:
            source = job.metadata.get("test_injection_configuration", {})
            expected_ctrl = "MGPDLab_UPO_PWM" if config._normalized_ctrl_source() == "upo_pwm" else "Keysight_or_custom_executor"
            if source.get("ctrl_source") != expected_ctrl:
                raise ValueError("Источник CTRL проверки должен совпадать с исходным свипом")
            if job.metadata["run_options"]["initialization_fclk_mhz"] != config.ASIC_MAIN_FCLK_MHZ:
                raise ValueError("ASIC_MAIN_FCLK_MHZ проверки должен совпадать с исходным свипом")
            if config._normalized_ctrl_source() == "upo_pwm":
                saved = job.metadata.get("acquisition_sequence", {}).get("upo_pwm_settings", {})
                current = config.build_upo_pwm_settings()
                if saved.get("frequency_khz") != current.frequency_khz or saved.get("high_time_ns") != current.high_time_ns:
                    raise ValueError("Частота/длительность PWM проверки должны совпадать с исходным свипом")
        exposure = prepared[1][0].settings.scurve.shutter_duration_s
        print(f"Реальная проверка: {len(prepared[1])} проходов, УПО shutter={exposure:g} с для noise и S-кривых")
        with (config.build_generator() as generator, config.build_oscilloscope() as oscilloscope,
              config.build_upo_client() as client):
            hardware = config.injection_hardware_arguments(generator)
            if config.VERIFY_REFERENCE_STEPS_BEFORE_TEST:
                hardware.update(
                    reference_step_oscilloscope=oscilloscope,
                    reference_step_verification_settings=config.build_reference_verification_settings(),
                    reference_verification_pwm_frequency_khz=config.UPO_CTRL_FREQUENCY_KHZ,
                    reference_verification_pwm_high_time_ns=config.UPO_CTRL_HIGH_TIME_NS,
                )
            checked = verify_gain_equalization(
                client, calibration_files, prepared=prepared, hardware_arguments=hardware,
                generate_plots=not arguments.no_plots,
            )
        print(f"Реальная проверка GAIN: {checked['verification_directory']}")
        print(f"Отчет измерений: {checked['report']}")


if __name__ == "__main__":
    main()
