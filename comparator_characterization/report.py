from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .storage import atomic_write_text, utc_now_text


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if math.isfinite(result) else float("nan")


def _fmt(value: Any, *, digits: int = 3, suffix: str = "") -> str:
    number = _number(value)
    if not math.isfinite(number):
        return "н/д"
    return f"{number:.{digits}f}{suffix}"


def _int_or_zero(value: Any) -> int:
    number = _number(value)
    return int(number) if math.isfinite(number) else 0


def _median(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame:
        return float("nan")
    values = pd.to_numeric(frame[column], errors="coerce")
    values = values[np.isfinite(values)]
    return float(values.median()) if len(values) else float("nan")


def _mad(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame:
        return float("nan")
    values = pd.to_numeric(frame[column], errors="coerce")
    values = values[np.isfinite(values)]
    if not len(values):
        return float("nan")
    median = float(values.median())
    return float((values - median).abs().median())


def _std(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame:
        return float("nan")
    values = pd.to_numeric(frame[column], errors="coerce")
    values = values[np.isfinite(values)]
    return float(values.std(ddof=1)) if len(values) > 1 else float("nan")


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["Данные отсутствуют."]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(str(value).replace("|", "\\|") for value in row)
            + " |"
        )
    return lines


def _relative_link(analysis_directory: Path, path: Path, label: str) -> str:
    relative = os.path.relpath(path, analysis_directory).replace(os.sep, "/")
    return f"[{label}]({relative})"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size <= 2:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, OSError):
        return pd.DataFrame()


def _dac_grid_description(frame: pd.DataFrame) -> str:
    if frame.empty or "threshold_dac_code" not in frame:
        return "нет"
    codes = np.sort(
        pd.to_numeric(frame["threshold_dac_code"], errors="coerce")
        .dropna()
        .unique()
        .astype(float)
    )
    if not len(codes):
        return "нет"
    minimum_step = float(np.min(np.diff(codes))) if len(codes) > 1 else float("nan")
    step_text = _fmt(minimum_step, digits=0) if math.isfinite(minimum_step) else "н/д"
    return (
        f"N={len(codes)}, диапазон {_fmt(codes[0], digits=0)}.."
        f"{_fmt(codes[-1], digits=0)}, min step {step_text}"
    )


def _key_figures(analysis_directory: Path) -> list[tuple[str, Path]]:
    plot_directory = analysis_directory / "plots"
    candidates = [
        ("Распределения порогов", plot_directory / "threshold_distributions_individual_scale.png"),
        ("Матрица до и после эквализации", plot_directory / "baseline_equalization_overview.png"),
        ("S-кривые матрицы", plot_directory / "matrix_scurves_all.png"),
        ("Выбор REF и common-mode", plot_directory / "reference_pair_selection.png"),
        ("Качество S-curve fit", plot_directory / "scurve_fit_quality.png"),
    ]
    return [(label, path) for label, path in candidates if path.exists()]


def generate_analysis_report(
    *,
    analysis_directory: Path,
    experiment_root: Path | None,
    metadata: Mapping[str, Any] | None,
    coverage: Mapping[str, Any],
    noise_statistics: pd.DataFrame,
    noise_fits: pd.DataFrame,
    scurve_efficiency: pd.DataFrame,
    scurve_results: pd.DataFrame,
    scurve_amplitude_summary: pd.DataFrame,
    scurve_gain_results: pd.DataFrame,
    scurve_branch_summary: pd.DataFrame,
    scurve_transition_precision: pd.DataFrame,
    crosstalk_summary: pd.DataFrame,
    target_voltage: float | None,
) -> Path:
    """Create a concise Russian measurement report from saved analysis products."""

    metadata = dict(metadata or {})
    report_path = analysis_directory / "REPORT.md"
    experiment_id = metadata.get("experiment_id") or (
        experiment_root.name if experiment_root is not None else "offline_statistics"
    )
    window = metadata.get("window", "н/д")
    comparator = metadata.get("comparator_under_test", "н/д")
    counter = metadata.get("counter_key", "н/д")
    status = metadata.get("status", coverage.get("source_status", "н/д"))
    reference_pairs = (
        _read_csv(experiment_root / "inputs" / "reference_pair_selection.csv")
        if experiment_root is not None
        else pd.DataFrame()
    )
    if reference_pairs.empty and not scurve_efficiency.empty:
        source_columns = {
            "requested_injection_voltage_step_v": "requested_voltage_step_v",
            "injection_voltage_step_v": "actual_voltage_step_v",
            "injection_voltage_step_error_v": "voltage_step_error_v",
            "ref1_dac_code": "ref1_code",
            "ref1_voltage_v": "ref1_voltage_v",
            "ref2_dac_code": "ref2_code",
            "ref2_voltage_v": "ref2_voltage_v",
            "reference_common_mode_v": "reference_common_mode_v",
        }
        available = [
            name for name in source_columns if name in scurve_efficiency.columns
        ]
        if "reference_common_mode_v" in available:
            reference_pairs = (
                scurve_efficiency[available]
                .drop_duplicates()
                .rename(columns=source_columns)
            )
    lines: list[str] = [
        f"# Отчет по характеризации {experiment_id}",
        "",
        f"Отчет создан: `{utc_now_text()}`.",
        "",
        "## Краткий итог",
        "",
    ]

    conclusions: list[str] = []
    stages = set(noise_fits.get("stage", pd.Series(dtype=str)).astype(str))
    final_noise = noise_fits[noise_fits.get("stage", pd.Series(dtype=str)) == "equalized_final"] if not noise_fits.empty else pd.DataFrame()
    if not final_noise.empty:
        center_median = _median(final_noise, "center_selected_v")
        center_std = _std(final_noise, "center_selected_v")
        center_mad = _mad(final_noise, "center_selected_v")
        conclusions.append(
            "Эквализация проверена финальным noise scan: медиана baseline "
            f"{_fmt(center_median, digits=6, suffix=' V')}, "
            f"std {_fmt(1000 * center_std, digits=3, suffix=' mV')}, "
            f"MAD {_fmt(1000 * center_mad, digits=3, suffix=' mV')}."
        )
    elif noise_fits.empty:
        reference = metadata.get("noise_reference", {})
        if reference:
            conclusions.append(
                "Noise scan в этом каталоге не измерялся. Для S-кривой использована "
                "замороженная копия внешнего noise reference."
            )
        else:
            conclusions.append("Noise scan в доступных данных отсутствует.")
    else:
        conclusions.append(
            "Финальный `equalized_final` отсутствует. Карты trim являются "
            "рекомендациями и не считаются экспериментально проверенной эквализацией."
        )

    if not scurve_branch_summary.empty:
        fitted = int(pd.to_numeric(
            scurve_branch_summary.get("fitted_pixel_count"), errors="coerce"
        ).fillna(0).sum())
        step_one = int(pd.to_numeric(
            scurve_branch_summary.get("fine_step_one_pixel_count"), errors="coerce"
        ).fillna(0).sum())
        conclusions.append(
            f"S-curve: V50 найден для {fitted} наборов пиксель-амплитуда; "
            f"у {step_one} из них переход подтвержден fine-сеткой с локальным шагом 1 DAC."
        )
        if not scurve_results.empty and "fit_status" in scurve_results:
            statuses = scurve_results["fit_status"].astype(str).value_counts()
            ok_count = int(statuses.get("ok", 0))
            poor_count = int(statuses.get("poor_quality", 0))
            unbracketed_count = int(statuses.get("transition_not_bracketed", 0))
            conclusions.append(
                "Качество S-curve fit: "
                f"ok {ok_count}, poor_quality {poor_count}, "
                f"transition_not_bracketed {unbracketed_count}."
            )
        ratio = _median(
            scurve_branch_summary, "effective_to_timing_nominal_ratio"
        )
        if math.isfinite(ratio) and abs(ratio - 1.0) > 0.05:
            conclusions.append(
                "PWM: эффективное плато отличается от расчетного `F*T` на "
                f"{_fmt(100 * (ratio - 1.0), digits=1, suffix='%')}. "
                "Это не аппаратный подсчет фронтов, а нормировка по чистому плато."
            )
    if crosstalk_summary.empty or crosstalk_summary.get(
        "injection_pattern", pd.Series(dtype=str)
    ).astype(str).nunique() <= 1:
        conclusions.append(
            "Crosstalk между режимами разбиения не оценен, поскольку измерен только один pattern."
        )
    if not reference_pairs.empty and "reference_common_mode_v" in reference_pairs:
        common_modes = pd.to_numeric(
            reference_pairs["reference_common_mode_v"], errors="coerce"
        ).dropna()
        if len(common_modes):
            common_mode_span_mv = 1000.0 * float(
                common_modes.max() - common_modes.min()
            )
            if common_mode_span_mv > 10.0:
                conclusions.append(
                    "REF common-mode меняется между ступеньками на "
                    f"{_fmt(common_mode_span_mv, digits=3, suffix=' mV')}. "
                    "Амплитудную характеристику нельзя интерпретировать как "
                    "зависимость только от DeltaV без дополнительной проверки."
                )
            else:
                conclusions.append(
                    "REF common-mode согласован для набора ступенек: span "
                    f"{_fmt(common_mode_span_mv, digits=3, suffix=' mV')}."
                )
    for item in conclusions:
        lines.append(f"- {item}")

    lines.extend(
        [
            "",
            "## Конфигурация и происхождение данных",
            "",
            *_markdown_table(
                ["Параметр", "Значение"],
                [
                    ["Окно", window],
                    ["Компаратор", comparator],
                    ["Счетчик", counter],
                    ["Статус эксперимента", status],
                    ["Источник", coverage.get("source_kind", "raw_acquisitions")],
                    ["Target threshold", _fmt(target_voltage, digits=6, suffix=" V")],
                    ["Noise stages", ", ".join(sorted(stages)) or "нет"],
                ],
            ),
        ]
    )
    warning = str(coverage.get("warning_ru", "")).strip()
    if warning:
        lines.extend(["", f"> Ограничение данных: {warning}"])

    eo_overrides = metadata.get("eo_overrides", {})
    if eo_overrides:
        lines.extend(["", "### EO_CFG overrides", ""])
        lines.extend(
            _markdown_table(
                ["Поле", "Код"],
                [[key, value] for key, value in sorted(eo_overrides.items())],
            )
        )

    if experiment_root is not None:
        gain_map = _read_csv(experiment_root / "inputs" / "gain_map.csv")
        if not gain_map.empty and "gain" in gain_map:
            gain = pd.to_numeric(gain_map["gain"], errors="coerce").dropna()
            lines.extend(
                [
                    "",
                    "### Примененная карта GAIN",
                    "",
                    f"Запрограммировано пикселей: {len(gain)}. Диапазон кодов: "
                    f"{_fmt(gain.min(), digits=0)}..{_fmt(gain.max(), digits=0)}, "
                    f"медиана {_fmt(gain.median(), digits=1)}.",
                    "",
                    "Эта карта является примененной, но не оптимальной. Для рекомендации "
                    "оптимального GAIN требуется отдельный sweep GAIN с одинаковыми "
                    "амплитудами, экспозицией и threshold scan.",
                ]
            )

    if not reference_pairs.empty:
        lines.extend(["", "### Выбранные REF1/REF2", ""])
        reference_rows: list[list[Any]] = []
        for _, row in reference_pairs.sort_values(
            "requested_voltage_step_v", na_position="last"
        ).iterrows():
            reference_rows.append(
                [
                    _fmt(1000 * _number(row.get("requested_voltage_step_v")), digits=3),
                    _fmt(1000 * _number(row.get("actual_voltage_step_v")), digits=3),
                    _fmt(1e6 * _number(row.get("voltage_step_error_v")), digits=3),
                    _fmt(row.get("ref1_code"), digits=0),
                    _fmt(row.get("ref1_voltage_v"), digits=6),
                    _fmt(row.get("ref2_code"), digits=0),
                    _fmt(row.get("ref2_voltage_v"), digits=6),
                    _fmt(row.get("reference_common_mode_v"), digits=6),
                ]
            )
        lines.extend(
            _markdown_table(
                [
                    "Requested, mV",
                    "Actual, mV",
                    "Error, uV",
                    "REF1 code",
                    "REF1, V",
                    "REF2 code",
                    "REF2, V",
                    "Common-mode, V",
                ],
                reference_rows,
            )
        )

    if not noise_fits.empty:
        lines.extend(["", "## Noise scan и эквализация", ""])
        noise_rows: list[list[Any]] = []
        for stage in ("trim_00", "trim_31", "equalized_final", "baseline_noise"):
            data = noise_fits[noise_fits["stage"].astype(str) == stage]
            if data.empty:
                continue
            valid = pd.to_numeric(data.get("center_selected_v"), errors="coerce").notna()
            noise_rows.append(
                [
                    stage,
                    int(valid.sum()),
                    _fmt(_median(data, "center_selected_v"), digits=6),
                    _fmt(1000 * _std(data, "center_selected_v"), digits=3),
                    _fmt(1000 * _mad(data, "center_selected_v"), digits=3),
                    _fmt(1000 * _median(data, "sigma_fit_v"), digits=3),
                ]
            )
        lines.extend(
            _markdown_table(
                ["Stage", "Пикселей", "Median, V", "Std, mV", "MAD, mV", "Noise sigma, mV"],
                noise_rows,
            )
        )
        recommendation = _read_csv(analysis_directory / "recommendation_summary.csv")
        if not recommendation.empty:
            lines.extend(["", "### Рекомендованные trim и mask", ""])
            rows = []
            for _, row in recommendation.iterrows():
                method = str(row.get("method", "н/д"))
                trim_file = analysis_directory / f"trim_recommendations_{method}.csv"
                mask_file = analysis_directory / f"bad_pixels_suggested_{method}.json"
                rows.append(
                    [
                        method,
                        _int_or_zero(row.get("proposed_trim_count")),
                        _int_or_zero(
                            row.get("mask_candidate_count_including_user_mask")
                        ),
                        _relative_link(analysis_directory, trim_file, "trim CSV") if trim_file.exists() else "нет",
                        _relative_link(analysis_directory, mask_file, "mask JSON") if mask_file.exists() else "нет",
                    ]
                )
            lines.extend(
                _markdown_table(
                    ["Метод", "Trim предложено", "Mask candidates", "Карта", "Mask"],
                    rows,
                )
            )

    if not scurve_branch_summary.empty:
        lines.extend(["", "## S-кривые", ""])
        amplitude_rows: list[list[Any]] = []
        for _, branch in scurve_branch_summary.sort_values(
            "requested_injection_voltage_step_v", na_position="last"
        ).iterrows():
            stage = str(branch.get("stage", ""))
            result = scurve_results[
                scurve_results.get("stage", pd.Series(dtype=str)).astype(str) == stage
            ] if not scurve_results.empty else pd.DataFrame()
            good = result[
                result.get("fit_status", pd.Series(dtype=str)).astype(str) == "ok"
            ] if not result.empty else pd.DataFrame()
            poor_count = int(
                (result.get("fit_status", pd.Series(dtype=str)).astype(str)
                 == "poor_quality").sum()
            ) if not result.empty else 0
            unbracketed_count = int(
                (result.get("fit_status", pd.Series(dtype=str)).astype(str)
                 == "transition_not_bracketed").sum()
            ) if not result.empty else 0
            amplitude_rows.append(
                [
                    _fmt(1000 * _number(branch.get("injection_voltage_step_v")), digits=3),
                    _fmt(_number(branch.get("injection_charge_electrons")) / 1000.0, digits=3),
                    _fmt(branch.get("baseline_noise_boundary_code"), digits=0),
                    f"{_fmt(branch.get('timing_nominal_injections'), digits=0)} / "
                    f"{_fmt(branch.get('effective_injections_for_analysis'), digits=0)}",
                    f"{len(good)} / {poor_count} / {unbracketed_count}",
                    _fmt(_median(good, "d50_code"), digits=3),
                    _fmt(_median(good, "v50_v"), digits=6),
                    _fmt(_median(good, "sigma_dac_codes"), digits=3),
                    _fmt(1000 * _median(good, "sigma_v"), digits=3),
                    f"{_fmt(_median(good, 'fit_lower_plateau_efficiency'), digits=3)} / "
                    f"{_fmt(_median(good, 'fit_upper_plateau_efficiency'), digits=3)}",
                    _fmt(branch.get("fine_step_one_at_d50_fraction"), digits=3),
                ]
            )
        lines.extend(
            _markdown_table(
                [
                    "Step, mV",
                    "Qnom, ke",
                    "Baseline DAC",
                    "N timing/effective",
                    "Fit ok / poor / no bracket",
                    "D50",
                    "V50, V",
                    "Sigma, DAC",
                    "Sigma, mV",
                    "Fit plateau low/high",
                    "Fine step=1 fraction",
                ],
                amplitude_rows,
            )
        )
        fit_models = sorted(
            {
                str(value)
                for value in scurve_results.get(
                    "fit_model", pd.Series(dtype=str)
                ).dropna()
                if str(value).strip()
            }
        )
        if fit_models:
            lines.extend(
                [
                    "",
                    "Модель fit: `" + "`, `".join(fit_models) + "`. "
                    "Уровни plateau low/high оцениваются для каждого пикселя, "
                    "а V50 соответствует половине их локального динамического "
                    "диапазона. Isotonic-проекция применяется только внутри fit; "
                    "измеренные точки не изменяются.",
                ]
            )
        grid_rows: list[list[Any]] = []
        for _, branch in scurve_branch_summary.sort_values(
            "requested_injection_voltage_step_v", na_position="last"
        ).iterrows():
            stage = str(branch.get("stage", ""))
            stage_points = scurve_efficiency[
                scurve_efficiency.get("stage", pd.Series(dtype=str)).astype(str)
                == stage
            ] if not scurve_efficiency.empty else pd.DataFrame()
            phases = stage_points.get("scan_phase", pd.Series(dtype=str)).astype(str)
            coarse = stage_points[phases == "coarse"]
            fine = stage_points[phases == "fine"]
            expanded = stage_points[phases.str.startswith("expand")]
            pair_count = (
                stage_points["pair_id"].nunique()
                if "pair_id" in stage_points
                else 0
            )
            grid_rows.append(
                [
                    _fmt(
                        1000 * _number(branch.get("injection_voltage_step_v")),
                        digits=3,
                    ),
                    int(pair_count),
                    _dac_grid_description(coarse),
                    _dac_grid_description(expanded),
                    _dac_grid_description(fine),
                ]
            )
        lines.extend(
            [
                "",
                "### Фактически измеренная сетка DAC",
                "",
                *_markdown_table(
                    ["Step, mV", "Парных acquisitions", "Coarse", "Expand", "Fine"],
                    grid_rows,
                ),
            ]
        )
        lines.extend(
            [
                "",
                "`Baseline DAC` получен из paired background. В fit включены только "
                "коды не ниже этой границы. Все точки обратной полярности сохранены "
                "в raw CSV и могут быть показаны на raw-count графиках до локального "
                "шумового максимума, но исключены из физического V50/sigma fit.",
                "",
                "`N effective` для UPO PWM является робастной нормировкой по чистому "
                "плато. Она не заменяет осциллографический или аппаратный счетчик фронтов.",
            ]
        )
        no_fit = scurve_branch_summary[
            pd.to_numeric(
                scurve_branch_summary.get("fitted_pixel_count"), errors="coerce"
            ).fillna(0) == 0
        ]
        if not no_fit.empty:
            amplitudes = ", ".join(
                _fmt(1000 * _number(value), digits=3, suffix=" mV")
                for value in no_fit.get(
                    "injection_voltage_step_v", pd.Series(dtype=float)
                )
            )
            lines.extend(
                [
                    "",
                    f"> Переход не ограничен для амплитуд: {amplitudes}. "
                    "V50 и sigma для них не сообщаются.",
                ]
            )

    if not scurve_gain_results.empty:
        valid_gain = scurve_gain_results[
            pd.to_numeric(scurve_gain_results.get("fit_r2"), errors="coerce") >= 0.8
        ]
        lines.extend(["", "### Амплитудная характеристика ЗЧУ", ""])
        if valid_gain.empty:
            lines.append(
                "Надежный slope V50(Q) не получен: недостаточно качественных амплитудных точек."
            )
        else:
            lines.append(
                f"Пикселей с `R2 >= 0.8`: {len(valid_gain)}. Медианный номинальный "
                f"gain: {_fmt(_median(valid_gain, 'nominal_gain_mv_per_ke'), digits=3, suffix=' mV/ke')}."
            )
            lines.append(
                "Заряд номинальный, его масштаб наследует допуск инжекционной емкости."
            )

    lines.extend(["", "## Crosstalk", ""])
    patterns = (
        sorted(crosstalk_summary["injection_pattern"].dropna().astype(str).unique())
        if not crosstalk_summary.empty and "injection_pattern" in crosstalk_summary
        else []
    )
    if len(patterns) <= 1:
        lines.append(
            "Сравнение crosstalk требует минимум двух режимов из `all`, "
            "`tile_2x2`, `tile_4x4`, `tile_8x8`. В данном анализе сравнение отсутствует."
        )
    else:
        rows = []
        for _, row in crosstalk_summary.iterrows():
            rows.append(
                [
                    row.get("injection_pattern", ""),
                    _fmt(row.get("median_active_pixels_per_shot"), digits=0),
                    _fmt(1000 * _number(row.get("median_abs_delta_v50_v_to_reference")), digits=3),
                    _fmt(row.get("median_sigma_ratio_to_reference"), digits=3),
                    _fmt(row.get("inactive_excess_hit_fraction_p95"), digits=5),
                ]
            )
        lines.extend(
            _markdown_table(
                ["Pattern", "Active pixels", "|Delta V50|, mV", "Sigma ratio", "Inactive excess p95"],
                rows,
            )
        )

    figures = _key_figures(analysis_directory)
    if figures:
        lines.extend(["", "## Основные графики", ""])
        for label, path in figures[:2]:
            relative = os.path.relpath(path, analysis_directory).replace(os.sep, "/")
            lines.extend([f"### {label}", "", f"![{label}]({relative})", ""])

    lines.extend(
        [
            "## Ключевые файлы",
            "",
        ]
    )
    file_rows = []
    for filename, label in (
        ("noise_fit_results.csv", "Noise fit по пикселям"),
        ("scurve_efficiency.csv", "Paired S-curve points"),
        ("scurve_results.csv", "V50 и sigma по пикселям"),
        ("scurve_branch_summary.csv", "Границы ветви и denominator"),
        ("scurve_transition_precision.csv", "Проверка fine шага около V50"),
        ("injection_crosstalk_summary.csv", "Crosstalk summary"),
    ):
        path = analysis_directory / filename
        if path.exists():
            file_rows.append([label, _relative_link(analysis_directory, path, filename)])
    if experiment_root is not None:
        reference_path = experiment_root / "inputs" / "reference_pair_selection.csv"
        if reference_path.exists():
            file_rows.append(
                [
                    "Выбор REF1/REF2",
                    _relative_link(
                        analysis_directory,
                        reference_path,
                        "reference_pair_selection.csv",
                    ),
                ]
            )
    lines.extend(_markdown_table(["Содержание", "Файл"], file_rows))

    lines.extend(
        [
            "",
            "## Методические ограничения",
            "",
            "- Raw acquisitions не изменяются при offline-анализе. Фильтрация ветви "
            "отражается отдельными признаками в `scurve_efficiency.csv`.",
            "- `Qnom = Cinj * DeltaV`. Допуск `Cinj` должен учитываться как "
            "систематическая неопределенность масштаба заряда.",
            "- Mask candidates являются рекомендацией по качеству данных, а не "
            "доказательством физически битого пикселя.",
            "- Оптимальный GAIN нельзя выбирать без явного sweep GAIN и сравнения "
            "одинаковых тестовых условий.",
            "",
            "Методическая структура отчета следует принятой практике: baseline "
            "equalization по threshold scan, per-pixel распределения, карты trim/noise, "
            "S-кривые при нескольких зарядах и явный учет допуска test-pulse capacitance. "
            "См. [Timepix4 front-end characterization](https://arxiv.org/abs/2203.15912) "
            "и [Medipix4 electrical measurements](https://arxiv.org/abs/2310.10188).",
        ]
    )
    return atomic_write_text(report_path, "\n".join(lines))
