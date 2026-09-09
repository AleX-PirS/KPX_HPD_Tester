# Верхнеуровневые запуски

Все параметры пользователя собраны в `characterization_config.py`. Файлы
`run_*.py` запускают конкретные измерения, `preview_ref_selection.py` проверяет
выбор REF1/REF2 без стенда, `run_reference_verification.py` отдельно проверяет
ступеньки осциллографом, `plot_characterization.py` повторно анализирует
эксперимент, а `run_plot_dashboard.py` открывает локальную страницу графиков.

Запускайте команды из корня проекта. Рекомендуемый вариант:

```bash
python -m comparator_characterization.high_level.preview_ref_selection
python -m comparator_characterization.high_level.run_reference_verification
python -m comparator_characterization.high_level.run_noise_scan
python -m comparator_characterization.high_level.run_noise_equalization
python -m comparator_characterization.high_level.run_full_trim_sweep
python -m comparator_characterization.high_level.run_scurve
python -m comparator_characterization.high_level.run_full_characterization
python -m comparator_characterization.high_level.run_all_windows
python -m comparator_characterization.high_level.run_eo_parameter_sweep
python -m comparator_characterization.high_level.run_crosstalk
python -m comparator_characterization.high_level.run_clock_noise
python -m comparator_characterization.high_level.plot_characterization results/EXPERIMENT
python -m comparator_characterization.high_level.run_plot_dashboard results/EXPERIMENT
python -m comparator_characterization.high_level.analyze_all_windows results/PARENT_ALL
```

Допустим и прямой запуск файла, например:

```bash
python comparator_characterization/high_level/run_scurve.py
```

Перед реальным измерением проверьте все пути и параметры, затем осознанно
установите `ENABLE_HARDWARE_RUN = True`.

Для последовательной характеризации трех окон задайте `WINDOW = "ALL"`.
`run_full_characterization.py` создаст один родительский каталог и три
дочерних AB/BC/CD. Константы `ALL_WINDOW_FINAL_REF_STEP_COUNT` и
`ALL_WINDOW_FINAL_REF_REPEATS` управляют дополнительным финальным sweep REF2
при фиксированных и равномерно разнесенных по измеренному напряжению порогах
D/C/B. Подробная физическая интерпретация и ограничения приведены в
`COMPARATOR_CHARACTERIZATION.md`.

Файл `run_all_windows.py` выполняет тот же связанный сценарий независимо от
текущего значения `WINDOW`. Все остальные параметры, включая
`RESUME_EXPERIMENT`, он берет из `characterization_config.py`.

Если все три окна и финальный REF sweep уже измерены, а ошибка возникла только
на этапе общего анализа, повторять стенд не нужно:

```bash
python -m comparator_characterization.high_level.analyze_all_windows results/PARENT_ALL
```

Команда создаст новый `analysis/vNNN` из сохраненных данных. Альтернативно
можно указать родительский `PARENT_ALL` в `RESUME_EXPERIMENT` и снова запустить
`run_all_windows.py`: завершенные окна и REF sweep будут пропущены, но этот
вариант все равно открывает соединения с приборами.

Для продолжения незавершенной S-кривой, созданной версией до `0.17.0`, сначала
сохраните прежнюю схему съема:

```python
SCURVE_BACKGROUND_MODE = "paired"
SCURVE_ADAPTIVE_REPEATS = False
# Только если использовался tile-режим:
SCURVE_TILE_MODE = "tile_crosstalk"
```

Старый tile-код оставлял неактивные пиксели в состоянии `MASK=1, TST_EN=0`.
Для старого режима `all` tile-настройка не влияет на матрицу. Если параметры не
совпадают, проверка resume завершится до инициализации тестовой матрицы и
первого `GET_SHOT`, но после открытия соединений верхнеуровневым файлом, и
покажет точные требуемые значения. Новые недостающие поля metadata
интерпретируются явно и фиксируются в `resume_metadata_migrations`.

Визуализация карт по умолчанию сохраняет квадратную область изображения.
`PLOT_SQUARE_PHYSICAL_PIXELS=True` показывает физически квадратные ячейки и
прямоугольную половину 16x32. Для offline-запуска используйте
`--square-pixels`.

`run_noise_scan.py` подходит для короткого пилота с ограниченной областью DAC:
он не измеряет trim 0/31 и не проводит эквализацию, а сохраняет baseline noise
при исходных trim (16 по умолчанию). Для подстроек нужны endpoint/full trim
данные из `run_noise_equalization.py` или `run_full_trim_sweep.py`.

