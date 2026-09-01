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
    UpoPwmSettings,
    UpoPwmShotExecutor,
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

# Перед каждым аппаратным тестом FCLK явно устанавливается в 50 МГц, затем
# загружаются EO_cfg.DEFAULT_REGISTERS и конфигурация всех 1024 PX:
# Col 0..15 получают 0x00000000, Col 16..31 настраиваются по логике теста.
ASIC_INITIALIZATION_FCLK_MHZ = 50

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

# Окно и пиксели. Возможные окна: AB, BC, CD.
WINDOW = "AB"
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
INJECTION_STEPS_MV = (10.0, 20.0, 30.0, 40.0, 50.0)

# По умолчанию оба выбранных кода строго больше 400.
MINIMUM_REFERENCE_CODE = 401
# Верхняя граница включительно, также применяется к обоим REF.
MAXIMUM_REFERENCE_CODE = 800
# Это отдельное ограничение по измеренному напряжению, обычно оставляется None.
MINIMUM_REFERENCE_VOLTAGE_V: float | None = None
# Необязательная цель общего уровня (V_REF1 + V_REF2)/2.
PREFERRED_REFERENCE_COMMON_MODE_V: float | None = None
# Максимально допустимая ошибка выбранной ступеньки, None отключает предел.
MAXIMUM_REFERENCE_STEP_ERROR_V: float | None = None

