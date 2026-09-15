"""Конфигурация v2: основные настройки тестов, без путей и старых имен.

Пути: .env в корне проекта. Продвинутые параметры: metadata.py рядом.
Единый запуск: python comparator_characterization/high_level/run_test.py
Проверка без подключения: та же команда с --check-config.
"""
from pathlib import Path

from .config_schema import (
    AcquisitionConfig, AllWindowsConfig, ClockNoiseConfig, ConnectionConfig,
    DashboardConfig, EoSweepConfig, GainEqualizationConfig, NoiseConfig,
    PlotConfig, ReferenceConfig, RunConfig, ScurveConfig,
)
from .env_paths import load_paths
from . import metadata

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PATHS = load_paths(PROJECT_ROOT / ".env", project_root=PROJECT_ROOT)

# 1. Какой тест запустить. Нужные ссылки для каждого теста: README.md рядом.
# full / noise / equalize / trim_sweep / scurve / clock_noise / crosstalk /
# gain_equalization / offline / all_windows / ref_preview / reference_verification /
# dashboard / eo_sweep
RUN = RunConfig(
    test="full", window="CD",         # окно AB / BC / CD / ALL
    pixels="all",                     # либо [(16, 0), (20, 7)]
    hardware_enabled=True,            # True разрешает реальный стенд
    resume=False,                      # True активирует resume-ссылку из .env
    eo_overrides=None,                 # фиксированные EO-параметры одиночного теста
)

# 2. Соединения. Импорт конфига никогда не открывает приборы.
CONNECTION = ConnectionConfig(
    host="127.0.0.1", port=0xBEEB, timeout_s=8.0,
    oscilloscope_address=None, oscilloscope_idn="DSO9104H",
    generator_address=None,            # None: VISA-автопоиск только для Keysight
)

# 3. Общие условия измерения. Shutter также выставляется в GUI УПО.
# Если два времени равны, между noise и S-curve нет паузы Enter.
ACQUISITION = AcquisitionConfig(
    main_fclk_mhz=100, measurement_fclk_mhz=5,
    noise_shutter_s=0.010, scurve_shutter_s=0.010,
    ctrl_source="upo_pwm",              # либо keysight_burst
    pwm_frequency_khz=100, pwm_high_time_ns=5_000,
    burst_injections=1000,             # только Keysight; UPO считает N из F*T
)

# 4. Noise / trim equalization. Fine step=1 и локальный поиск в metadata.py.
NOISE = NoiseConfig(
    repeats=4,
    coarse_range=(0, 1023, 16),         # start, stop, step
    manual_range=None,                # либо (start, stop, step), без auto range
)

# 5. S-кривые / GAIN-свип. Старой настройки _UG больше нет.
# gain=10: равномерный код; gain=(4, 8, 10, 12, 20): последовательный свип.
# gain_source="csv": GAIN_MAP_CSV из .env, скалярный gain не используется.
SCURVE = ScurveConfig(
    gain=4, gain_source="uniform",     # uniform / csv, без неявного приоритета карты
    repeats=1, patterns=("all",),      # all / tile_2x2 / tile_4x4 / tile_8x8
    background_mode="sparse",          # sparse / paired
    tile_mode="tile_crosstalk",         # tile_measurement / tile_crosstalk
    dac_range=(100, 900),              # low, high; (None, None): весь LUT
)

# 6. Инжекция и REF. Наличие пути LUT не меняет явно выбранный режим.
REFERENCE = ReferenceConfig(
    mode="manual",                        # lut / manual
    steps_mv=(10, 20, 50, 100, 200),
    manual_ref1=600, manual_ref2=800, manual_step_mv=100.0,
    code_limits=(500, 900),            # только LUT, оба кода включительно
    lut_voltage_unit="auto",           # auto / V / mV / uV
    verify_with_scope=False,
)

# 7. Clock noise. .env CLOCK_TRIM_REFERENCE переносит только comparator trims.
CLOCK_NOISE = ClockNoiseConfig(fclk_mhz=(5, 10, 25, 50, 75, 100, 125, 150), step_mv=40, pattern="all")

# 8. Офлайн-подбор GAIN + опциональная реальная проверка полученных карт.
# Источник: .env GAIN_SWEEP_SOURCE. Веса/статистика/fit quality в metadata.py.
GAIN_EQUALIZATION = GainEqualizationConfig(
    target_codes=[4, 10, 22], check_map=False, check_all_windows=False,
    map_reference_window=None,        # для check_all_windows=True задайте AB / BC / CD
)

# 9. Дополнительный финальный Q-sweep полного ALL-теста.
# Недоступен для manual REF и GAIN-свипа, это будет явно показано.
ALL_WINDOWS = AllWindowsConfig(final_q_sweep=True, q_steps=100, repeats=4, pattern="all")

# 10. Только тест eo_sweep: grid не влияет на другие режимы.
EO_SWEEP = EoSweepConfig(grid=None)
# Пример: EO_SWEEP.grid = {"DAC_CMP_BIAS_LSB": [200, 500], "DAC_CMP_VB5": [500, 1000]}

# 11. Общий стиль аппаратных и офлайн-графиков.
PLOTS = PlotConfig(
    language="ru",                     # ru / en
    geometry="matrix_square",          # matrix_square / pixel_square
    pixels=(), patterns=(), representative_pixels=6,
    dpi=300, save_pdf=True, generate=True,
)
DASHBOARD = DashboardConfig(port=0, open_browser=True)

# Реализация сборки/проверки отделена от пользовательских настроек.
from .config_runtime import (
    base_pixel_config, build_all_window_settings, build_analysis_settings, build_burst_settings, build_generator, build_oscilloscope,
    build_reference_verification_settings, build_settings, build_upo_client,
    build_upo_pwm_settings, configuration_snapshot, configure_runtime_logging,
    gain_hardware_arguments, gain_sweep_enabled, injection_hardware_arguments,
    injection_voltage_steps_v, noise_reference_path, normalized_ctrl_source,
    print_recommendation_paths, print_result_paths, reference_calibration_files,
    reference_hardware_arguments, require_hardware_run_enabled,
    resume_experiment_path, resume_sweep_path, run_characterization,
    configured_gain_codes, threshold_calibration_files, validate_configuration,
)
