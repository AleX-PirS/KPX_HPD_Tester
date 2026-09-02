"""Локальная HTML-страница для настройки и сохранения графиков."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization.interactive_plots import serve_plot_dashboard
from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Открыть локальную страницу графиков готового эксперимента"
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="каталог эксперимента или analysis/vNNN",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="не открывать браузер автоматически",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.PLOT_DASHBOARD_PORT,
        help="локальный TCP-порт, 0 означает выбрать свободный",
    )
    arguments = parser.parse_args()
    source = arguments.path or config.PLOT_DASHBOARD_EXPERIMENT
    if source is None:
        parser.error(
            "укажите путь аргументом или задайте PLOT_DASHBOARD_EXPERIMENT "
            "в characterization_config.py"
        )
    serve_plot_dashboard(
        source,
        port=arguments.port,
        open_browser=(
            config.PLOT_DASHBOARD_OPEN_BROWSER and not arguments.no_browser
        ),
    )


if __name__ == "__main__":
    main()
