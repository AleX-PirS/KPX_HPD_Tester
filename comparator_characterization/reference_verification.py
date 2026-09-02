"""Oscilloscope verification of calibrated REF1/REF2 test steps.

The verifier never changes OMR, polarity, ICR or DCR. It uses the existing
KIPIX ``Configuration`` interface, routes ``TST_SIG`` to AMUX, creates CTRL PWM
through MGPDLab/UPO and captures CH1/CH4 on the oscilloscope. Every raw waveform
is retained before any derived step metric is calculated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import time
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from configuration import Configuration
from mgpd import MGPDClient

from .calibration import ReferencePairSelection
from .storage import atomic_write_json, atomic_write_table, utc_now_text


@dataclass(frozen=True)
class ReferenceStepVerificationSettings:
    """Fully explicit oscilloscope and acceptance settings for REF verification."""

    enabled: bool = True
    signal_channel: int = 1
    trigger_channel: int = 4
    trigger_level_v: float = 0.5
    trigger_slope: str = "NEG"
    time_scale_s: float = 5e-7
    time_offset_s: float = 0.0
    waveform_points: int = 12_500
    averaging_enabled: bool = False
    average_count: int = 1
    signal_scale_v: float = 0.2
    signal_offset_v: float = 0.4
    trigger_scale_v: float = 0.5
    trigger_offset_v: float = 1.5
    scope_arm_delay_s: float = 0.10
    acquisition_time_s: float = 0.25
    clock_settling_time_s: float = 0.05
    plateau_guard_s: float = 1.5e-7
    plateau_window_s: float = 1.0e-6
    maximum_scope_step_error_v: float = 1e-3
    clock_on_frequency_mhz: int = 50
    capture_clk_off: bool = True
    capture_clk_on: bool = True
    acquisition_retries: int = 2
    retry_backoff_s: float = 0.25
    abort_on_failure: bool = True
    save_screenshots: bool = False

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("reference verification enabled must be bool")
        for name in ("signal_channel", "trigger_channel"):
            value = getattr(self, name)
            if value not in (1, 2, 3, 4):
                raise ValueError(f"{name} must be 1, 2, 3 or 4")
        if self.signal_channel == self.trigger_channel:
            raise ValueError("signal_channel and trigger_channel must differ")
        if str(self.trigger_slope).strip().upper() != "NEG":
            raise ValueError("REF verification requires NEG trigger slope")
        for name in (
            "time_scale_s",
            "signal_scale_v",
            "trigger_scale_v",
            "plateau_guard_s",
            "plateau_window_s",
            "maximum_scope_step_error_v",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in (
            "trigger_level_v",
            "time_offset_s",
            "signal_offset_v",
            "trigger_offset_v",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        for name in (
            "scope_arm_delay_s",
            "acquisition_time_s",
            "clock_settling_time_s",
            "retry_backoff_s",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        if not isinstance(self.waveform_points, int) or self.waveform_points < 100:
            raise ValueError("waveform_points must be an integer >= 100")
        if not isinstance(self.average_count, int) or self.average_count < 1:
            raise ValueError("average_count must be a positive integer")
        if not isinstance(self.acquisition_retries, int) or self.acquisition_retries < 0:
            raise ValueError("acquisition_retries must be an integer >= 0")
        if self.clock_on_frequency_mhz not in MGPDClient.FCLK_ALLOWED_MHZ or (
            self.clock_on_frequency_mhz == 0
        ):
            raise ValueError("clock_on_frequency_mhz must be a supported non-zero FCLK")
        if not self.capture_clk_off or not self.capture_clk_on:
            raise ValueError("both CLK OFF and CLK ON captures are required")


@dataclass(frozen=True)
class ReferenceStepVerificationResult:
    output_directory: Path
    capture_table: Path
    comparison_table: Path
    result_json: Path
    passed: bool
    capture_count: int
    failed_capture_count: int


class ReferenceStepVerificationError(RuntimeError):
    """Raised after verification artifacts were saved and acceptance failed."""

    def __init__(
        self,
        message: str,
        *,
        result: ReferenceStepVerificationResult | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result


def _run_directory(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    candidate = parent / f"run_{stamp}"
    candidate.mkdir()
    return candidate


def _status(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _capture_waveforms(
    *,
    client: MGPDClient,
    oscilloscope: Any,
    settings: ReferenceStepVerificationSettings,
    pwm_frequency_khz: int,
    pwm_high_time_ns: int,
) -> tuple[pd.DataFrame, dict[str, float]]:
    signal_channel = settings.signal_channel
    trigger_channel = settings.trigger_channel
    last_error: BaseException | None = None
    for attempt in range(settings.acquisition_retries + 1):
        try:
            if not client.set_ctrl(0):
                raise RuntimeError("MGPDLab rejected CTRL=0 before scope capture")
            oscilloscope.run_acquisition()
            time.sleep(settings.scope_arm_delay_s)
            if not client.set_ctrl_pwm(pwm_frequency_khz, pwm_high_time_ns):
                raise RuntimeError("MGPDLab rejected CTRL PWM for scope capture")
            time.sleep(settings.acquisition_time_s)
            oscilloscope.stop_acquisition()
            if not client.set_ctrl(0):
                raise RuntimeError("MGPDLab rejected CTRL=0 after scope capture")
            signal, signal_origin, signal_increment = oscilloscope.read_waveform(
                signal_channel
            )
            trigger, trigger_origin, trigger_increment = oscilloscope.read_waveform(
                trigger_channel
            )
            signal_array = np.asarray(signal, dtype=float)
            trigger_array = np.asarray(trigger, dtype=float)
            if len(signal_array) < 20 or len(trigger_array) < 20:
                raise RuntimeError("oscilloscope returned too few waveform points")
            if not np.all(np.isfinite(signal_array)) or not np.all(
                np.isfinite(trigger_array)
            ):
                raise RuntimeError("oscilloscope waveform contains NaN or infinity")
            count = min(len(signal_array), len(trigger_array))
            signal_time = signal_origin + np.arange(count) * signal_increment
            trigger_time = trigger_origin + np.arange(count) * trigger_increment
            frame = pd.DataFrame(
                {
                    "signal_time_s": signal_time,
                    f"channel_{signal_channel}_v": signal_array[:count],
                    "trigger_time_s": trigger_time,
                    f"channel_{trigger_channel}_v": trigger_array[:count],
                }
            )
            timing = {
                "signal_x_origin_s": float(signal_origin),
                "signal_x_increment_s": float(signal_increment),
                "trigger_x_origin_s": float(trigger_origin),
                "trigger_x_increment_s": float(trigger_increment),
                "saved_point_count": int(count),
                "capture_attempt": int(attempt + 1),
            }
            return frame, timing
        except BaseException as error:
            last_error = error
            try:
                oscilloscope.stop_acquisition()
            except Exception:
                pass
            try:
                client.set_ctrl(0)
            except Exception:
                pass
            if attempt >= settings.acquisition_retries:
                break
            time.sleep(settings.retry_backoff_s * (2**attempt))
    raise RuntimeError("oscilloscope REF-step capture failed after retries") from last_error


def _measure_step(
    frame: pd.DataFrame,
    settings: ReferenceStepVerificationSettings,
) -> dict[str, Any]:
    signal_name = f"channel_{settings.signal_channel}_v"
    trigger_name = f"channel_{settings.trigger_channel}_v"
    trigger = frame[trigger_name].to_numpy(dtype=float)
    trigger_time = frame["trigger_time_s"].to_numpy(dtype=float)
    crossings = np.flatnonzero(
        (trigger[:-1] >= settings.trigger_level_v)
        & (trigger[1:] < settings.trigger_level_v)
    )
    if not len(crossings):
        raise RuntimeError(
            f"CH{settings.trigger_channel} has no falling crossing of "
            f"{settings.trigger_level_v:g} V"
        )
    crossing_index = int(
        crossings[np.argmin(np.abs(trigger_time[crossings] - settings.time_offset_s))]
    )
    edge_time = float(trigger_time[crossing_index + 1])
    signal_time = frame["signal_time_s"].to_numpy(dtype=float)
    signal = frame[signal_name].to_numpy(dtype=float)
    guard = settings.plateau_guard_s
    window = settings.plateau_window_s
    pre_mask = (signal_time >= edge_time - guard - window) & (
        signal_time <= edge_time - guard
    )
    post_mask = (signal_time >= edge_time + guard) & (
        signal_time <= edge_time + guard + window
    )
    minimum_samples = 10
    if int(pre_mask.sum()) < minimum_samples or int(post_mask.sum()) < minimum_samples:
        raise RuntimeError(
            "not enough CH1 samples in pre/post plateau windows; adjust timebase, "
            "plateau_guard_s or plateau_window_s"
        )
    pre = signal[pre_mask]
    post = signal[post_mask]
    pre_level = float(np.median(pre))
    post_level = float(np.median(post))
    signed_step = post_level - pre_level
    absolute_step = abs(signed_step)
    pre_mad = float(np.median(np.abs(pre - pre_level)))
    post_mad = float(np.median(np.abs(post - post_level)))
    pre_std = float(np.std(pre, ddof=1)) if len(pre) > 1 else 0.0
    post_std = float(np.std(post, ddof=1)) if len(post) > 1 else 0.0
    return {
        "trigger_crossing_time_s": edge_time,
        "pre_plateau_start_s": float(edge_time - guard - window),
        "pre_plateau_stop_s": float(edge_time - guard),
        "post_plateau_start_s": float(edge_time + guard),
        "post_plateau_stop_s": float(edge_time + guard + window),
        "pre_plateau_sample_count": int(len(pre)),
        "post_plateau_sample_count": int(len(post)),
        "pre_plateau_median_v": pre_level,
        "post_plateau_median_v": post_level,
        "signed_post_minus_pre_step_v": signed_step,
        "measured_absolute_step_v": absolute_step,
        "observed_step_direction": "rising" if signed_step > 0 else "falling",
        "pre_plateau_mad_v": pre_mad,
        "post_plateau_mad_v": post_mad,
        "pre_plateau_std_v": pre_std,
        "post_plateau_std_v": post_std,
        "combined_plateau_rms_noise_v": math.sqrt(
            0.5 * (pre_std**2 + post_std**2)
        ),
    }


def _comparison_table(captures: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for amplitude_index, data in captures.groupby("amplitude_index", sort=True):
        by_clock = {
            str(row["clock_state"]): row for _, row in data.iterrows()
        }
        off = by_clock.get("off")
        on = by_clock.get("on")
        if off is None or on is None:
            continue
        rows.append(
            {
                "amplitude_index": int(amplitude_index),
                "requested_voltage_step_v": float(off["requested_voltage_step_v"]),
                "selected_lut_voltage_step_v": float(off["selected_lut_voltage_step_v"]),
                "ref1_code": int(off["ref1_code"]),
                "ref2_code": int(off["ref2_code"]),
                "ref1_voltage_v": float(off["ref1_voltage_v"]),
                "ref2_voltage_v": float(off["ref2_voltage_v"]),
                "measured_step_clk_off_v": float(off["measured_absolute_step_v"]),
                "measured_step_clk_on_v": float(on["measured_absolute_step_v"]),
                "clock_induced_step_shift_v": float(
                    on["measured_absolute_step_v"] - off["measured_absolute_step_v"]
                ),
                "clock_induced_absolute_step_shift_v": abs(
                    float(on["measured_absolute_step_v"] - off["measured_absolute_step_v"])
                ),
                "clock_induced_pre_plateau_shift_v": float(
                    on["pre_plateau_median_v"] - off["pre_plateau_median_v"]
                ),
                "clock_induced_post_plateau_shift_v": float(
                    on["post_plateau_median_v"] - off["post_plateau_median_v"]
                ),
                "clock_induced_signed_step_shift_v": float(
                    on["signed_post_minus_pre_step_v"]
                    - off["signed_post_minus_pre_step_v"]
                ),
                "plateau_rms_noise_clk_off_v": float(
                    off["combined_plateau_rms_noise_v"]
                ),
                "plateau_rms_noise_clk_on_v": float(
                    on["combined_plateau_rms_noise_v"]
                ),
                "clock_induced_rms_noise_change_v": float(
                    on["combined_plateau_rms_noise_v"]
                    - off["combined_plateau_rms_noise_v"]
                ),
                "clk_off_pass": bool(off["capture_pass"]),
                "clk_on_pass": bool(on["capture_pass"]),
                "pair_pass": bool(off["capture_pass"] and on["capture_pass"]),
            }
        )
    return pd.DataFrame(rows)


def _save_summary_plot(comparison: pd.DataFrame, output: Path) -> Path | None:
    if comparison.empty:
        return None
    import matplotlib.pyplot as plt
    requested = 1000 * comparison["requested_voltage_step_v"].to_numpy(dtype=float)
    figure, axes = plt.subplots(1, 2, figsize=(10.8, 4.4), layout="constrained")
    axes[0].plot(
        requested,
        1000 * comparison["selected_lut_voltage_step_v"],
        marker="o",
        label="LUT",
    )
    axes[0].plot(
        requested,
        1000 * comparison["measured_step_clk_off_v"],
        marker="s",
        label="CLK OFF",
    )
    axes[0].plot(
        requested,
        1000 * comparison["measured_step_clk_on_v"],
        marker="^",
        label="CLK ON",
    )
    low = float(requested.min())
    high = float(requested.max())
    axes[0].plot([low, high], [low, high], color="black", linestyle="--", linewidth=0.8)
    axes[0].set_xlabel("Requested step, mV")
    axes[0].set_ylabel("Selected/measured step, mV")
    axes[0].set_title("REF-step verification")
    axes[0].legend()
    axes[1].axhline(0.0, color="black", linewidth=0.8)
    axes[1].plot(
        requested,
        1000 * comparison["clock_induced_step_shift_v"],
        marker="o",
        color="#d62728",
    )
    axes[1].set_xlabel("Requested step, mV")
    axes[1].set_ylabel("CLK ON - CLK OFF step, mV")
    axes[1].set_title("Clock influence on measured step")
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=200, bbox_inches="tight")
    plt.close(figure)
    return output


def verify_reference_steps(
    *,
    client: MGPDClient,
    configuration: Configuration,
    oscilloscope: Any,
    selections: Sequence[ReferencePairSelection],
    output_parent: str | Path,
    settings: ReferenceStepVerificationSettings,
    pwm_frequency_khz: int,
    pwm_high_time_ns: int,
    status_callback: Callable[[str], None] | None = None,
) -> ReferenceStepVerificationResult:
    """Capture every selected REF step with FCLK forced OFF and ON.

    The caller must have established the normal ASIC defaults first. This
    function restores TEST_MUX, REF1, REF2, CTRL=0 and the configured non-zero
    FCLK before returning or raising.
    """

    settings.validate()
    if not settings.enabled:
        raise ValueError("verify_reference_steps called with settings.enabled=False")
    selections = tuple(selections)
    if not selections:
        raise ValueError("reference verification requires at least one REF pair")
    ref1_codes = {item.ref1_code for item in selections}
    ref1_voltages = {item.ref1_voltage_v for item in selections}
    if len(ref1_codes) != 1 or len(ref1_voltages) != 1:
        raise ValueError("reference verification requires one fixed REF1 for all amplitudes")
    if not isinstance(pwm_frequency_khz, int) or not isinstance(pwm_high_time_ns, int):
        raise TypeError("PWM frequency and high time must be integers")
    # Reuse the protocol validation without entering a shutter sequence.
    from .hardware import UpoPwmSettings

    UpoPwmSettings(
        frequency_khz=pwm_frequency_khz,
        high_time_ns=pwm_high_time_ns,
    ).validate()
    output_directory = _run_directory(Path(output_parent))
    waveform_directory = output_directory / "waveforms"
    waveform_directory.mkdir()
    settings_path = output_directory / "verification_settings.json"
    atomic_write_json(
        settings_path,
        {
            "created_utc": utc_now_text(),
            "settings": asdict(settings),
            "pwm_frequency_khz": pwm_frequency_khz,
            "pwm_high_time_ns": pwm_high_time_ns,
            "amux_signal": "TST_SIG",
            "physical_ref_order": "V_REF1 > V_REF2",
            "acceptance_uses_step_magnitude": True,
        },
    )

    previous_fields = {
        name: configuration.get_data(name)
        for name in ("TEST_MUX", "DAC_TST_REF1", "DAC_TST_REF2")
    }
    captures: list[dict[str, Any]] = []
    primary_error: BaseException | None = None
    restore_errors: list[str] = []
    clock_states = (("off", 0), ("on", settings.clock_on_frequency_mhz))
    total = len(selections) * len(clock_states)
    completed = 0
    try:
        if not client.set_ctrl(0):
            raise RuntimeError("failed to establish CTRL=0 before REF verification")
        if not client.set_fclk(settings.clock_on_frequency_mhz):
            raise RuntimeError("failed to enable FCLK before REF programming")
        if not configuration.set_amux("TST_SIG"):
            raise RuntimeError("failed to route TST_SIG to analog multiplexer")
        oscilloscope.configure_frame(
            channels=(settings.signal_channel, settings.trigger_channel),
            trigger_enabled=True,
            trigger_source=settings.trigger_channel,
            trigger_level_v=settings.trigger_level_v,
            trigger_slope=settings.trigger_slope,
            average_count=settings.average_count,
            time_scale_s=settings.time_scale_s,
            time_offset_s=settings.time_offset_s,
            input_modes={
                settings.signal_channel: "DC",
                settings.trigger_channel: "DC",
            },
            waveform_points=settings.waveform_points,
            run_after_config=False,
            averaging_enabled=settings.averaging_enabled,
            voltage_scale_dict={
                settings.signal_channel: settings.signal_scale_v,
                settings.trigger_channel: settings.trigger_scale_v,
            },
            voltage_offset_dict={
                settings.signal_channel: settings.signal_offset_v,
                settings.trigger_channel: settings.trigger_offset_v,
            },
        )
        _status(
            status_callback,
            "Проверка REF: TST_SIG выведен на AMUX, осциллограф CH1/CH4 настроен",
        )

        for amplitude_index, selection in enumerate(selections):
            if not client.set_fclk(settings.clock_on_frequency_mhz):
                raise RuntimeError("failed to enable FCLK before REF update")
            for name, value in (
                ("DAC_TST_REF1", selection.ref1_code),
                ("DAC_TST_REF2", selection.ref2_code),
            ):
                if not configuration.set_data(name, value):
                    raise RuntimeError(f"failed to program {name}={value}")
            for clock_state, clock_frequency_mhz in clock_states:
                if not client.set_fclk(clock_frequency_mhz):
                    raise RuntimeError(
                        f"failed to set FCLK={clock_frequency_mhz} MHz for REF verification"
                    )
                time.sleep(settings.clock_settling_time_s)
                _status(
                    status_callback,
                    f"Проверка REF {completed + 1}/{total}: "
                    f"{1000 * selection.requested_voltage_step_v:g} мВ, "
                    f"CLK {clock_state.upper()}",
                )
                waveform, timing = _capture_waveforms(
                    client=client,
                    oscilloscope=oscilloscope,
                    settings=settings,
                    pwm_frequency_khz=pwm_frequency_khz,
                    pwm_high_time_ns=pwm_high_time_ns,
                )
                metrics = _measure_step(waveform, settings)
                waveform_name = (
                    f"amplitude_{amplitude_index:03d}_"
                    f"requested_{1000 * selection.requested_voltage_step_v:.3f}mV_"
                    f"clk_{clock_state}.csv"
                )
                waveform_path = atomic_write_table(
                    waveform_directory / waveform_name, waveform
                )
                screenshot_relative = None
                if settings.save_screenshots:
                    screenshot = oscilloscope.save_screenshot(
                        waveform_path.with_suffix(".png")
                    )
                    screenshot_relative = Path(screenshot).relative_to(
                        output_directory
                    ).as_posix()
                scope_lut_error = abs(
                    metrics["measured_absolute_step_v"]
                    - selection.actual_voltage_step_v
                )
                scope_requested_error = abs(
                    metrics["measured_absolute_step_v"]
                    - selection.requested_voltage_step_v
                )
                capture_pass = bool(
                    selection.absolute_voltage_step_error_v
                    <= settings.maximum_scope_step_error_v
                    and scope_lut_error <= settings.maximum_scope_step_error_v
                    and scope_requested_error <= settings.maximum_scope_step_error_v
                )
                captures.append(
                    {
                        "timestamp_utc": utc_now_text(),
                        "amplitude_index": amplitude_index,
                        "clock_state": clock_state,
                        "fclk_mhz": clock_frequency_mhz,
                        "requested_voltage_step_v": selection.requested_voltage_step_v,
                        "selected_lut_voltage_step_v": selection.actual_voltage_step_v,
                        "lut_step_error_v": selection.voltage_step_error_v,
                        "ref1_code": selection.ref1_code,
                        "ref2_code": selection.ref2_code,
                        "ref1_voltage_v": selection.ref1_voltage_v,
                        "ref2_voltage_v": selection.ref2_voltage_v,
                        "ref1_above_ref2": selection.ref1_voltage_v
                        > selection.ref2_voltage_v,
                        **metrics,
                        "scope_to_lut_absolute_error_v": scope_lut_error,
                        "scope_to_requested_absolute_error_v": scope_requested_error,
                        "maximum_allowed_error_v": settings.maximum_scope_step_error_v,
                        "capture_pass": capture_pass,
                        "waveform_csv": waveform_path.relative_to(
                            output_directory
                        ).as_posix(),
                        "screenshot_png": screenshot_relative,
                        **timing,
                    }
                )
                completed += 1
    except BaseException as error:
        primary_error = error
    finally:
        try:
            if not client.set_ctrl(0):
                raise RuntimeError("MGPDLab rejected final CTRL=0")
        except BaseException as error:
            restore_errors.append(f"CTRL restore: {error}")
        try:
            if not client.set_fclk(settings.clock_on_frequency_mhz):
                raise RuntimeError("MGPDLab rejected final non-zero FCLK")
            for name in ("DAC_TST_REF1", "DAC_TST_REF2", "TEST_MUX"):
                if not configuration.set_data(name, previous_fields[name]):
                    raise RuntimeError(f"failed to restore {name}")
        except BaseException as error:
            restore_errors.append(f"KIPIX restore: {error}")

    capture_table = atomic_write_table(
        output_directory / "capture_metrics.csv", pd.DataFrame(captures)
    )
    comparison = _comparison_table(pd.DataFrame(captures))
    comparison_table = atomic_write_table(
        output_directory / "clk_comparison.csv", comparison
    )
    plot_path = _save_summary_plot(
        comparison, output_directory / "reference_step_verification.png"
    )
    failed_count = sum(not bool(row.get("capture_pass")) for row in captures)
    passed = bool(
        primary_error is None
        and not restore_errors
        and len(captures) == total
        and failed_count == 0
    )
    result_json = output_directory / "verification_result.json"
    atomic_write_json(
        result_json,
        {
            "completed_utc": utc_now_text(),
            "status": "passed" if passed else "failed",
            "passed": passed,
            "expected_capture_count": total,
            "completed_capture_count": len(captures),
            "failed_acceptance_count": failed_count,
            "primary_error": (
                None
                if primary_error is None
                else f"{type(primary_error).__name__}: {primary_error}"
            ),
            "restore_errors": restore_errors,
            "capture_metrics_csv": capture_table.name,
            "clk_comparison_csv": comparison_table.name,
            "summary_plot_png": plot_path.name if plot_path is not None else None,
            "fixed_ref1_code": selections[0].ref1_code,
            "fixed_ref1_voltage_v": selections[0].ref1_voltage_v,
        },
    )
    result = ReferenceStepVerificationResult(
        output_directory=output_directory,
        capture_table=capture_table,
        comparison_table=comparison_table,
        result_json=result_json,
        passed=passed,
        capture_count=len(captures),
        failed_capture_count=failed_count,
    )
    if primary_error is not None:
        raise ReferenceStepVerificationError(
            f"REF-step oscilloscope verification failed; data saved in {output_directory}",
            result=result,
        ) from primary_error
    if restore_errors:
        raise ReferenceStepVerificationError(
            "REF-step verification cleanup failed: " + "; ".join(restore_errors),
            result=result,
        )
    if not passed and settings.abort_on_failure:
        raise ReferenceStepVerificationError(
            f"{failed_count} oscilloscope REF captures exceed the allowed error; "
            f"data saved in {output_directory}",
            result=result,
        )
    return result
