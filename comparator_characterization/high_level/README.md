# Верхнеуровневые запуски

Все параметры пользователя собраны в `characterization_config.py`. Файлы
`run_*.py` запускают конкретные измерения, `preview_ref_selection.py` проверяет
выбор REF1/REF2 без стенда, а `plot_characterization.py` повторно анализирует
сохраненный эксперимент.

Запускайте команды из корня проекта. Рекомендуемый вариант:

```bash
python -m comparator_characterization.high_level.preview_ref_selection
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
