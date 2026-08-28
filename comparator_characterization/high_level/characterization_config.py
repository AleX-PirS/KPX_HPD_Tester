"""Единый пользовательский файл настроек верхнего уровня.

Перед аппаратным запуском заполните пути и установите
``ENABLE_HARDWARE_RUN = True``. Импорт этого файла не подключается к приборам.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from comparator_characterization import (
    AnalysisSettings,
    CharacterizationSettings,
    KeysightBurstSettings,
    load_gain_map_csv,
)

if TYPE_CHECKING:
    from generator_cfg import TwoChannelGenerator
    from mgpd import MGPDClient


# Файл находится в comparator_characterization/high_level, поэтому корень
# проекта расположен на два уровня выше. Пути к calibration, configs и results
# остаются привязаны к корню проекта, а не к каталогу запускаемых файлов.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Защита от случайного запуска реального стенда.
ENABLE_HARDWARE_RUN = False

# Соединение с УПО / MGPDLab.
UPO_HOST = "127.0.0.1"
UPO_PORT = 0xBEEB
UPO_TIMEOUT_S = 8.0

# Генератор Keysight 81150A/81160A. None включает VISA-автопоиск.
GENERATOR_VISA_ADDRESS: str | None = None
GENERATOR_CHANNEL = 1
SHUTTER_START_DELAY_S = 0.5
POST_BURST_GUARD_S = 0.1

# Окно и пиксели. Возможные окна: AB, BC, CD.
WINDOW = "AB"
PIXELS: str | list[tuple[int, int]] = "all"

# JSON, сохраненный на странице Matrix. Здесь должны быть все исследуемые пиксели.
BASE_PIXEL_CONFIG: Path | None = None

# Полные измеренные характеристики пороговых ЦАП.
THRESHOLD_DAC_LUTS = {
    "DAC_CMP_A": PROJECT_ROOT / "calibration" / "DAC_CMP_A.csv",
    "DAC_CMP_B": PROJECT_ROOT / "calibration" / "DAC_CMP_B.csv",
    "DAC_CMP_C": PROJECT_ROOT / "calibration" / "DAC_CMP_C.csv",
    "DAC_CMP_D": PROJECT_ROOT / "calibration" / "DAC_CMP_D.csv",
}

# Полные измеренные характеристики REF. Допустимы ключи REF1/REF2.
REFERENCE_DAC_LUTS = {
    "REF1": PROJECT_ROOT / "calibration" / "REF1.csv",
    "REF2": PROJECT_ROOT / "calibration" / "REF2.csv",
}

# auto определяет V/mV/uV по имени столбца. Для общего имени Voltage явно
# задайте "V", "mV" или "uV", чтобы исключить неверную единицу.
REFERENCE_LUT_VOLTAGE_UNIT = "auto"

# Пользователь задает только требуемые положительные ступеньки REF1-REF2.
# Единица здесь mV. Скрипт выбирает измеренные LUT-точки и всегда требует
# физическое условие V_REF1 > V_REF2.
INJECTION_STEPS_MV = (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0)

# По умолчанию оба выбранных кода строго больше 400.
MINIMUM_REFERENCE_CODE = 401
# Это отдельное ограничение по измеренному напряжению, обычно оставляется None.
MINIMUM_REFERENCE_VOLTAGE_V: float | None = None
# Необязательная цель общего уровня (V_REF1 + V_REF2)/2.
PREFERRED_REFERENCE_COMMON_MODE_V = 0.7
# Максимально допустимая ошибка выбранной ступеньки, None отключает предел.
MAXIMUM_REFERENCE_STEP_ERROR_V: float | None = None

# CSV GAIN: столбцы column,row,gain с физическими координатами.
GAIN_MAP_CSV: Path | None = None

# Режим одного обычного S-curve запуска. Можно оставить ровно один режим или
# перечислить несколько. Сравнение всех четырех вынесено в run_crosstalk.py.
SCURVE_PATTERNS = ("all",)
N_INJECTIONS = 1000
NOISE_SHUTTER_DURATION_S = 0.001
SCURVE_SHUTTER_DURATION_S = 1.0
NOISE_REPEATS = 10

RESULTS_ROOT = PROJECT_ROOT / "results"
# Нужен для отдельного S-curve/crosstalk запуска. Укажите каталог завершенного
# noise+equalization эксперимента.
NOISE_REFERENCE_EXPERIMENT: Path | None = None

# Пиксели для подробных графиков задаются физическими (column, row).
# Пустой кортеж включает автоматический выбор типичных пикселей.
PLOT_PIXELS: tuple[tuple[int, int], ...] = ()
# Пустой кортеж строит графики всех измеренных режимов. Это не влияет на съем.
PLOT_SCURVE_PATTERNS: tuple[str, ...] = ()
REPRESENTATIVE_PIXEL_COUNT = 6
SAVE_PDF_PLOTS = True
PLOT_DPI = 300


def require_hardware_run_enabled() -> None:
    if not ENABLE_HARDWARE_RUN:
        raise RuntimeError(
            "Аппаратный запуск заблокирован. Проверьте файл "
            "comparator_characterization/high_level/characterization_config.py "
            "и установите ENABLE_HARDWARE_RUN = True."
        )


def _required_path(value: Path | None, description: str) -> Path:
    if value is None:
        raise ValueError(f"Не задан путь: {description}")
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(f"Не найден файл {description}: {path}")
    return path


def threshold_calibration_files() -> dict[str, Path]:
    return {
        name: _required_path(Path(path), f"LUT {name}")
        for name, path in THRESHOLD_DAC_LUTS.items()
    }


def reference_calibration_files() -> dict[str, Path]:
    return {
        name: _required_path(Path(path), f"LUT {name}")
        for name, path in REFERENCE_DAC_LUTS.items()
    }


def base_pixel_config_path() -> Path:
    return _required_path(BASE_PIXEL_CONFIG, "базовая Matrix-конфигурация")


def gain_map() -> dict[tuple[int, int], int]:
    path = _required_path(GAIN_MAP_CSV, "GAIN-карта")
    return load_gain_map_csv(path)


def noise_reference_path() -> Path:
    if NOISE_REFERENCE_EXPERIMENT is None:
        raise ValueError("Не задан NOISE_REFERENCE_EXPERIMENT")
    path = Path(NOISE_REFERENCE_EXPERIMENT)
    if not (path / "metadata.json").is_file():
        raise FileNotFoundError(f"Не найден завершенный noise-эксперимент: {path}")
    return path


def injection_voltage_steps_v() -> tuple[float, ...]:
    return tuple(float(value) * 1e-3 for value in INJECTION_STEPS_MV)


def build_settings(
    *,
    injection_patterns: tuple[str, ...] | None = None,
    scan_all_trim_codes: bool = False,
) -> CharacterizationSettings:
    settings = CharacterizationSettings()
    settings.noise.noise_repeats = NOISE_REPEATS
    settings.noise.shutter_duration_s = NOISE_SHUTTER_DURATION_S
    settings.equalization.scan_all_trim_codes = scan_all_trim_codes
    settings.scurve.n_injections = N_INJECTIONS
    settings.scurve.shutter_duration_s = SCURVE_SHUTTER_DURATION_S
    settings.scurve.injection_patterns = injection_patterns or SCURVE_PATTERNS
    settings.scurve.minimum_reference_code = MINIMUM_REFERENCE_CODE
    settings.scurve.minimum_reference_voltage_v = MINIMUM_REFERENCE_VOLTAGE_V
    settings.scurve.preferred_reference_common_mode_v = (
        PREFERRED_REFERENCE_COMMON_MODE_V
    )
    settings.scurve.maximum_reference_step_error_v = (
        MAXIMUM_REFERENCE_STEP_ERROR_V
    )
    settings.analysis = AnalysisSettings(
        representative_pixels=REPRESENTATIVE_PIXEL_COUNT,
        plot_pixels=PLOT_PIXELS,
        plot_injection_patterns=PLOT_SCURVE_PATTERNS,
        plot_dpi=PLOT_DPI,
        save_pdf_plots=SAVE_PDF_PLOTS,
    )
    settings.validate()
    return settings


def build_upo_client() -> "MGPDClient":
    from mgpd import MGPDClient

    return MGPDClient(host=UPO_HOST, port=UPO_PORT, timeout=UPO_TIMEOUT_S)


def build_generator() -> "TwoChannelGenerator":
    from generator_cfg import TwoChannelGenerator

    return TwoChannelGenerator(
        gen_address=GENERATOR_VISA_ADDRESS,
        max_amplitude_v=3.3,
        max_abs_level_v=3.3,
        min_output_v=0.0,
    )


def build_burst_settings() -> KeysightBurstSettings:
    return KeysightBurstSettings(
        channel=GENERATOR_CHANNEL,
        shutter_start_delay_s=SHUTTER_START_DELAY_S,
        post_burst_guard_s=POST_BURST_GUARD_S,
    )
