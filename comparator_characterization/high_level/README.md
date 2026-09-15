# Анализ компараторов, версия 2

Настройки старых версий больше не поддерживаются. Уже измеренные результаты
по-прежнему можно анализировать и использовать как noise/trim/S-curve референсы.
Аппаратное продолжение прерванного теста допускается только для тестов v2.

## Где что задается

| Файл | Что редактировать |
| --- | --- |
| `.env` в корне проекта | Только пути: LUT, результаты, карты, референсы, источники анализа и resume |
| `characterization_config.py` рядом с этим README | Основные блоки тестов: RUN, ACQUISITION, NOISE, SCURVE, REFERENCE, CLOCK_NOISE, GAIN_EQUALIZATION, ALL_WINDOWS, PLOTS |
| `metadata.py` рядом с конфигом | Продвинутые параметры: fit, заряд/Cinj, trim-поиск, веса GAIN-подбора, адаптивный съем, ресурсы компьютера, протокол и scope |

`config_runtime.py`, `config_schema.py`, `env_paths.py` являются реализацией,
обычному пользователю их редактировать не нужно. Новых Python-зависимостей нет.

## Запуск

Из корня проекта:

```shell
python -m comparator_characterization --check-config
python -m comparator_characterization
```

Первая команда проверяет **выбранный** `RUN.test`, нужные ему пути и настройки,
показывает активные ссылки и не открывает приборы. Вторая запускает этот тест.
Аппаратные тесты требуют `RUN.hardware_enabled=True`; по умолчанию False.

Можно выбрать тест на один запуск, не меняя `RUN.test`:

```shell
python -m comparator_characterization --test scurve --check-config
python -m comparator_characterization --test gain_equalization
python -m comparator_characterization --test offline --no-plots
```

Эквивалентный вход: `python comparator_characterization/high_level/run_test.py`.
Отдельные `run_*.py` тоже используют новый конфиг и выполняют preflight.
Старые плоские настройки и прежняя многофлаговая offline CLI удалены.
`--check-config` проверяет условия запуска, но не гарантирует пригодность будущего
fit и успешность связи с прибором. Для реальной GAIN-проверки все полученные
карты дополнительно проверяются после офлайн-подбора, до открытия приборов.

## Какой референс нужен выбранному тесту

Все аппаратные тесты используют `THRESHOLD_LUT_A/B/C/D`.
S-кривые используют `REF_LUT_1/2` только при `REFERENCE.mode="lut"`.
При `REFERENCE.verify_with_scope=True` REF-настройки нужны и noise-тестам.

| `RUN.test` | Что выполняется | Окна | Дополнительная ссылка в `.env` |
| --- | --- | --- | --- |
| `full` | Свежий noise, trim equalization, затем S-кривые | AB/BC/CD/ALL | Не нужна; `SCURVE_NOISE_REFERENCE` игнорируется |
| `all_windows` | Тот же полный тест, принудительно ALL | ALL | Не нужна |
| `noise` | Пилотный noise scan без подбора trim | AB/BC/CD/ALL | Не нужна |
| `equalize` | Noise + trim equalization 0/16/31 и уточнение | AB/BC/CD/ALL | Не нужна |
| `trim_sweep` | Полные сканы всех 32 trim-кодов | AB/BC/CD/ALL | Не нужна |
| `scurve` | Только S-кривые / равномерный GAIN-свип | AB/BC/CD/ALL | **SCURVE_NOISE_REFERENCE** |
| `crosstalk` | S-кривые четырех patterns, неактивные фазы count-enabled | AB/BC/CD | **SCURVE_NOISE_REFERENCE** |
| `clock_noise` | Ширина S-перехода и отклик по measurement FCLK | AB/BC/CD | CLOCK_TRIM_REFERENCE, необязательно, только trim-карта |
| `gain_equalization` | Офлайн-подбор карт для target_codes, опционально реальная проверка | AB/BC/CD/ALL | **GAIN_SWEEP_SOURCE** |
| `offline` | Повторный анализ результатов с текущим стилем | По данным | **OFFLINE_SOURCE** |
| `dashboard` | Локальная страница пользовательских графиков | По данным | **OFFLINE_SOURCE** |
| `ref_preview` | Расчет/показ реализуемых REF-ступенек без стенда | Не существенно | REF_LUT_1/2 только в LUT-режиме |
| `reference_verification` | AMUX + scope проверка ступенек | AB/BC/CD | REF_LUT_1/2 только в LUT-режиме; включить verify_with_scope |
| `eo_sweep` | Полные тесты декартова произведения EO_SWEEP.grid | AB/BC/CD | Не нужна; resume отдельно |

