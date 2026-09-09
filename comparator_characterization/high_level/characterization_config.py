"""Единый пользовательский файл настроек верхнего уровня.

Перед аппаратным запуском заполните пути и установите
``ENABLE_HARDWARE_RUN = True``. Импорт этого файла не подключается к приборам.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

from comparator_characterization import (
    AnalysisSettings,
    CharacterizationSettings,
    KeysightBurstSettings,
    ReferenceStepVerificationSettings,
    UpoPwmSettings,
    UpoPwmShotExecutor,
    load_gain_map_csv,
)

if TYPE_CHECKING:
    from generator_cfg import TwoChannelGenerator
    from mgpd import MGPDClient
    from oscilloscope_cfg import Oscilloscope


# Файл находится в comparator_characterization/high_level, поэтому корень
# проекта расположен на два уровня выше. Пути к calibration, configs и results
# остаются привязаны к корню проекта, а не к каталогу запускаемых файлов.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Защита от случайного запуска реального стенда.
ENABLE_HARDWARE_RUN = True

# None/{}: один обычный тест. Иначе декартово произведение значений EO_cfg.
# Например: {"DAC_CMP_BIAS_LSB": [200, 500], "DAC_CMP_VB5": [500, 1000]}.
# Каждая комбинация получает свои raw, noise, S-curve, trim-карты и plots.
EO_PARAMETER_GRID = None
EO_OVERRIDES = None  # Необязательные фиксированные EO-параметры одиночного теста.
RESUME_EXPERIMENT = None  # Папка одиночного эксперимента с metadata.json.
RESUME_SWEEP = None  # Папка всей серии с sweep.json; grid/настройки не менять.

# Соединение с УПО / MGPDLab.
UPO_HOST = "127.0.0.1"
UPO_PORT = 0xBEEB
UPO_TIMEOUT_S = 8.0
UPO_RECONNECT_ATTEMPTS = 3
UPO_RECONNECT_BACKOFF_S = 0.5

# Перед каждым аппаратным тестом все выбранные REF-ступеньки проверяются на
# осциллографе. CH1 = TST_SIG с AMUX, CH4 = CTRL. Оба входа DC, 1 МОм;
# trigger CH4, NEG, 0.5 V; развертка 500 нс/дел. Для каждой ступеньки
# сохраняются отдельные raw CSV при CLK OFF и CLK ON.
VERIFY_REFERENCE_STEPS_BEFORE_TEST = False
OSCILLOSCOPE_VISA_ADDRESS: str | None = None
OSCILLOSCOPE_IDN_SUBSTRING = "DSO9104H"
OSCILLOSCOPE_TIMEOUT_MS = 5_000
REFERENCE_SIGNAL_CHANNEL = 1
REFERENCE_TRIGGER_CHANNEL = 4
REFERENCE_TRIGGER_LEVEL_V = 0.5
REFERENCE_TIME_SCALE_S = 5e-7
REFERENCE_TIME_OFFSET_S = 0.0
REFERENCE_SIGNAL_SCALE_V = 0.2
REFERENCE_SIGNAL_OFFSET_V = 0.4
REFERENCE_TRIGGER_SCALE_V = 0.5
REFERENCE_TRIGGER_OFFSET_V = 1.5
REFERENCE_WAVEFORM_POINTS = 12_500
REFERENCE_SCOPE_ARM_DELAY_S = 0.10
REFERENCE_SCOPE_ACQUISITION_TIME_S = 0.25
REFERENCE_PLATEAU_GUARD_S = 1.5e-7
REFERENCE_PLATEAU_WINDOW_S = 1.0e-6
REFERENCE_MAXIMUM_SCOPE_STEP_ERROR_V = 1e-3
REFERENCE_VERIFICATION_CLK_ON_MHZ = 50
REFERENCE_VERIFICATION_RETRIES = 2
REFERENCE_VERIFICATION_RETRY_BACKOFF_S = 0.25
REFERENCE_VERIFICATION_ABORT_ON_FAILURE = True
REFERENCE_VERIFICATION_SAVE_SCREENSHOTS = False

# Основной FCLK используется при загрузке EO/PX, восстановлении после связи и
# GET_PIXEL. Измерительный FCLK включается непосредственно перед GET_SHOT,
# остается на все время блокирующей экспозиции и сразу после ответа заменяется
# основным до первого GET_PIXEL. Допустимые значения УПО:
# 0, 1, 5, 10, 25, 50, 75, 100, 125, 150 МГц.
ASIC_MAIN_FCLK_MHZ = 100
ASIC_MEASUREMENT_FCLK_MHZ = 5
# Совместимое имя для пользовательских файлов предыдущих версий.
ASIC_INITIALIZATION_FCLK_MHZ = ASIC_MAIN_FCLK_MHZ

# Основной источник CTRL: "upo_pwm". Резервный вариант: "keysight_burst".
# В UPO PWM параметр N_INJECTIONS игнорируется: число фронтов для анализа
# автоматически вычисляется из Freal и SCURVE_SHUTTER_DURATION_S.
CTRL_INJECTION_SOURCE = "upo_pwm"
UPO_CTRL_FREQUENCY_KHZ = 100
UPO_CTRL_HIGH_TIME_NS = 5_000
UPO_CTRL_EDGE_COUNT_UNCERTAINTY = 1

# Резервный внешний генератор Keysight 81150A/81160A.
# GENERATOR_VISA_ADDRESS=None включает VISA-автопоиск только при выборе
# CTRL_INJECTION_SOURCE="keysight_burst". В режиме upo_pwm VISA не открывается.
GENERATOR_VISA_ADDRESS: str | None = None
GENERATOR_CHANNEL = 1
SHUTTER_START_DELAY_S = 0.8
# ВАЖНО: задержка от вызова GET_SHOT, НЕ от открытия shutter. В GET_SHOT
# входит загрузка всей матрицы. Значение 0.8 с выбрано с запасом относительно
# наблюдавшейся загрузки около 0.63 с. Это временная оценка, а не измеренный
# сигнал shutter; окончательно проверьте взаимное положение SHUTTER/CTRL.
POST_BURST_GUARD_S = 0.1

# Окно и пиксели. Возможные окна: AB, BC, CD, ALL.
WINDOW = "ALL"
PIXELS: str | list[tuple[int, int]] = "all"
# None: использовать все выбранные пиксели. Или путь к CSV/JSON либо список
# физических (column, row): [(16, 0), (20, 7)]. True/1 в карте = ИСКЛЮЧИТЬ.
# Эти пиксели всегда получают MASK=0 и TST_EN=0, даже при восстановлении связи.
BAD_PIXEL_MAP: Path | list[tuple[int, int]] | None = None

# Необязательная расширенная замена baseline выбранных пикселей. При None
# используется встроенная конфигурация: GAIN=10, SHT=2, MASK=1, SH_EN=0,
# TST_EN=0, BUF_NEN=1, trims=16. Перед ее применением все 512 принадлежащих
# пикселей всегда получают стандартную конфигурацию с MASK=0.
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
INJECTION_STEPS_MV = (10.0, 20.0, 30.0, 40.0, 50.0, 75.0, 100.0, 150.0, 250.0)

# Быстрый автономный тест шума по измерительному FCLK. Для полноценного sweep
# задайте, например, (1, 5, 10, 25, 50). Безопасный исходный default содержит
# одну частоту и не увеличивает время теста неожиданно.
CLOCK_NOISE_MEASUREMENT_FCLK_MHZ = (5, 10, 25, 50, 75, 100, 125, 150)
CLOCK_NOISE_INJECTION_STEP_MV = 40
CLOCK_NOISE_INJECTION_PATTERN = "all"
# Необязательный завершенный noise+equalization эксперимент. Из него быстрый
# clock-noise тест берет ТОЛЬКО финальную trim-карту соответствующего окна.
# Старые шумовые counts, границы и статистика в новый тест не переносятся.
CLOCK_NOISE_TRIM_REFERENCE_EXPERIMENT: Path | None = None

# По умолчанию оба выбранных кода строго больше 400.
MINIMUM_REFERENCE_CODE = 401
# Верхняя граница включительно, также применяется к обоим REF.
MAXIMUM_REFERENCE_CODE = 900
# Это отдельное ограничение по измеренному напряжению, обычно оставляется None.
MINIMUM_REFERENCE_VOLTAGE_V: float | None = None
# Устаревшие параметры сохранены для совместимости старых конфигов. Новый
# алгоритм фиксирует один самый низкий допустимый REF1 и меняет только REF2.
PREFERRED_REFERENCE_COMMON_MODE_V: float | None = None
REFERENCE_COMMON_MODE_STEP_ERROR_SLACK_V = 0.0
# Максимально допустимая ошибка выбранной по LUT ступеньки: 1 мВ.
MAXIMUM_REFERENCE_STEP_ERROR_V: float | None = 1e-3

# Выберите ровно один источник GAIN для S-curve: код ИЛИ CSV.
# Значения GAIN: целые числа 0..31 для каждого выбранного исправного пикселя.
_GAIN_VALUES = [
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 0
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 1
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 2
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 3
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 4
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 5
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 6
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 7
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 8
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 9
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 10
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 11
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 12
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 13
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 14
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 15
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 16
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 17
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 18
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 19
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 20
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 21
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 22
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 23
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 24
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 25
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 26
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 27
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 28
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 29
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 30
    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],  # row 31
]

# Формат:
#   key   = (column, row)
#   value = GAIN, 0..31
GAIN_MAP: Mapping[tuple[int, int], int] = {
    (column, row): _GAIN_VALUES[row][column - 16]
    for row in range(32)
    for column in range(16, 32)
}
# GAIN_MAP: Mapping[tuple[int, int], int] | Sequence[int] | Sequence[Sequence[int]] | None = None

# Пример словаря (10 и 12 здесь только пример, задайте свои значения):
# GAIN_MAP = {(column, row): 10 for row in range(32) for column in range(16, 32)}
# GAIN_MAP[(20, 7)] = 12  # физические column=20, row=7
# Или список 32x16: GAIN_MAP[row][column - 16]. Каждая строка независима.
# GAIN_MAP = [[10 for _ in range(16)] for _ in range(32)]
# GAIN_MAP[7][4] = 12    # тот же физический пиксель (20, 7)
# Допустим и плоский список 512 значений: индекс row * 16 + (column - 16).
# Для numpy-массива используйте GAIN_MAP = array.tolist().
# CSV: столбцы column,row,gain с физическими координатами; GAIN_MAP = None.
GAIN_MAP_CSV: Path | None = None

# Режим одного обычного S-curve запуска. Можно оставить ровно один режим или
# перечислить несколько. Сравнение всех четырех вынесено в run_crosstalk.py.
SCURVE_PATTERNS = ("all",)
# paired: отдельный background перед каждым signal-shot.
# sparse: background только периодически, а около перехода снимаются полные
# повторы. Это стандартный ускоренный режим; raw signal никогда не заменяется.
SCURVE_BACKGROUND_MODE = "sparse"
SCURVE_SPARSE_BACKGROUND_INTERVAL_CODES = 16
SCURVE_REPEATS = 1
SCURVE_ADAPTIVE_REPEATS = True
SCURVE_WEAK_SIGNAL_STEP_THRESHOLD_V = 0.025
# tile_measurement: неактивная фаза MASK=0, TST_EN=0.
# tile_crosstalk: неактивная фаза MASK=1, TST_EN=0 и ее отклик анализируется.
SCURVE_TILE_MODE = "tile_crosstalk"
# Используется только в режиме keysight_burst. Для upo_pwm это значение не
# влияет ни на управление, ни на анализ.
N_INJECTIONS = 1000
NOISE_SHUTTER_DURATION_S = 0.001
# Это единственное значение экспозиции, по которому upo_pwm вычисляет
# N_nom=round(Freal*T). Оно должно совпадать с ручной настройкой GUI УПО.
# При 100 кГц и 0.010 с получается номинально 1000 отрицательных фронтов.
SCURVE_SHUTTER_DURATION_S = 0.010
NOISE_REPEATS = 4
# Пороговый ЦАП S-curve идет от большого кода к меньшему. Это оставляет в
# измерении полезную инжекцию на отрицательном фронте CTRL и не продолжает
# скан далеко ниже базовой линии к ветви противоположной полярности.
SCURVE_SCAN_DESCENDING = True
# None означает крайний код, реально присутствующий в LUT. Для полной
# характеристики 0..1023 явные значения ниже эквивалентны None.
SCURVE_COARSE_HIGH_CODE: int | None = 1023
SCURVE_COARSE_LOW_CODE: int | None = 0
SCURVE_COARSE_STEP = 8
# Переход V50 и шумовой колокол всегда измеряются с шагом 1.
# При первом отклике после крупного шага пропущенные коды заполняются.
SCURVE_FINE_STEP = 1
SCURVE_FINE_MARGIN_CODES = 8

# После превышения фоном уровня N скан проходит максимум и спад к N.
# Требуется заданное число соседних подтверждений с шагом 1 после защищенной
# шумовой области. PIXEL_FRACTION используется в offline-выборе физической ветви.
# COARSE_BASELINE_NOISE_CONSECUTIVE_CODES сохранен для старых конфигов;
# новый адаптивный проход не останавливается на первой шумовой точке.
SCURVE_BASELINE_NOISE_STOP_ENABLED = True
SCURVE_BASELINE_NOISE_COUNT_MULTIPLIER = 1.0
SCURVE_BASELINE_NOISE_PIXEL_FRACTION = 0.10
SCURVE_COARSE_BASELINE_NOISE_CONSECUTIVE_CODES = 1
SCURVE_BASELINE_NOISE_CONSECUTIVE_CODES = 2

# Ограничения coarse и автоматического fine сканирования включительно.
# Например, 400..900 сокращает поиск, но исключенные области не измеряются.
# Универсальный узкий диапазон неизвестен: задайте его по своему пилотному скану.
NOISE_COARSE_START = 0
NOISE_COARSE_STOP = 1023
NOISE_COARSE_STEP = 16
# Полный список DAC-кодов всегда проходится. Если вся выбранная матрица дважды
# подряд валидно вернула ноль на одном коде, оставшиеся повторы только этой
# точки пропускаются. Ошибка чтения никогда не считается пустой матрицей.
NOISE_EMPTY_MATRIX_REPEATS_TO_SKIP_REMAINING: int | None = 1
# Устаревшая настройка оставлена для импорта старых пользовательских файлов.
# В версии 0.13 она не обрезает хвост DAC-диапазона.
NOISE_CONSECUTIVE_EMPTY_CODES_TO_STOP: int | None = None

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
# False: квадратная область карты, как в прежних gain-графиках. True:
# физически квадратные ячейки и прямоугольная принадлежащая половина 16x32.
PLOT_SQUARE_PHYSICAL_PIXELS = False
# Язык автоматических PNG/PDF и общего анализа: "ru" или "en".
PLOT_LANGUAGE = "ru"

# Источник для локальной HTML-страницы. Допустим каталог эксперимента или
# конкретный analysis/vNNN. None означает, что путь задается в командной строке.
PLOT_DASHBOARD_EXPERIMENT: Path | None = None
PLOT_DASHBOARD_PORT = 0
PLOT_DASHBOARD_OPEN_BROWSER = True

# S-curve fit: робастная оценка индивидуальных плато, монотонное упорядочивание
# точек и probit-fit только центральных 5-95% перехода. Это не сглаживает raw и
# не меняет измеренные значения, а не дает неоднородному плато завышать sigma.
SCURVE_FIT_CORE_LOW_FRACTION = 0.05
SCURVE_FIT_CORE_HIGH_FRACTION = 0.95
# На raw-графиках слева дополнительно показывается ближайший подтвержденный
# максимум шумового плеча в пределах +/-32 кодов от матричной fit-границы.
# Локальная baseline пикселя может быть сдвинута в любую сторону. Для защиты от
# одиночного выброса требуется еще одна точка не ниже 5% найденного максимума.
SCURVE_PLOT_NOISE_PEAK_SEARCH_CODES = 32
SCURVE_PLOT_NOISE_PEAK_SUPPORT_FRACTION = 0.05

# Параллельный анализ. 0 = авто, 1 = последовательно; числа >1 задают лимит.
# Авто: до 8 процессов для больших наборов кривых, до 4 для PNG/PDF,
# до 8 потоков чтения CSV. Это не распараллеливает команды стенду.
ANALYSIS_WORKERS = 0
PLOT_WORKERS = 0
RAW_READ_WORKERS = 0
# Небольшие наборы считаются без процессов, чтобы не тратить время на spawn.
ANALYSIS_PARALLEL_MIN_GROUPS = 128

# Дополнительный этап только для WINDOW="ALL". После независимых AB/BC/CD
# пороги D/C/B равномерно распределяются по измеренному напряжению, A=1023,
# затем REF2 свипируется при одном фиксированном REF1. На каждом Q выполняется
# указанное число парных background/signal экспозиций. Этот финальный
# трехоконный sweep намеренно остается paired независимо от обычного режима
# SCURVE_BACKGROUND_MODE, поскольку это более короткая итоговая проверка.
ALL_WINDOW_FINAL_REF_SWEEP_ENABLED = True
ALL_WINDOW_FINAL_REF_STEP_COUNT = 100
ALL_WINDOW_FINAL_REF_REPEATS = 4
ALL_WINDOW_FINAL_REF_INJECTION_PATTERN = "all"
ALL_WINDOW_COMMON_SHIFT_Z_THRESHOLD = 3.0
ALL_WINDOW_COMMON_SHIFT_MAX_DIFFERENTIAL_Z = 1.0
ALL_WINDOW_COMPARATOR_OUTLIER_Z_THRESHOLD = 3.0
ALL_WINDOW_GOOD_FIT_R2 = 0.80


def configure_runtime_logging() -> None:
    """Включить краткие статусы теста и предупреждения о восстановлении УПО."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def print_recommendation_paths(analysis_path: Path | None) -> None:
    """Показать итоговые предложения; ничего не записывать в ASIC автоматически."""

    if analysis_path is None:
        return
    print(f"Анализ и графики: {analysis_path}")
    for method in ("fit", "centroid", "maximum"):
        trim = analysis_path / f"trim_recommendations_{method}.csv"
        mask = analysis_path / f"bad_pixels_suggested_{method}.json"
        if trim.is_file():
            print(f"{method}: подстройки {trim}; предложение маски {mask}")
    print("Это предложения. Причины и необходимость проверки указаны в CSV; новые маски автоматически не применяются.")


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


