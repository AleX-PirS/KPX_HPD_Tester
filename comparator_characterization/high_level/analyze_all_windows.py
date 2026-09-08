"""Повторный offline-анализ родительского эксперимента WINDOW='ALL'."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import AnalysisSettings, analyze_all_windows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Совместный offline-анализ AB/BC/CD по родительской папке ALL."
    )
    parser.add_argument("experiment", type=Path)
    parser.add_argument(
        "--square-pixels", action="store_true",
        help="Физически квадратные пиксели и прямоугольная половина 16x32.",
    )
    parser.add_argument(
        "--reuse-child-analysis", action="store_true",
        help="Не создавать новые offline-анализы AB/BC/CD.",
    )
    parser.add_argument(
        "--no-plots", action="store_true",
        help="Пересчитать только CSV и REPORT.md без PNG/PDF.",
    )
    parser.add_argument("--no-pdf", action="store_true")
    parser.add_argument("--dpi", type=int, default=300)
    arguments = parser.parse_args()
    outputs = analyze_all_windows(
        arguments.experiment,
        settings=AnalysisSettings(
            square_physical_pixels=arguments.square_pixels,
            save_pdf_plots=not arguments.no_pdf,
            plot_dpi=arguments.dpi,
        ),
        reanalyze_children=not arguments.reuse_child_analysis,
        generate_plots=not arguments.no_plots,
    )
    print(f"Совместный анализ: {outputs['analysis_directory']}")
    print(f"Отчет: {outputs['report']}")


if __name__ == "__main__":
    main()
