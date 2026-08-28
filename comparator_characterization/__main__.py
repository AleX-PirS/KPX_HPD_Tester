from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analysis import analyze_saved_experiment
from .models import AnalysisSettings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Офлайн-анализ сохраненного эксперимента компараторов."
    )
    parser.add_argument(
        "experiment", type=Path, help="Каталог эксперимента с metadata.json"
    )
    parser.add_argument(
        "--target-voltage",
        type=float,
        default=None,
        help="Необязательная новая цель эквализации в вольтах.",
    )
    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="Пересчитать таблицы без построения графиков.",
    )
    parser.add_argument(
        "--n-injections",
        type=int,
        default=None,
        help=(
            "Проверка сохраненного физического числа инжекций. Для старых данных "
            "может заполнить отсутствующее значение, но не переопределяет измеренный запуск."
        ),
    )
    parser.add_argument(
        "--pixel",
        nargs=2,
        action="append",
        type=int,
        metavar=("COLUMN", "ROW"),
        help=(
            "Физический пиксель для отдельных графиков. Параметр можно повторять, "
            "например --pixel 16 0 --pixel 20 12."
        ),
    )
    parser.add_argument(
        "--representative-pixels",
        type=int,
        default=6,
        help="Число автоматически выбранных пикселей, если --pixel не задан.",
    )
    parser.add_argument(
        "--all-trim-heatmaps",
        action="store_true",
        help="Дополнительно построить отдельную heatmap для каждого trim-кода 0..31.",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        choices=("all", "tile_2x2", "tile_4x4", "tile_8x8"),
        help=(
            "Показывать графики только выбранного измеренного injection pattern. "
            "Параметр можно повторять; таблицы остаются полными."
        ),
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Сохранять только PNG, без дублирования графиков в PDF.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="Разрешение PNG, по умолчанию 300 dpi.",
    )
    args = parser.parse_args()
    analysis_settings = AnalysisSettings(
        representative_pixels=args.representative_pixels,
        plot_pixels=tuple(tuple(pixel) for pixel in (args.pixel or ())),
        plot_injection_patterns=tuple(args.pattern or ()),
        plot_all_trim_heatmaps=args.all_trim_heatmaps,
        plot_dpi=args.dpi,
        save_pdf_plots=not args.no_pdf,
    )
    outputs = analyze_saved_experiment(
        args.experiment,
        settings=analysis_settings,
        target_voltage=args.target_voltage,
        n_injections=args.n_injections,
        generate_plots=not args.no_plots,
    )
    serializable = {}
    for key, value in outputs.items():
        if isinstance(value, Path):
            serializable[key] = str(value)
        elif isinstance(value, dict):
            serializable[key] = {
                nested_key: [str(item) for item in nested_value]
                for nested_key, nested_value in value.items()
            }
        else:
            serializable[key] = value
    print(json.dumps(serializable, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