def base_pixel_config() -> Path | None:
    if BASE_PIXEL_CONFIG is None:
        return None
    return _required_path(BASE_PIXEL_CONFIG, "базовая Matrix-конфигурация")


def gain_map() -> Mapping[tuple[int, int], int] | Sequence[int] | Sequence[Sequence[int]]:
    """Получить GAIN из кода или CSV без обращения к стенду.

    Общая проверка значений и покрытия выбранных исправных пикселей выполняется
    в characterize_comparator через resolve_gain_map до программирования ASIC.
    """

    if GAIN_MAP is not None and GAIN_MAP_CSV is not None:
        raise ValueError(
            "Заданы и GAIN_MAP, и GAIN_MAP_CSV. Оставьте один источник, "
            "а второй установите в None."
        )
    if GAIN_MAP is not None:
        return GAIN_MAP
    if GAIN_MAP_CSV is not None:
        return load_gain_map_csv(_required_path(GAIN_MAP_CSV, "GAIN-карта"))
    raise ValueError(
        "Не задана GAIN-карта. Укажите GAIN_MAP (словарь или список) "
        "либо путь GAIN_MAP_CSV в characterization_config.py."
    )


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
    settings.noise.coarse_start = NOISE_COARSE_START
    settings.noise.coarse_stop = NOISE_COARSE_STOP
    settings.noise.coarse_step = NOISE_COARSE_STEP
    settings.noise.stop_after_consecutive_empty_codes = (
        NOISE_CONSECUTIVE_EMPTY_CODES_TO_STOP
    )
    settings.noise.empty_matrix_repeats_to_skip_remaining = (
        NOISE_EMPTY_MATRIX_REPEATS_TO_SKIP_REMAINING
    )
    settings.noise.upo_reconnect_attempts = UPO_RECONNECT_ATTEMPTS
    settings.noise.upo_reconnect_backoff_s = UPO_RECONNECT_BACKOFF_S
    settings.equalization.scan_all_trim_codes = scan_all_trim_codes
    settings.scurve.n_injections = (
        N_INJECTIONS if _normalized_ctrl_source() == "keysight_burst" else 1
    )
    settings.scurve.shutter_duration_s = SCURVE_SHUTTER_DURATION_S
    settings.scurve.injection_patterns = injection_patterns or SCURVE_PATTERNS
    settings.scurve.background_mode = SCURVE_BACKGROUND_MODE
    settings.scurve.sparse_background_interval_codes = (
        SCURVE_SPARSE_BACKGROUND_INTERVAL_CODES
    )
    settings.scurve.repeats = SCURVE_REPEATS
    settings.scurve.adaptive_repeats = SCURVE_ADAPTIVE_REPEATS
    settings.scurve.weak_signal_dense_scan_below_v = SCURVE_WEAK_SIGNAL_STEP_THRESHOLD_V
    settings.scurve.tile_mode = SCURVE_TILE_MODE
    settings.scurve.minimum_reference_code = MINIMUM_REFERENCE_CODE
    settings.scurve.maximum_reference_code = MAXIMUM_REFERENCE_CODE
    settings.scurve.minimum_reference_voltage_v = MINIMUM_REFERENCE_VOLTAGE_V
    settings.scurve.preferred_reference_common_mode_v = (
        PREFERRED_REFERENCE_COMMON_MODE_V
    )
    settings.scurve.reference_common_mode_step_error_slack_v = (
        REFERENCE_COMMON_MODE_STEP_ERROR_SLACK_V
    )
    settings.scurve.maximum_reference_step_error_v = (
        MAXIMUM_REFERENCE_STEP_ERROR_V
    )
    settings.scurve.scan_descending = SCURVE_SCAN_DESCENDING
    settings.scurve.coarse_high_code = SCURVE_COARSE_HIGH_CODE
    settings.scurve.coarse_low_code = SCURVE_COARSE_LOW_CODE
    settings.scurve.coarse_step = SCURVE_COARSE_STEP
    settings.scurve.fine_step = SCURVE_FINE_STEP
    settings.scurve.fine_margin_codes = SCURVE_FINE_MARGIN_CODES
    settings.scurve.baseline_noise_stop_enabled = (
        SCURVE_BASELINE_NOISE_STOP_ENABLED
    )
    settings.scurve.baseline_noise_count_multiplier = (
        SCURVE_BASELINE_NOISE_COUNT_MULTIPLIER
    )
    settings.scurve.baseline_noise_pixel_fraction = (
        SCURVE_BASELINE_NOISE_PIXEL_FRACTION
    )
    settings.scurve.coarse_baseline_noise_consecutive_codes = (
        SCURVE_COARSE_BASELINE_NOISE_CONSECUTIVE_CODES
    )
    settings.scurve.baseline_noise_consecutive_codes = (
        SCURVE_BASELINE_NOISE_CONSECUTIVE_CODES
    )
    settings.analysis = AnalysisSettings(
        workers=ANALYSIS_WORKERS,
        plot_workers=PLOT_WORKERS,
        read_workers=RAW_READ_WORKERS,
        parallel_min_groups=ANALYSIS_PARALLEL_MIN_GROUPS,
        representative_pixels=REPRESENTATIVE_PIXEL_COUNT,
        plot_pixels=PLOT_PIXELS,
        plot_injection_patterns=PLOT_SCURVE_PATTERNS,
        plot_dpi=PLOT_DPI,
        save_pdf_plots=SAVE_PDF_PLOTS,
        square_physical_pixels=PLOT_SQUARE_PHYSICAL_PIXELS,
        plot_language=PLOT_LANGUAGE,
        scurve_fit_core_low_fraction=SCURVE_FIT_CORE_LOW_FRACTION,
        scurve_fit_core_high_fraction=SCURVE_FIT_CORE_HIGH_FRACTION,
        scurve_plot_noise_peak_search_codes=(
            SCURVE_PLOT_NOISE_PEAK_SEARCH_CODES
        ),
        scurve_plot_noise_peak_support_fraction=(
            SCURVE_PLOT_NOISE_PEAK_SUPPORT_FRACTION
        ),
    )
    settings.validate()
    return settings


