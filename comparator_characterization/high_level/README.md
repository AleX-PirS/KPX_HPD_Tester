# Верхнеуровневые запуски

Все параметры пользователя собраны в `characterization_config.py`. Файлы
`run_*.py` запускают конкретные измерения, `preview_ref_selection.py` проверяет
выбор REF1/REF2 без стенда, а `plot_characterization.py` повторно анализирует
сохраненный эксперимент.

Запускайте команды из корня проекта. Рекомендуемый вариант:

```bash
python -m comparator_characterization.high_level.preview_ref_selection
python -m comparator_characterization.high_level.run_noise_scan
python -m comparator_characterization.high_level.run_noise_equalization
python -m comparator_characterization.high_level.run_full_trim_sweep
python -m comparator_characterization.high_level.run_scurve
python -m comparator_characterization.high_level.run_full_characterization
python -m comparator_characterization.high_level.run_crosstalk
python -m comparator_characterization.high_level.plot_characterization results/EXPERIMENT
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
`EO_cfg.DEFAULT_REGISTERS` и стандартную PX-конфигурацию всей принадлежащей
половины. `PX_MASK=0` отключает цифровой счет, поэтому скрипт включает
`PX_MASK=1` только для выбранных тестом пикселей, кроме `BAD_PIXEL_MAP`.
Исключенные пиксели всегда имеют `MASK=0, TST_EN=0`, включая reconnect и cleanup.

Основные новые настройки в `characterization_config.py`:

```python
MINIMUM_REFERENCE_CODE = 401
MAXIMUM_REFERENCE_CODE = 900  # пример; штатный default 1023
NOISE_COARSE_START = 400     # пример, подберите по своему пилотному скану
NOISE_COARSE_STOP = 900
NOISE_COARSE_STEP = 4
BAD_PIXEL_MAP = [(16, 0), (20, 5)]  # либо путь CSV/JSON, либо None
```

Не исключайте пиксели только из-за одного нулевого endpoint или неудачного fit.
В конце печатаются пути к `trim_recommendations_fit/centroid/maximum.csv` и
`bad_pixels_suggested_fit/centroid/maximum.json`. Это предложения, не применяемые
автоматически. Причины и надежность находятся в CSV, сводка в
`recommendation_summary.csv`, карты в `plots/`.

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