Каждый аппаратный запуск автоматически устанавливает основной FCLK, global
`EO_cfg.DEFAULT_REGISTERS` и PX-конфигурацию всей физической матрицы.
В другой половине, Col 0..15, Row 0..31, все 512 слов равны `0x00000000`.
Для вашей половины Col 16..31 сохраняется стандартная логика теста.
Нулевая половина также заново загружается при reconnect и не участвует в
измерениях. `PX_MASK=0` отключает цифровой счет, поэтому скрипт включает
`PX_MASK=1` только для выбранных тестом пикселей, кроме `BAD_PIXEL_MAP`.
Исключенные пиксели всегда имеют `MASK=0, TST_EN=0`, включая reconnect и cleanup.
PX сначала полностью ставятся в виртуальную память УПО, затем скрипт явно
вызывает `SET_PIXEL_CFG WRITE_TO_CHIP` и требует подтверждение до съемки.
Команда применяется только к 32-битным конфигурациям пикселей и не вызывается
для global DAC, EO-регистров, REF1/REF2 или FCLK. Непосредственно перед `GET_SHOT`
выставляется измерительный FCLK. Сам `GET_SHOT` выполняется в основном потоке и
полностью завершается, затем восстанавливается основной FCLK, и только после
этого разрешен первый `GET_PIXEL`. В основном
режиме CTRL также управляется последовательно через тот же канал УПО:
`measurement FCLK -> PWM -> GET_SHOT -> CTRL=0 -> main FCLK -> GET_PIXEL`.
Для background вместо PWM явно устанавливается `CTRL=0`. Во время теста не нажимайте команды в
отдельном GUI УПО, поскольку межпроцессную конкуренцию Python заблокировать не может.

Для AB свипируется B, A устанавливается на верхнюю границу по LUT,
C и D получают код 1023. Аналогично для BC компараторы вне окна A/D получают
1023, для CD это A/B. Фиксированные пороги также восстанавливаются при reconnect.

Основные новые настройки в `characterization_config.py`:

```python
CTRL_INJECTION_SOURCE = "upo_pwm"
ASIC_MAIN_FCLK_MHZ = 50
ASIC_MEASUREMENT_FCLK_MHZ = 5
UPO_CTRL_FREQUENCY_KHZ = 100
UPO_CTRL_HIGH_TIME_NS = 5000
SCURVE_SHUTTER_DURATION_S = 0.010
SCURVE_BACKGROUND_MODE = "sparse"       # или "paired"
SCURVE_SPARSE_BACKGROUND_INTERVAL_CODES = 8
SCURVE_REPEATS = 4
SCURVE_ADAPTIVE_REPEATS = True
SCURVE_TILE_MODE = "tile_measurement"   # или "tile_crosstalk"
SCURVE_SCAN_DESCENDING = True
SCURVE_COARSE_HIGH_CODE = 1023
SCURVE_COARSE_LOW_CODE = 0
SCURVE_COARSE_STEP = 8
SCURVE_FINE_STEP = 1
SCURVE_FINE_MARGIN_CODES = 8

SCURVE_BASELINE_NOISE_STOP_ENABLED = True
SCURVE_BASELINE_NOISE_COUNT_MULTIPLIER = 1.0
SCURVE_BASELINE_NOISE_PIXEL_FRACTION = 0.10
SCURVE_COARSE_BASELINE_NOISE_CONSECUTIVE_CODES = 1
SCURVE_BASELINE_NOISE_CONSECUTIVE_CODES = 2

MINIMUM_REFERENCE_CODE = 401
MAXIMUM_REFERENCE_CODE = 900
MAXIMUM_REFERENCE_STEP_ERROR_V = 1e-3
NOISE_COARSE_START = 400     # пример, подберите по своему пилотному скану
NOISE_COARSE_STOP = 900
NOISE_COARSE_STEP = 4
NOISE_REPEATS = 4
NOISE_EMPTY_MATRIX_REPEATS_TO_SKIP_REMAINING = 2
BAD_PIXEL_MAP = [(16, 0), (20, 5)]  # либо путь CSV/JSON, либо None

CLOCK_NOISE_MEASUREMENT_FCLK_MHZ = (1, 5, 10, 25, 50)
CLOCK_NOISE_INJECTION_STEP_MV = 10.0
CLOCK_NOISE_INJECTION_PATTERN = "all"
CLOCK_NOISE_TRIM_REFERENCE_EXPERIMENT = None
PLOT_LANGUAGE = "ru"                    # или "en"
```

