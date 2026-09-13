"""Офлайн-карты усиления и индивидуальных GAIN-кодов по завершенному свипу."""

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


if __name__ == "__main__":
    main()