def build_upo_client() -> "MGPDClient":
    from mgpd import MGPDClient

    return MGPDClient(
        host=UPO_HOST,
        port=UPO_PORT,
        timeout=UPO_TIMEOUT_S,
        reconnect_attempts=UPO_RECONNECT_ATTEMPTS,
        reconnect_backoff_s=UPO_RECONNECT_BACKOFF_S,
    )


def build_oscilloscope():
    """Контекст осциллографа или пустой контекст при отключенной проверке."""

    if not VERIFY_REFERENCE_STEPS_BEFORE_TEST:
        return nullcontext(None)
    from oscilloscope_cfg import Oscilloscope

    return Oscilloscope(
        osc_address=OSCILLOSCOPE_VISA_ADDRESS,
        idn_substring=OSCILLOSCOPE_IDN_SUBSTRING,
        timeout_ms=OSCILLOSCOPE_TIMEOUT_MS,
    )


def build_reference_verification_settings() -> ReferenceStepVerificationSettings:
    """Собрать настройки проверки REF через CH1/CH4."""

    return ReferenceStepVerificationSettings(
        plot_language=PLOT_LANGUAGE,
        enabled=VERIFY_REFERENCE_STEPS_BEFORE_TEST,
        signal_channel=REFERENCE_SIGNAL_CHANNEL,
        trigger_channel=REFERENCE_TRIGGER_CHANNEL,
        trigger_level_v=REFERENCE_TRIGGER_LEVEL_V,
        trigger_slope="NEG",
        time_scale_s=REFERENCE_TIME_SCALE_S,
        time_offset_s=REFERENCE_TIME_OFFSET_S,
        waveform_points=REFERENCE_WAVEFORM_POINTS,
        averaging_enabled=False,
        average_count=1,
        signal_scale_v=REFERENCE_SIGNAL_SCALE_V,
        signal_offset_v=REFERENCE_SIGNAL_OFFSET_V,
        trigger_scale_v=REFERENCE_TRIGGER_SCALE_V,
        trigger_offset_v=REFERENCE_TRIGGER_OFFSET_V,
        scope_arm_delay_s=REFERENCE_SCOPE_ARM_DELAY_S,
        acquisition_time_s=REFERENCE_SCOPE_ACQUISITION_TIME_S,
        plateau_guard_s=REFERENCE_PLATEAU_GUARD_S,
        plateau_window_s=REFERENCE_PLATEAU_WINDOW_S,
        maximum_scope_step_error_v=REFERENCE_MAXIMUM_SCOPE_STEP_ERROR_V,
        clock_on_frequency_mhz=REFERENCE_VERIFICATION_CLK_ON_MHZ,
        acquisition_retries=REFERENCE_VERIFICATION_RETRIES,
        retry_backoff_s=REFERENCE_VERIFICATION_RETRY_BACKOFF_S,
        abort_on_failure=REFERENCE_VERIFICATION_ABORT_ON_FAILURE,
        save_screenshots=REFERENCE_VERIFICATION_SAVE_SCREENSHOTS,
    )