Для полного набора амплитуд REF-пары выбираются совместно. Алгоритм находит один
самый низкий по измеренному напряжению уровень REF1, на котором достижимо
максимальное подмножество требуемых ступенек с заданной ошибкой. Этот REF1
остается одинаковым во всех точках, меняется только REF2. Для каждой пары проверяется
`V_REF1 > V_REF2`; сравнение выполняется по напряжению LUT, а не по коду.
Недостижимые ступеньки печатаются до обращения к стенду, исключаются из съема и
сохраняются вместе с причиной в `inputs/reference_step_availability.csv`.
Устаревшие common-mode параметры принимаются API только для совместимости и не
участвуют в новом выборе.

## Проверка REF осциллографом до теста

По умолчанию `VERIFY_REFERENCE_STEPS_BEFORE_TEST = True`, поэтому каждый
аппаратный запуск, включая noise-only и resume, после стандартной инициализации
ASIC, но до настройки окна и первого `GET_SHOT`, выполняет одинаковую проверку:

1. `TST_SIG` выводится на AMUX, REF1/REF2 программируются из выбранной таблицы.
2. Осциллограф: CH1 = TST_SIG, CH4 = CTRL, оба входа DC 1 МОм.
3. Trigger: CH4, отрицательный фронт, 0.5 В; развертка 500 нс/дел.
4. Для каждой ступеньки снимается raw-кадр при `FCLK=0`, затем при рабочей FCLK.
5. Ступенька CH1 считается по медианам плато до и после фронта CH4.
6. Проверяется ошибка относительно LUT, по умолчанию не более 1 мВ.
7. Восстанавливаются `TEST_MUX`, REF1, REF2, CTRL=0 и рабочая FCLK.

Raw CH1/CH4 сохраняются в
`reference_verification/run_TIMESTAMP/waveforms/*.csv`; рядом находятся
`capture_metrics.csv`, `clk_comparison.csv`, JSON результата и сводный PNG.
В `clk_comparison.csv` отдельно записано изменение ступеньки и шума плато при
включении CLK. При ошибке данные сохраняются, затем тест безопасно прерывается.
Вертикальные масштабы, окна плато, число повторных попыток и допуск находятся в
`characterization_config.py`. Если нужно только проверить ступеньки, запустите
`run_reference_verification.py`.

Серия по параметрам EO_CFG задается декартовым произведением:

```python
EO_PARAMETER_GRID = {
    "DAC_CMP_BIAS_LSB": [200, 500],
    "DAC_CMP_VB5": [500, 1000],
}
EO_OVERRIDES = None
RESUME_SWEEP = None
```

Это четыре последовательных независимых эксперимента в папках
`DAC_CMP_BIAS_LSB=.../DAC_CMP_VB5=...`, у каждого свои raw, графики и
рекомендации. После сбоя укажите корневую папку серии в `RESUME_SWEEP`, не меняя
grid и остальные входы. Завершенные комбинации будут проверены и пропущены,
незавершенная продолжится с сохраненных acquisition. Для одного набора вместо
grid используйте `EO_OVERRIDES`. Пороговые ЦАП и REF, которыми владеет внутренний
scan, а также OMR/ICR/DCR через этот интерфейс свипировать нельзя.

При `upo_pwm` число импульсов отдельно не задается. Анализ вычисляет
`N_nom=round(F_real*T_shutter)`: при 100 kHz и 0.010 s это 1000 отрицательных
фронтов с неопределенностью границы +/-1. `N_INJECTIONS` в этом режиме
игнорируется. Экспозицию 0.010 s нужно один раз указать в конфиге и вручную
установить в GUI УПО как `10000 мкс`. PWM включается только для signal-shot.
Background всегда выполняется при `CTRL=0`. После `GET_SHOT` PWM выключается до первого
`GET_PIXEL`. В начале нового или возобновленного теста CTRL также принудительно
переводится в 0 до конфигурации ASIC.

Пороговый ЦАП S-curve по умолчанию сканируется от кода 1023 к коду 0. Цель
такого направления: измерить полезную положительную инжекцию на отрицательном
фронте CTRL и не продолжать проход далеко ниже шумовой базовой линии к отклику
противоположной полярности от положительного фронта.

