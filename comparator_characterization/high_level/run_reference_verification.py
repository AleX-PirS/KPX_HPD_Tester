"""Отдельная проверка всех REF-ступенек через AMUX и осциллограф."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    config.configure_runtime_logging()
    config.require_hardware_run_enabled()
    if not config.VERIFY_REFERENCE_STEPS_BEFORE_TEST:
        raise ValueError(
            "Для отдельного запуска установите "
            "VERIFY_REFERENCE_STEPS_BEFORE_TEST = True"
        )
    with (
        config.build_oscilloscope() as oscilloscope,
        config.build_upo_client() as client,
    ):
        result = config.run_characterization(
            client,
            config.threshold_calibration_files(),
            window=config.WINDOW,
            pixels=config.PIXELS,
            bad_pixel_map=config.BAD_PIXEL_MAP,
            base_pixel_config=config.base_pixel_config(),
            results_root=config.RESULTS_ROOT,
            settings=config.build_settings(),
            run_noise_scan=False,
            run_equalization=False,
            run_scurve=False,
            initialization_fclk_mhz=config.ASIC_INITIALIZATION_FCLK_MHZ,
            **config.reference_hardware_arguments(
                oscilloscope, required_for_scurve=False
            ),
        )
    print(f"Проверка REF завершена: {result.experiment_path}")
    print(
        "Raw CSV и сравнение CLK находятся в подпапке "
        "reference_verification последнего запуска."
    )


if __name__ == "__main__":
    main()
