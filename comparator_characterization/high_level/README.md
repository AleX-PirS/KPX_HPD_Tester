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
python -m comparator_characterization.high_level.run_eo_parameter_sweep
python -m comparator_characterization.high_level.run_crosstalk
python -m comparator_characterization.high_level.plot_characterization results/EXPERIMENT
python -m comparator_characterization.high_level.run_plot_dashboard results/EXPERIMENT
```

Допустим и прямой запуск файла, например:

```bash
python comparator_characterization/high_level/run_scurve.py
```

Перед реальным измерением проверьте все пути и параметры, затем осознанно
установите `ENABLE_HARDWARE_RUN = True`.

`run_noise_scan.py` подходит для короткого пилота с ограниченной областью DAC:
он не измеряет trim 0/31 и не проводит эквализацию, а сохраняет baseline noise
при исходных trim (16 по умолчанию). Для подстроек нужны endpoint/full trim
данные из `run_noise_equalization.py` или `run_full_trim_sweep.py`.

Каждый аппаратный запуск автоматически устанавливает FCLK 50 МГц, global
`EO_cfg.DEFAULT_REGISTERS` и PX-конфигурацию всей физической матрицы.
В другой половине, Col 0..15, Row 0..31, все 512 слов равны `0x00000000`.
Для вашей половины Col 16..31 сохраняется стандартная логика теста.
Нулевая половина также заново загружается при reconnect и не участвует в
измерениях. `PX_MASK=0` отключает цифровой счет, поэтому скрипт включает
`PX_MASK=1` только для выбранных тестом пикселей, кроме `BAD_PIXEL_MAP`.
Исключенные пиксели всегда имеют `MASK=0, TST_EN=0`, включая reconnect и cleanup.
PX сначала полностью ставятся в виртуальную память УПО. Отдельный
`WRITE_TO_CHIP` перед съемом не вызывается: единственную загрузку матрицы в ASIC
делает `GET_SHOT` в своей фазе `Load settings`. Сам `GET_SHOT` выполняется в
основном потоке и полностью завершается до первого `GET_PIXEL`. В основном
режиме CTRL также управляется последовательно через тот же канал УПО:
`PWM -> GET_SHOT -> CTRL=0 -> GET_PIXEL`. Во время теста не нажимайте команды в
отдельном GUI УПО, поскольку межпроцессную конкуренцию Python заблокировать не может.

Для AB свипируется B, A устанавливается на верхнюю границу по LUT,
C и D получают код 1023. Аналогично для BC компараторы вне окна A/D получают
1023, для CD это A/B. Фиксированные пороги также восстанавливаются при reconnect.

Основные новые настройки в `characterization_config.py`:

```python
CTRL_INJECTION_SOURCE = "upo_pwm"
UPO_CTRL_FREQUENCY_KHZ = 100
UPO_CTRL_HIGH_TIME_NS = 5000
SCURVE_SHUTTER_DURATION_S = 0.010
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
BAD_PIXEL_MAP = [(16, 0), (20, 5)]  # либо путь CSV/JSON, либо None
```

Для полного набора амплитуд REF-пары выбираются совместно. Алгоритм находит один
самый низкий по измеренному напряжению уровень REF1, на котором все требуемые
ступеньки достижимы с ошибкой не более 1 мВ. Этот REF1 остается одинаковым во
всех точках, меняется только REF2. Для каждой пары обязательно проверяется
`V_REF1 > V_REF2`; сравнение выполняется по напряжению LUT, а не по коду.
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
установить в GUI УПО как `10000 мкс`. PWM включается только для signal-shot;
paired background всегда выполняется при `CTRL=0`. После `GET_SHOT` PWM выключается до первого
`GET_PIXEL`. В начале нового или возобновленного теста CTRL также принудительно
переводится в 0 до конфигурации ASIC.

Пороговый ЦАП S-curve по умолчанию сканируется от кода 1023 к коду 0. Цель
такого направления: измерить полезную положительную инжекцию на отрицательном
фронте CTRL и не продолжать проход далеко ниже шумовой базовой линии к отклику
противоположной полярности от положительного фронта.

На каждом коде сначала полностью сохраняется paired background, затем signal.
После точки скрипт проверяет background. При стандартных настройках точка
считается шумовой, если минимум 10% валидных пикселей имеют счет строго больше
расчетного `N_timing`. Coarse с шагом 8 останавливается после первой полностью
сохраненной шумовой точки: требование двух соседних coarse-точек могло
перепрыгнуть узкий пик baseline и снова отправить scan к коду 0. Затем fine с
шагом 1 полностью сохраняет две соседние шумовые точки и только после этого
останавливается. Поэтому характерные 1-2 точки со счетом намного выше плато
остаются в raw, но противоположная ветвь не сканируется. Для PWM `N_timing`
масштабируется с экспозицией УПО: при 1 мс и 100 кГц это 100, при 100 мс это
10000.

Coarse-проход выполняется с шагом `SCURVE_COARSE_STEP`. После него переход 50%
ищется отдельно для каждого пикселя, включая прямой переход между соседними
coarse-точками с эффективностью около 0 и 1. Эти интервалы переснимаются с
`SCURVE_FINE_STEP=1`, поэтому крупный coarse-шаг не становится шагом итогового
определения V50. Noise reference используется для контроля фона и fit, но больше
не прореживает программируемую сетку S-curve.

В `analysis/vNNN/plots` дополнительно сохраняются `matrix_scurve_raw_counts_*`
и `pixel_*_scurve_raw_counts_*`. Они показывают signal, paired background,
линию эффективной нормировки и сохраненные точки шумовой базовой линии на
логарифмически удобной шкале. DAC-код на всех S-curve графиках возрастает слева
направо. На raw-count графиках масштаб слева доходит до ближайшего
подтвержденного локального максимума монотонного шумового плеча, но не дальше
чем на `SCURVE_PLOT_NOISE_PEAK_SEARCH_CODES`. Требование второй поддерживающей
точки защищает масштаб от одиночного выброса. Эта дополнительная область
является только визуальной: V50-fit по-прежнему начинается выше baseline.
Справа остается короткий подтвержденный нулевой хвост.

Итоговая S-curve линия использует попиксельные нижнее/верхнее плато, взвешенную
монотонную PAVA-проекцию и только центральные
`SCURVE_FIT_CORE_LOW_FRACTION..SCURVE_FIT_CORE_HIGH_FRACTION` перехода. Это
уменьшает ложное уширение sigma из-за гуляющего PWM-плато. Исходные точки не
сглаживаются и не перезаписываются.

Каждый аппаратный и офлайн-анализ создает `analysis/vNNN/REPORT.md`. В нем есть
конфигурация, фактически измеренные coarse/fine диапазоны, timing и effective
PWM normalization, V50/sigma, проверка шага 1 около D50, trim/mask рекомендации,
ограничения GAIN и до двух основных графиков.

## Локальная страница дополнительных графиков

Автоматические графики в `analysis/vNNN/plots` остаются без изменений. Для
дополнительного оформления запустите:

```bash
python -m comparator_characterization.high_level.run_plot_dashboard results/EXPERIMENT
```

Откроется локальный адрес `127.0.0.1`; данные во внешнюю сеть не отправляются.
На странице можно выбрать отдельный график или набор, конкретный пиксель,
noise-stage, `all/tile_2x2/tile_4x4/tile_8x8`, амплитуду, русский или английский
язык, размеры заголовка/осей/делений/легенды, квадратные или растянутые ячейки
матрицы, PNG/PDF/SVG и явные границы DAC. Для S-кривой стандартно показывается
только положительная физическая ветвь; вариант `Полная / bipolar` оставляет обе
полярности. Новые файлы и точный JSON запроса сохраняются в
`analysis/vNNN/custom_plots/render_TIMESTAMP`.

Резервный `keysight_burst` сохраняет задержку 0.8 s, `*TRG` и конечное число
периодов `N_INJECTIONS`. В основном режиме `upo_pwm` внешний генератор и VISA не
открываются.

Не исключайте пиксели только из-за одного нулевого endpoint или неудачного fit.
В конце печатаются пути к `trim_recommendations_fit/centroid/maximum.csv` и
`bad_pixels_suggested_fit/centroid/maximum.json`. Это предложения, не применяемые
автоматически. Причины и надежность находятся в CSV, сводка в
`recommendation_summary.csv`, карты в `plots/`.

## Параллельный анализ

Дополнительных зависимостей нет. В `characterization_config.py` доступны:

```python
ANALYSIS_WORKERS = 0  # авто, до 8 процессов; 1 = последовательно
PLOT_WORKERS = 0      # авто, до 4 процессов PNG/PDF
RAW_READ_WORKERS = 0  # авто, до 8 потоков чтения CSV
ANALYSIS_PARALLEL_MIN_GROUPS = 2048
```

Для офлайн-запуска используйте отдельные CLI-настройки:

```bash
python -m comparator_characterization.high_level.plot_characterization results/EXPERIMENT --workers 8 --plot-workers 4 --read-workers 8
```

Статистика повторов ускорена векторными операциями независимо от числа процессов.
Небольшие наборы fit не распараллеливаются, чтобы избежать затрат на `spawn`.
В собственном запускаемом Python-файле обязательно поместите вызов анализа
в `if __name__ == "__main__":`. Все готовые скрипты это уже делают.
Настройка касается только анализа и рисунков, команды УПО не распараллеливаются.
Чем больше процессов отрисовки, тем больше требуется RAM.

Изменение числа процессов не запрещает resume, но изменение аппаратных условий
запрещает: старый эксперимент без фиксации неиспользуемых порогов на 1023 нужно
пересчитывать офлайн, а новый съем проводить в новом каталоге.

## GAIN из кода или файла

Для S-кривых, полного теста и теста наводок задайте в
`characterization_config.py` ровно один источник: `GAIN_MAP` либо
`GAIN_MAP_CSV`. При задании обоих источников будет ошибка.

Словарь использует физические координаты `(column, row)`:

```python
GAIN_MAP_CSV = None
GAIN_MAP = {(column, row): 10 for row in range(32) for column in range(16, 32)}
GAIN_MAP[(20, 7)] = 12
```

Числа 10 и 12 здесь пример: замените их своими кодами GAIN (целые 0..31).
Можно задать список 32 строк по 16 столбцов:

```python
GAIN_MAP_CSV = None
GAIN_MAP = [[10 for _ in range(16)] for _ in range(32)]
GAIN_MAP[7][4] = 12  # физические column=20, row=7
```

Индекс строки равен физическому `row=0..31`, индекс столбца `0..15`
соответствует физическому `column=16..31`. Плоский список из 512 значений
тоже допустим: индекс `row * 16 + (column - 16)`. Для NumPy-массива передайте
`array.tolist()`. Не создавайте строки через `[[10] * 16] * 32`: тогда они
будут ссылаться на один и тот же список.

Либо сохраните прежнюю загрузку файла:

```python
GAIN_MAP = None
GAIN_MAP_CSV = PROJECT_ROOT / "configs" / "gain.csv"  # column,row,gain
```

Словарь/CSV должен покрывать все выбранные исправные пиксели; при `PIXELS="all"`
это вся принадлежащая половина, кроме `BAD_PIXEL_MAP`. Для исключенных пикселей
GAIN не требуется, они остаются `MASK=0, TST_EN=0`. Noise-only тесты не требуют
этих настроек. Независимо от источника примененная карта сохраняется в
`inputs/gain_map.csv` эксперимента и проверяется при resume.

Офлайн можно передать и каталог только с `noise_statistics.csv`. Будет создан
`reanalysis/vNNN`; такой пересчет явно помечается как анализ без исходных raw.
Дополнительная маска: `--bad-pixels configs/bad_pixels.json`. Полная инструкция
на русском находится в `COMPARATOR_CHARACTERIZATION.md` в корне проекта.

Краткий статус и проценты видны в консоли и сохраняются в
`results/EXPERIMENT/experiment.log`. Параметры ранней остановки noise scan и
переподключения УПО находятся в `characterization_config.py`.
