from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .analysis import analyze_saved_experiment, analyze_saved_noise_statistics
from .models import AnalysisSettings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Офлайн-анализ сохраненного эксперимента компараторов."
    )
    parser.add_argument(
        "experiment", type=Path, help="Каталог с metadata.json или noise_statistics.csv (также допустим сам CSV)"
    )
    parser.add_argument(
        "--bad-pixels", type=Path, default=None,
        help="Дополнительная маска CSV/JSON: bad=True исключает пиксель из анализа; raw не изменяется.",
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
    parser.add_argument("--workers", type=int, default=0,
                        help="Процессы анализа: 0=авто (до 8), 1=последовательно.")
    parser.add_argument("--plot-workers", type=int, default=0,
                        help="Процессы PNG/PDF: 0=авто (до 4), 1=последовательно.")
    parser.add_argument("--read-workers", type=int, default=0,
                        help="Потоки чтения CSV: 0=авто (до 8), 1=последовательно.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    analysis_settings = AnalysisSettings(
        workers=args.workers, plot_workers=args.plot_workers, read_workers=args.read_workers,
        representative_pixels=args.representative_pixels,
        plot_pixels=tuple(tuple(pixel) for pixel in (args.pixel or ())),
        plot_injection_patterns=tuple(args.pattern or ()),
        plot_all_trim_heatmaps=args.all_trim_heatmaps,
        plot_dpi=args.dpi,
        save_pdf_plots=not args.no_pdf,
    )
    common = dict(
        settings=analysis_settings, target_voltage=args.target_voltage,
        bad_pixel_map=args.bad_pixels, generate_plots=not args.no_plots,
    )
    if (args.experiment / "metadata.json").is_file():
        outputs = analyze_saved_experiment(args.experiment, n_injections=args.n_injections, **common)
    else:
        if args.n_injections is not None:
            parser.error("Для noise_statistics.csv число S-curve инжекций не применяется")
        outputs = analyze_saved_noise_statistics(args.experiment, **common)
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
