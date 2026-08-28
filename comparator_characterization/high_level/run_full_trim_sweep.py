"""Полный noise scan для каждого равномерного trim-кода 0..31."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import characterize_comparator

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    config.require_hardware_run_enabled()
    settings = config.build_settings(scan_all_trim_codes=True)
    with config.build_upo_client() as client:
        result = characterize_comparator(
            client,
            config.threshold_calibration_files(),
            window=config.WINDOW,
            pixels=config.PIXELS,
            base_pixel_config=config.base_pixel_config_path(),
            results_root=config.RESULTS_ROOT,
            settings=settings,
            run_noise_scan=True,
            run_equalization=True,
            run_scurve=False,
        )
    print(f"Полный trim-sweep завершен: {result.experiment_path}")


if __name__ == "__main__":
    main()