Наличие ссылки в `.env` не меняет тип теста. У S-кривой и GAIN-свипа один
noise-источник, больше нет общей ссылки плюс второго источника с приоритетом.
Clock-тест не берет из него counts/шумовые границы: он переносит только trims
из своей отдельной ссылки. GAIN-проверка переносит параметры из исходного свипа.

## `.env`: правила путей

Рабочий `.env` входит в архив. Если его нет, скопируйте `.env.example` в `.env`.
Относительные пути считаются от корня проекта независимо от текущего каталога.
Пути с пробелами можно заключать в одинарные или двойные кавычки. Обратный слеш
не является escape-последовательностью, `r"..."` писать не нужно.

```dotenv
RESULTS_DIR=results
SCURVE_NOISE_REFERENCE="C:\Users\Administrator\Desktop\MO testing\results\noise_eq"
CLOCK_TRIM_REFERENCE="C:\tests\results\noise_eq"
GAIN_SWEEP_SOURCE="C:\tests\results\ALL_gain_sweep"
OFFLINE_SOURCE="C:\tests\results\ALL_gain_sweep"
```

Пустое значение отключает необязательную ссылку:

```dotenv
CLOCK_TRIM_REFERENCE=
# SCURVE_NOISE_REFERENCE="C:\tests\results\previous_reference"
```

Архивные ссылки храните комментариями. Два активных одинаковых ключа, неизвестный
ключ или неправильные кавычки приводят к понятной ошибке с номером строки.
Переменные окружения ОС не переопределяют `.env`. Подстановки `$VAR`, `%VAR%`
и escape-преобразования не выполняются; указывайте сами пути.
Встроенные пути не служат запасными источниками при пустом ключе.

`BASE_PIXEL_CONFIG` и `BAD_PIXEL_MASK` необязательны. При отсутствии base config
используется встроенная стандартная PX-конфигурация с trims=16.
`GAIN_MAP_CSV` применяется только при `SCURVE.gain_source="csv"`. Оставшаяся
ссылка на эту карту не мешает равномерному GAIN или свипу при gain_source="uniform".

## Примеры основных сценариев

В примерах изменяются поля уже созданных блоков в `characterization_config.py`.

### Полный тест всех окон

```python
RUN.test = "full"
RUN.window = "ALL"
RUN.hardware_enabled = True
RUN.resume = False
SCURVE.gain_source = "uniform"
SCURVE.gain = 10
REFERENCE.mode = "lut"
```

В `.env` нужны LUT пороговых ЦАП, REF LUT и RESULTS_DIR. Старый noise-референс
не используется. Noise и S-кривые используют времена из ACQUISITION. Если они
одинаковы, Enter между этапами не ожидается. Установите эти времена в GUI УПО:
Python не имеет readback реального shutter и не выставляет его автоматически.

`ALL_WINDOWS.final_q_sweep` включает дополнительный совместный Q-sweep.
При manual REF и GAIN-свипе он недоступен и явно отмечается как отключенный.

### S-кривые с перебором усилений, по уже пройденному noise

```python
RUN.test = "scurve"
RUN.window = "ALL"
RUN.hardware_enabled = True
SCURVE.gain_source = "uniform"
SCURVE.gain = (4, 8, 10, 12, 20)
```

В `.env` заполните SCURVE_NOISE_REFERENCE. Для ALL укажите родительский noise
эксперимент со всеми тремя окнами. Свежий noise/trim тест здесь не выполняется.
Коды сохраняются отдельно в gain_XX. Референс передает финальные comparator trims
и шумовые границы, но старый background не подставляется вместо новых counts.

### Офлайн GAIN-подбор и его аппаратная проверка

```python
RUN.test = "gain_equalization"
RUN.window = "ALL"
GAIN_EQUALIZATION.target_codes = [4, 10, 20]
GAIN_EQUALIZATION.check_map = False
```