# Выберите ровно один источник GAIN для S-curve: код ИЛИ CSV.
# Значения GAIN: целые числа 0..31 для каждого выбранного исправного пикселя.
_GAIN_VALUES = [
    [7, 7, 7, 18, 7, 7, 12, 7, 18, 12, 7, 7, 11, 7, 7, 7],       # row 0
    [12, 10, 7, 12, 10, 12, 17, 18, 11, 12, 6, 7, 10, 7, 7, 10], # row 1
    [17, 7, 7, 18, 7, 18, 10, 18, 18, 10, 18, 7, 18, 7, 12, 12], # row 2
    [7, 7, 12, 7, 7, 10, 18, 18, 11, 7, 7, 18, 18, 18, 18, 12], # row 3
    [18, 18, 7, 12, 7, 7, 7, 10, 10, 18, 7, 7, 7, 7, 7, 7],    # row 4
    [7, 7, 7, 10, 10, 18, 11, 12, 18, 18, 18, 7, 18, 18, 10, 7], # row 5
    [12, 12, 12, 10, 10, 18, 7, 18, 7, 18, 17, 10, 7, 18, 7, 10], # row 6
    [10, 7, 6, 10, 10, 17, 18, 7, 10, 18, 10, 12, 7, 17, 18, 7], # row 7
    [18, 12, 18, 18, 18, 10, 7, 18, 10, 12, 7, 7, 18, 9, 10, 10], # row 8
    [18, 7, 18, 18, 7, 18, 18, 10, 10, 18, 18, 7, 12, 7, 10, 18], # row 9
    [7, 18, 18, 11, 12, 7, 18, 18, 17, 18, 10, 7, 18, 18, 10, 10], # row 10
    [7, 17, 7, 6, 10, 18, 6, 18, 10, 10, 10, 18, 6, 11, 10, 6], # row 11
    [10, 18, 10, 10, 18, 7, 10, 17, 10, 7, 9, 18, 18, 18, 17, 18], # row 12
    [9, 17, 6, 18, 10, 12, 7, 10, 17, 10, 10, 10, 10, 7, 10, 17], # row 13
    [10, 10, 7, 18, 9, 10, 18, 17, 18, 10, 17, 18, 10, 18, 10, 7], # row 14
    [17, 18, 7, 17, 17, 10, 6, 10, 9, 7, 9, 18, 6, 9, 10, 17], # row 15
    [10, 18, 18, 10, 10, 7, 10, 17, 9, 10, 10, 10, 18, 6, 6, 9], # row 16
    [10, 17, 18, 6, 7, 17, 10, 6, 10, 17, 10, 10, 10, 10, 10, 18], # row 17
    [17, 6, 6, 10, 6, 10, 7, 10, 17, 7, 17, 10, 18, 17, 10, 10], # row 18
    [9, 17, 17, 18, 18, 6, 6, 10, 18, 17, 18, 10, 10, 17, 17, 7], # row 19
    [17, 17, 10, 9, 17, 10, 10, 6, 18, 7, 10, 10, 6, 12, 7, 7], # row 20
    [9, 10, 17, 18, 6, 6, 9, 7, 17, 9, 6, 9, 6, 18, 9, 17],     # row 21
    [18, 17, 17, 9, 6, 18, 9, 18, 18, 10, 6, 17, 18, 17, 17, 18], # row 22
    [10, 17, 6, 17, 10, 9, 17, 10, 10, 18, 10, 6, 10, 17, 17, 10], # row 23
    [9, 6, 10, 17, 10, 17, 10, 17, 18, 17, 18, 6, 10, 9, 17, 10], # row 24
    [17, 6, 6, 17, 6, 6, 10, 10, 9, 10, 18, 18, 17, 17, 9, 18], # row 25
    [6, 9, 9, 17, 18, 9, 17, 7, 17, 17, 6, 6, 10, 10, 6, 17],   # row 26
    [10, 10, 6, 17, 9, 18, 6, 10, 10, 17, 9, 9, 10, 17, 17, 9], # row 27
    [6, 6, 6, 6, 9, 9, 17, 17, 10, 10, 17, 17, 17, 17, 6, 6],  # row 28
    [6, 16, 9, 9, 17, 17, 17, 10, 10, 9, 9, 6, 17, 10, 9, 10], # row 29
    [6, 17, 17, 17, 9, 6, 9, 6, 6, 17, 10, 17, 10, 6, 6, 6],   # row 30
    [10, 9, 9, 17, 10, 6, 9, 17, 9, 6, 9, 17, 17, 6, 9, 17],   # row 31
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
# Используется только в режиме keysight_burst. Для upo_pwm это значение не
# влияет ни на управление, ни на анализ.
N_INJECTIONS = 1000
NOISE_SHUTTER_DURATION_S = 0.001
# Это единственное значение экспозиции, по которому upo_pwm вычисляет
# N_nom=round(Freal*T). Оно должно совпадать с ручной настройкой GUI УПО.
# При 100 кГц и 0.010 с получается номинально 1000 отрицательных фронтов.
SCURVE_SHUTTER_DURATION_S = 0.010
NOISE_REPEATS = 10
# Ограничения coarse и автоматического fine сканирования включительно.
# Например, 400..900 сокращает поиск, но исключенные области не измеряются.
# Универсальный узкий диапазон неизвестен: задайте его по своему пилотному скану.
NOISE_COARSE_START = 0
NOISE_COARSE_STOP = 1023
NOISE_COARSE_STEP = 16
# После обнаружения активности три полностью пустые DAC-точки подряд завершают
# текущую фазу noise scan. None отключает умную раннюю остановку.
NOISE_CONSECUTIVE_EMPTY_CODES_TO_STOP: int | None = 3

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

# Параллельный анализ. 0 = авто, 1 = последовательно; числа >1 задают лимит.
# Авто: до 8 процессов для больших наборов кривых, до 4 для PNG/PDF,
# до 8 потоков чтения CSV. Это не распараллеливает команды стенду.
ANALYSIS_WORKERS = 0
PLOT_WORKERS = 0
RAW_READ_WORKERS = 0
# Небольшие наборы считаются без процессов, чтобы не тратить время на spawn.
ANALYSIS_PARALLEL_MIN_GROUPS = 2048


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
    settings.noise.upo_reconnect_attempts = UPO_RECONNECT_ATTEMPTS
    settings.noise.upo_reconnect_backoff_s = UPO_RECONNECT_BACKOFF_S
    settings.equalization.scan_all_trim_codes = scan_all_trim_codes
    settings.scurve.n_injections = (
        N_INJECTIONS if _normalized_ctrl_source() == "keysight_burst" else 1
    )
    settings.scurve.shutter_duration_s = SCURVE_SHUTTER_DURATION_S
    settings.scurve.injection_patterns = injection_patterns or SCURVE_PATTERNS
    settings.scurve.minimum_reference_code = MINIMUM_REFERENCE_CODE
    settings.scurve.maximum_reference_code = MAXIMUM_REFERENCE_CODE
    settings.scurve.minimum_reference_voltage_v = MINIMUM_REFERENCE_VOLTAGE_V
    settings.scurve.preferred_reference_common_mode_v = (
        PREFERRED_REFERENCE_COMMON_MODE_V
    )
    settings.scurve.maximum_reference_step_error_v = (
        MAXIMUM_REFERENCE_STEP_ERROR_V
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
    from comparator_characterization import characterize_comparator, characterize_parameter_sweep
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
    if hasattr(result, "combinations"):
        for entry in result.combinations:
            print(f"EO {entry['eo_overrides']}: {entry['status']}")
            if entry.get("analysis"):
                print_recommendation_paths(result.experiment_path / entry["analysis"])
        print(f"Сводка серии: {result.experiment_path / 'sweep_summary.csv'}")
    else:
        print_recommendation_paths(result.analysis_path)