def reference_hardware_arguments(
    oscilloscope: Any,
    *,
    required_for_scurve: bool,
    injection_steps_mv: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Передать REF LUT и при включении добавить проверку до измерений."""

    verify = bool(VERIFY_REFERENCE_STEPS_BEFORE_TEST)
    if not (required_for_scurve or verify):
        return {}
    arguments: dict[str, Any] = {
        "reference_calibration_files": reference_calibration_files(),
        "injection_voltage_steps_v": tuple(
            float(value) * 1e-3
            for value in (
                INJECTION_STEPS_MV
                if injection_steps_mv is None
                else injection_steps_mv
            )
        ),
        "reference_calibration_voltage_unit": REFERENCE_LUT_VOLTAGE_UNIT,
    }
    if verify:
        if oscilloscope is None:
            raise RuntimeError("Проверка REF включена, но осциллограф не был открыт")
        arguments.update(
            reference_step_oscilloscope=oscilloscope,
            reference_step_verification_settings=build_reference_verification_settings(),
            reference_verification_pwm_frequency_khz=UPO_CTRL_FREQUENCY_KHZ,
            reference_verification_pwm_high_time_ns=UPO_CTRL_HIGH_TIME_NS,
        )
    return arguments


def _normalized_ctrl_source() -> str:
    source = str(CTRL_INJECTION_SOURCE).strip().lower()
    if source not in {"upo_pwm", "keysight_burst"}:
        raise ValueError(
            "CTRL_INJECTION_SOURCE must be 'upo_pwm' or 'keysight_burst'"
        )
    return source


def build_generator():
    """Return a context manager; upo_pwm deliberately performs no VISA scan."""

    if _normalized_ctrl_source() == "upo_pwm":
        return nullcontext(None)
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


def build_upo_pwm_settings() -> UpoPwmSettings:
    return UpoPwmSettings(
        frequency_khz=UPO_CTRL_FREQUENCY_KHZ,
        high_time_ns=UPO_CTRL_HIGH_TIME_NS,
        edge_count_uncertainty=UPO_CTRL_EDGE_COUNT_UNCERTAINTY,
    )


def injection_hardware_arguments(generator: Any) -> dict[str, Any]:
    """Build exactly one S-curve CTRL backend from the selected source."""

    if _normalized_ctrl_source() == "upo_pwm":
        if generator is not None:
            raise RuntimeError("upo_pwm must not open an external generator")
        return {"shot_executor": UpoPwmShotExecutor(build_upo_pwm_settings())}
    if generator is None:
        raise RuntimeError("keysight_burst requires an opened generator")
    return {
        "keysight_generator": generator,
        "keysight_burst_settings": build_burst_settings(),
    }


def run_characterization(client, calibration_files, **kwargs):
    """Общий вход run_*.py: одиночный запуск либо возобновляемая EO-серия."""
    from comparator_characterization import (
        AllWindowSettings,
        characterize_all_windows,
        characterize_comparator,
        characterize_parameter_sweep,
    )
    requested_window = str(kwargs.get("window", WINDOW)).strip().upper()
    if requested_window == "ALL":
        if EO_PARAMETER_GRID or RESUME_SWEEP is not None:
            raise ValueError("WINDOW='ALL' пока не объединяется с EO_PARAMETER_GRID")
        if EO_OVERRIDES:
            kwargs["eo_overrides"] = EO_OVERRIDES
        kwargs.pop("window", None)
        return characterize_all_windows(
            client,
            calibration_files,
            resume_experiment=RESUME_EXPERIMENT,
            all_window_settings=AllWindowSettings(
                final_ref_sweep_enabled=ALL_WINDOW_FINAL_REF_SWEEP_ENABLED,
                final_ref_step_count=ALL_WINDOW_FINAL_REF_STEP_COUNT,
                final_ref_repeats=ALL_WINDOW_FINAL_REF_REPEATS,
                final_ref_injection_pattern=ALL_WINDOW_FINAL_REF_INJECTION_PATTERN,
                common_shift_z_threshold=ALL_WINDOW_COMMON_SHIFT_Z_THRESHOLD,
                common_shift_max_differential_z=(
                    ALL_WINDOW_COMMON_SHIFT_MAX_DIFFERENTIAL_Z
                ),
                comparator_outlier_z_threshold=(
                    ALL_WINDOW_COMPARATOR_OUTLIER_Z_THRESHOLD
                ),
                good_fit_r2=ALL_WINDOW_GOOD_FIT_R2,
            ),
            **kwargs,
        )
    if EO_PARAMETER_GRID or RESUME_SWEEP is not None:
        if RESUME_EXPERIMENT is not None or EO_OVERRIDES:
            raise ValueError("Для EO-серии используйте только EO_PARAMETER_GRID и RESUME_SWEEP")
        return characterize_parameter_sweep(
            client, calibration_files, eo_parameter_grid=EO_PARAMETER_GRID,
            resume_sweep=RESUME_SWEEP, **kwargs,
        )
    return characterize_comparator(
        client, calibration_files, resume_experiment=RESUME_EXPERIMENT,
        eo_overrides=EO_OVERRIDES, **kwargs,
    )


def print_result_paths(result):
    if hasattr(result, "window_results"):
        for window, child in result.window_results.items():
            print(f"Окно {window}: {child.experiment_path}")
            print_recommendation_paths(child.analysis_path)
        print(f"Совместный отчет: {result.analysis_path / 'REPORT.md'}")
        for method in ("fit", "centroid", "maximum"):
            path = result.analysis_path / f"combined_trim_map_{method}.csv"
            if path.exists():
                print(f"Совместная trim-карта {method}: {path}")
    elif hasattr(result, "combinations"):
        for entry in result.combinations:
            print(f"EO {entry['eo_overrides']}: {entry['status']}")
            if entry.get("analysis"):
                print_recommendation_paths(result.experiment_path / entry["analysis"])
        print(f"Сводка серии: {result.experiment_path / 'sweep_summary.csv'}")
    else:
        print_recommendation_paths(result.analysis_path)
