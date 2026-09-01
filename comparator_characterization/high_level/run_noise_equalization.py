"""Запуск noise scan и оптимизированной эквализации trim-кодов."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import characterize_comparator

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    config.configure_runtime_logging()
    config.require_hardware_run_enabled()
    settings = config.build_settings(scan_all_trim_codes=False)
    with config.build_upo_client() as client:
        result = config.run_characterization(
            client,
            config.threshold_calibration_files(),
            window=config.WINDOW,
            pixels=config.PIXELS,
            bad_pixel_map=config.BAD_PIXEL_MAP,
            base_pixel_config=config.base_pixel_config(),
            results_root=config.RESULTS_ROOT,
            settings=settings,
            run_noise_scan=True,
            run_equalization=True,
            run_scurve=False,
            initialization_fclk_mhz=config.ASIC_INITIALIZATION_FCLK_MHZ,
        )
    print(f"Эксперимент завершен: {result.experiment_path}")
    config.print_result_paths(result)


if __name__ == "__main__":
    main()
