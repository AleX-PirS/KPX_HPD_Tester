"""Local HTML controller for reproducible custom characterization plots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import secrets
import threading
from typing import Any, Iterable
from urllib.parse import unquote, urlparse
import webbrowser

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .storage import atomic_write_json, atomic_write_text


_PLOT_TYPES = {
    "pixel_scurve",
    "matrix_scurve",
    "pixel_raw_counts",
    "noise_pixel",
    "noise_distributions",
    "heatmap",
    "scurve_set",
    "noise_set",
}
_FORMATS = {"png", "pdf", "svg"}
_LANGUAGES = {"ru", "en"}
_GEOMETRIES = {"square", "stretched"}
_BRANCHES = {"positive", "full"}
_HEATMAP_METRICS = {
    "noise_center_v",
    "noise_sigma_v",
    "trim_code",
    "scurve_d50_code",
    "scurve_sigma_codes",
    "scurve_v50_v",
}


@dataclass(frozen=True)
class PlotRenderResult:
    output_directory: Path
    files: tuple[Path, ...]
    request_json: Path


def resolve_analysis_directory(path: str | Path) -> Path:
    """Resolve an analysis/vNNN directory from either it or experiment root."""

    source = Path(path).expanduser().resolve()
    if (source / "analysis_manifest.json").is_file() or any(
        (source / name).is_file()
        for name in ("noise_statistics.csv", "scurve_efficiency.csv")
    ):
        return source
    analysis_root = source / "analysis"
    candidates = sorted(
        (
            item
            for item in analysis_root.glob("v[0-9][0-9][0-9]")
            if item.is_dir()
        ),
        key=lambda item: int(item.name[1:]),
    )
    if not candidates:
        raise FileNotFoundError(
            f"Не найден каталог analysis/vNNN для эксперимента: {source}"
        )
    return candidates[-1]


def _read_csv(path: Path, *, required: bool = False) -> pd.DataFrame:
    if not path.is_file() or path.stat().st_size <= 2:
        if required:
            raise FileNotFoundError(f"Нет данных для выбранного графика: {path}")
        return pd.DataFrame()
    try:
        return pd.read_csv(path, keep_default_na=False)
    except pd.errors.EmptyDataError:
        if required:
            raise ValueError(f"CSV пуст: {path}")
        return pd.DataFrame()


def _bool_series(frame: pd.DataFrame, column: str, *, default: bool = False) -> pd.Series:
    if column not in frame:
        return pd.Series(default, index=frame.index, dtype=bool)
    values = frame[column]
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(default)
    return values.astype(str).str.strip().str.lower().isin(("1", "true", "yes"))


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _request_value(request: dict[str, Any], name: str, default: Any) -> Any:
    value = request.get(name, default)
    return default if value is None else value


def _validated_request(request: dict[str, Any]) -> dict[str, Any]:
    result = dict(request)
    result["plot_type"] = str(_request_value(result, "plot_type", "pixel_scurve"))
    result["language"] = str(_request_value(result, "language", "ru"))
    result["output_format"] = str(_request_value(result, "output_format", "png")).lower()
    result["pixel_geometry"] = str(_request_value(result, "pixel_geometry", "square"))
    result["branch_view"] = str(_request_value(result, "branch_view", "positive"))
    result["heatmap_metric"] = str(
        _request_value(result, "heatmap_metric", "noise_center_v")
    )
    for name, allowed in (
        ("plot_type", _PLOT_TYPES),
        ("language", _LANGUAGES),
        ("output_format", _FORMATS),
        ("pixel_geometry", _GEOMETRIES),
        ("branch_view", _BRANCHES),
        ("heatmap_metric", _HEATMAP_METRICS),
    ):
        if result[name] not in allowed:
            raise ValueError(f"Недопустимое значение {name}: {result[name]}")
    for name, default, lower, upper in (
        ("title_font_size", 14.0, 6.0, 40.0),
        ("axis_font_size", 11.0, 6.0, 30.0),
        ("tick_font_size", 9.0, 5.0, 24.0),
        ("legend_font_size", 9.0, 5.0, 24.0),
        ("dpi", 200.0, 72.0, 600.0),
    ):
        value = _finite(_request_value(result, name, default))
        if value is None or not lower <= value <= upper:
            raise ValueError(f"{name} должен быть в диапазоне {lower:g}..{upper:g}")
        result[name] = value
    for name in ("column", "row"):
        value = _request_value(result, name, None)
        result[name] = None if value in (None, "") else int(value)
    for name in ("dac_min", "dac_max"):
        value = _request_value(result, name, None)
        result[name] = None if value in (None, "") else float(value)
    if (
        result["dac_min"] is not None
        and result["dac_max"] is not None
        and result["dac_min"] >= result["dac_max"]
    ):
        raise ValueError("dac_min должен быть меньше dac_max")
    result["stage"] = str(_request_value(result, "stage", "all"))
    result["injection_pattern"] = str(
        _request_value(result, "injection_pattern", "all")
    )
    amplitude = _request_value(result, "amplitude_index", "all")
    result["amplitude_index"] = "all" if amplitude in (None, "", "all") else int(amplitude)
    return result


class AnalysisPlotData:
    """Lazy in-memory cache for one generated analysis directory."""

    def __init__(self, analysis_directory: str | Path):
        self.analysis_directory = resolve_analysis_directory(analysis_directory)
        self._cache: dict[str, pd.DataFrame] = {}
        self._scurve_subset_cache: dict[tuple[Any, ...], pd.DataFrame] = {}

    def table(self, name: str, *, required: bool = False) -> pd.DataFrame:
        if name not in self._cache:
            self._cache[name] = _read_csv(self.analysis_directory / name)
        frame = self._cache[name]
        if required and frame.empty:
            raise ValueError(f"Таблица {name} отсутствует или пуста")
        return frame

    def _scurve_columns(self) -> list[str]:
        path = self.analysis_directory / "scurve_efficiency.csv"
        if not path.is_file() or path.stat().st_size <= 2:
            raise ValueError("Таблица scurve_efficiency.csv отсутствует или пуста")
        return list(pd.read_csv(path, nrows=0).columns)

    def scurve_points(
        self,
        request: dict[str, Any],
        *,
        pixel: tuple[int, int] | None = None,
    ) -> pd.DataFrame:
        """Read only a requested S-curve slice from a potentially huge CSV."""

        key = (
            request["injection_pattern"],
            request["amplitude_index"],
            request["branch_view"],
            pixel,
        )
        cached = self._scurve_subset_cache.get(key)
        if cached is not None:
            return cached.copy()
        wanted = {
            "stage", "scan_phase", "threshold_dac_code", "threshold_voltage_v",
            "repeat_index", "column", "row", "local_trim_code",
            "injection_pattern", "active_injection_pixel_bool", "signal_count",
            "background_count", "efficiency", "physical_branch_valid",
            "injection_voltage_step_v", "requested_injection_voltage_step_v",
            "injection_charge_electrons", "effective_injections_for_analysis",
        }
        available = self._scurve_columns()
        usecols = [name for name in available if name in wanted]
        frames: list[pd.DataFrame] = []
        path = self.analysis_directory / "scurve_efficiency.csv"
        for chunk in pd.read_csv(path, usecols=usecols, chunksize=100_000, keep_default_na=False):
            selected = _select_scurve(chunk, request)
            if pixel is not None and not selected.empty:
                selected = selected[
                    (_numeric(selected, "column") == pixel[0])
                    & (_numeric(selected, "row") == pixel[1])
                ]
            if not selected.empty:
                frames.append(selected)
        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=usecols)
        # Keep a small number of useful slices, never the full matrix table.
        if pixel is not None and len(self._scurve_subset_cache) < 16:
            self._scurve_subset_cache[key] = result.copy()
        return result

    def scurve_matrix_summary(self, request: dict[str, Any]) -> pd.DataFrame:
        """Stream exact per-code median and 10-90% without retaining the raw matrix."""

        wanted = {
            "stage", "threshold_dac_code", "injection_pattern",
            "active_injection_pixel_bool", "physical_branch_valid", "efficiency",
            "injection_voltage_step_v", "injection_charge_electrons",
        }
        available = self._scurve_columns()
        usecols = [name for name in available if name in wanted]
        values: dict[tuple[str, float], list[np.ndarray]] = {}
        stage_metadata: dict[str, dict[str, float]] = {}
        path = self.analysis_directory / "scurve_efficiency.csv"
        for chunk in pd.read_csv(path, usecols=usecols, chunksize=100_000, keep_default_na=False):
            selected = _select_scurve(chunk, request)
            if "active_injection_pixel_bool" in selected:
                selected = selected[_bool_series(selected, "active_injection_pixel_bool")]
            if selected.empty:
                continue
            selected = selected.assign(
                threshold_dac_code=_numeric(selected, "threshold_dac_code"),
                efficiency=_numeric(selected, "efficiency"),
            ).dropna(subset=["threshold_dac_code", "efficiency"])
            for (stage, code), group in selected.groupby(
                ["stage", "threshold_dac_code"], sort=False
            ):
                values.setdefault((str(stage), float(code)), []).append(
                    group["efficiency"].to_numpy(dtype=float)
                )
            for stage, group in selected.groupby("stage", sort=False):
                if str(stage) not in stage_metadata:
                    stage_metadata[str(stage)] = {
                        "injection_voltage_step_v": float(
                            _numeric(group, "injection_voltage_step_v").dropna().median()
                        ) if _numeric(group, "injection_voltage_step_v").notna().any() else math.nan,
                        "injection_charge_electrons": float(
                            _numeric(group, "injection_charge_electrons").dropna().median()
                        ) if _numeric(group, "injection_charge_electrons").notna().any() else math.nan,
                    }
        rows: list[dict[str, Any]] = []
        for (stage, code), chunks in values.items():
            array = np.concatenate(chunks)
            rows.append(
                {
                    "stage": stage,
                    "threshold_dac_code": code,
                    "median": float(np.median(array)),
                    "low": float(np.quantile(array, 0.10)),
                    "high": float(np.quantile(array, 0.90)),
                    **stage_metadata.get(stage, {}),
                }
            )
        return pd.DataFrame(rows)

    def options(self) -> dict[str, Any]:
        noise = self.table("noise_statistics.csv")
        noise_fit = self.table("noise_fit_results.csv")
        scurve = self.table("scurve_results.csv")
        pixels: set[tuple[int, int]] = set()
        for frame in (noise, noise_fit, scurve):
            if not frame.empty and {"column", "row"}.issubset(frame.columns):
                pairs = zip(_numeric(frame, "column"), _numeric(frame, "row"))
                pixels.update(
                    (int(column), int(row))
                    for column, row in pairs
                    if math.isfinite(column) and math.isfinite(row)
                )
        stages = sorted(
            set(noise.get("stage", pd.Series(dtype=str)).astype(str))
            | set(noise_fit.get("stage", pd.Series(dtype=str)).astype(str))
        )
        patterns = sorted(
            set(scurve.get("injection_pattern", pd.Series(dtype=str)).astype(str))
        )
        amplitudes_by_index: dict[int, dict[str, Any]] = {}
        if not scurve.empty and "stage" in scurve:
            columns = [
                name
                for name in (
                    "stage",
                    "injection_voltage_step_v",
                    "injection_charge_electrons",
                )
                if name in scurve
            ]
            unique = scurve[columns].drop_duplicates("stage")
            for stage, data in unique.sort_values("stage").groupby("stage", sort=True):
                try:
                    index = int(str(stage).split("pulse_amplitude_")[1].split("_")[0])
                except (IndexError, ValueError):
                    continue
                first = data.iloc[0]
                charge = _finite(first.get("injection_charge_electrons"))
                step = _finite(first.get("injection_voltage_step_v"))
                label = f"#{index}"
                if charge is not None:
                    label += f" ({charge / 1000.0:.4g} ke)"
                elif step is not None:
                    label += f" ({1000.0 * step:.4g} mV)"
                amplitudes_by_index.setdefault(index, {"value": index, "label": label})
        amplitudes = [amplitudes_by_index[index] for index in sorted(amplitudes_by_index)]
        default_pixel = min(pixels) if pixels else None
        return {
            "analysis_directory": str(self.analysis_directory),
            "pixels": [
                {"column": column, "row": row, "label": f"C{column:02d} R{row:02d}"}
                for column, row in sorted(pixels)
            ],
            "default_pixel": (
                {"column": default_pixel[0], "row": default_pixel[1]}
                if default_pixel is not None
                else None
            ),
            "noise_stages": stages,
            "scurve_patterns": patterns,
            "amplitudes": amplitudes,
            "available": {
                "noise": not noise.empty or not noise_fit.empty,
            "scurve": (self.analysis_directory / "scurve_efficiency.csv").is_file(),
            },
        }


def _labels(language: str) -> dict[str, str]:
    if language == "en":
        return {
            "dac": "Threshold DAC code",
            "efficiency": "Detection efficiency",
            "count": "Decoded count",
            "pixels": "Pixels",
            "threshold": "Threshold center, DAC code",
            "column": "Physical column",
            "row": "Physical row",
            "signal": "Signal",
            "background": "Background",
            "median": "Matrix median",
            "noise": "Noise response",
        }
    return {
        "dac": "Код порогового ЦАП",
        "efficiency": "Эффективность регистрации",
        "count": "Декодированное число срабатываний",
        "pixels": "Пиксели",
        "threshold": "Центр порога, код ЦАП",
        "column": "Физический столбец",
        "row": "Физическая строка",
        "signal": "Сигнал",
        "background": "Фон",
        "median": "Медиана матрицы",
        "noise": "Шумовой отклик",
    }


def _style_axis(axis: Any, request: dict[str, Any]) -> None:
    axis.title.set_fontsize(request["title_font_size"])
    axis.xaxis.label.set_fontsize(request["axis_font_size"])
    axis.yaxis.label.set_fontsize(request["axis_font_size"])
    axis.tick_params(labelsize=request["tick_font_size"])
    legend = axis.get_legend()
    if legend is not None:
        for text in legend.get_texts():
            text.set_fontsize(request["legend_font_size"])
    axis.grid(True, alpha=0.25)


def _apply_dac_range(
    axis: Any,
    request: dict[str, Any],
    codes: Iterable[float],
    *,
    preferred: tuple[float, float] | None = None,
) -> None:
    minimum = request["dac_min"]
    maximum = request["dac_max"]
    finite = np.asarray([value for value in codes if math.isfinite(float(value))], dtype=float)
    if minimum is None and preferred is not None:
        minimum = preferred[0]
    if maximum is None and preferred is not None:
        maximum = preferred[1]
    if minimum is None and len(finite):
        minimum = float(np.min(finite)) - 1.0
    if maximum is None and len(finite):
        maximum = float(np.max(finite)) + 1.0
    if minimum is not None or maximum is not None:
        axis.set_xlim(left=minimum, right=maximum)


def _transition_limits(results: pd.DataFrame) -> tuple[float, float] | None:
    if results.empty:
        return None
    d50 = _numeric(results, "d50_code")
    sigma = _numeric(results, "sigma_dac_codes").abs()
    valid = d50.notna() & sigma.notna() & (sigma > 0)
    if not valid.any():
        return None
    margin = np.maximum(8.0, 6.0 * sigma[valid].to_numpy(dtype=float))
    centers = d50[valid].to_numpy(dtype=float)
    return float(np.min(centers - margin)), float(np.max(centers + margin))


def _select_pixel(frame: pd.DataFrame, request: dict[str, Any]) -> pd.DataFrame:
    if frame.empty:
        return frame
    column = request["column"]
    row = request["row"]
    if column is None or row is None:
        available = frame[["column", "row"]].drop_duplicates()
        if available.empty:
            return frame.iloc[0:0]
        column, row = map(int, available.sort_values(["column", "row"]).iloc[0])
        request["column"], request["row"] = column, row
    return frame[(_numeric(frame, "column") == column) & (_numeric(frame, "row") == row)].copy()


def _ensure_pixel(data: AnalysisPlotData, request: dict[str, Any]) -> tuple[int, int]:
    if request["column"] is None or request["row"] is None:
        default = data.options().get("default_pixel")
        if default is None:
            raise ValueError("В данных нет координат пикселей")
        request["column"] = int(default["column"])
        request["row"] = int(default["row"])
    return int(request["column"]), int(request["row"])


def _select_scurve(frame: pd.DataFrame, request: dict[str, Any]) -> pd.DataFrame:
    data = frame.copy()
    pattern = request["injection_pattern"]
    if pattern != "all" and "injection_pattern" in data:
        data = data[data["injection_pattern"].astype(str) == pattern]
    amplitude = request["amplitude_index"]
    if amplitude != "all" and "stage" in data:
        token = f"pulse_amplitude_{int(amplitude):03d}_"
        data = data[data["stage"].astype(str).str.startswith(token)]
    if request["branch_view"] == "positive" and "physical_branch_valid" in data:
        data = data[_bool_series(data, "physical_branch_valid")]
    return data


def _amplitude_label(data: pd.DataFrame, *, language: str) -> str:
    charge = _numeric(data, "injection_charge_electrons").dropna()
    step = _numeric(data, "injection_voltage_step_v").dropna()
    if len(charge):
        return f"{float(charge.median()) / 1000.0:.4g} ke"
    if len(step):
        return f"{1000.0 * float(step.median()):.4g} mV"
    return "amplitude" if language == "en" else "амплитуда"


def _fit_curve(result: pd.Series, x: np.ndarray) -> np.ndarray | None:
    d50 = _finite(result.get("d50_code"))
    sigma = _finite(result.get("sigma_dac_codes"))
    low = _finite(result.get("fit_lower_plateau_efficiency"))
    high = _finite(result.get("fit_upper_plateau_efficiency"))
    if d50 is None or sigma is None or sigma <= 0:
        return None
    low = 0.0 if low is None else low
    high = 1.0 if high is None else high
    z = (x - d50) / (math.sqrt(2.0) * sigma)
    erf = np.asarray([math.erf(float(value)) for value in z])
    direction = str(result.get("code_transition_direction", "decreasing")).lower()
    core = 0.5 * (1.0 + erf)
    if "decreas" in direction or "desc" in direction:
        core = 1.0 - core
    return low + (high - low) * core


def _save_figure(
    figure: Any,
    output_directory: Path,
    stem: str,
    request: dict[str, Any],
) -> Path:
    path = output_directory / f"{stem}.{request['output_format']}"
    figure.savefig(
        path,
        dpi=int(request["dpi"]),
        bbox_inches="tight",
        metadata={"Creator": "comparator_characterization interactive plots"},
    )
    plt.close(figure)
    return path


def _plot_pixel_scurve(
    data: AnalysisPlotData, request: dict[str, Any], output: Path
) -> Path:
    pixel = _ensure_pixel(data, request)
    points = data.scurve_points(request, pixel=pixel)
    if points.empty:
        raise ValueError("Для выбранного пикселя/режима/амплитуды нет S-curve точек")
    results = _select_pixel(
        _select_scurve(data.table("scurve_results.csv"), request), request
    )
    label = _labels(request["language"])
    figure, axis = plt.subplots(figsize=(8.2, 5.5), layout="constrained")
    codes_seen: list[float] = []
    colors = plt.rcParams["axes.prop_cycle"].by_key().get("color", ["C0"])
    for curve_index, (stage, group) in enumerate(points.groupby("stage", sort=True)):
        color = colors[curve_index % len(colors)]
        grouped = group.assign(
            code=_numeric(group, "threshold_dac_code"),
            value=_numeric(group, "efficiency"),
        ).dropna(subset=["code", "value"]).groupby("code")["value"].median().sort_index()
        if grouped.empty:
            continue
        curve_label = _amplitude_label(group, language=request["language"])
        axis.scatter(
            grouped.index, grouped.values, s=18, label=curve_label, color=color
        )
        codes_seen.extend(grouped.index.tolist())
        fit = results[results.get("stage", pd.Series(dtype=str)).astype(str) == str(stage)]
        if not fit.empty:
            x = np.linspace(float(grouped.index.min()), float(grouped.index.max()), 500)
            y = _fit_curve(fit.iloc[0], x)
            if y is not None:
                axis.plot(x, y, linewidth=1.4, color=color)
    axis.set_xlabel(label["dac"])
    axis.set_ylabel(label["efficiency"])
    title = (
        f"Pixel C{request['column']:02d} R{request['row']:02d}: S-curves"
        if request["language"] == "en"
        else f"Пиксель C{request['column']:02d} R{request['row']:02d}: S-кривые"
    )
    axis.set_title(title)
    axis.set_ylim(-0.04, 1.08)
    preferred = (
        _transition_limits(results) if request["branch_view"] == "positive" else None
    )
    _apply_dac_range(axis, request, codes_seen, preferred=preferred)
    axis.legend()
    _style_axis(axis, request)
    return _save_figure(figure, output, "pixel_scurves", request)


def _plot_matrix_scurve(
    data: AnalysisPlotData, request: dict[str, Any], output: Path
) -> Path:
    summary = data.scurve_matrix_summary(request)
    if summary.empty:
        raise ValueError("Нет S-curve точек для выбранного режима")
    label = _labels(request["language"])
    figure, axis = plt.subplots(figsize=(8.2, 5.5), layout="constrained")
    codes_seen: list[float] = []
    for _, group in summary.groupby("stage", sort=True):
        group = group.sort_values("threshold_dac_code")
        if group.empty:
            continue
        curve_label = _amplitude_label(group, language=request["language"])
        axis.plot(group["threshold_dac_code"], group["median"], marker="o", markersize=3, label=curve_label)
        axis.fill_between(
            group["threshold_dac_code"].to_numpy(dtype=float), group["low"].to_numpy(dtype=float),
            group["high"].to_numpy(dtype=float), alpha=0.14,
        )
        codes_seen.extend(group["threshold_dac_code"].tolist())
    axis.set_xlabel(label["dac"])
    axis.set_ylabel(label["efficiency"])
    axis.set_title("Matrix S-curves, median and 10-90%" if request["language"] == "en" else "S-кривые матрицы, медиана и 10-90%")
    axis.set_ylim(-0.04, 1.08)
    fit_results = _select_scurve(data.table("scurve_results.csv"), request)
    preferred = (
        _transition_limits(fit_results)
        if request["branch_view"] == "positive"
        else None
    )
    _apply_dac_range(axis, request, codes_seen, preferred=preferred)
    axis.legend()
    _style_axis(axis, request)
    return _save_figure(figure, output, "matrix_scurves", request)


def _plot_pixel_raw_counts(
    data: AnalysisPlotData, request: dict[str, Any], output: Path
) -> Path:
    pixel = _ensure_pixel(data, request)
    points = data.scurve_points(request, pixel=pixel)
    if points.empty:
        raise ValueError("Нет raw S-curve точек для выбранного пикселя")
    label = _labels(request["language"])
    figure, axis = plt.subplots(figsize=(8.2, 5.5), layout="constrained")
    codes_seen: list[float] = []
    for _, group in points.groupby("stage", sort=True):
        temp = pd.DataFrame(
            {
                "code": _numeric(group, "threshold_dac_code"),
                "signal": _numeric(group, "signal_count"),
                "background": _numeric(group, "background_count"),
            }
        ).dropna(subset=["code"])
        summary = temp.groupby("code").median(numeric_only=True).sort_index()
        curve_label = _amplitude_label(group, language=request["language"])
        axis.plot(summary.index, summary["signal"], marker="o", markersize=3, label=f"{label['signal']}: {curve_label}")
        if summary["background"].notna().any():
            axis.plot(summary.index, summary["background"], linestyle="--", linewidth=1.0, label=f"{label['background']}: {curve_label}")
        codes_seen.extend(summary.index.tolist())
    axis.set_xlabel(label["dac"])
    axis.set_ylabel(label["count"])
    axis.set_title("Raw S-curve counts" if request["language"] == "en" else "Raw-отсчеты S-кривой")
    fit_results = _select_pixel(
        _select_scurve(data.table("scurve_results.csv"), request), request
    )
    preferred = (
        _transition_limits(fit_results)
        if request["branch_view"] == "positive"
        else None
    )
    _apply_dac_range(axis, request, codes_seen, preferred=preferred)
    axis.legend()
    _style_axis(axis, request)
    return _save_figure(figure, output, "pixel_raw_counts", request)


def _plot_noise_pixel(
    data: AnalysisPlotData, request: dict[str, Any], output: Path
) -> Path:
    points = _select_pixel(data.table("noise_statistics.csv", required=True), request)
    if request["stage"] != "all":
        points = points[points["stage"].astype(str) == request["stage"]]
    if points.empty:
        raise ValueError("Нет noise-точек для выбранного пикселя/stage")
    label = _labels(request["language"])
    figure, axis = plt.subplots(figsize=(8.2, 5.5), layout="constrained")
    codes_seen: list[float] = []
    for stage, group in points.groupby("stage", sort=True):
        code = _numeric(group, "threshold_dac_code")
        count = _numeric(group, "mean_count")
        valid = code.notna() & count.notna()
        order = np.argsort(code[valid].to_numpy())
        x = code[valid].to_numpy()[order]
        y = count[valid].to_numpy()[order]
        axis.plot(x, y, marker="o", markersize=3, label=str(stage))
        codes_seen.extend(x.tolist())
    axis.set_xlabel(label["dac"])
    axis.set_ylabel(label["count"])
    axis.set_title("Pixel noise curves" if request["language"] == "en" else "Шумовые кривые пикселя")
    active_codes = _numeric(points, "threshold_dac_code")[
        _numeric(points, "mean_count") > 0
    ].dropna()
    preferred = None
    if len(active_codes):
        preferred = (float(active_codes.min()) - 4.0, float(active_codes.max()) + 4.0)
    _apply_dac_range(axis, request, codes_seen, preferred=preferred)
    axis.legend()
    _style_axis(axis, request)
    return _save_figure(figure, output, "pixel_noise_curves", request)


def _plot_noise_distributions(
    data: AnalysisPlotData, request: dict[str, Any], output: Path
) -> Path:
    fits = data.table("noise_fit_results.csv", required=True).copy()
    if request["stage"] != "all":
        fits = fits[fits["stage"].astype(str) == request["stage"]]
    if fits.empty:
        raise ValueError("Нет noise fit для выбранного stage")
    label = _labels(request["language"])
    figure, axis = plt.subplots(figsize=(8.2, 5.5), layout="constrained")
    values_seen: list[float] = []
    for stage, group in fits.groupby("stage", sort=True):
        values = _numeric(group, "center_max_dac_code").dropna().to_numpy(dtype=float)
        if not len(values):
            continue
        bins = max(10, min(80, int(math.sqrt(len(values)) * 2)))
        axis.hist(values, bins=bins, histtype="step", linewidth=1.5, label=str(stage))
        values_seen.extend(values.tolist())
    axis.set_xlabel(label["threshold"])
    axis.set_ylabel(label["pixels"])
    axis.set_title("Pixel threshold distributions" if request["language"] == "en" else "Распределения порогов пикселей")
    _apply_dac_range(axis, request, values_seen)
    axis.legend()
    _style_axis(axis, request)
    return _save_figure(figure, output, "noise_threshold_distributions", request)


def _matrix_from_rows(frame: pd.DataFrame, value_column: str) -> tuple[np.ndarray, list[int], list[int]]:
    values = frame[["column", "row", value_column]].copy()
    values["column"] = _numeric(values, "column")
    values["row"] = _numeric(values, "row")
    values[value_column] = _numeric(values, value_column)
    values = values.dropna().groupby(["column", "row"], as_index=False)[value_column].median()
    columns = sorted(values["column"].astype(int).unique())
    rows = sorted(values["row"].astype(int).unique())
    matrix = np.full((len(rows), len(columns)), np.nan)
    column_index = {value: index for index, value in enumerate(columns)}
    row_index = {value: index for index, value in enumerate(rows)}
    for _, item in values.iterrows():
        matrix[row_index[int(item["row"])], column_index[int(item["column"])]] = float(item[value_column])
    return matrix, columns, rows


def _plot_heatmap(
    data: AnalysisPlotData, request: dict[str, Any], output: Path
) -> Path:
    metric = request["heatmap_metric"]
    if metric in {"noise_center_v", "noise_sigma_v", "trim_code"}:
        frame = data.table("noise_fit_results.csv", required=True).copy()
        if request["stage"] != "all":
            frame = frame[frame["stage"].astype(str) == request["stage"]]
        else:
            available_stages = set(frame["stage"].astype(str))
            preferred_stage = next(
                (
                    stage
                    for stage in (
                        "equalized_final",
                        "baseline_noise",
                        "trim_00",
                        "trim_31",
                    )
                    if stage in available_stages
                ),
                None,
            )
            if preferred_stage is not None:
                frame = frame[frame["stage"].astype(str) == preferred_stage]
        source = {
            "noise_center_v": "center_selected_v",
            "noise_sigma_v": "sigma_fit_v",
            "trim_code": "local_trim_code",
        }[metric]
    else:
        frame = _select_scurve(data.table("scurve_results.csv", required=True), request)
        source = {
            "scurve_d50_code": "d50_code",
            "scurve_sigma_codes": "sigma_dac_codes",
            "scurve_v50_v": "v50_v",
        }[metric]
    if frame.empty or source not in frame:
        raise ValueError("Нет данных для выбранной heatmap")
    matrix, columns, rows = _matrix_from_rows(frame, source)
    if matrix.size == 0:
        raise ValueError("Heatmap не содержит конечных значений")
    square = request["pixel_geometry"] == "square"
    width = 6.0 if square else 9.0
    height = max(4.5, width * len(rows) / max(len(columns), 1)) if square else 5.2
    figure, axis = plt.subplots(figsize=(width, height), layout="constrained")
    image = axis.imshow(
        matrix,
        origin="lower",
        aspect="equal" if square else "auto",
        interpolation="nearest",
        extent=(min(columns) - 0.5, max(columns) + 0.5, min(rows) - 0.5, max(rows) + 0.5),
    )
    label = _labels(request["language"])
    metric_titles = {
        "ru": {
            "noise_center_v": "Центр шумового порога, В",
            "noise_sigma_v": "Ширина шумового отклика, В",
            "trim_code": "Код локальной подстройки",
            "scurve_d50_code": "D50 S-кривой, код ЦАП",
            "scurve_sigma_codes": "Ширина S-кривой, коды ЦАП",
            "scurve_v50_v": "V50 S-кривой, В",
        },
        "en": {
            "noise_center_v": "Noise threshold center, V",
            "noise_sigma_v": "Noise response width, V",
            "trim_code": "Local trim code",
            "scurve_d50_code": "S-curve D50, DAC code",
            "scurve_sigma_codes": "S-curve width, DAC codes",
            "scurve_v50_v": "S-curve V50, V",
        },
    }
    axis.set_xlabel(label["column"])
    axis.set_ylabel(label["row"])
    axis.set_title(metric_titles[request["language"]][metric])
    colorbar = figure.colorbar(image, ax=axis)
    colorbar.set_label(
        metric_titles[request["language"]][metric],
        fontsize=request["axis_font_size"],
    )
    colorbar.ax.tick_params(labelsize=request["tick_font_size"])
    _style_axis(axis, request)
    return _save_figure(figure, output, f"heatmap_{metric}", request)


def render_custom_plots(
    analysis_directory: str | Path,
    request: dict[str, Any],
) -> PlotRenderResult:
    """Render one plot or a predefined set without changing automatic plots."""

    validated = _validated_request(request)
    data = AnalysisPlotData(analysis_directory)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = data.analysis_directory / "custom_plots" / f"render_{stamp}"
    output.mkdir(parents=True)
    request_path = output / "plot_request.json"
    dispatch = {
        "pixel_scurve": _plot_pixel_scurve,
        "matrix_scurve": _plot_matrix_scurve,
        "pixel_raw_counts": _plot_pixel_raw_counts,
        "noise_pixel": _plot_noise_pixel,
        "noise_distributions": _plot_noise_distributions,
        "heatmap": _plot_heatmap,
    }
    requested = validated["plot_type"]
    if requested == "scurve_set":
        plot_types = ("pixel_scurve", "matrix_scurve", "pixel_raw_counts", "heatmap")
        validated["heatmap_metric"] = "scurve_d50_code"
    elif requested == "noise_set":
        plot_types = ("noise_pixel", "noise_distributions", "heatmap")
        validated["heatmap_metric"] = "noise_center_v"
    else:
        plot_types = (requested,)
    validated["resolved_plot_types"] = list(plot_types)
    atomic_write_json(request_path, validated)
    files: list[Path] = []
    errors: list[str] = []
    for plot_type in plot_types:
        try:
            files.append(dispatch[plot_type](data, validated, output))
        except (ValueError, FileNotFoundError) as error:
            if len(plot_types) == 1:
                raise
            errors.append(f"{plot_type}: {error}")
    if not files:
        raise ValueError("Ни один график набора не создан: " + "; ".join(errors))
    if errors:
        atomic_write_text(output / "skipped_plots.txt", "\n".join(errors))
    return PlotRenderResult(output, tuple(files), request_path)


_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Comparator characterization plots</title>
<style>
:root{color-scheme:light dark;font-family:Inter,Segoe UI,Arial,sans-serif}body{margin:0;background:#10151d;color:#eef3f8}.wrap{max-width:1250px;margin:auto;padding:24px}.panel{background:#18212d;border:1px solid #2b3b4e;border-radius:14px;padding:18px;margin-bottom:18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:#aebdcb}select,input,button{font:inherit;border:1px solid #41566d;border-radius:8px;padding:9px;background:#111923;color:#f4f7fa}button{background:#2775ca;border-color:#4394eb;cursor:pointer;font-weight:600}button:disabled{opacity:.55}.wide{grid-column:span 2}.status{white-space:pre-wrap;color:#bdd8f2}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}.card{background:#fff;border-radius:10px;padding:8px;color:#111}.card img{width:100%;height:auto;display:block}.card a{display:block;padding:9px;color:#1766aa;word-break:break-all}.hint{color:#90a5b8;font-size:13px}h1{font-size:24px;margin-top:0}@media(max-width:600px){.wide{grid-column:span 1}.cards{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><h1>Графики характеризации</h1><div class="panel"><div id="source" class="hint"></div><div class="grid">
<label>График или набор<select id="plot_type"><option value="pixel_scurve">S-кривые пикселя</option><option value="matrix_scurve">S-кривые матрицы</option><option value="pixel_raw_counts">Raw S-curve counts</option><option value="noise_pixel">Noise-кривые пикселя</option><option value="noise_distributions">Распределения порогов</option><option value="heatmap">Heatmap</option><option value="scurve_set">Набор S-curve</option><option value="noise_set">Набор noise</option></select></label>
<label>Язык<select id="language"><option value="ru">Русский</option><option value="en">English</option></select></label>
<label>Пиксель<select id="pixel"></select></label><label>Noise stage<select id="stage"><option value="all">Все</option></select></label>
<label>S-curve pattern<select id="pattern"><option value="all">Все</option></select></label><label>Амплитуда<select id="amplitude"><option value="all">Все</option></select></label>
<label>Ветвь S-curve<select id="branch"><option value="positive">Положительная, стандарт</option><option value="full">Полная / bipolar</option></select></label>
<label>Метрика heatmap<select id="metric"><option value="noise_center_v">Noise center, V</option><option value="noise_sigma_v">Noise sigma, V</option><option value="trim_code">Trim code</option><option value="scurve_d50_code">S-curve D50, DAC</option><option value="scurve_sigma_codes">S-curve sigma, DAC</option><option value="scurve_v50_v">S-curve V50, V</option></select></label>
<label>Геометрия PX<select id="geometry"><option value="square">Квадратные ячейки</option><option value="stretched">Растянуть поле</option></select></label><label>Формат<select id="format"><option>png</option><option>pdf</option><option>svg</option></select></label>
<label>DAC слева<input id="dac_min" type="number" step="1" placeholder="авто"></label><label>DAC справа<input id="dac_max" type="number" step="1" placeholder="авто"></label>
<label>Заголовок, pt<input id="title_font" type="number" value="14" min="6" max="40"></label><label>Оси, pt<input id="axis_font" type="number" value="11" min="6" max="30"></label>
<label>Деления, pt<input id="tick_font" type="number" value="9" min="5" max="24"></label><label>Легенда, pt<input id="legend_font" type="number" value="9" min="5" max="24"></label>
<label>DPI<input id="dpi" type="number" value="200" min="72" max="600"></label><label><span>&nbsp;</span><button id="render">Создать</button></label>
</div><p class="hint">Стандартный вид S-кривой показывает только физическую положительную ветвь. Полный вид оставляет обе полярности. Эти файлы добавляются в custom_plots и не изменяют автоматические графики.</p><div id="status" class="status"></div></div><div id="cards" class="cards"></div></div>
<script>
const $=id=>document.getElementById(id);let options={};
async function init(){let r=await fetch('api/options');options=await r.json();$('source').textContent='Источник: '+options.analysis_directory;for(const p of options.pixels){let o=document.createElement('option');o.value=p.column+','+p.row;o.textContent=p.label;$('pixel').appendChild(o)}for(const s of options.noise_stages){let o=document.createElement('option');o.value=s;o.textContent=s;$('stage').appendChild(o)}for(const p of options.scurve_patterns){let o=document.createElement('option');o.value=p;o.textContent=p;$('pattern').appendChild(o)}for(const a of options.amplitudes){let o=document.createElement('option');o.value=a.value;o.textContent=a.label;$('amplitude').appendChild(o)}}
function num(id){return $(id).value===''?null:Number($(id).value)}
$('render').onclick=async()=>{let px=$('pixel').value.split(',');let body={plot_type:$('plot_type').value,language:$('language').value,column:px[0]?Number(px[0]):null,row:px[1]?Number(px[1]):null,stage:$('stage').value,injection_pattern:$('pattern').value,amplitude_index:$('amplitude').value,branch_view:$('branch').value,heatmap_metric:$('metric').value,pixel_geometry:$('geometry').value,output_format:$('format').value,dac_min:num('dac_min'),dac_max:num('dac_max'),title_font_size:num('title_font'),axis_font_size:num('axis_font'),tick_font_size:num('tick_font'),legend_font_size:num('legend_font'),dpi:num('dpi')};$('render').disabled=true;$('status').textContent='Построение...';$('cards').innerHTML='';try{let r=await fetch('api/render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let out=await r.json();if(!r.ok)throw Error(out.error||r.statusText);$('status').textContent='Создано: '+out.output_directory;for(const f of out.files){let d=document.createElement('div');d.className='card';if(f.preview){let i=document.createElement('img');i.src=f.url;d.appendChild(i)}let a=document.createElement('a');a.href=f.url;a.target='_blank';a.textContent=f.name;d.appendChild(a);$('cards').appendChild(d)}}catch(e){$('status').textContent='Ошибка: '+e.message}finally{$('render').disabled=false}};init().catch(e=>$('status').textContent='Ошибка загрузки: '+e.message);
</script></body></html>"""


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ComparatorPlotDashboard/1.0"

    def _route(self) -> str | None:
        path = unquote(urlparse(self.path).path).strip("/")
        prefix = self.server.token  # type: ignore[attr-defined]
        if path == prefix:
            return ""
        if path.startswith(prefix + "/"):
            return path[len(prefix) + 1 :]
        return None

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802
        route = self._route()
        if route is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if route == "":
            self._send(HTTPStatus.OK, _HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if route == "api/options":
            try:
                self._json(HTTPStatus.OK, self.server.plot_data.options())  # type: ignore[attr-defined]
            except Exception as error:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        if route.startswith("files/"):
            requested = (self.server.analysis_directory / route[6:]).resolve()  # type: ignore[attr-defined]
            root = (self.server.analysis_directory / "custom_plots").resolve()  # type: ignore[attr-defined]
            if not requested.is_relative_to(root) or not requested.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": "file not found"})
                return
            content_type = {
                ".png": "image/png",
                ".svg": "image/svg+xml",
                ".pdf": "application/pdf",
                ".json": "application/json",
            }.get(requested.suffix.lower(), "application/octet-stream")
            self._send(HTTPStatus.OK, requested.read_bytes(), content_type)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        route = self._route()
        if route != "api/render":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise ValueError("Некорректный размер запроса")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(request, dict):
                raise TypeError("JSON должен быть объектом")
            result = render_custom_plots(self.server.analysis_directory, request)  # type: ignore[attr-defined]
            files = []
            for path in result.files:
                relative = path.relative_to(self.server.analysis_directory).as_posix()  # type: ignore[attr-defined]
                files.append(
                    {
                        "name": path.name,
                        "url": "files/" + relative,
                        "preview": path.suffix.lower() in {".png", ".svg"},
                    }
                )
            self._json(
                HTTPStatus.OK,
                {
                    "output_directory": str(result.output_directory),
                    "files": files,
                },
            )
        except Exception as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"{type(error).__name__}: {error}"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve_plot_dashboard(
    experiment_or_analysis: str | Path,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Serve the plot controller on localhost until Ctrl+C."""

    analysis_directory = resolve_analysis_directory(experiment_or_analysis)
    token = secrets.token_urlsafe(18)
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), _DashboardHandler)
    server.token = token  # type: ignore[attr-defined]
    server.analysis_directory = analysis_directory  # type: ignore[attr-defined]
    server.plot_data = AnalysisPlotData(analysis_directory)  # type: ignore[attr-defined]
    address = f"http://127.0.0.1:{server.server_port}/{token}/"
    print(f"Локальная страница графиков: {address}")
    print("Для остановки нажмите Ctrl+C. Данные не передаются во внешнюю сеть.")
    if open_browser:
        threading.Timer(0.15, partial(webbrowser.open, address)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
