from __future__ import annotations

import json
from collections import deque
from contextvars import ContextVar
from functools import wraps
import inspect
import logging
import math
from pathlib import Path
import pickle
import re
from statistics import NormalDist
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pixel_matrix import OWNED_COLUMNS

from .models import AnalysisSettings
from .parallel import process_pool, process_workers


_NORMAL = NormalDist()
logger = logging.getLogger(__name__)
_FIGURE_WRITER: ContextVar = ContextVar("characterization_figure_writer", default=None)


def _save_figure_local(
    figure: plt.Figure,
    directory: Path,
    stem: str,
    settings: AnalysisSettings,
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    outputs = [directory / f"{stem}.png"]
    figure.savefig(outputs[0], dpi=settings.plot_dpi, bbox_inches="tight")
    if settings.save_pdf_plots:
        outputs.append(directory / f"{stem}.pdf")
        figure.savefig(outputs[-1], bbox_inches="tight")
    plt.close(figure)
    return outputs


def _render_pickled_figure(payload: bytes) -> list[Path]:
    """Only accept in-memory figures produced by this process, never input files."""

    from matplotlib.backends.backend_agg import FigureCanvasAgg

    figure, directory, stem, settings, rc = pickle.loads(payload)
    FigureCanvasAgg(figure)
    with plt.rc_context(rc):
        return _save_figure_local(figure, directory, stem, settings)


class _FigureWriter:
    def __init__(self, settings: AnalysisSettings):
        self.settings = settings
        requested = settings.plot_workers
        self.count = (
            process_workers(requested, auto_limit=4) if requested
            else min(4, process_workers(settings.workers, auto_limit=4))
        )
        if not requested and settings.plot_dpi < 150 and not settings.save_pdf_plots:
            self.count = 1  # low-resolution preview is usually faster without spawn
        self.pool_context = None
        self.pool = None
        self.pending: deque = deque()
        self.submitted = 0
        self.completed = 0

    def save(self, figure, directory, stem, settings):
        if self.count <= 1:
            return _save_figure_local(figure, directory, stem, settings)
        directory.mkdir(parents=True, exist_ok=True)
        # Only two pending figures per process, not hundreds of 300-dpi images.
        if len(self.pending) >= 2 * self.count:
            self.finish_one()
        rc = {key: value for key, value in plt.rcParams.items()
              if key not in ("backend", "backend_fallback")}
        try:
            payload = pickle.dumps((figure, directory, stem, settings, rc),
                                   protocol=pickle.HIGHEST_PROTOCOL)
        except (pickle.PicklingError, TypeError, AttributeError):
            logger.warning("График %s не сериализуется, сохраняется последовательно", stem)
            return _save_figure_local(figure, directory, stem, settings)
        if self.pool is None:
            self.pool_context = process_pool(self.count)
            self.pool = self.pool_context.__enter__()
            logger.info("Графики PNG/PDF: процессов %d", self.count)
        self.pending.append((stem, self.pool.submit(_render_pickled_figure, payload)))
        self.submitted += 1
        plt.close(figure)
        return [directory / f"{stem}.{extension}" for extension in
                (("png", "pdf") if settings.save_pdf_plots else ("png",))]

    def finish_one(self):
        stem, future = self.pending.popleft()
        future.result()  # propagate rendering and disk errors, never report fake success
        self.completed += 1
        if self.completed % 5 == 0:
            logger.info("Графики: сохранено %d, поставлено в очередь %d (%s)",
                        self.completed, self.submitted, stem)

    def close(self, *, success: bool):
        try:
            if success:
                while self.pending:
                    self.finish_one()
                if self.submitted:
                    logger.info("Графики: 100%% (%d/%d)", self.completed, self.submitted)
        finally:
            for _, future in self.pending:
                future.cancel()
            if self.pool_context is not None:
                self.pool_context.__exit__(None, None, None)


def _parallel_figures(function):
    """Keep pyplot in one parent thread, render/save independent figures in processes."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        if _FIGURE_WRITER.get() is not None:
            return function(*args, **kwargs)
        settings = inspect.signature(function).bind(*args, **kwargs).arguments["settings"]
        writer = _FigureWriter(settings)
        token = _FIGURE_WRITER.set(writer)
        success = False
        try:
            result = function(*args, **kwargs)
            success = True
            return result
        finally:
            try:
                writer.close(success=success)
            finally:
                _FIGURE_WRITER.reset(token)
    return wrapped


def _save_figure(figure, directory, stem, settings) -> list[Path]:
    writer = _FIGURE_WRITER.get()
    if writer is not None:
        return writer.save(figure, directory, stem, settings)
    return _save_figure_local(figure, directory, stem, settings)


def _finite_values(frame: pd.DataFrame, column: str) -> np.ndarray:
    if frame.empty or column not in frame:
        return np.array([], dtype=float)
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
    return values[np.isfinite(values)]


def _save_noise_zoom(figure, axis, data, directory, stem, settings):
    """Additional view of ALL observed nonzero response, without changing fits."""
    active = data[pd.to_numeric(data["mean_count"], errors="coerce") > 0]
    x = _finite_values(active, "threshold_voltage_v")
    if len(x) < 2 or x.max() <= x.min():
        return []
    margin = 0.08 * (x.max() - x.min())
    axis.set_xlim(x.min() - margin, x.max() + margin)
    return _save_figure(figure, directory, stem, settings)


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _stage_frame(frame: pd.DataFrame, stage: str) -> pd.DataFrame:
    if frame.empty or "stage" not in frame:
        return frame.iloc[0:0].copy()
    return frame[frame["stage"] == stage].copy()


def _heatmap(
    frame: pd.DataFrame,
    *,
    value_column: str,
    title: str,
    colorbar_label: str,
    cmap: str = "viridis",
    center_zero: bool = False,
) -> plt.Figure | None:
    if frame.empty or value_column not in frame:
        return None
    data = frame[["column", "row", value_column]].copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column])
    if data.empty:
        return None
    pivot = data.pivot_table(index="row", columns="column", values=value_column, aggfunc="first")
    columns = np.asarray(OWNED_COLUMNS, dtype=int)
    rows = np.arange(0, 32)
    pivot = pivot.reindex(index=rows, columns=columns)
    array = pivot.to_numpy(dtype=float)
    kwargs: dict[str, Any] = {}
    if center_zero:
        limit = float(np.nanmax(np.abs(array))) if np.any(np.isfinite(array)) else 1.0
        kwargs.update(vmin=-limit, vmax=limit)
    figure, axis = plt.subplots(figsize=(7.2, 6.0))
    image = axis.imshow(
        array,
        origin="lower",
        aspect="auto",
        interpolation="nearest",
        extent=(columns[0] - 0.5, columns[-1] + 0.5, -0.5, 31.5),
        cmap=cmap,
        **kwargs,
    )
    axis.set_xlabel("Physical ASIC column")
    axis.set_ylabel("Physical ASIC row")
    axis.set_title(title)
    colorbar = figure.colorbar(image, ax=axis, pad=0.02)
    colorbar.set_label(colorbar_label)
    return figure


def _representative_coordinates(
    frame: pd.DataFrame,
    value_column: str,
    count: int,
) -> list[tuple[int, int]]:
    if frame.empty or any(
        column not in frame for column in ("column", "row", value_column)
    ):
        return []
    data = frame[["column", "row", value_column]].copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column]).sort_values(value_column)
    if data.empty:
        return []
    indices = np.unique(np.rint(np.linspace(0, len(data) - 1, min(count, len(data)))).astype(int))
    return [
        (int(data.iloc[index]["column"]), int(data.iloc[index]["row"]))
        for index in indices
    ]


def _plot_coordinates(
    settings: AnalysisSettings,
    frame: pd.DataFrame,
    value_column: str,
) -> list[tuple[int, int]]:
    if frame.empty or "column" not in frame or "row" not in frame:
        return []
    if settings.plot_pixels:
        available = {
            (int(row["column"]), int(row["row"]))
            for _, row in frame[["column", "row"]].drop_duplicates().iterrows()
        }
        return [coordinate for coordinate in settings.plot_pixels if coordinate in available]
    return _representative_coordinates(
        frame, value_column, settings.representative_pixels
    )


def _matrix_array(
    frame: pd.DataFrame,
    value_column: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if frame.empty or value_column not in frame:
        return None
    data = frame[["column", "row", value_column]].copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column])
    if data.empty:
        return None
    columns = np.arange(int(data["column"].min()), int(data["column"].max()) + 1)
    rows = np.arange(int(data["row"].min()), int(data["row"].max()) + 1)
    pivot = data.pivot_table(
        index="row", columns="column", values=value_column, aggfunc="first"
    ).reindex(index=rows, columns=columns)
    return pivot.to_numpy(dtype=float), columns, rows


def _safe_stem(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return text or "unnamed"


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if math.isfinite(result) else float("nan")


def _amplitude_label(frame: pd.DataFrame) -> str:
    if not frame.empty:
        for column, scale, suffix in (
            ("injection_charge_electrons", 1e-3, "ke"),
            ("injection_voltage_step_v", 1e3, "mV"),
        ):
            if column in frame:
                values = pd.to_numeric(frame[column], errors="coerce").dropna()
                if len(values):
                    value = float(values.iloc[0]) * scale
                    return f"{value:.4g} {suffix}"
        if "pulse_amplitude_native" in frame:
            raw = str(frame.iloc[0]["pulse_amplitude_native"])
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    if "requested_voltage_step_v" in decoded:
                        return f"requested {1000 * float(decoded['requested_voltage_step_v']):.4g} mV"
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            return raw if len(raw) <= 48 else raw[:45] + "..."
    return "unknown amplitude"


def _truthy_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(False, index=frame.index, dtype=bool)
    values = frame[column]
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(False).astype(bool)
    return values.astype(str).str.strip().str.lower().isin(("true", "1", "yes"))


def _scurve_raw_count_envelope(
    frame: pd.DataFrame,
    *,
    count_column: str,
    valid_column: str,
    active_pixels_only: bool,
) -> pd.DataFrame:
    """Aggregate raw paired counts without applying S-curve fit rejection."""

    required = {"threshold_dac_code", count_column, valid_column}
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame()
    use = _truthy_series(frame, valid_column)
    if active_pixels_only:
        use &= _truthy_series(frame, "active_injection_pixel_bool")
    data = frame.loc[use].copy()
    data["threshold_dac_code"] = pd.to_numeric(
        data["threshold_dac_code"], errors="coerce"
    )
    data[count_column] = pd.to_numeric(data[count_column], errors="coerce")
    data["injections_for_analysis"] = pd.to_numeric(
        data.get("injections_for_analysis"), errors="coerce"
    )
    data = data.dropna(subset=["threshold_dac_code", count_column])
    if data.empty:
        return pd.DataFrame()
    envelope = data.groupby("threshold_dac_code", as_index=False).agg(
        count_median=(count_column, "median"),
        count_q10=(count_column, lambda values: values.quantile(0.10)),
        count_q90=(count_column, lambda values: values.quantile(0.90)),
        count_q95=(count_column, lambda values: values.quantile(0.95)),
        count_maximum=(count_column, "max"),
        effective_injections=("injections_for_analysis", "median"),
    )
    return envelope.sort_values("threshold_dac_code")


def _scurve_noise_peak_lower_code(
    frame: pd.DataFrame,
    *,
    boundary_code: float,
    settings: AnalysisSettings,
) -> float:
    """Extend a raw-count view to a supported local baseline-noise maximum.

    The matrix-level fit boundary remains unchanged. A pixel baseline may be
    displaced to either side of that boundary, so the display searches a local
    neighborhood in both directions. A point on the higher-code falling
    shoulder must support the maximum, so a single isolated counter excursion
    does not move the left plot edge.
    """

    if settings.scurve_plot_noise_peak_search_codes <= 0:
        return boundary_code
    required = {"threshold_dac_code", "background_count", "background_valid"}
    if frame.empty or not required.issubset(frame.columns):
        return boundary_code
    data = frame[_truthy_series(frame, "background_valid")].copy()
    if "background_counter_saturated" in data:
        data = data[~_truthy_series(data, "background_counter_saturated")]
    data["threshold_dac_code"] = pd.to_numeric(
        data["threshold_dac_code"], errors="coerce"
    )
    data["background_count"] = pd.to_numeric(
        data["background_count"], errors="coerce"
    )
    search_span = float(settings.scurve_plot_noise_peak_search_codes)
    lower_limit = boundary_code - search_span
    upper_limit = boundary_code + search_span
    data = data[
        data["threshold_dac_code"].between(lower_limit, upper_limit)
    ].dropna(subset=["threshold_dac_code", "background_count"])
    if data.empty:
        return boundary_code
    trace = (
        data.groupby("threshold_dac_code", as_index=False)
        .agg(background_q90=("background_count", lambda values: values.quantile(0.90)))
        .sort_values("threshold_dac_code")
        .reset_index(drop=True)
    )
    if len(trace) < 2:
        return boundary_code

    values = trace["background_q90"].to_numpy(dtype=float)
    codes = trace["threshold_dac_code"].to_numpy(dtype=float)
    peak_index = int(np.nanargmax(values))
    peak_value = max(float(values[peak_index]), 0.0)
    tolerance = float(settings.scurve_plot_noise_peak_support_fraction)
    nominal = (
        pd.to_numeric(data["injections_for_analysis"], errors="coerce").dropna()
        if "injections_for_analysis" in data
        else pd.Series(dtype=float)
    )
    substantial_limit = float(nominal.median()) if len(nominal) else 0.0
    if peak_value <= substantial_limit:
        return boundary_code
    # At increasing DAC code the baseline-noise peak must have a measured
    # falling shoulder. It may be sparse because the first point came from the
    # coarse grid, hence support is relative rather than requiring code + 1.
    shoulder = values[peak_index + 1 :]
    supported = bool(np.any(shoulder >= tolerance * peak_value))
    if not supported:
        return boundary_code
    return float(codes[peak_index])


def _scurve_plot_code_window(
    frame: pd.DataFrame,
    settings: AnalysisSettings,
) -> tuple[float, float] | None:
    """Return a tight ascending-code view of the measured positive branch."""

    if frame.empty or "threshold_dac_code" not in frame:
        return None
    data = frame.copy()
    data["threshold_dac_code"] = pd.to_numeric(
        data["threshold_dac_code"], errors="coerce"
    )
    data = data[data["threshold_dac_code"].notna()]
    if data.empty:
        return None
    all_data = data.copy()
    physical = (
        data[_truthy_series(data, "physical_branch_valid")].copy()
        if "physical_branch_valid" in data
        else data.copy()
    )
    if physical.empty:
        return None
    boundary = _numeric_series(
        physical, "baseline_noise_boundary_code"
    ).dropna()
    fit_lower = (
        float(boundary.median())
        if len(boundary)
        else float(physical["threshold_dac_code"].min())
    )
    lower = _scurve_noise_peak_lower_code(
        all_data,
        boundary_code=fit_lower,
        settings=settings,
    )
    lower = max(
        float(all_data["threshold_dac_code"].min()),
        lower - float(settings.scurve_plot_code_margin),
    )
    signal = physical.copy()
    if "active_injection_pixel_bool" in signal:
        signal = signal[_truthy_series(signal, "active_injection_pixel_bool")]
    if "signal_valid" in signal:
        signal = signal[_truthy_series(signal, "signal_valid")]
    signal["signal_count"] = pd.to_numeric(
        signal.get("signal_count"), errors="coerce"
    )
    signal = signal.dropna(subset=["signal_count"])
    if signal.empty:
        upper = min(
            float(physical["threshold_dac_code"].max()), fit_lower + 32.0
        )
        return lower, max(upper, lower + 1.0)
    envelope = (
        signal.groupby("threshold_dac_code", as_index=False)
        .agg(
            signal_q90=("signal_count", lambda values: values.quantile(0.90)),
            effective_n=("injections_for_analysis", "median"),
        )
        .sort_values("threshold_dac_code")
    )
    envelope = envelope[envelope["threshold_dac_code"] >= fit_lower]
    nominal = pd.to_numeric(envelope["effective_n"], errors="coerce").dropna()
    zero_limit = max(
        1.0,
        0.001 * float(nominal.median()) if len(nominal) else 1.0,
    )
    active = envelope[
        pd.to_numeric(envelope["signal_q90"], errors="coerce") > zero_limit
    ]
    if active.empty:
        upper = min(
            float(envelope["threshold_dac_code"].max()), fit_lower + 32.0
        )
    else:
        active_max = float(active["threshold_dac_code"].max())
        upper = float(envelope["threshold_dac_code"].max())
        zero_streak = 0
        for _, row in envelope[
            envelope["threshold_dac_code"] > active_max
        ].iterrows():
            if float(row["signal_q90"]) <= zero_limit:
                zero_streak += 1
            else:
                zero_streak = 0
            if zero_streak >= settings.scurve_plot_zero_tail_points:
                upper = float(row["threshold_dac_code"])
                break
    upper = min(
        float(physical["threshold_dac_code"].max()),
        upper + float(settings.scurve_plot_code_margin),
    )
    return lower, max(upper, lower + 1.0)


def _scurve_plot_window_rows(
    frame: pd.DataFrame,
    window: tuple[float, float] | None,
) -> pd.DataFrame:
    if window is None or frame.empty:
        return frame
    code = pd.to_numeric(frame["threshold_dac_code"], errors="coerce")
    return frame[(code >= window[0]) & (code <= window[1])].copy()


def _style_scurve_code_axis(
    axis: plt.Axes,
    window: tuple[float, float] | None,
) -> None:
    if window is not None:
        axis.set_xlim(window[0], window[1])
    axis.set_yscale("symlog", linthresh=1.0)
    axis.set_ylim(bottom=0)
    axis.set_xlabel("Threshold DAC code")
    axis.set_ylabel("Raw decoded count")


def _noise_prediction(fit: pd.Series, voltage: np.ndarray) -> np.ndarray | None:
    if fit.get("fit_status") != "ok":
        return None
    center = _number(fit.get("center_fit_v"))
    sigma = _number(fit.get("sigma_fit_v"))
    amplitude = _number(fit.get("fit_amplitude_count"))
    background = _number(fit.get("fit_background_count"))
    if not all(math.isfinite(value) for value in (center, sigma, amplitude, background)) or sigma <= 0:
        return None
    model = str(fit.get("fit_model", ""))
    if model.startswith("gaussian"):
        return background + amplitude * np.exp(-0.5 * ((voltage - center) / sigma) ** 2)
    if model == "edge_probit":
        direction = 1 if fit.get("fit_transition_direction") == "ascending" else -1
        return background + amplitude * np.array(
            [_NORMAL.cdf(direction * (value - center) / sigma) for value in voltage]
        )
    return None


@_parallel_figures
def generate_recommendation_plots(directory: Path, settings: AnalysisSettings) -> dict[str, list[Path]]:
    """Plot proposals separately from measured/verified equalization maps."""

    outputs: dict[str, list[Path]] = {}
    plot_directory = directory / "plots"
    for method in ("fit", "centroid", "maximum"):
        source = directory / f"trim_recommendations_{method}.csv"
        if not source.exists() or source.stat().st_size < 3:
            continue
        data = pd.read_csv(source)
        if data.empty:
            continue
        data["mask_candidate"] = data["mask_recommended"].astype(str).str.lower().isin(("true", "1")).astype(int)
        data["unresolved_trim"] = data["recommended_trim_code"].isna().astype(int)
        data["review_required_map"] = data.get(
            "review_required", pd.Series(False, index=data.index)
        ).astype(str).str.lower().isin(("true", "1")).astype(int)
        data["trim_range_limited_map"] = data.get(
            "trim_range_limited", pd.Series(False, index=data.index)
        ).astype(str).str.lower().isin(("true", "1")).astype(int)
        for field, stem, title, label in (
            ("recommended_trim_code", f"proposed_trim_{method}", f"{method}: proposed trim (requires verification)", "Proposed trim code"),
            ("mask_candidate", f"proposed_mask_{method}", f"{method}: masking candidates, NOT confirmed dead pixels", "1 = exclusion proposed; read CSV reasons"),
            ("unresolved_trim", f"unresolved_trim_{method}", f"{method}: unresolved trim settings", "1 = no defensible trim proposal"),
            ("trim_range_limited_map", f"trim_range_limited_{method}", f"{method}: target outside measured trim range", "1 = trim range limited; NOT a dead-pixel claim"),
            ("review_required_map", f"review_required_{method}", f"{method}: pixels requiring review", "1 = inspect reason_codes"),
        ):
            figure = _heatmap(data, value_column=field, title=title, colorbar_label=label)
            if figure is not None:
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)
    diagnostic_path = directory / "noise_curve_diagnostics.csv"
    if diagnostic_path.exists() and diagnostic_path.stat().st_size > 3:
        diagnostics = pd.read_csv(diagnostic_path)
        for stage, data in diagnostics.groupby("stage"):
            for field, label in (
                ("peak_mean_count", "Peak mean count per exposure"),
                ("peak_fano_factor", "Variance / mean at peak (not ENC)"),
                ("no_response_in_measured_range", "1 = no counts in measured range"),
            ):
                if field not in data:
                    continue
                if field == "no_response_in_measured_range":
                    data = data.copy()
                    data[field] = data[field].astype(str).str.lower().isin(("true", "1")).astype(int)
                stem = f"{stage}_{field}"
                figure = _heatmap(data, value_column=field, title=f"{stage}: {label}", colorbar_label=label)
                if figure is not None:
                    outputs[stem] = _save_figure(figure, plot_directory, stem, settings)
    return outputs


def _scurve_prediction(fit: pd.Series, voltage: np.ndarray) -> np.ndarray | None:
    v50 = _number(fit.get("v50_v"))
    sigma = _number(fit.get("sigma_v"))
    if not math.isfinite(v50) or not math.isfinite(sigma) or sigma <= 0:
        return None
    sign = 1 if fit.get("transition_direction") == "ascending" else -1
    lower = _number(fit.get("fit_lower_plateau_efficiency"))
    upper = _number(fit.get("fit_upper_plateau_efficiency"))
    if not math.isfinite(lower) or not math.isfinite(upper) or upper <= lower:
        lower, upper = 0.0, 1.0
    probability = np.array(
        [_NORMAL.cdf(sign * (value - v50) / sigma) for value in voltage]
    )
    return lower + (upper - lower) * probability


def _scurve_code_prediction(fit: pd.Series, code: np.ndarray) -> np.ndarray | None:
    d50 = _number(fit.get("d50_code"))
    sigma = _number(fit.get("sigma_dac_codes"))
    if not math.isfinite(d50) or not math.isfinite(sigma) or sigma <= 0:
        return None
    code_direction = fit.get(
        "code_transition_direction", fit.get("transition_direction")
    )
    sign = 1 if code_direction == "ascending" else -1
    lower = _number(fit.get("fit_lower_plateau_efficiency"))
    upper = _number(fit.get("fit_upper_plateau_efficiency"))
    if not math.isfinite(lower) or not math.isfinite(upper) or upper <= lower:
        lower, upper = 0.0, 1.0
    probability = np.array(
        [_NORMAL.cdf(sign * (value - d50) / sigma) for value in code]
    )
    return lower + (upper - lower) * probability


@_parallel_figures
def generate_diagnostic_plots(
    *,
    analysis_directory: Path,
    noise_statistics: pd.DataFrame,
    noise_fits: pd.DataFrame,
    trim_characterization: pd.DataFrame,
    trim_characterization_summary: pd.DataFrame,
    scurve_efficiency: pd.DataFrame,
    scurve_results: pd.DataFrame,
    scurve_amplitude_summary: pd.DataFrame,
    scurve_gain_results: pd.DataFrame,
    crosstalk_pixel_metrics: pd.DataFrame,
    crosstalk_summary: pd.DataFrame,
    target_voltage: float | None,
    settings: AnalysisSettings,
) -> dict[str, list[Path]]:
    """Generate every plot exclusively from saved/processed experiment data."""

    plot_directory = analysis_directory / "plots"
    outputs: dict[str, list[Path]] = {}
    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.labelsize": 10,
            "axes.titlesize": 11,
            "legend.fontsize": 9,
            "figure.dpi": 120,
            "savefig.facecolor": "white",
            "axes.grid": True,
            "grid.alpha": 0.25,
        }
    )

    if settings.plot_injection_patterns:
        selected_patterns = set(settings.plot_injection_patterns)

        def selected_pattern_rows(frame: pd.DataFrame) -> pd.DataFrame:
            if frame.empty or "injection_pattern" not in frame:
                return frame
            return frame[frame["injection_pattern"].isin(selected_patterns)].copy()

        scurve_efficiency = selected_pattern_rows(scurve_efficiency)
        scurve_results = selected_pattern_rows(scurve_results)
        scurve_amplitude_summary = selected_pattern_rows(scurve_amplitude_summary)
        scurve_gain_results = selected_pattern_rows(scurve_gain_results)
        crosstalk_pixel_metrics = selected_pattern_rows(crosstalk_pixel_metrics)
        crosstalk_summary = selected_pattern_rows(crosstalk_summary)

    stage_styles = {
        "trim_00": ("Trim = 0", "#1f77b4"),
        "trim_31": ("Trim = 31", "#d62728"),
        "equalized_final": ("Equalized", "#2ca02c"),
        "baseline_noise": ("Baseline", "#9467bd"),
    }
    final_available = not _stage_frame(noise_fits, "equalized_final").empty
    distribution_title = (
        "Threshold distributions before and after equalization"
        if final_available else "Measured noise-threshold distributions; final equalization NOT measured"
    )
    available_distributions: dict[str, np.ndarray] = {}
    for stage in stage_styles:
        values = _finite_values(
            _stage_frame(noise_fits, stage), "center_selected_v"
        )
        if len(values):
            available_distributions[stage] = values
    if available_distributions:
        combined = np.concatenate(list(available_distributions.values()))
        bins = np.histogram_bin_edges(combined, bins="auto")
        if len(bins) < 8:
            bins = np.linspace(float(combined.min()), float(combined.max()), 16)
        figure, axis = plt.subplots(figsize=(7.2, 4.8))
        for stage, values in available_distributions.items():
            label, color = stage_styles[stage]
            axis.hist(values, bins=bins, histtype="step", linewidth=1.8, label=f"{label} (n={len(values)})", color=color)
        if target_voltage is not None:
            axis.axvline(float(target_voltage), color="black", linestyle="--", label="Target")
        axis.set_xlabel("Effective threshold voltage, V")
        axis.set_ylabel("Pixel count")
        axis.set_title(distribution_title)
        axis.legend()
        outputs["threshold_distributions"] = _save_figure(
            figure, plot_directory, "threshold_distributions", settings
        )

        figure, axis = plt.subplots(figsize=(7.2, 4.8))
        for stage in stage_styles:
            if stage not in available_distributions:
                continue
            values = available_distributions[stage]
            label, color = stage_styles[stage]
            axis.hist(
                values,
                bins=bins,
                alpha=0.28 if stage != "equalized_final" else 0.55,
                label=f"{label} (n={len(values)})",
                color=color,
            )
        if target_voltage is not None:
            axis.axvline(float(target_voltage), color="black", linestyle="--", linewidth=1.2)
        axis.set_xlabel("Effective threshold voltage, V")
        axis.set_ylabel("Pixel count")
        axis.set_title("Medipix-style threshold equalization" if final_available else "Measured distributions; no final equalization data")
        axis.legend()
        outputs["medipix_equalization"] = _save_figure(
            figure, plot_directory, "medipix_equalization", settings
        )

        figure, axis = plt.subplots(figsize=(7.2, 4.8))
        for stage in stage_styles:
            if stage not in available_distributions:
                continue
            values = available_distributions[stage]
            label, color = stage_styles[stage]
            axis.hist(
                values,
                bins=bins,
                histtype="step",
                linewidth=1.7,
                label=f"{label} (n={len(values)})",
                color=color,
            )
        axis.set_yscale("log")
        axis.set_ylim(bottom=0.8)
        axis.set_xlabel("Effective threshold voltage, V")
        axis.set_ylabel("Pixel count, log scale")
        axis.set_title("Measured threshold distributions, logarithmic view")
        axis.legend()
        outputs["threshold_distributions_log"] = _save_figure(
            figure, plot_directory, "threshold_distributions_log", settings
        )

        # A common x axis is useful for the physical trim displacement, but it
        # visually hides the much narrower equalized population. Keep both
        # views and give every measured stage an independent, data-driven scale.
        displayed_stages = [
            stage for stage in ("trim_00", "equalized_final", "trim_31", "baseline_noise")
            if stage in available_distributions
        ]
        figure, axes = plt.subplots(
            1,
            len(displayed_stages),
            figsize=(4.4 * len(displayed_stages), 4.5),
            squeeze=False,
        )
        for axis, stage in zip(axes[0], displayed_stages):
            values = available_distributions[stage]
            stage_bins = np.histogram_bin_edges(values, bins="auto")
            if len(stage_bins) < 21 and float(np.max(values)) > float(np.min(values)):
                stage_bins = np.linspace(
                    float(np.min(values)), float(np.max(values)), 21
                )
            label, color = stage_styles[stage]
            axis.hist(values, bins=stage_bins, color=color, alpha=0.55, edgecolor=color)
            median = float(np.median(values))
            mad = float(np.median(np.abs(values - median)))
            std = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
            axis.axvline(median, color="black", linestyle="--", linewidth=1.0)
            axis.set_title(f"{label}\nstd={1000 * std:.2f} mV, MAD={1000 * mad:.2f} mV")
            axis.set_xlabel("Effective threshold, V")
            axis.set_ylabel("Pixel count")
        outputs["threshold_distributions_individual_scale"] = _save_figure(
            figure,
            plot_directory,
            "threshold_distributions_individual_scale",
            settings,
        )

        # Compare the same physical pixels, so missing endpoints cannot mimic
        # an improvement of the threshold dispersion.
        comparable = noise_fits[noise_fits["stage"].isin(available_distributions)].pivot_table(
            index=["column", "row"], columns="stage", values="center_selected_v", aggfunc="first"
        ).reindex(columns=list(available_distributions)).dropna()
        if not comparable.empty and len(comparable.columns) >= 2:
            figure, axis = plt.subplots(figsize=(7.2, 4.8))
            common_bins = np.histogram_bin_edges(comparable.to_numpy().ravel(), bins="auto")
            for stage in comparable.columns:
                label, color = stage_styles[stage]
                axis.hist(comparable[stage], bins=common_bins, histtype="step", linewidth=1.7, color=color, label=f"{label} (n={len(comparable)})")
            axis.set_xlabel("Effective threshold voltage, V")
            axis.set_ylabel("Pixel count")
            axis.set_title("Identical physical pixel population across measured stages")
            axis.legend()
            outputs["threshold_distributions_matched_pixels"] = _save_figure(
                figure, plot_directory, "threshold_distributions_matched_pixels", settings
            )

        dispersion_rows = []
        for stage, values in available_distributions.items():
            median = float(np.median(values))
            dispersion_rows.append(
                {
                    "stage": stage,
                    "standard_deviation": float(np.std(values, ddof=1)) if len(values) > 1 else 0.0,
                    "mad": float(np.median(np.abs(values - median))),
                    "peak_to_peak": float(np.ptp(values)),
                }
            )
        dispersion = pd.DataFrame(dispersion_rows)
        if not dispersion.empty:
            figure, axis = plt.subplots(figsize=(7.4, 4.6))
            positions = np.arange(len(dispersion))
            width = 0.25
            for metric_index, (column, label) in enumerate(
                (("standard_deviation", "Std"), ("mad", "MAD"), ("peak_to_peak", "Peak-to-peak"))
            ):
                axis.bar(
                    positions + (metric_index - 1) * width,
                    1000 * dispersion[column],
                    width=width,
                    label=label,
                )
            axis.set_xticks(
                positions,
                [stage_styles[stage][0] for stage in dispersion["stage"]],
            )
            axis.set_ylabel("Threshold dispersion, mV")
            axis.set_title("Equalization dispersion metrics" if final_available else "Endpoint dispersion only; unequal valid-pixel populations")
            axis.legend()
            outputs["equalization_improvement"] = _save_figure(
                figure, plot_directory, "equalization_improvement", settings
            )

        figure, axis = plt.subplots(figsize=(7.2, 4.6))
        plotted_widths = False
        for stage, (label, color) in stage_styles.items():
            stage_fits = _stage_frame(noise_fits, stage)
            if "fit_status" in stage_fits:
                stage_fits = stage_fits[stage_fits["fit_status"] == "ok"]
            widths = 1000 * _finite_values(stage_fits, "sigma_fit_v")
            if not len(widths):
                continue
            axis.hist(
                widths,
                bins="auto",
                histtype="step",
                linewidth=1.6,
                color=color,
                label=f"{label} (valid fits: {len(widths)})",
            )
            plotted_widths = True
        if plotted_widths:
            axis.set_xlabel("Fitted noise width, mV")
            axis.set_ylabel("Pixel count")
            axis.set_title("Accepted noise-fit widths; not an input-charge ENC measurement")
            axis.legend()
            outputs["noise_width_distributions"] = _save_figure(
                figure, plot_directory, "noise_width_distributions", settings
            )
        else:
            plt.close(figure)

        overview_order = ("trim_00", "equalized_final", "trim_31")
        if all(stage in available_distributions for stage in overview_order):
            overview_frames = {
                stage: _stage_frame(noise_fits, stage) for stage in overview_order
            }
            arrays = {
                stage: _matrix_array(frame, "center_selected_v")
                for stage, frame in overview_frames.items()
            }
            if all(value is not None for value in arrays.values()):
                figure = plt.figure(figsize=(12.8, 8.2), layout="constrained")
                grid = figure.add_gridspec(
                    2, 3, height_ratios=(0.85, 1.15), hspace=0.16
                )
                histogram_axis = figure.add_subplot(grid[0, :])
                for stage in overview_order:
                    values = available_distributions[stage]
                    label, color = stage_styles[stage]
                    histogram_axis.hist(
                        values,
                        bins=bins,
                        histtype="stepfilled",
                        alpha=0.18,
                        linewidth=1.5,
                        color=color,
                        label=label,
                    )
                    histogram_axis.hist(
                        values,
                        bins=bins,
                        histtype="step",
                        linewidth=1.5,
                        color=color,
                    )
                if target_voltage is not None:
                    histogram_axis.axvline(
                        float(target_voltage), color="black", linestyle="--", linewidth=1.2
                    )
                histogram_axis.set_xlabel("Effective threshold voltage, V")
                histogram_axis.set_ylabel("Pixel count")
                histogram_axis.set_title("Baseline distributions and spatial maps")
                histogram_axis.legend(ncol=3)

                common_values = np.concatenate(
                    [
                        arrays[stage][0][np.isfinite(arrays[stage][0])]
                        for stage in overview_order
                    ]
                )
                vmin = float(np.min(common_values))
                vmax = float(np.max(common_values))
                image = None
                for index, stage in enumerate(overview_order):
                    array, columns, rows = arrays[stage]
                    axis = figure.add_subplot(grid[1, index])
                    image = axis.imshow(
                        array,
                        origin="lower",
                        aspect="auto",
                        interpolation="nearest",
                        extent=(
                            columns[0] - 0.5,
                            columns[-1] + 0.5,
                            rows[0] - 0.5,
                            rows[-1] + 0.5,
                        ),
                        cmap="coolwarm",
                        vmin=vmin,
                        vmax=vmax,
                    )
                    axis.set_xlabel("Physical column")
                    axis.set_ylabel("Physical row" if index == 0 else "")
                    axis.set_title(stage_styles[stage][0])
                if image is not None:
                    colorbar = figure.colorbar(
                        image,
                        ax=figure.axes[1:],
                        pad=0.02,
                        fraction=0.025,
                    )
                    colorbar.set_label("Effective threshold, V")
                outputs["noise_equalization_overview"] = _save_figure(
                    figure,
                    plot_directory,
                    "noise_equalization_overview",
                    settings,
                )

        for stage, (label, _) in stage_styles.items():
            stage_data = _stage_frame(noise_fits, stage)
            if stage_data.empty:
                continue
            center_map = _heatmap(
                stage_data,
                value_column="center_selected_v",
                title=f"{label}: effective-threshold map",
                colorbar_label="Effective threshold, V",
                cmap="viridis",
            )
            if center_map is not None:
                outputs[f"threshold_map_{stage}"] = _save_figure(
                    center_map,
                    plot_directory,
                    f"effective_threshold_map_{stage}",
                    settings,
                )
            centers = pd.to_numeric(
                stage_data["center_selected_v"], errors="coerce"
            )
            median_center = float(centers.median())
            offset_data = stage_data.copy()
            offset_data["spatial_offset_v"] = centers - median_center
            offset_map = _heatmap(
                offset_data,
                value_column="spatial_offset_v",
                title=f"{label}: spatial offset from stage median",
                colorbar_label="Offset, V",
                cmap="coolwarm",
                center_zero=True,
            )
            if offset_map is not None:
                outputs[f"spatial_offset_map_{stage}"] = _save_figure(
                    offset_map,
                    plot_directory,
                    f"spatial_offset_map_{stage}",
                    settings,
                )

    matrix_curve_stages = [
        stage for stage in stage_styles
        if not _stage_frame(noise_statistics, stage).empty
    ]
    if matrix_curve_stages:
        figure, axis = plt.subplots(figsize=(7.2, 4.8))
        for stage in matrix_curve_stages:
            data = _stage_frame(noise_statistics, stage)
            envelope = (
                data.groupby("threshold_voltage_v")["mean_count"]
                .agg(
                    median="median",
                    mean="mean",
                    q10=lambda values: values.quantile(0.10),
                    q90=lambda values: values.quantile(0.90),
                )
                .reset_index()
                .sort_values("threshold_voltage_v")
            )
            label, color = stage_styles[stage]
            x = envelope["threshold_voltage_v"].to_numpy(dtype=float)
            axis.plot(x, envelope["mean"], color=color, linewidth=1.5, label=f"{label}: mean")
            axis.plot(x, envelope["median"], color=color, linewidth=1.0, linestyle="--", label=f"{label}: median")
            axis.fill_between(
                x,
                envelope["q10"].to_numpy(dtype=float),
                envelope["q90"].to_numpy(dtype=float),
                color=color,
                alpha=0.12,
            )
        axis.set_xlabel("Threshold voltage, V")
        axis.set_ylabel("Decoded counter value")
        axis.set_title("Unaligned matrix noise: mean, median and 10-90% pixel band")
        axis.legend()
        outputs["noise_matrix_curves"] = _save_figure(
            figure, plot_directory, "noise_matrix_curves", settings
        )
        zoom = _save_noise_zoom(figure, axis, noise_statistics[
            noise_statistics["stage"].isin(matrix_curve_stages)],
            plot_directory, "noise_matrix_curves_zoom", settings)
        if zoom:
            outputs["noise_matrix_curves_zoom"] = zoom

    if not trim_characterization_summary.empty:
        summary = trim_characterization_summary.sort_values("trim_code")
        trim_codes = pd.to_numeric(summary["trim_code"], errors="coerce").to_numpy(dtype=float)
        figure, axes = plt.subplots(1, 3, figsize=(13.5, 4.2), sharex=True)
        axes[0].plot(trim_codes, summary["center_median_v"], marker="o", markersize=3)
        axes[0].fill_between(
            trim_codes,
            pd.to_numeric(summary["center_q10_v"], errors="coerce").to_numpy(dtype=float),
            pd.to_numeric(summary["center_q90_v"], errors="coerce").to_numpy(dtype=float),
            alpha=0.22,
            label="10-90% pixels",
        )
        axes[0].set_ylabel("Effective threshold, V")
        axes[0].legend()
        axes[1].plot(
            trim_codes,
            1000 * pd.to_numeric(summary["noise_sigma_median_v"], errors="coerce"),
            marker="o",
            markersize=3,
            color="#d95f02",
        )
        axes[1].fill_between(
            trim_codes,
            1000 * pd.to_numeric(summary["noise_sigma_q10_v"], errors="coerce").to_numpy(dtype=float),
            1000 * pd.to_numeric(summary["noise_sigma_q90_v"], errors="coerce").to_numpy(dtype=float),
            alpha=0.22,
            color="#d95f02",
        )
        axes[1].set_ylabel("Noise width, mV")
        axes[2].plot(
            trim_codes,
            pd.to_numeric(summary["successful_fit_fraction"], errors="coerce"),
            marker="o",
            markersize=3,
            color="#1b9e77",
        )
        axes[2].set_ylim(-0.02, 1.02)
        axes[2].set_ylabel("Successful-fit fraction")
        for axis in axes:
            axis.set_xlabel("Uniform trim code")
            axis.set_xlim(-0.5, 31.5)
        figure.suptitle("Uniform-trim response of the owned matrix")
        outputs["uniform_trim_response_summary"] = _save_figure(
            figure, plot_directory, "uniform_trim_response_summary", settings
        )

    if not trim_characterization.empty and trim_characterization["trim_code"].nunique() > 2:
        trim_data = trim_characterization.copy()
        trim_data["trim_code"] = pd.to_numeric(trim_data["trim_code"], errors="coerce")
        trim_data["center_selected_v"] = pd.to_numeric(
            trim_data["center_selected_v"], errors="coerce"
        )
        trim_data = trim_data.dropna(subset=["trim_code", "center_selected_v"])
        if not trim_data.empty:
            centers = trim_data["center_selected_v"].to_numpy(dtype=float)
            bins = np.histogram_bin_edges(centers, bins="auto")
            if len(bins) < 12:
                bins = np.linspace(float(np.min(centers)), float(np.max(centers)), 24)
            figure, axis = plt.subplots(figsize=(8.0, 5.2))
            color_map = plt.get_cmap("viridis")
            for trim_code, group in trim_data.groupby("trim_code", sort=True):
                axis.hist(
                    group["center_selected_v"],
                    bins=bins,
                    histtype="step",
                    linewidth=0.8,
                    color=color_map(float(trim_code) / 31.0),
                    alpha=0.9,
                )
            scalar = plt.cm.ScalarMappable(cmap=color_map, norm=plt.Normalize(0, 31))
            colorbar = figure.colorbar(scalar, ax=axis, pad=0.02)
            colorbar.set_label("Uniform trim code")
            axis.set_xlabel("Effective threshold voltage, V")
            axis.set_ylabel("Pixel count")
            axis.set_title("Threshold distributions for every uniform trim code")
            outputs["uniform_trim_distributions"] = _save_figure(
                figure, plot_directory, "uniform_trim_distributions", settings
            )

            coordinates = _plot_coordinates(
                settings, trim_data, "center_selected_v"
            )
            for column, row in coordinates:
                pixel = trim_data[
                    (trim_data["column"] == column) & (trim_data["row"] == row)
                ].sort_values("trim_code")
                if pixel.empty:
                    continue
                figure, axes = plt.subplots(1, 2, figsize=(10.4, 4.2))
                axes[0].plot(
                    pixel["trim_code"], pixel["center_selected_v"], marker="o", markersize=3
                )
                axes[0].set_ylabel("Effective threshold, V")
                axes[1].plot(
                    pixel["trim_code"],
                    1000 * pd.to_numeric(pixel["sigma_fit_v"], errors="coerce"),
                    marker="o",
                    markersize=3,
                    color="#d95f02",
                )
                axes[1].set_ylabel("Noise width, mV")
                for axis in axes:
                    axis.set_xlabel("Uniform trim code")
                    axis.set_xlim(-0.5, 31.5)
                figure.suptitle(f"Pixel C{column:02d} R{row:02d}: trim transfer")
                stem = f"pixel_C{column:02d}_R{row:02d}_trim_transfer"
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

            if settings.plot_all_trim_heatmaps:
                for trim_code, group in trim_data.groupby("trim_code", sort=True):
                    figure = _heatmap(
                        group,
                        value_column="center_selected_v",
                        title=f"Uniform trim {int(trim_code):02d}: effective threshold",
                        colorbar_label="Effective threshold, V",
                        cmap="viridis",
                    )
                    if figure is not None:
                        stem = f"uniform_trim_{int(trim_code):02d}_threshold_map"
                        outputs[stem] = _save_figure(
                            figure, plot_directory, stem, settings
                        )

        uniform_stages = noise_statistics[
            noise_statistics["stage"].astype(str).isin(("trim_00", "trim_31"))
            | noise_statistics["stage"].astype(str).str.startswith("trim_full_")
        ].copy()
        if not uniform_stages.empty:
            figure, axis = plt.subplots(figsize=(8.0, 5.2))
            color_map = plt.get_cmap("viridis")
            plotted_matrix_trim = False
            for trim_code, group in uniform_stages.groupby("local_trim_code", sort=True):
                curve = (
                    group.groupby("threshold_voltage_v")["mean_count"]
                    .mean()
                    .sort_index()
                )
                if curve.empty:
                    continue
                axis.plot(
                    curve.index,
                    curve.values,
                    linewidth=0.85,
                    color=color_map(float(trim_code) / 31.0),
                )
                plotted_matrix_trim = True
            if plotted_matrix_trim:
                scalar = plt.cm.ScalarMappable(cmap=color_map, norm=plt.Normalize(0, 31))
                colorbar = figure.colorbar(scalar, ax=axis, pad=0.02)
                colorbar.set_label("Uniform trim code")
                axis.set_xlabel("Threshold voltage, V")
                axis.set_ylabel("Matrix-mean counter value")
                axis.set_title("Unaligned matrix-mean noise curves for measured trims")
                outputs["matrix_all_trim_noise_curves"] = _save_figure(
                    figure, plot_directory, "matrix_all_trim_noise_curves", settings
                )
            else:
                plt.close(figure)

            coordinates = _plot_coordinates(
                settings, trim_characterization, "center_selected_v"
            )
            for column, row in coordinates:
                pixel_curves = uniform_stages[
                    (uniform_stages["column"] == column)
                    & (uniform_stages["row"] == row)
                ].copy()
                if pixel_curves.empty:
                    continue
                figure, axis = plt.subplots(figsize=(8.0, 5.2))
                color_map = plt.get_cmap("viridis")
                for trim_code, curve in pixel_curves.groupby("local_trim_code", sort=True):
                    curve = curve.sort_values("threshold_voltage_v")
                    axis.plot(
                        curve["threshold_voltage_v"],
                        curve["mean_count"],
                        linewidth=0.85,
                        color=color_map(float(trim_code) / 31.0),
                    )
                scalar = plt.cm.ScalarMappable(cmap=color_map, norm=plt.Normalize(0, 31))
                colorbar = figure.colorbar(scalar, ax=axis, pad=0.02)
                colorbar.set_label("Uniform trim code")
                axis.set_xlabel("Threshold voltage, V")
                axis.set_ylabel("Decoded counter value")
                axis.set_title(f"Pixel C{column:02d} R{row:02d}: noise curves for all trims")
                stem = f"pixel_C{column:02d}_R{row:02d}_all_trim_noise_curves"
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    final = _stage_frame(noise_fits, "equalized_final")
    if not final.empty:
        trims = _finite_values(final, "local_trim_code")
        if len(trims):
            figure, axis = plt.subplots(figsize=(7.2, 4.5))
            axis.hist(trims, bins=np.arange(-0.5, 32.5, 1), color="#4c78a8", edgecolor="white")
            axis.set_xlim(-0.5, 31.5)
            axis.set_xlabel("Selected local trim code")
            axis.set_ylabel("Pixel count")
            axis.set_title("Equalization trim-code distribution")
            outputs["trim_histogram"] = _save_figure(
                figure, plot_directory, "trim_code_histogram", settings
            )

        heatmap_specs = (
            ("local_trim_code", "Selected trim map", "Trim code", "viridis", False, "trim_map"),
            (
                "center_selected_v",
                "Equalized effective-threshold map",
                "Effective threshold, V",
                "viridis",
                False,
                "effective_threshold_map",
            ),
            (
                "sigma_fit_v",
                "Threshold-domain noise map",
                "Fitted width, V",
                "magma",
                False,
                "noise_map",
            ),
        )
        if target_voltage is not None:
            final["residual_v"] = final["center_selected_v"] - float(target_voltage)
            heatmap_specs += (
                (
                    "residual_v",
                    "Equalized threshold residual map",
                    "Residual, V",
                    "coolwarm",
                    True,
                    "residual_map",
                ),
            )
        for value, title, label, cmap, centered, stem in heatmap_specs:
            figure = _heatmap(
                final,
                value_column=value,
                title=title,
                colorbar_label=label,
                cmap=cmap,
                center_zero=centered,
            )
            if figure is not None:
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    representative_stage = next((
        stage for stage in ("equalized_final", "trim_00", "baseline_noise", "trim_31")
        if not _stage_frame(noise_fits, stage).empty
    ), "trim_00")
    representative_fits = _stage_frame(noise_fits, representative_stage)
    representatives = _plot_coordinates(
        settings, representative_fits, "center_selected_v"
    )
    if representatives and not noise_statistics.empty:
        figure, axis = plt.subplots(figsize=(7.4, 5.0))
        for column, row in representatives:
            curve = noise_statistics[
                (noise_statistics["stage"] == representative_stage)
                & (noise_statistics["column"] == column)
                & (noise_statistics["row"] == row)
            ].sort_values("threshold_voltage_v")
            if curve.empty:
                continue
            axis.errorbar(
                curve["threshold_voltage_v"],
                curve["mean_count"],
                yerr=curve["sem_count"],
                marker=".",
                markersize=3,
                linewidth=0.9,
                capsize=1.5,
                label=f"C{column:02d} R{row:02d}",
            )
        axis.set_xlabel("Threshold voltage, V")
        axis.set_ylabel("Decoded counter value")
        axis.set_title(f"Noise curves (repeat mean and SEM): {representative_stage}")
        axis.legend(ncol=2)
        outputs["representative_noise_curves"] = _save_figure(
            figure, plot_directory, "representative_noise_curves", settings
        )

        for column, row in representatives:
            figure, axis = plt.subplots(figsize=(7.6, 5.0))
            plotted = False
            for stage in stage_styles:
                curve = noise_statistics[
                    (noise_statistics["stage"] == stage)
                    & (noise_statistics["column"] == column)
                    & (noise_statistics["row"] == row)
                ].sort_values("threshold_voltage_v")
                if curve.empty:
                    continue
                label, color = stage_styles[stage]
                axis.errorbar(
                    curve["threshold_voltage_v"],
                    curve["mean_count"],
                    yerr=curve.get("sem_count"),
                    marker=".",
                    markersize=3,
                    linewidth=0,
                    elinewidth=0.7,
                    capsize=1.2,
                    color=color,
                    alpha=0.75,
                    label=f"{label}: data",
                )
                fit = noise_fits[
                    (noise_fits["stage"] == stage)
                    & (noise_fits["column"] == column)
                    & (noise_fits["row"] == row)
                ]
                if not fit.empty:
                    voltage_grid = np.linspace(
                        float(curve["threshold_voltage_v"].min()),
                        float(curve["threshold_voltage_v"].max()),
                        400,
                    )
                    predicted = _noise_prediction(fit.iloc[0], voltage_grid)
                    if predicted is not None:
                        axis.plot(
                            voltage_grid,
                            predicted,
                            color=color,
                            linewidth=1.4,
                            label=f"{label}: fit",
                        )
                plotted = True
            if plotted:
                axis.set_xlabel("Threshold voltage, V")
                axis.set_ylabel("Decoded counter value")
                axis.set_title(f"Pixel C{column:02d} R{row:02d}: " + (
                    "noise before/after equalization" if final_available else "measured noise curves"
                ))
                axis.legend(ncol=2)
                stem = f"pixel_C{column:02d}_R{row:02d}_noise_before_after"
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)
                zoom = _save_noise_zoom(
                    figure,
                    axis,
                    noise_statistics[
                        (noise_statistics["column"] == column)
                        & (noise_statistics["row"] == row)
                        & noise_statistics["stage"].isin(stage_styles)
                    ],
                    plot_directory,
                    stem + "_zoom",
                    settings,
                )
                if zoom:
                    outputs[stem + "_zoom"] = zoom
            else:
                plt.close(figure)

    if not noise_fits.empty:
        r2 = _finite_values(noise_fits, "fit_r2")
        if len(r2):
            figure, axis = plt.subplots(figsize=(7.2, 4.5))
            axis.hist(r2, bins=30, color="#7a5195", edgecolor="white")
            axis.axvline(settings.gaussian_min_r2, color="black", linestyle="--")
            axis.set_xlabel("Noise-curve fit $R^2$")
            axis.set_ylabel("Fit count")
            axis.set_title("Noise fit-quality distribution")
            outputs["noise_fit_quality"] = _save_figure(
                figure, plot_directory, "noise_fit_quality", settings
            )

    background_stage = representative_stage
    background = _stage_frame(noise_statistics, background_stage)
    if not background.empty:
        envelope = background.groupby("threshold_voltage_v")["mean_count"].agg(
            median="median",
            q10=lambda values: values.quantile(0.10),
            q90=lambda values: values.quantile(0.90),
        ).reset_index().sort_values("threshold_voltage_v")
        figure, axis = plt.subplots(figsize=(7.2, 4.5))
        x = envelope["threshold_voltage_v"].to_numpy(dtype=float)
        axis.plot(x, envelope["median"], color="#1f77b4", label="Pixel median")
        axis.fill_between(
            x,
            envelope["q10"].to_numpy(dtype=float),
            envelope["q90"].to_numpy(dtype=float),
            alpha=0.25,
            color="#1f77b4",
            label="10-90% pixels",
        )
        axis.set_xlabel("Threshold voltage, V")
        axis.set_ylabel("Background count")
        axis.set_title("Background occupancy versus threshold")
        axis.legend()
        outputs["background_occupancy"] = _save_figure(
            figure, plot_directory, "background_occupancy", settings
        )

    if not scurve_efficiency.empty:
        raw_patterns = sorted(
            scurve_efficiency["injection_pattern"].dropna().astype(str).unique(),
            key=lambda value: (value != "all", value),
        )
        raw_color_map = plt.get_cmap("viridis")
        for pattern in raw_patterns:
            pattern_raw = scurve_efficiency[
                scurve_efficiency["injection_pattern"].astype(str) == pattern
            ].copy()
            amplitude_frames = list(
                pattern_raw.groupby(
                    "pulse_amplitude_native", dropna=False, sort=False
                )
            )
            for amplitude_index, (_, amplitude_raw) in enumerate(amplitude_frames):
                plot_window = _scurve_plot_code_window(amplitude_raw, settings)
                amplitude_plot = _scurve_plot_window_rows(
                    amplitude_raw, plot_window
                )
                signal_envelope = _scurve_raw_count_envelope(
                    amplitude_plot,
                    count_column="signal_count",
                    valid_column="signal_valid",
                    active_pixels_only=True,
                )
                background_envelope = _scurve_raw_count_envelope(
                    amplitude_plot,
                    count_column="background_count",
                    valid_column="background_valid",
                    active_pixels_only=False,
                )
                if signal_envelope.empty and background_envelope.empty:
                    continue
                figure, axes = plt.subplots(
                    1, 2, figsize=(12.2, 4.8), layout="constrained"
                )
                nominal_values: list[float] = []
                if not signal_envelope.empty:
                    x = signal_envelope["threshold_dac_code"].to_numpy(dtype=float)
                    axes[0].plot(
                        x,
                        signal_envelope["count_median"],
                        marker="o",
                        markersize=3.0,
                        linewidth=1.1,
                        color="#1f77b4",
                        label="Signal median",
                    )
                    axes[0].fill_between(
                        x,
                        signal_envelope["count_q10"].to_numpy(dtype=float),
                        signal_envelope["count_q90"].to_numpy(dtype=float),
                        color="#1f77b4",
                        alpha=0.20,
                        label="Signal 10-90% pixels",
                    )
                    axes[0].plot(
                        x,
                        signal_envelope["count_q95"],
                        color="#1f77b4",
                        linestyle=":",
                        linewidth=1.0,
                        label="Signal q95",
                    )
                    nominal_values.extend(
                        pd.to_numeric(
                            signal_envelope["effective_injections"], errors="coerce"
                        ).dropna().tolist()
                    )
                if not background_envelope.empty:
                    x = background_envelope["threshold_dac_code"].to_numpy(dtype=float)
                    axes[1].plot(
                        x,
                        background_envelope["count_median"],
                        marker="o",
                        markersize=3.0,
                        linewidth=1.1,
                        color="#4daf4a",
                        label="Background median",
                    )
                    axes[1].plot(
                        x,
                        background_envelope["count_q90"],
                        color="#ff7f00",
                        linewidth=1.2,
                        label="Background q90",
                    )
                    axes[1].plot(
                        x,
                        background_envelope["count_q95"],
                        color="#e41a1c",
                        linewidth=1.2,
                        label="Background q95",
                    )
                    axes[1].plot(
                        x,
                        background_envelope["count_maximum"],
                        color="#984ea3",
                        linestyle=":",
                        linewidth=0.9,
                        label="Background maximum",
                    )
                    nominal_values.extend(
                        pd.to_numeric(
                            background_envelope["effective_injections"], errors="coerce"
                        ).dropna().tolist()
                    )
                nominal = (
                    float(np.median(nominal_values)) if nominal_values else float("nan")
                )
                for axis in axes:
                    _style_scurve_code_axis(axis, plot_window)
                    if math.isfinite(nominal):
                        axis.axhline(
                            nominal,
                            color="black",
                            linestyle="--",
                            linewidth=1.0,
                            label=f"Effective analysis N = {nominal:g}",
                        )
                    axis.legend()
                axes[0].set_title("Injected pixels: signal counts")
                axes[1].set_title(
                    "Paired background: retained baseline-noise points"
                )
                figure.suptitle(
                    f"Raw S-curve counts, {_amplitude_label(amplitude_raw)}, "
                    f"pattern {pattern}; measured positive branch"
                )
                stem = (
                    f"matrix_scurve_raw_counts_{_safe_stem(pattern)}_"
                    f"amplitude_{amplitude_index:03d}"
                )
                outputs[stem] = _save_figure(
                    figure, plot_directory, stem, settings
                )

            pattern_results = (
                scurve_results[
                    scurve_results["injection_pattern"].astype(str) == pattern
                ].copy()
                if not scurve_results.empty
                else pd.DataFrame()
            )
            if not pattern_results.empty:
                coordinate_source = pattern_results
                coordinate_value = "v50_v"
            else:
                coordinate_source = pattern_raw[
                    _truthy_series(pattern_raw, "active_injection_pixel_bool")
                ].copy()
                coordinate_source = coordinate_source.groupby(
                    ["column", "row"], as_index=False
                ).agg(threshold_voltage_v=("threshold_voltage_v", "median"))
                coordinate_value = "threshold_voltage_v"
            raw_coordinates = _plot_coordinates(
                settings, coordinate_source, coordinate_value
            )
            for column, row in raw_coordinates:
                pixel_pattern_raw = pattern_raw[
                    (pd.to_numeric(pattern_raw["column"], errors="coerce") == column)
                    & (pd.to_numeric(pattern_raw["row"], errors="coerce") == row)
                ]
                plot_window = _scurve_plot_code_window(
                    pixel_pattern_raw, settings
                )
                figure, axes = plt.subplots(
                    1, 2, figsize=(12.2, 4.8), layout="constrained"
                )
                plotted = False
                nominal_values = []
                for amplitude_index, (_, amplitude_raw) in enumerate(amplitude_frames):
                    pixel_raw = amplitude_raw[
                        (pd.to_numeric(amplitude_raw["column"], errors="coerce") == column)
                        & (pd.to_numeric(amplitude_raw["row"], errors="coerce") == row)
                    ]
                    pixel_raw = _scurve_plot_window_rows(
                        pixel_raw, plot_window
                    )
                    signal_envelope = _scurve_raw_count_envelope(
                        pixel_raw,
                        count_column="signal_count",
                        valid_column="signal_valid",
                        active_pixels_only=True,
                    )
                    background_envelope = _scurve_raw_count_envelope(
                        pixel_raw,
                        count_column="background_count",
                        valid_column="background_valid",
                        active_pixels_only=False,
                    )
                    color = raw_color_map(
                        amplitude_index / max(len(amplitude_frames) - 1, 1)
                    )
                    label = _amplitude_label(amplitude_raw)
                    if not signal_envelope.empty:
                        x = signal_envelope["threshold_dac_code"].to_numpy(dtype=float)
                        axes[0].plot(
                            x,
                            signal_envelope["count_median"],
                            marker="o",
                            markersize=2.8,
                            linewidth=1.0,
                            color=color,
                            label=label,
                        )
                        axes[0].fill_between(
                            x,
                            signal_envelope["count_q10"].to_numpy(dtype=float),
                            signal_envelope["count_q90"].to_numpy(dtype=float),
                            color=color,
                            alpha=0.08,
                        )
                        nominal_values.extend(
                            pd.to_numeric(
                                signal_envelope["effective_injections"], errors="coerce"
                            ).dropna().tolist()
                        )
                        plotted = True
                    if not background_envelope.empty:
                        x = background_envelope["threshold_dac_code"].to_numpy(dtype=float)
                        axes[1].plot(
                            x,
                            background_envelope["count_q95"],
                            marker="o",
                            markersize=2.8,
                            linewidth=1.0,
                            color=color,
                            label=label,
                        )
                        axes[1].plot(
                            x,
                            background_envelope["count_median"],
                            linestyle=":",
                            linewidth=0.8,
                            color=color,
                        )
                        nominal_values.extend(
                            pd.to_numeric(
                                background_envelope["effective_injections"], errors="coerce"
                            ).dropna().tolist()
                        )
                        plotted = True
                if not plotted:
                    plt.close(figure)
                    continue
                nominal = (
                    float(np.median(nominal_values)) if nominal_values else float("nan")
                )
                for axis in axes:
                    _style_scurve_code_axis(axis, plot_window)
                    if math.isfinite(nominal):
                        axis.axhline(
                            nominal,
                            color="black",
                            linestyle="--",
                            linewidth=1.0,
                            label=f"Effective analysis N = {nominal:g}",
                        )
                    axis.legend(ncol=2)
                axes[0].set_title("Signal median and 10-90% repeats")
                axes[1].set_title("Background q95; dotted lines are medians")
                figure.suptitle(
                    f"Pixel C{column:02d} R{row:02d}: raw S-curve counts, "
                    f"pattern {pattern}"
                )
                stem = (
                    f"pixel_C{column:02d}_R{row:02d}_"
                    f"scurve_raw_counts_{_safe_stem(pattern)}"
                )
                outputs[stem] = _save_figure(
                    figure, plot_directory, stem, settings
                )

        valid = scurve_efficiency[
            scurve_efficiency["fit_valid"].astype(str).str.lower().isin(("true", "1"))
        ].copy()
        if not valid.empty:
            amplitude_order = (
                valid.groupby("pulse_amplitude_native", dropna=False, as_index=False)
                .agg(
                    requested_step=("requested_injection_voltage_step_v", "median"),
                    actual_step=("injection_voltage_step_v", "median"),
                    charge=("injection_charge_electrons", "median"),
                )
            )
            amplitude_order["sort_value"] = pd.to_numeric(
                amplitude_order["actual_step"], errors="coerce"
            )
            missing_sort = amplitude_order["sort_value"].isna()
            amplitude_order.loc[missing_sort, "sort_value"] = np.arange(
                int(missing_sort.sum()), dtype=float
            ) + 1e6
            amplitudes = amplitude_order.sort_values("sort_value")[
                "pulse_amplitude_native"
            ].tolist()
            patterns = list(valid["injection_pattern"].drop_duplicates())
            patterns = sorted(patterns, key=lambda value: (value != "all", str(value)))
            color_map = plt.get_cmap("viridis")

            for pattern in patterns:
                pattern_data = valid[valid["injection_pattern"] == pattern]
                pattern_results = (
                    scurve_results[scurve_results["injection_pattern"] == pattern]
                    if not scurve_results.empty
                    else pd.DataFrame()
                )
                coordinates = _plot_coordinates(
                    settings,
                    pattern_results if not pattern_results.empty else pattern_data,
                    "v50_v" if not pattern_results.empty else "threshold_voltage_v",
                )
                for column, row in coordinates:
                    pixel_pattern_data = pattern_data[
                        (pattern_data["column"] == column)
                        & (pattern_data["row"] == row)
                    ]
                    plot_window = _scurve_plot_code_window(
                        pixel_pattern_data, settings
                    )
                    figure, axis = plt.subplots(figsize=(7.8, 5.2))
                    plotted = False
                    for amplitude_index, amplitude in enumerate(amplitudes):
                        points_frame = pattern_data[
                            (pattern_data["pulse_amplitude_native"] == amplitude)
                            & (pattern_data["column"] == column)
                            & (pattern_data["row"] == row)
                        ]
                        if points_frame.empty:
                            continue
                        points_frame = _scurve_plot_window_rows(
                            points_frame, plot_window
                        )
                        if points_frame.empty:
                            continue
                        points = (
                            points_frame.groupby("threshold_dac_code")["efficiency"]
                            .mean()
                            .sort_index()
                        )
                        color = color_map(
                            amplitude_index / max(len(amplitudes) - 1, 1)
                        )
                        label = _amplitude_label(points_frame)
                        axis.plot(
                            points.index,
                            points.values,
                            marker="o",
                            markersize=2.8,
                            linewidth=0,
                            color=color,
                            label=label,
                        )
                        fit = (
                            pattern_results[
                                (pattern_results["pulse_amplitude_native"] == amplitude)
                                & (pattern_results["column"] == column)
                                & (pattern_results["row"] == row)
                            ]
                            if not pattern_results.empty
                            else pd.DataFrame()
                        )
                        if not fit.empty and fit.iloc[0]["fit_status"] in (
                            "ok",
                            "poor_quality",
                        ):
                            code_grid = np.linspace(
                                float(points.index.min()), float(points.index.max()), 350
                            )
                            predicted = _scurve_code_prediction(
                                fit.iloc[0], code_grid
                            )
                            if predicted is not None:
                                axis.plot(
                                    code_grid,
                                    predicted,
                                    color=color,
                                    linewidth=1.2,
                                )
                        plotted = True
                    if plotted:
                        axis.set_ylim(-0.05, 1.05)
                        if plot_window is not None:
                            axis.set_xlim(plot_window[0], plot_window[1])
                        axis.set_xlabel("Threshold DAC code")
                        axis.set_ylabel("Detection efficiency")
                        axis.set_title(
                            f"Pixel C{column:02d} R{row:02d}: S-curves, {pattern}"
                        )
                        axis.legend(ncol=2, title="Injected amplitude")
                        stem = (
                            f"pixel_C{column:02d}_R{row:02d}_"
                            f"scurves_{_safe_stem(pattern)}"
                        )
                        outputs[stem] = _save_figure(
                            figure, plot_directory, stem, settings
                        )
                    else:
                        plt.close(figure)

                matrix_window = _scurve_plot_code_window(
                    pattern_data, settings
                )
                figure, axes = plt.subplots(1, 2, figsize=(12.2, 4.7))
                plotted_matrix = False
                for amplitude_index, amplitude in enumerate(amplitudes):
                    data = pattern_data[
                        pattern_data["pulse_amplitude_native"] == amplitude
                    ]
                    if data.empty:
                        continue
                    data = _scurve_plot_window_rows(data, matrix_window)
                    if data.empty:
                        continue
                    envelope = (
                        data.groupby("threshold_dac_code")["efficiency"]
                        .agg(
                            median="median",
                            q10=lambda values: values.quantile(0.10),
                            q90=lambda values: values.quantile(0.90),
                        )
                        .reset_index()
                        .sort_values("threshold_dac_code")
                    )
                    x = envelope["threshold_dac_code"].to_numpy(dtype=float)
                    y = envelope["median"].to_numpy(dtype=float)
                    color = color_map(
                        amplitude_index / max(len(amplitudes) - 1, 1)
                    )
                    label = _amplitude_label(data)
                    axes[0].plot(x, y, color=color, linewidth=1.25, label=label)
                    axes[0].fill_between(
                        x,
                        envelope["q10"].to_numpy(dtype=float),
                        envelope["q90"].to_numpy(dtype=float),
                        color=color,
                        alpha=0.08,
                    )
                    if len(x) >= 3:
                        order = np.argsort(x)
                        derivative = np.abs(np.gradient(y[order], x[order]))
                        axes[1].plot(
                            x[order], derivative, color=color, linewidth=1.25, label=label
                        )
                    plotted_matrix = True
                if plotted_matrix:
                    axes[0].set_ylim(-0.05, 1.05)
                    if matrix_window is not None:
                        for axis in axes:
                            axis.set_xlim(matrix_window[0], matrix_window[1])
                    axes[0].set_xlabel("Threshold DAC code")
                    axes[0].set_ylabel("Median efficiency")
                    axes[0].set_title("S-curves, median and 10-90% pixel band")
                    axes[1].set_xlabel("Threshold DAC code")
                    axes[1].set_ylabel("|d efficiency / dDAC|")
                    axes[1].set_title("Derivative of the matrix-median S-curve")
                    axes[0].legend(ncol=2, title="Injected amplitude")
                    figure.suptitle(f"Owned-matrix response, pattern {pattern}")
                    stem = f"matrix_scurves_{_safe_stem(pattern)}"
                    outputs[stem] = _save_figure(
                        figure, plot_directory, stem, settings
                    )
                else:
                    plt.close(figure)

            if not scurve_results.empty:
                for pattern in patterns:
                    for amplitude_index, amplitude in enumerate(amplitudes):
                        data = scurve_results[
                            (scurve_results["injection_pattern"] == pattern)
                            & (scurve_results["pulse_amplitude_native"] == amplitude)
                            & scurve_results["fit_status"].isin(("ok", "poor_quality"))
                        ].copy()
                        if data.empty:
                            continue
                        v50_map = _matrix_array(data, "v50_v")
                        sigma_map = _matrix_array(data, "sigma_v")
                        if v50_map is None or sigma_map is None:
                            continue
                        figure, axes = plt.subplots(
                            2, 2, figsize=(10.7, 8.4), layout="constrained"
                        )
                        v50_array, columns, rows = v50_map
                        sigma_array, sigma_columns, sigma_rows = sigma_map
                        image_v50 = axes[0, 0].imshow(
                            v50_array,
                            origin="lower",
                            aspect="auto",
                            interpolation="nearest",
                            extent=(columns[0] - 0.5, columns[-1] + 0.5, rows[0] - 0.5, rows[-1] + 0.5),
                            cmap="viridis",
                        )
                        figure.colorbar(image_v50, ax=axes[0, 0], pad=0.02, label="V50, V")
                        image_sigma = axes[0, 1].imshow(
                            1000 * sigma_array,
                            origin="lower",
                            aspect="auto",
                            interpolation="nearest",
                            extent=(sigma_columns[0] - 0.5, sigma_columns[-1] + 0.5, sigma_rows[0] - 0.5, sigma_rows[-1] + 0.5),
                            cmap="magma",
                        )
                        figure.colorbar(image_sigma, ax=axes[0, 1], pad=0.02, label="Sigma, mV")
                        axes[1, 0].hist(
                            pd.to_numeric(data["v50_v"], errors="coerce").dropna(),
                            bins="auto",
                            color="#4c78a8",
                            edgecolor="white",
                        )
                        axes[1, 0].set_xlabel("V50, V")
                        axes[1, 0].set_ylabel("Pixel count")
                        axes[1, 1].hist(
                            1000 * pd.to_numeric(data["sigma_v"], errors="coerce").dropna(),
                            bins="auto",
                            color="#e45756",
                            edgecolor="white",
                        )
                        axes[1, 1].set_xlabel("S-curve width, mV")
                        axes[1, 1].set_ylabel("Pixel count")
                        for axis in axes[0, :]:
                            axis.set_xlabel("Physical column")
                            axis.set_ylabel("Physical row")
                        label = _amplitude_label(data)
                        figure.suptitle(f"S-curve parameters: {label}, pattern {pattern}")
                        stem = (
                            f"scurve_parameters_{_safe_stem(pattern)}_"
                            f"amplitude_{amplitude_index:03d}"
                        )
                        outputs[stem] = _save_figure(
                            figure, plot_directory, stem, settings
                        )

    if not scurve_amplitude_summary.empty:
        summary = scurve_amplitude_summary.copy()
        actual_step = _numeric_series(summary, "injection_voltage_step_v")
        requested_step = _numeric_series(
            summary, "requested_injection_voltage_step_v"
        )
        if actual_step.notna().any():
            figure, axes = plt.subplots(1, 3, figsize=(14.2, 4.2))
            pair_table = (
                summary.assign(actual_step=actual_step, requested_step=requested_step)
                .dropna(subset=["actual_step"])
                .drop_duplicates("pulse_amplitude_native")
                .sort_values("actual_step")
            )
            if not pair_table.empty:
                x = 1000 * pair_table["requested_step"].fillna(pair_table["actual_step"])
                y = 1000 * pair_table["actual_step"]
                axes[0].plot(x, y, marker="o", linewidth=1.0)
                low = float(min(x.min(), y.min()))
                high = float(max(x.max(), y.max()))
                axes[0].plot([low, high], [low, high], color="black", linestyle="--", linewidth=1)
                axes[0].set_xlabel("Requested REF1-REF2 step, mV")
                axes[0].set_ylabel("Selected measured step, mV")
                axes[1].bar(
                    np.arange(len(pair_table)),
                    1e6 * (pair_table["actual_step"] - pair_table["requested_step"]),
                    color="#4c78a8",
                )
                axes[1].axhline(0, color="black", linewidth=0.8)
                axes[1].set_xlabel("Amplitude index")
                axes[1].set_ylabel("Selection error, uV")
                common_mode = _numeric_series(
                    pair_table, "reference_common_mode_v"
                )
                axes[2].plot(
                    x,
                    common_mode,
                    marker="o",
                    linewidth=1.0,
                    color="#e45756",
                )
                if common_mode.notna().any():
                    axes[2].axhline(
                        float(common_mode.median()),
                        color="black",
                        linestyle="--",
                        linewidth=0.9,
                    )
                    span_mv = 1000.0 * float(
                        common_mode.max() - common_mode.min()
                    )
                    axes[2].set_title(f"Common-mode span = {span_mv:.3f} mV")
                axes[2].set_xlabel("Requested REF1-REF2 step, mV")
                axes[2].set_ylabel("(VREF1 + VREF2) / 2, V")
                figure.suptitle("REF LUT pair-selection accuracy and common mode")
                outputs["reference_pair_selection"] = _save_figure(
                    figure, plot_directory, "reference_pair_selection", settings
                )
            else:
                plt.close(figure)

        for pattern, data in summary.groupby("injection_pattern", sort=True):
            charge = _numeric_series(data, "injection_charge_electrons")
            step = _numeric_series(data, "injection_voltage_step_v")
            if charge.notna().all() and charge.nunique() > 1:
                x = charge.to_numpy(dtype=float) / 1000.0
                x_label = "Nominal injected charge, ke"
            elif step.notna().all() and step.nunique() > 1:
                x = 1000 * step.to_numpy(dtype=float)
                x_label = "Measured REF1-REF2 step, mV"
            else:
                continue
            order = np.argsort(x)
            x = x[order]
            data = data.iloc[order]
            figure, axes = plt.subplots(1, 2, figsize=(10.9, 4.3))
            axes[0].plot(x, data["v50_median_v"], marker="o")
            axes[0].fill_between(
                x,
                pd.to_numeric(data["v50_q10_v"], errors="coerce").to_numpy(dtype=float),
                pd.to_numeric(data["v50_q90_v"], errors="coerce").to_numpy(dtype=float),
                alpha=0.22,
                label="10-90% pixels",
            )
            axes[0].set_ylabel("V50, V")
            axes[0].legend()
            axes[1].plot(
                x,
                1000 * pd.to_numeric(data["sigma_median_v"], errors="coerce"),
                marker="o",
                color="#d95f02",
            )
            axes[1].fill_between(
                x,
                1000 * pd.to_numeric(data["sigma_q10_v"], errors="coerce").to_numpy(dtype=float),
                1000 * pd.to_numeric(data["sigma_q90_v"], errors="coerce").to_numpy(dtype=float),
                alpha=0.22,
                color="#d95f02",
            )
            axes[1].set_ylabel("S-curve width, mV")
            for axis in axes:
                axis.set_xlabel(x_label)
            figure.suptitle(f"Matrix amplitude response, pattern {pattern}")
            stem = f"matrix_amplitude_response_{_safe_stem(pattern)}"
            outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    if not scurve_gain_results.empty:
        for pattern, data in scurve_gain_results.groupby("injection_pattern", sort=True):
            gain = pd.to_numeric(data["nominal_gain_mv_per_ke"], errors="coerce")
            usable = data[gain.notna()].copy()
            if usable.empty:
                continue
            gain_map = _matrix_array(usable, "nominal_gain_mv_per_ke")
            if gain_map is None:
                continue
            array, columns, rows = gain_map
            figure, axes = plt.subplots(1, 2, figsize=(10.6, 4.5))
            image = axes[0].imshow(
                array,
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=(columns[0] - 0.5, columns[-1] + 0.5, rows[0] - 0.5, rows[-1] + 0.5),
                cmap="viridis",
            )
            figure.colorbar(image, ax=axes[0], pad=0.02, label="Nominal gain, mV/ke")
            axes[0].set_xlabel("Physical column")
            axes[0].set_ylabel("Physical row")
            axes[1].hist(
                pd.to_numeric(usable["nominal_gain_mv_per_ke"], errors="coerce").dropna(),
                bins="auto",
                color="#59a14f",
                edgecolor="white",
            )
            axes[1].set_xlabel("Nominal gain, mV/ke")
            axes[1].set_ylabel("Pixel count")
            figure.suptitle(
                f"Per-pixel nominal charge response, pattern {pattern}"
            )
            stem = f"nominal_gain_{_safe_stem(pattern)}"
            outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    if not scurve_results.empty:
        usable_results = scurve_results[
            scurve_results["fit_status"].isin(("ok", "poor_quality"))
        ].copy()
        if not usable_results.empty:
            coordinates = _plot_coordinates(settings, usable_results, "v50_v")
            for pattern, pattern_data in usable_results.groupby(
                "injection_pattern", sort=True
            ):
                for column, row in coordinates:
                    pixel = pattern_data[
                        (pattern_data["column"] == column)
                        & (pattern_data["row"] == row)
                    ].copy()
                    if len(pixel) < 2:
                        continue
                    charge = _numeric_series(pixel, "injection_charge_electrons")
                    step = _numeric_series(pixel, "injection_voltage_step_v")
                    if charge.notna().all() and charge.nunique() > 1:
                        x = charge.to_numpy(dtype=float) / 1000.0
                        x_label = "Nominal injected charge, ke"
                    elif step.notna().all() and step.nunique() > 1:
                        x = 1000 * step.to_numpy(dtype=float)
                        x_label = "Measured REF1-REF2 step, mV"
                    else:
                        continue
                    order = np.argsort(x)
                    figure, axis = plt.subplots(figsize=(6.5, 4.5))
                    axis.errorbar(
                        x[order],
                        pd.to_numeric(pixel["v50_v"], errors="coerce").to_numpy(dtype=float)[order],
                        yerr=_numeric_series(
                            pixel, "v50_uncertainty_v"
                        ).to_numpy(dtype=float)[order],
                        marker="o",
                        capsize=2,
                    )
                    axis.set_xlabel(x_label)
                    axis.set_ylabel("V50, V")
                    axis.set_title(
                        f"Pixel C{column:02d} R{row:02d}: amplitude response, {pattern}"
                    )
                    stem = (
                        f"pixel_C{column:02d}_R{row:02d}_"
                        f"amplitude_response_{_safe_stem(pattern)}"
                    )
                    outputs[stem] = _save_figure(
                        figure, plot_directory, stem, settings
                    )

        r2 = _finite_values(scurve_results, "fit_r2")
        if len(r2):
            figure, axis = plt.subplots(figsize=(7.2, 4.5))
            axis.hist(r2, bins=30, color="#ef5675", edgecolor="white")
            axis.axvline(0.8, color="black", linestyle="--")
            axis.set_xlabel("S-curve fit $R^2$")
            axis.set_ylabel("Fit count")
            axis.set_title("S-curve fit-quality distribution")
            outputs["scurve_fit_quality"] = _save_figure(
                figure, plot_directory, "scurve_fit_quality", settings
            )

    if not crosstalk_pixel_metrics.empty:
        pixel_metrics = crosstalk_pixel_metrics.copy()
        pixel_metrics["delta_v50_mv"] = 1000 * pd.to_numeric(
            pixel_metrics["delta_v50_v"], errors="coerce"
        )
        pixel_metrics["sigma_fractional_change"] = pd.to_numeric(
            pixel_metrics["sigma_ratio_to_reference"], errors="coerce"
        ) - 1.0
        metric_groups = pixel_metrics.groupby(
            ["pulse_amplitude_native", "injection_pattern"],
            dropna=False,
            sort=True,
        )
        for metric_index, ((amplitude, pattern), data) in enumerate(metric_groups):
            reference_patterns = set(data["reference_injection_pattern"].dropna().astype(str))
            if str(pattern) in reference_patterns:
                continue
            delta_map = _matrix_array(data, "delta_v50_mv")
            sigma_map = _matrix_array(data, "sigma_fractional_change")
            if delta_map is None or sigma_map is None:
                continue
            delta_array, columns, rows = delta_map
            sigma_array, sigma_columns, sigma_rows = sigma_map
            figure, axes = plt.subplots(
                2, 2, figsize=(10.8, 8.4), layout="constrained"
            )
            delta_limit = (
                float(np.nanmax(np.abs(delta_array)))
                if np.any(np.isfinite(delta_array))
                else 1.0
            )
            sigma_limit = (
                float(np.nanmax(np.abs(sigma_array)))
                if np.any(np.isfinite(sigma_array))
                else 0.1
            )
            if delta_limit == 0:
                delta_limit = 1e-6
            if sigma_limit == 0:
                sigma_limit = 1e-6
            image_delta = axes[0, 0].imshow(
                delta_array,
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=(columns[0] - 0.5, columns[-1] + 0.5, rows[0] - 0.5, rows[-1] + 0.5),
                cmap="coolwarm",
                vmin=-delta_limit,
                vmax=delta_limit,
            )
            figure.colorbar(image_delta, ax=axes[0, 0], pad=0.02, label="Delta V50, mV")
            image_sigma = axes[0, 1].imshow(
                sigma_array,
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=(sigma_columns[0] - 0.5, sigma_columns[-1] + 0.5, sigma_rows[0] - 0.5, sigma_rows[-1] + 0.5),
                cmap="coolwarm",
                vmin=-sigma_limit,
                vmax=sigma_limit,
            )
            figure.colorbar(
                image_sigma,
                ax=axes[0, 1],
                pad=0.02,
                label="Sigma / reference sigma - 1",
            )
            axes[1, 0].hist(
                _finite_values(data, "delta_v50_mv"),
                bins="auto",
                color="#4c78a8",
                edgecolor="white",
            )
            axes[1, 0].set_xlabel("Delta V50, mV")
            axes[1, 0].set_ylabel("Pixel count")
            axes[1, 1].hist(
                _finite_values(data, "sigma_fractional_change"),
                bins="auto",
                color="#f28e2b",
                edgecolor="white",
            )
            axes[1, 1].set_xlabel("Sigma / reference sigma - 1")
            axes[1, 1].set_ylabel("Pixel count")
            for axis in axes[0, :]:
                axis.set_xlabel("Physical column")
                axis.set_ylabel("Physical row")
            figure.suptitle(
                f"Injection-density shift: {_amplitude_label(data)}, pattern {pattern}"
            )
            stem = (
                f"crosstalk_pixel_maps_{_safe_stem(pattern)}_"
                f"amplitude_{metric_index:03d}"
            )
            outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    if not crosstalk_summary.empty and crosstalk_summary["injection_pattern"].nunique() > 1:
        amplitudes = list(
            crosstalk_summary["pulse_amplitude_native"].drop_duplicates()
        )
        for amplitude_index, amplitude in enumerate(amplitudes):
            data = crosstalk_summary[
                crosstalk_summary["pulse_amplitude_native"] == amplitude
            ].copy()
            data = data.sort_values("median_active_pixels_per_shot")
            x = pd.to_numeric(
                data["median_active_pixels_per_shot"], errors="coerce"
            ).to_numpy(dtype=float)
            positive_x = x[np.isfinite(x) & (x > 0)]
            if not len(positive_x):
                continue
            figure, axes = plt.subplots(1, 3, figsize=(13.2, 4.2))
            axes[0].plot(
                x,
                1000
                * pd.to_numeric(
                    data["median_abs_delta_v50_v_to_reference"], errors="coerce"
                ),
                marker="o",
            )
            axes[0].set_ylabel("Median |delta V50|, mV")
            axes[1].plot(
                x,
                pd.to_numeric(
                    data["median_sigma_ratio_to_reference"], errors="coerce"
                ),
                marker="o",
            )
            axes[1].axhline(1.0, color="black", linestyle="--", linewidth=1)
            axes[1].set_ylabel("Median sigma / reference sigma")
            axes[2].plot(
                x,
                pd.to_numeric(
                    data["inactive_excess_hit_fraction_p95"], errors="coerce"
                ),
                marker="o",
            )
            axes[2].set_ylabel("Inactive excess hit fraction, p95")
            for axis in axes:
                if len(np.unique(positive_x)) > 1:
                    axis.set_xscale("log", base=2)
                    axis.set_xlim(
                        positive_x.min() / 1.2, positive_x.max() * 1.2
                    )
                else:
                    center = float(positive_x[0])
                    axis.set_xlim(center * 0.8, center * 1.2)
                axis.set_xlabel("Active pixels per shot")
            figure.suptitle(
                f"Injection-density coupling diagnostics, amplitude {amplitude}"
            )
            suffix = f"_{amplitude_index:03d}" if len(amplitudes) > 1 else ""
            outputs[f"injection_crosstalk{suffix}"] = _save_figure(
                figure,
                plot_directory,
                f"injection_crosstalk_metrics{suffix}",
                settings,
            )

    return outputs