Заполните GAIN_SWEEP_SOURCE. Равномерные целевые коды должны реально
присутствовать в исходном свипе. Веса и target statistic задаются в metadata.py:

```python
GAIN_EQUALIZATION.target_statistic = "median"
GAIN_EQUALIZATION.amplitude_weight = 1.0
GAIN_EQUALIZATION.gain_weight = 1.0
```

Этот последний фрагмент относится к `metadata.py`, не к основному конфигу.
При одной ступеньке усиление вычисляется как A/Q. При переносе общей базы
между GAIN это предположение явно отмечается в CSV.

Чтобы после офлайн-анализа реально применить полученные карты:

```python
RUN.hardware_enabled = True
GAIN_EQUALIZATION.check_map = True
GAIN_EQUALIZATION.check_all_windows = True
GAIN_EQUALIZATION.map_reference_window = "AB"  # явный выбор общей карты
```

В реальной проверке используются source trims, EO, REF-пары, Cinj и экспозиция,
а не текущие GAIN/base-карты и новые REF-ступеньки. ACQUISITION.scurve_shutter_s
и CTRL/PWM timing должны соответствовать исходному свипу. При несоответствии
запуск отклоняется до открытия приборов. Noise повторяется с уже примененной
индивидуальной GAIN-картой, после него снимаются S-кривые. Между ними Enter нет.
Произвольного автоматического trim-уравнивания трех окон нет.

Результаты аппаратной проверки находятся в `RESULTS_DIR/gain_check/vMMM`.
Внутренние проходы имеют короткие каталоги `rNNN_WINDOW_gXX`; соответствие
FCLK, pattern, target GAIN и исходному окну записано в manifest. Такая структура
не дублирует длинный путь source-анализа и укладывается в обычный лимит Windows.
Максимальная ожидаемая длина проверяется до открытия приборов. Если сам
`RESULTS_DIR` расположен слишком глубоко, запуск попросит сократить его.
Есть measured_gain_map.csv, реальные pixel metrics, сравнение до/прогноз/измерение,
сводки разброса и совместные AB/BC/CD графики. Неудачные отклики остаются NaN
с причиной, не становятся нулями. При сбое завершенные raw/CSV сохраняются.

### Clock noise с сохраненными trims

```python
RUN.test = "clock_noise"
RUN.window = "AB"
RUN.hardware_enabled = True
CLOCK_NOISE.fclk_mhz = (5, 10, 25, 50, 100)
```

В `.env` необязательно заполните CLOCK_TRIM_REFERENCE. Если ссылка пуста,
используются стандартные trims. Измеряется собственный S-отклик при каждом FCLK,
старые шумовые counts не переносятся. В manual REF используется manual_step_mv;
CLOCK_NOISE.step_mv относится только к LUT-режиму.

### Ручные REF без LUT

```python
REFERENCE.mode = "manual"
REFERENCE.manual_ref1 = 600
REFERENCE.manual_ref2 = 800
REFERENCE.manual_step_mv = 100.0
```

Ровно одна ступенька, заряд считается по пользовательскому эквиваленту и Cinj.
Это не измеренная LUT-ступенька и не доказательство физического порядка уровней.

### Продолжение прерванного теста

В `.env` задайте RESUME_EXPERIMENT для одиночного/ALL теста либо EO_SWEEP_RESUME
для eo_sweep. Затем установите RUN.resume=True. Не изменяйте физические параметры.
Noise-reference и resume являются разными ролями, никакого взаимного fallback нет.
При RUN.resume=False эти ссылки не используются, даже если заполнены.
Resume старых версий отвергается; их завершенные результаты остаются доступны.

## Метаданные и сохранность

Все raw повторы и непригодные точки сохраняются прежним измерительным workflow.
Hardware metadata дополнены analysis_configuration_v2, офлайн результаты
configuration_v2.json: выбранный тест, блоки настроек, advanced metadata,
разрешенные пути и SHA256 исходного `.env`. Старые результаты не перезаписываются.
В аппаратной GAIN-проверке дополнительно сохранены фактически повторенные
source-настройки; configuration_v2 содержит пользовательские входные настройки.

Полная методика: [COMPARATOR_CHARACTERIZATION.md](../../COMPARATOR_CHARACTERIZATION.md).
Формулы и значения метрик: [METRICS.md](../METRICS.md).
