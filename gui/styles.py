from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QApplication


THEMES = {
    "dark": {
        "window": "#0f141b",
        "sidebar": "#111820",
        "header": "#121a23",
        "card": "#151e28",
        "border": "#2b3948",
        "border_soft": "#263140",
        "text": "#e7edf5",
        "text_strong": "#f3f7fb",
        "muted": "#8fa1b5",
        "input": "#0f161f",
        "input_border": "#334254",
        "input_focus": "#4f8cff",
        "button": "#1b2734",
        "button_hover": "#223246",
        "button_pressed": "#192433",
        "neutral_button": "#3b4652",
        "neutral_button_hover": "#485563",
        "button_disabled": "#151b23",
        "disabled_text": "#627183",
        "primary": "#2563eb",
        "primary_hover": "#2f72f2",
        "table": "#101720",
        "table_alt": "#131c26",
        "table_grid": "#23303e",
        "table_header": "#18222d",
        "table_header_text": "#9fb0c3",
        "selection": "#214b82",
        "selection_text": "#ffffff",
        "log": "#0c1219",
        "tooltip": "#1c2733",
        "modified_bg": "#332a17",
        "invalid_bg": "#351b22",
        "ok": "#47d18c",
        "warning": "#f0b84b",
        "error": "#ff667a",
        "debug": "#7f91a6",
        "log_info": "#b8c7d9",
        "log_message": "#d5dfeb",
        "log_time": "#68798d",
        "badge_off_fg": "#6f7d8d",
        "badge_off_bg": "#18202a",
        "badge_ok_fg": "#47d18c",
        "badge_ok_bg": "#163226",
        "badge_busy_fg": "#f0b84b",
        "badge_busy_bg": "#362b16",
        "badge_error_fg": "#ff667a",
        "badge_error_bg": "#3a1d25",
        "output_on_bg": "#167a4e",
        "output_off_bg": "#a42d42",
        "output_unknown_bg": "#334155",
    },
    "light": {
        "window": "#f4f6f8",
        "sidebar": "#eef1f4",
        "header": "#ffffff",
        "card": "#ffffff",
        "border": "#d6dde5",
        "border_soft": "#dce2e8",
        "text": "#253142",
        "text_strong": "#17202d",
        "muted": "#66758a",
        "input": "#ffffff",
        "input_border": "#c8d1dc",
        "input_focus": "#3676dc",
        "button": "#f3f5f7",
        "button_hover": "#e9edf2",
        "button_pressed": "#dfe5ec",
        "neutral_button": "#d4d9df",
        "neutral_button_hover": "#c6cdd5",
        "button_disabled": "#f1f3f5",
        "disabled_text": "#9aa5b1",
        "primary": "#2563eb",
        "primary_hover": "#1d57cf",
        "table": "#ffffff",
        "table_alt": "#f7f9fb",
        "table_grid": "#e3e8ee",
        "table_header": "#edf1f5",
        "table_header_text": "#4b5b70",
        "selection": "#d9e8ff",
        "selection_text": "#17202d",
        "log": "#f8fafc",
        "tooltip": "#ffffff",
        "modified_bg": "#fff4d8",
        "invalid_bg": "#ffe8ec",
        "ok": "#168653",
        "warning": "#a86800",
        "error": "#c83c50",
        "debug": "#718096",
        "log_info": "#40516a",
        "log_message": "#26354a",
        "log_time": "#7a8798",
        "badge_off_fg": "#68778a",
        "badge_off_bg": "#f3f5f7",
        "badge_ok_fg": "#16784b",
        "badge_ok_bg": "#e7f6ee",
        "badge_busy_fg": "#996400",
        "badge_busy_bg": "#fff5d8",
        "badge_error_fg": "#bd3b4d",
        "badge_error_bg": "#ffe9ed",
        "output_on_bg": "#168653",
        "output_off_bg": "#c43d50",
        "output_unknown_bg": "#7b8794",
    },
}


def normalize_theme(theme: str) -> str:
    name = str(theme).strip().lower()
    aliases = {
        "dark": "dark",
        "night": "dark",
        "light": "light",
        "neutral": "light",
    }
    if name not in aliases:
        raise ValueError(
            f"Unknown GUI theme '{theme}'. Available themes: dark, light."
        )
    return aliases[name]


def theme_colors(theme: str) -> dict[str, str]:
    return THEMES[normalize_theme(theme)]


def current_theme_name() -> str:
    app = QApplication.instance()
    if app is None:
        return "dark"
    value = app.property("chipTestTheme")
    return normalize_theme(value or "dark")


def current_theme_colors() -> dict[str, str]:
    return theme_colors(current_theme_name())


