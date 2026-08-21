import argparse
from pathlib import Path
import sys

from PyQt6.QtCore import QLocale
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from gui.main_window import MainWindow
from gui.styles import get_app_qss, normalize_theme


APP_THEME = "light"


def _parse_theme(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--theme", choices=("dark", "light"), default=APP_THEME)
    args, _ = parser.parse_known_args(argv[1:])
    return normalize_theme(args.theme)


def main(theme: str | None = None):
    selected_theme = normalize_theme(theme or _parse_theme(sys.argv))

    # Give Windows a stable application identity so the taskbar uses our
    # application icon rather than grouping the process under python.exe.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "KPX.HPD.ChipTestStand"
            )
        except Exception:
            pass

    # Force engineering-style decimal point input regardless of Windows locale.
    # This makes values such as 2.5 and 20e-9 valid in every FloatEdit.
    QLocale.setDefault(QLocale.c())

    app = QApplication(sys.argv)
    app.setApplicationName("Chip Test Stand")

    icon_path = Path(__file__).resolve().parent / "gui" / "app_icon.svg"
    app_icon = QIcon(str(icon_path))
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    app.setProperty("chipTestTheme", selected_theme)
    app.setStyleSheet(get_app_qss(selected_theme))

    window = MainWindow()
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