В режиме `paired` каждый signal имеет собственный background. В стандартном
`sparse` снимаются контрольные background: в начале, периодически, при первом
отклике, около перехода и во всей шумовой области. Пропущенные B остаются NaN;
для fit используется сигнал без вычитания фона. Пригодность проверяется по
максимуму двух окружающих контрольных точек текущего запуска.

Адаптивный проход идет от большого DAC к меньшему. В пустой области используется
крупный шаг. При первом ненулевом отклике пропущенные коды заполняются с шагом 1.
Переход V50 и шумовой колокол измеряются с шагом 1. Крупный шаг на чистом плато
разрешается после его подтверждения. Для ступенек меньше 25 мВ шаг 1 сохраняется
от первого отклика до шума. Пустые точки и плато измеряются одним повтором;
переход 10-90% получает полное SCURVE_REPEATS. Невалидный отсчет не считается нулем.

После обнаружения шума проход продолжается через максимум и спад к уровню N;
защищенная область из noise scan проходится полностью. Raw-count графики
сохраняют колокол и его левое плечо до N. Нормированные графики и fit используют
пригодную физическую ветвь. Решения записываются в online/scurve_sampling_*.csv.
В tile_crosstalk выполняются полные пары и повторы для статистики S-B неактивных
пикселей, включая отрицательные изменения: inactive_noise_statistics.csv.

Офлайн можно передать и каталог только с `noise_statistics.csv`. Будет создан
`reanalysis/vNNN`; такой пересчет явно помечается как анализ без исходных raw.
Дополнительная маска: `--bad-pixels configs/bad_pixels.json`. Полная инструкция
на русском находится в `COMPARATOR_CHARACTERIZATION.md` в корне проекта.

`run_clock_noise.py` после применения trim-карты выполняет новый noise scan,
затем S-curve тест одной
REF-ступеньки при массиве измерительных FCLK. Старый noise reference не нужен.
Если задан `CLOCK_NOISE_TRIM_REFERENCE_EXPERIMENT`, из него копируется только
финальная trim-карта нужного окна. Старые counts, границы и шумовая статистика
не используются. Для tile-режима
скрипт проходит все фазы подматрицы, поэтому каждый выбранный пиксель реально
инжектируется. Итоги находятся в `measurement_clock_noise_*.csv`, графиках и
`REPORT.md`.

Краткий статус и проценты видны в консоли и сохраняются в
`results/EXPERIMENT/experiment.log`. Noise scan всегда посещает весь заданный
список DAC-кодов. После настроенного числа валидных нулевых снимков он пропускает
лишь оставшиеся повторы текущего кода. Параметры оптимизации и переподключения
УПО находятся в `characterization_config.py`.

## Метрики и новые настройки

Полный справочник формул, единиц, q90/q95, fit, FCLK, наводок и PCA:
[METRICS.md](../METRICS.md).

Настройки в characterization_config.py:

```python
SCURVE_BACKGROUND_MODE = "sparse"  # Или "paired".
SCURVE_SPARSE_BACKGROUND_INTERVAL_CODES = 16
SCURVE_REPEATS = 1
SCURVE_ADAPTIVE_REPEATS = True
SCURVE_WEAK_SIGNAL_STEP_THRESHOLD_V = 0.025
SCURVE_TILE_MODE = "tile_measurement"  # Или "tile_crosstalk".
PLOT_SQUARE_PHYSICAL_PIXELS = False
PLOT_LANGUAGE = "ru"  # Или "en".
CLOCK_NOISE_TRIM_REFERENCE_EXPERIMENT = None  # Каталог эквализации окна или ALL.
```

Сохранены экспозиции исходного архива: NOISE_SHUTTER_DURATION_S=0.001 и
SCURVE_SHUTTER_DURATION_S=0.010 с. При равных значениях переходы между шумом,
S-кривыми и окнами ALL не требуют Enter. Экспозицию УПО задайте перед стартом.

Графики одного физического пикселя для AB/CMP_B, BC/CMP_C, CD/CMP_D сохраняются
в analysis/vNNN/plots/pixels: три панели подстройки, шума и сигнального счета;
зависимости V50 от заряда накладываются. Выбор: PLOT_PIXELS или автоматическая
выборка. Язык и геометрия локальной страницы берутся из указанных настроек.

Для восстановления графиков уже измеренного ALL без новых снимков:

```bash
python -m comparator_characterization.high_level.analyze_all_windows "results/EXPERIMENT_ALL" --language ru
```