def get_app_qss(theme: str = "dark") -> str:
    c = theme_colors(theme)
    checkmark = (Path(__file__).resolve().parent / "checkmark.svg").as_posix()
    return f"""
QMainWindow {{
    background: {c['window']};
}}

QWidget {{
    color: {c['text']};
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 10pt;
}}

QWidget#AppRoot {{
    background: {c['window']};
}}

/* Child text widgets must stay transparent.  A background on every QWidget
   creates dark/light rectangles behind QLabel text inside cards. */
QLabel, QCheckBox, QRadioButton {{
    background: transparent;
}}

QFrame#Sidebar {{
    background: {c['sidebar']};
    border-right: 1px solid {c['border_soft']};
}}

QFrame#Header, QFrame#LogPanel {{
    background: {c['header']};
    border: 1px solid {c['border_soft']};
    border-radius: 10px;
}}

QFrame#Card {{
    background: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}

QLabel#Title {{
    font-size: 18pt;
    font-weight: 650;
    color: {c['text_strong']};
}}

QLabel#SectionTitle {{
    font-size: 12pt;
    font-weight: 650;
    color: {c['text_strong']};
}}

QLabel#Muted {{
    color: {c['muted']};
}}

QPushButton {{
    background: {c['button']};
    border: 1px solid {c['input_border']};
    border-radius: 7px;
    padding: 7px 12px;
    color: {c['text']};
}}
QPushButton:hover {{
    background: {c['button_hover']};
    border-color: {c['input_focus']};
}}
QPushButton:pressed {{
    background: {c['button_pressed']};
}}
QPushButton:disabled {{
    color: {c['disabled_text']};
    background: {c['button_disabled']};
    border-color: {c['border_soft']};
}}

QPushButton#PrimaryButton {{
    background: {c['primary']};
    border-color: {c['primary']};
    color: white;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background: {c['primary_hover']};
}}

QPushButton#NeutralButton {{
    background: {c['neutral_button']};
    border-color: {c['input_border']};
    color: {c['text_strong']};
    font-weight: 600;
}}
QPushButton#NeutralButton:hover {{
    background: {c['neutral_button_hover']};
}}

QPushButton#DangerButton {{
    background: {c['output_off_bg']};
    border-color: {c['output_off_bg']};
    color: white;
    font-weight: 600;
}}
QPushButton#DangerButton:hover {{
    background: {c['error']};
    border-color: {c['error']};
}}

QPushButton#NavButton {{
    text-align: left;
    padding: 10px 14px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: {c['muted']};
}}
QPushButton#NavButton:hover {{
    background: {c['button_hover']};
    color: {c['text_strong']};
}}
QPushButton#NavButton:checked {{
    background: {c['selection']};
    color: {c['selection_text']};
    border-left: 3px solid {c['input_focus']};
}}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {c['input']};
    color: {c['text']};
    border: 1px solid {c['input_border']};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {c['primary']};
    selection-color: white;
}}
QSpinBox, QDoubleSpinBox {{
    padding-right: 28px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid {c['input_border']};
    border-bottom: 1px solid {c['input_border']};
    border-top-right-radius: 6px;
    background: {c['button']};
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 24px;
    border-left: 1px solid {c['input_border']};
    border-top: 1px solid {c['input_border']};
    border-bottom-right-radius: 6px;
    background: {c['button']};
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {c['button_hover']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {c['input_focus']};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {c['disabled_text']};
    background: {c['button_disabled']};
}}
QComboBox::drop-down {{
    border: 0;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: {c['card']};
    color: {c['text']};
    border: 1px solid {c['input_border']};
    selection-background-color: {c['selection']};
    selection-color: {c['selection_text']};
}}

QCheckBox {{
    spacing: 7px;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border: 2px solid {c['input_border']};
    border-radius: 4px;
    background: {c['input']};
}}
QCheckBox::indicator:hover {{
    border-color: {c['input_focus']};
}}
QCheckBox::indicator:checked {{
    background: {c['primary']};
    border-color: {c['primary']};
    image: url("{checkmark}");
}}
QCheckBox::indicator:disabled {{
    background: {c['button_disabled']};
    border-color: {c['border_soft']};
}}

/* Explicitly preserve the original AMUX checkbox styling inside the new
   combined Visualize tab. Keeping this selector local prevents future tab
   styling from changing the signal-selection controls. */
QCheckBox[amuxLegacy="true"] {{
    spacing: 7px;
    padding: 0px;
}}
QCheckBox[amuxLegacy="true"]::indicator {{
    width: 17px;
    height: 17px;
    border: 2px solid {c['input_border']};
    border-radius: 4px;
    background: {c['input']};
}}
QCheckBox[amuxLegacy="true"]::indicator:hover {{
    border-color: {c['input_focus']};
}}
QCheckBox[amuxLegacy="true"]::indicator:checked {{
    background: {c['primary']};
    border-color: {c['primary']};
    image: url("{checkmark}");
}}
QCheckBox[amuxLegacy="true"]::indicator:disabled {{
    background: {c['button_disabled']};
    border-color: {c['border_soft']};
}}

/* Large segmented selector for AMUX / Matrix sweep modes. */
QTabWidget#SweepModeTabs::pane {{
    border: 1px solid {c['border']};
    border-radius: 9px;
    background: transparent;
    top: -1px;
}}
QTabWidget#SweepModeTabs QTabBar::tab {{
    min-height: 40px;
    padding: 8px 18px;
    margin: 0px 3px 5px 0px;
    background: {c['button']};
    color: {c['text']};
    border: 1px solid {c['input_border']};
    border-radius: 8px;
    font-weight: 650;
}}
QTabWidget#SweepModeTabs QTabBar::tab:hover:!selected {{
    background: {c['button_hover']};
    border-color: {c['input_focus']};
}}
QTabWidget#SweepModeTabs QTabBar::tab:selected {{
    background: {c['primary']};
    color: white;
    border-color: {c['primary']};
    font-weight: 700;
}}
QTabWidget#SweepModeTabs QTabBar::tab:disabled {{
    background: {c['button_disabled']};
    color: {c['disabled_text']};
    border-color: {c['border_soft']};
}}

QPushButton[outputState="on"] {{
    background: {c['output_on_bg']};
    border-color: {c['output_on_bg']};
    color: white;
    font-weight: 700;
}}
QPushButton[outputState="on"]:hover {{
    background: {c['ok']};
    border-color: {c['ok']};
}}
QPushButton[outputState="off"] {{
    background: {c['output_off_bg']};
    border-color: {c['output_off_bg']};
    color: white;
    font-weight: 700;
}}
QPushButton[outputState="off"]:hover {{
    background: {c['error']};
    border-color: {c['error']};
}}
QPushButton[outputState="unknown"] {{
    background: {c['output_unknown_bg']};
    border-color: {c['output_unknown_bg']};
    color: white;
    font-weight: 650;
}}

QTableWidget {{
    background: {c['table']};
    alternate-background-color: {c['table_alt']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    gridline-color: {c['table_grid']};
    selection-background-color: {c['selection']};
    selection-color: {c['selection_text']};
}}
QTableWidget:disabled {{
    color: {c['text']};
}}
QHeaderView::section {{
    background: {c['table_header']};
    color: {c['table_header_text']};
    border: 0;
    border-bottom: 1px solid {c['border']};
    padding: 7px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background: {c['table_header']};
    border: 0;
}}

QTextEdit {{
    background: {c['log']};
    color: {c['text']};
    border: 1px solid {c['border_soft']};
    border-radius: 7px;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 9pt;
}}

QLabel#PreviewArea, QStackedWidget#VisualizationPreview {{
    background: {c['table']};
    color: {c['muted']};
    border: 1px solid {c['border_soft']};
    border-radius: 8px;
}}

QProgressBar {{
    background: {c['input']};
    color: {c['text']};
    border: 1px solid {c['input_border']};
    border-radius: 6px;
    min-height: 20px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {c['primary']};
    border-radius: 5px;
}}

QScrollArea {{
    border: 0;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {c['input_border']};
    min-height: 28px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c['muted']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {c['input_border']};
    min-width: 28px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {c['muted']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

QSplitter::handle {{
    background: {c['border_soft']};
    height: 2px;
}}

QLabel#MatrixWarning {{
    background: {c['badge_busy_bg']};
    color: {c['badge_busy_fg']};
    border: 1px solid {c['badge_busy_fg']};
    border-radius: 7px;
    padding: 8px 10px;
}}

QLabel#MatrixLegendUnknown {{
    background: {c['table_alt']};
    color: {c['text']};
    border: 1px solid {c['input_border']};
    border-radius: 5px;
    padding: 3px 7px;
    font-weight: 600;
}}
QLabel#MatrixLegendLocal {{
    background: {c['warning']};
    color: {c['window']};
    border-radius: 5px;
    padding: 3px 7px;
    font-weight: 600;
}}
QLabel#MatrixLegendStaged {{
    background: {c['primary']};
    color: white;
    border-radius: 5px;
    padding: 3px 7px;
    font-weight: 600;
}}
QLabel#MatrixLegendWritten {{
    background: {c['ok']};
    color: white;
    border-radius: 5px;
    padding: 3px 7px;
    font-weight: 600;
}}

QToolTip {{
    background: {c['tooltip']};
    color: {c['text_strong']};
    border: 1px solid {c['input_border']};
    padding: 5px;
}}
"""


# Backward-compatible name for code that imported APP_QSS directly.
APP_QSS = get_app_qss("dark")
