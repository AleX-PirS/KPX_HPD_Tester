from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import threading
import time
from typing import Any, Protocol

import EO_cfg
from configuration import Configuration
from lfsr_decoder import LFSRDecoder
from matrix_config_io import load_matrix_config
from mgpd import MGPDClient
from pixel_matrix import (
    MATRIX_COLS, MATRIX_ROWS, OWNED_COLUMNS, PIXEL_CODEC, PixelMatrixConfiguration,
)

from .models import NoiseScanSettings, WindowSpec
from .pixel_masks import (
    BadPixelMapInput, enforce_disabled_pixel, noise_baseline_pixel_word,
    normalize_bad_pixel_map,
)


logger = logging.getLogger(__name__)


def _validate_blocking_shot_elapsed(request: ShotRequest, elapsed_s: float) -> None:
    """Fail closed if UPO returns implausibly before the declared exposure."""

    expected = request.shutter_duration_s
    if expected is None or expected < 0.05:
        return
    # GET_SHOT also performs configuration and matrix readout, so its wall time
    # should not be shorter than the manually configured exposure. A 10% margin
    # avoids turning timer granularity into a false hardware failure.
    if elapsed_s < 0.90 * expected:
        raise RuntimeError(
            "GET_SHOT returned implausibly early: "
            f"elapsed={elapsed_s:.6g} s, declared UPO exposure={expected:.6g} s; "
            "GET_PIXEL is blocked"
        )


@dataclass(frozen=True)
class ShotRequest:
    """One complete counter acquisition requested from the stand."""

    measurement_kind: str
    acquisition_type: str
    shutter_duration_s: float | None
    test_pulses: bool = False
    n_injections: int | None = None
    pulse_amplitude: Any = None
    configure_get_shot_omr: bool = False
    counter_mode_bits: int = 16
    mode_read: int = 0b010
    crw_mode: int = 0


@dataclass(frozen=True)
class ShotExecutionResult:
    """Pulse-count provenance returned by one shutter sequence."""

    requested_injections: int | None
    programmed_injections: int | None
    actual_injections: int | None
    injections_for_analysis: int | None
    injection_count_source: str
    details: Mapping[str, Any]


class ShotExecutor(Protocol):
    def execute(
        self, client: MGPDClient, request: ShotRequest
    ) -> ShotExecutionResult | int | None:
        """Run reset/shutter/stimulus sequencing and report pulse provenance."""


class MGPDGetShotExecutor:
    """Existing MGPDLab ``GET_SHOT`` path for acquisitions without test pulses.

    The project protocol exposes no independent shutter-open, shutter-close or
    counter-reset command. Consequently ``shutter_duration_s`` is recorded as an
    externally configured MGPDLab setting and is not silently emulated here.
    """

    def execute(self, client: MGPDClient, request: ShotRequest) -> ShotExecutionResult:
        if request.test_pulses:
            raise RuntimeError(
                "The existing project has no API that injects an exact number of test pulses "
                "during a shutter. Supply a verified ShotExecutor for S-curve acquisition."
            )
        started = time.monotonic()
        shot_ok = client.get_shot(
            configure_omr=request.configure_get_shot_omr,
            mode_cnt=0 if request.counter_mode_bits == 16 else 1,
            mode_read=request.mode_read,
            crw_mode=request.crw_mode,
        )
        elapsed = time.monotonic() - started
        if not shot_ok:
            raise RuntimeError("MGPDLab GET_SHOT failed")
        _validate_blocking_shot_elapsed(request, elapsed)
        return ShotExecutionResult(
            requested_injections=request.n_injections,
            programmed_injections=0,
            actual_injections=0,
            injections_for_analysis=0,
            injection_count_source="no_test_pulses",
            details={
                "executor": type(self).__name__,
                "get_shot_elapsed_s": elapsed,
                "get_shot_minimum_elapsed_check": (
                    "at_least_90_percent_of_declared_exposure_when_exposure_at_least_50ms"
                ),
            },
        )


class CallableShotExecutor:
    """Adapter for a bench-specific, physically verified shot callback.

    The callback receives ``(client, ShotRequest)``. For a signal acquisition it
    must return the actual integer number of injected pulses. Returning ``None``
    is allowed only for acquisitions without test pulses.
    """

    def __init__(self, callback: Callable[[MGPDClient, ShotRequest], int | None]):
        if not callable(callback):
            raise TypeError("shot callback must be callable")
        self.callback = callback

    def execute(
        self, client: MGPDClient, request: ShotRequest
    ) -> ShotExecutionResult | int | None:
        result = self.callback(client, request)
        if isinstance(result, ShotExecutionResult):
            return result
        if request.test_pulses:
            if not isinstance(result, int) or isinstance(result, bool) or result < 0:
                raise RuntimeError(
                    "test-pulse ShotExecutor must return the actual non-negative integer "
                    "number of injected pulses"
                )
        elif result is not None and (
            not isinstance(result, int) or isinstance(result, bool) or result < 0
        ):
            raise RuntimeError("ShotExecutor returned an invalid pulse count")
        return result


@dataclass(frozen=True)
class UpoPwmSettings:
    """Continuous CTRL PWM generated by MGPDLab/UPO during one signal shot.

    The PWM has no finite-cycle counter. The nominal number of falling edges is
    therefore derived from the configured real PWM frequency and the manual UPO
    shutter exposure. The physical phase of PWM relative to shutter is not
    observed, so raw data retain an explicit edge-count uncertainty.
    """

    frequency_khz: int = 100
    high_time_ns: int = 5_000
    edge_count_uncertainty: int = 1
    get_shot_timeout_margin_s: float = 2.0

    def validate(self) -> None:
        if not isinstance(self.frequency_khz, int) or isinstance(
            self.frequency_khz, bool
        ):
            raise TypeError("UPO CTRL frequency_khz must be int")
        if not isinstance(self.high_time_ns, int) or isinstance(
            self.high_time_ns, bool
        ):
            raise TypeError("UPO CTRL high_time_ns must be int")
        if not (
            MGPDClient.CTRL_PWM_MIN_FREQUENCY_KHZ
            <= self.frequency_khz
            <= MGPDClient.CTRL_PWM_MAX_FREQUENCY_KHZ
        ):
            raise ValueError(
                "UPO CTRL frequency_khz is outside the MGPDLab range "
                f"{MGPDClient.CTRL_PWM_MIN_FREQUENCY_KHZ}.."
                f"{MGPDClient.CTRL_PWM_MAX_FREQUENCY_KHZ}"
            )
        if self.frequency_khz % MGPDClient.CTRL_PWM_FREQUENCY_STEP_KHZ:
            raise ValueError(
                "UPO CTRL frequency_khz must be a multiple of "
                f"{MGPDClient.CTRL_PWM_FREQUENCY_STEP_KHZ} kHz"
            )
        if self.high_time_ns < MGPDClient.CTRL_PWM_WIDTH_STEP_NS:
            raise ValueError(
                "UPO CTRL high_time_ns must be at least "
                f"{MGPDClient.CTRL_PWM_WIDTH_STEP_NS} ns"
            )
        if self.high_time_ns % MGPDClient.CTRL_PWM_WIDTH_STEP_NS:
            raise ValueError(
                "UPO CTRL high_time_ns must be a multiple of "
                f"{MGPDClient.CTRL_PWM_WIDTH_STEP_NS} ns"
            )
        period_ns = 1_000_000.0 / self.frequency_khz
        if self.high_time_ns > period_ns - MGPDClient.CTRL_PWM_WIDTH_STEP_NS:
            raise ValueError(
                f"UPO CTRL high_time_ns={self.high_time_ns} does not fit period "
                f"{period_ns:g} ns"
            )
        if not isinstance(self.edge_count_uncertainty, int) or isinstance(
            self.edge_count_uncertainty, bool
        ) or self.edge_count_uncertainty < 1:
            raise ValueError("UPO CTRL edge_count_uncertainty must be an integer >= 1")
        if self.get_shot_timeout_margin_s <= 0:
            raise ValueError("GET_SHOT timeout margin must be positive")

    @property
    def real_frequency_khz(self) -> float:
        return MGPDClient.ctrl_pwm_real_frequency_khz(self.frequency_khz)

    def nominal_edge_count(self, shutter_duration_s: float) -> tuple[int, float]:
        if not math.isfinite(shutter_duration_s) or shutter_duration_s <= 0:
            raise ValueError(
                "UPO PWM S-curve requires a positive manual shutter duration"
            )
        exact_nominal = self.real_frequency_khz * 1_000.0 * shutter_duration_s
        rounded = int(round(exact_nominal))
        if rounded <= 0:
            raise ValueError(
                "UPO PWM shutter exposure contains less than one nominal CTRL period"
            )
        return rounded, exact_nominal


class UpoPwmShotExecutor:
    """Generate continuous CTRL PWM through UPO around blocking ``GET_SHOT``.

    All commands use the same serialized MGPDClient connection. Background:
    ``CTRL=0 -> GET_SHOT -> GET_PIXEL``. Signal:
    ``CTRL PWM -> GET_SHOT -> CTRL=0 -> GET_PIXEL``. ``GET_PIXEL`` is called by
    MGPDMeasurementBackend only after this executor returns successfully.
    """

    def __init__(self, settings: UpoPwmSettings | None = None):
        self.settings = settings or UpoPwmSettings()
        self.settings.validate()
        self._get_shot_in_flight = False
        self._ctrl_pwm_active = False

    @property
    def upo_command_in_flight(self) -> bool:
        return self._get_shot_in_flight

    @property
    def ctrl_pwm_active(self) -> bool:
        return self._ctrl_pwm_active

    def nominal_injections(
        self, shutter_duration_s: float, *, counter_mode_bits: int = 16
    ) -> tuple[int, dict[str, Any]]:
        nominal, exact_nominal = self.settings.nominal_edge_count(shutter_duration_s)
        maximum_count = (1 << int(counter_mode_bits)) - 2
        if nominal > maximum_count:
            raise ValueError(
                f"UPO PWM gives about {nominal} falling edges per shutter, above "
                f"the non-wrapping {counter_mode_bits}-bit counter limit {maximum_count}. "
                "Reduce the manual UPO exposure or CTRL frequency."
            )
        uncertainty = self.settings.edge_count_uncertainty
        return nominal, {
            "source": "upo_pwm_real_frequency_times_manual_shutter",
            "requested_frequency_khz": self.settings.frequency_khz,
            "real_frequency_khz": self.settings.real_frequency_khz,
            "high_time_ns": self.settings.high_time_ns,
            "manual_shutter_duration_s": float(shutter_duration_s),
            "nominal_falling_edges_exact": exact_nominal,
            "nominal_falling_edges_for_analysis": nominal,
            "falling_edge_count_min": max(0, nominal - uncertainty),
            "falling_edge_count_max": nominal + uncertainty,
            "edge_count_uncertainty": uncertainty,
            "actual_edge_counter_available": False,
            "shutter_state_observable": False,
        }

    @staticmethod
    def _get_shot(client: MGPDClient, request: ShotRequest) -> bool:
        return bool(
            client.get_shot(
                configure_omr=False,
                mode_cnt=0 if request.counter_mode_bits == 16 else 1,
                mode_read=request.mode_read,
                crw_mode=request.crw_mode,
            )
        )

    def _set_idle(self, client: MGPDClient) -> None:
        if not client.set_ctrl(0):
            raise RuntimeError("MGPDLab rejected SET_CTRL_PIN 0")
        self._ctrl_pwm_active = False

    def recover_safe_state(self, client: MGPDClient) -> None:
        """First command after reconnect: stop any PWM left by a failed shot."""

        self._set_idle(client)

    def return_to_idle(self, client: MGPDClient) -> None:
        if self._ctrl_pwm_active:
            self._set_idle(client)

    def execute(self, client: MGPDClient, request: ShotRequest) -> ShotExecutionResult:
        if self._get_shot_in_flight:
            raise RuntimeError("previous UPO GET_SHOT has not finished")
        if request.configure_get_shot_omr:
            if not client.configure_get_shot_omr(
                mode_cnt=0 if request.counter_mode_bits == 16 else 1,
                mode_read=request.mode_read,
                crw_mode=request.crw_mode,
            ):
                raise RuntimeError("GET_SHOT OMR pre-configuration failed")
        if request.shutter_duration_s is None:
            raise ValueError(
                "UPO PWM requires shutter_duration_s matching the manual MGPDLab exposure"
            )
        if float(client.timeout) <= (
            request.shutter_duration_s + self.settings.get_shot_timeout_margin_s
        ):
            raise ValueError(
                "MGPDClient socket timeout is too short for the UPO exposure; use timeout>"
                f"{request.shutter_duration_s + self.settings.get_shot_timeout_margin_s:g} s"
            )

        if not request.test_pulses:
            self._set_idle(client)
            started = time.monotonic()
            self._get_shot_in_flight = True
            try:
                shot_ok = self._get_shot(client, request)
            finally:
                elapsed = time.monotonic() - started
                self._get_shot_in_flight = False
            if not shot_ok:
                raise RuntimeError("MGPDLab GET_SHOT failed")
            _validate_blocking_shot_elapsed(request, elapsed)
            return ShotExecutionResult(
                requested_injections=None,
                programmed_injections=0,
                actual_injections=0,
                injections_for_analysis=0,
                injection_count_source="no_test_pulses_ctrl_forced_low",
                details={
                    "executor": type(self).__name__,
                    "ctrl_source": "MGPDLab_UPO_PWM",
                    "ctrl_forced_low_before_background": True,
                    "get_shot_elapsed_s": elapsed,
                    "get_shot_minimum_elapsed_check": (
                        "at_least_90_percent_of_declared_exposure_when_exposure_at_least_50ms"
                    ),
                },
            )

        nominal, count_details = self.nominal_injections(
            request.shutter_duration_s,
            counter_mode_bits=request.counter_mode_bits,
        )
        if request.n_injections is not None and request.n_injections != nominal:
            raise ValueError(
                "internal S-curve denominator differs from the UPO PWM nominal count: "
                f"request={request.n_injections}, PWM={nominal}"
            )
        pwm_started_utc = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        if not client.set_ctrl_pwm(
            self.settings.frequency_khz, self.settings.high_time_ns
        ):
            raise RuntimeError("MGPDLab rejected SET_CTRL_PIN PWM")
        self._ctrl_pwm_active = True

        shot_error: BaseException | None = None
        idle_error: BaseException | None = None
        shot_ok = False
        started = time.monotonic()
        self._get_shot_in_flight = True
        try:
            shot_ok = self._get_shot(client, request)
        except BaseException as error:
            shot_error = error
        finally:
            elapsed = time.monotonic() - started
            self._get_shot_in_flight = False
            try:
                self._set_idle(client)
            except BaseException as error:
                idle_error = error

        if shot_error is not None:
            if idle_error is not None:
                raise RuntimeError(
                    "GET_SHOT failed and CTRL could not be returned to 0; reconnect is required"
                ) from shot_error
            raise shot_error
        if idle_error is not None:
            raise RuntimeError(
                "GET_SHOT finished but CTRL could not be returned to 0; GET_PIXEL is blocked"
            ) from idle_error
        if not shot_ok:
            raise RuntimeError("MGPDLab GET_SHOT failed")
        _validate_blocking_shot_elapsed(request, elapsed)

        return ShotExecutionResult(
            requested_injections=None,
            programmed_injections=None,
            actual_injections=None,
            injections_for_analysis=nominal,
            injection_count_source=(
                "upo_pwm_nominal_from_real_frequency_and_manual_shutter_no_edge_counter"
            ),
            details={
                "executor": type(self).__name__,
                "ctrl_source": "MGPDLab_UPO_PWM",
                "ctrl_pwm_started_before_get_shot_utc": pwm_started_utc,
                "ctrl_pwm_stopped_after_get_shot_before_get_pixel": True,
                "ctrl_active_during_upo_internal_matrix_read": True,
                "get_shot_elapsed_s": elapsed,
                "get_shot_minimum_elapsed_check": (
                    "at_least_90_percent_of_declared_exposure_when_exposure_at_least_50ms"
                ),
                **count_details,
            },
        )


@dataclass(frozen=True)
class KeysightBurstSettings:
    """CTRL burst settings for Keysight 81150A/81160A generators."""

    channel: int = 1
    frequency_hz: float = 100_000.0
    duty_cycle_percent: float = 50.0
    low_level_v: float = 0.0
    high_level_v: float = 3.3
    load_ohm: float = 1_000_000.0
    shutter_start_delay_s: float = 0.8
    post_burst_guard_s: float = 0.1
    get_shot_timeout_margin_s: float = 2.0

    def validate(self) -> None:
        if self.channel not in (1, 2):
            raise ValueError("Keysight CTRL channel must be 1 or 2")
        if self.frequency_hz <= 0:
            raise ValueError("CTRL frequency_hz must be positive")
        if not 0 < self.duty_cycle_percent < 100:
            raise ValueError("CTRL duty cycle must be between 0 and 100 percent")
        if self.low_level_v != 0.0:
            raise ValueError("CTRL idle/low level is fixed to 0 V for this measurement")
        if self.high_level_v != 3.3:
            raise ValueError("CTRL high level is fixed to 3.3 V for this measurement")
        if self.load_ohm != 1_000_000.0:
            raise ValueError("CTRL generator load setting must be 1 MOhm")
        if self.shutter_start_delay_s < 0 or self.post_burst_guard_s < 0:
            raise ValueError("shutter/burst sleeps must be non-negative")
        if self.get_shot_timeout_margin_s <= 0:
            raise ValueError("GET_SHOT timeout margin must be positive")


class KeysightBurstGenerator(Protocol):
    idn: str

    def set_output_load(self, channel: int, load_ohm: float) -> float: ...
    def configure_channel(self, **kwargs: Any) -> None: ...
    def set_square_duty_cycle(
        self, channel: int, duty_cycle_percent: float
    ) -> float: ...
    def configure_triggered_burst(
        self, channel: int, cycles: int, *, trigger_source: str = "MAN"
    ) -> Mapping[str, Any]: ...
    def software_trigger(self) -> None: ...
    def enable_channel(self, channel: int) -> None: ...
    def disable_channel(self, channel: int) -> None: ...


class KeysightBurstShotExecutor:
    """Run UPO synchronously; a worker controls ONLY the external generator.

    The delay is measured from GET_SHOT invocation, not observed shutter-open.
    Generator queryback proves the programmed number of periods but is not an
    independent physical edge counter. Raw data preserve this distinction.
    """

    def __init__(
        self,
        generator: KeysightBurstGenerator,
        settings: KeysightBurstSettings | None = None,
    ):
        self.generator = generator
        self.settings = settings or KeysightBurstSettings()
        self.settings.validate()
        self._prepared_cycles: int | None = None
        self._preparation: dict[str, Any] = {}
        self._get_shot_in_flight = False
        self._active_trigger_thread: threading.Thread | None = None

    @property
    def upo_command_in_flight(self) -> bool:
        """Whether the calling thread is inside blocking UPO GET_SHOT."""
        return self._get_shot_in_flight

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

    def _prepare(self, cycles: int) -> dict[str, Any]:
        if self._prepared_cycles == cycles:
            return dict(self._preparation)
        other_channel = 2 if self.settings.channel == 1 else 1
        self.generator.disable_channel(other_channel)
        self.generator.disable_channel(self.settings.channel)
        queried_load = self.generator.set_output_load(
            self.settings.channel, self.settings.load_ohm
        )
        self.generator.configure_channel(
            channel=self.settings.channel,
            frequency_hz=self.settings.frequency_hz,
            shape="SQU",
            low_level_v=self.settings.low_level_v,
            high_level_v=self.settings.high_level_v,
            enable_after_config=False,
        )
        queried_duty = self.generator.set_square_duty_cycle(
            self.settings.channel, self.settings.duty_cycle_percent
        )
        burst = dict(
            self.generator.configure_triggered_burst(
                self.settings.channel, cycles, trigger_source="MAN"
            )
        )
        self.generator.enable_channel(self.settings.channel)
        self._prepared_cycles = cycles
        self._preparation = {
            "generator_idn": str(getattr(self.generator, "idn", "unknown")),
            "model_assumption": "Keysight 81150A_or_81160A",
            "channel": self.settings.channel,
            "waveform": "SQU",
            "frequency_hz": self.settings.frequency_hz,
            "duty_cycle_percent_queryback": queried_duty,
            "low_level_v": self.settings.low_level_v,
            "high_level_v": self.settings.high_level_v,
            "falling_edges_per_period": 1,
            "load_ohm_queryback": queried_load,
            "idle_level_commanded_v": 0.0,
            "idle_level_scope_verification_required": True,
            **burst,
        }
        return dict(self._preparation)

    def prepare_injections(self, cycles: int) -> dict[str, Any]:
        """Prepare and arm the channel before the paired background shot."""

        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles <= 0:
            raise ValueError("cycles must be a positive integer")
        return self._prepare(cycles)

    def return_to_idle(self) -> None:
        """Disable the CTRL output after GET_SHOT has completed."""
        if self._active_trigger_thread is not None and self._active_trigger_thread.is_alive():
            raise RuntimeError("CTRL worker still owns the generator; cleanup deferred")
        self.generator.disable_channel(self.settings.channel)
        self._prepared_cycles = None
        self._preparation = {}

    @staticmethod
    def _get_shot(client: MGPDClient, request: ShotRequest) -> bool:
        return bool(
            client.get_shot(
                configure_omr=False,
                mode_cnt=0 if request.counter_mode_bits == 16 else 1,
                mode_read=request.mode_read,
                crw_mode=request.crw_mode,
            )
        )

    def execute(self, client: MGPDClient, request: ShotRequest) -> ShotExecutionResult:
        if self._get_shot_in_flight or (
            self._active_trigger_thread is not None
            and self._active_trigger_thread.is_alive()
        ):
            raise RuntimeError("previous shot/CTRL worker has not finished")
        if request.configure_get_shot_omr:
            if not client.configure_get_shot_omr(
                mode_cnt=0 if request.counter_mode_bits == 16 else 1,
                mode_read=request.mode_read,
                crw_mode=request.crw_mode,
            ):
                raise RuntimeError("GET_SHOT OMR pre-configuration failed")

        if not request.test_pulses:
            generator_was_prepared = self._prepared_cycles is not None
            if generator_was_prepared:
                # A paired background must contain no CTRL edge. Keep the CTRL
                # connector electrically disabled throughout the blocking shot,
                # then re-enable the already armed burst only after GET_SHOT has
                # returned and before the subsequent signal acquisition.
                self.generator.disable_channel(self.settings.channel)
            self._get_shot_in_flight = True
            background_started = time.monotonic()
            try:
                if not self._get_shot(client, request):
                    raise RuntimeError("MGPDLab GET_SHOT failed")
            finally:
                background_elapsed = time.monotonic() - background_started
                self._get_shot_in_flight = False
            _validate_blocking_shot_elapsed(request, background_elapsed)
            if generator_was_prepared:
                self.generator.enable_channel(self.settings.channel)
            return ShotExecutionResult(
                requested_injections=request.n_injections,
                programmed_injections=0,
                actual_injections=0,
                injections_for_analysis=0,
                injection_count_source="no_test_pulses",
                details={
                    "executor": type(self).__name__,
                    "generator_prepared_for_signal": generator_was_prepared,
                    "generator_output_disabled_during_background": (
                        generator_was_prepared
                    ),
                    "get_shot_elapsed_s": background_elapsed,
                    "get_shot_minimum_elapsed_check": (
                        "at_least_90_percent_of_declared_exposure_when_exposure_at_least_50ms"
                    ),
                    **self._preparation,
                },
            )

        cycles = request.n_injections
        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles <= 0:
            raise ValueError("signal ShotRequest requires positive integer n_injections")
        if request.shutter_duration_s is None:
            raise ValueError(
                "S-curve shutter_duration_s is required and must match the manual UPO exposure"
            )
        preparation = self._prepare(cycles)
        burst_duration_s = cycles / self.settings.frequency_hz
        minimum_exposure_s = (
            self.settings.shutter_start_delay_s
            + burst_duration_s
            + self.settings.post_burst_guard_s
        )
        if request.shutter_duration_s < minimum_exposure_s:
            raise ValueError(
                f"UPO exposure {request.shutter_duration_s:g} s is shorter than the "
                f"required delay+burst+guard {minimum_exposure_s:g} s"
            )
        if float(client.timeout) <= (
            request.shutter_duration_s + self.settings.get_shot_timeout_margin_s
        ):
            raise ValueError(
                "MGPDClient socket timeout is too short for the UPO exposure; "
                "use timeout>"
                f"{request.shutter_duration_s + self.settings.get_shot_timeout_margin_s:g} s"
            )

        state: dict[str, Any] = {}
        cancelled = threading.Event()
        started = threading.Event()

        def trigger_worker() -> None:
            # No client, register, pixel or reconnect calls in this worker.
            try:
                started.wait()
                if cancelled.wait(self.settings.shutter_start_delay_s):
                    return
                state["software_trigger_utc"] = self._utc_now()
                state["trigger_monotonic_s"] = time.monotonic()
                self.generator.software_trigger()
                time.sleep(burst_duration_s + self.settings.post_burst_guard_s)
            except BaseException as error:
                state["trigger_error"] = error

        thread = threading.Thread(target=trigger_worker, name="ctrl-burst", daemon=True)
        self._active_trigger_thread = thread
        thread.start()
        start_monotonic = time.monotonic()
        call_utc = self._utc_now()
        self._get_shot_in_flight = True
        try:
            started.set()
            # This is the SAME thread that stages PX and later calls GET_PIXEL.
            shot_ok = self._get_shot(client, request)
        finally:
            return_monotonic = time.monotonic()
            return_utc = self._utc_now()
            self._get_shot_in_flight = False
            cancelled.set()
            thread.join(timeout=float(client.timeout) + self.settings.get_shot_timeout_margin_s)
            if not thread.is_alive():
                self._active_trigger_thread = None

        if thread.is_alive():
            raise RuntimeError("CTRL worker did not finish; no new acquisition is safe")
        if not shot_ok:
            raise RuntimeError("MGPDLab GET_SHOT failed")
        elapsed = return_monotonic - start_monotonic
        _validate_blocking_shot_elapsed(request, elapsed)
        if "trigger_error" in state:
            raise RuntimeError("CTRL trigger failed after waiting for GET_SHOT") from state["trigger_error"]
        if "trigger_monotonic_s" not in state:
            raise RuntimeError("GET_SHOT returned before the configured CTRL start delay")
        if return_monotonic < state["trigger_monotonic_s"] + burst_duration_s:
            raise RuntimeError("GET_SHOT returned before the CTRL burst completed")

        return ShotExecutionResult(
            requested_injections=cycles,
            programmed_injections=cycles,
            actual_injections=None,
            injections_for_analysis=cycles,
            injection_count_source=(
                "keysight_programmed_cycles_with_scpi_queryback_no_independent_edge_counter"
            ),
            details={
                "executor": type(self).__name__,
                **preparation,
                "shutter_state_observable": False,
                "shutter_duration_s_manual_upo_setting": request.shutter_duration_s,
                "shutter_start_delay_s": self.settings.shutter_start_delay_s,
                "burst_duration_s": burst_duration_s,
                "post_burst_guard_s": self.settings.post_burst_guard_s,
                "get_shot_call_utc": call_utc,
                "software_trigger_utc": state["software_trigger_utc"],
                "get_shot_return_utc": return_utc,
                "get_shot_elapsed_s": elapsed,
                "get_shot_minimum_elapsed_check": (
                    "at_least_90_percent_of_declared_exposure_when_exposure_at_least_50ms"
                ),
                "upo_thread": threading.current_thread().name,
                "delay_origin": "GET_SHOT_invocation_not_shutter_open",
                "burst_inside_shutter_physically_verified": False,
            },
        )


def load_base_pixel_configs(
    source: str | Path | Mapping[tuple[int, int], int],
) -> dict[tuple[int, int], int]:
    """Load physical ``(column, row) -> raw PX word`` baseline values."""

    if isinstance(source, (str, Path)):
        internal = load_matrix_config(source)
        # Matrix JSON uses the existing project's internal (row, col) keys.
        return {(int(col), int(row)): int(raw) for (row, col), raw in internal.items()}

    result: dict[tuple[int, int], int] = {}
    for coordinate, raw in source.items():
        if not isinstance(coordinate, (tuple, list)) or len(coordinate) != 2:
            raise TypeError("base pixel configuration keys must be (column, row)")
        column, row = coordinate
        if not isinstance(column, int) or isinstance(column, bool):
            raise TypeError("base pixel column must be int")
        if not isinstance(row, int) or isinstance(row, bool):
            raise TypeError("base pixel row must be int")
        PIXEL_CODEC.validate_raw(int(raw))
        result[(column, row)] = int(raw)
    return result


STANDARD_CHARACTERIZATION_FCLK_MHZ = 50
STANDARD_CHARACTERIZATION_PIXEL_FIELDS = {
    "PX_GAIN": 10,
    "PX_SHT": 2,
    # PX_MASK=0 disables the pixel digital counting path.
    "PX_MASK": 0,
    "PX_SH_EN": 0,
    "PX_TST_EN": 0,
    "PX_BUF_NEN": 1,
    "PX_CMPD_TR": 16,
    "PX_CMPC_TR": 16,
    "PX_CMPB_TR": 16,
    "PX_CMPA_TR": 16,
}


def build_standard_characterization_pixel_configs(
    *,
    digital_counting_enabled: bool,
) -> dict[tuple[int, int], int]:
    """Build the owned 16x32 matrix baseline in physical ``(column, row)`` order."""

    fields = dict(STANDARD_CHARACTERIZATION_PIXEL_FIELDS)
    fields["PX_MASK"] = int(bool(digital_counting_enabled))
    raw = PIXEL_CODEC.pack(fields)
    return {
        (column, row): raw
        for row in range(MATRIX_ROWS)
        for column in OWNED_COLUMNS
    }


class MGPDMeasurementBackend:
    """Thin measurement adapter over the project's existing ASIC API."""

    def __init__(
        self,
        client: MGPDClient,
        *,
        base_pixel_configs: str | Path | Mapping[tuple[int, int], int] | None,
        counter_key: str,
        noise_settings: NoiseScanSettings,
        shot_executor: ShotExecutor | None = None,
        status_callback: Callable[[str], None] | None = None,
        bad_pixel_map: BadPixelMapInput = None,
    ):
        if not client.connected:
            raise RuntimeError("MGPDClient must be connected before creating the backend")
        if counter_key not in ("low", "mid", "high"):
            raise ValueError("counter_key must be low, mid or high")
        noise_settings.validate()

        self.client = client
        self.cfg = Configuration(
            client,
            EO_cfg.DEFAULT_REGISTERS,
            EO_cfg.AMUX_SIGNALS,
            EO_cfg.REGS_FIELDS,
            EO_cfg.AMUX_MAP,
        )
        self.matrix = PixelMatrixConfiguration(client)
        # Separate adapter for the explicitly requested zero-only half.
        # The normal measurement adapter retains its owned-column restriction.
        self._zeroed_columns = tuple(
            column for column in range(MATRIX_COLS) if column not in OWNED_COLUMNS
        )
        self._zeroed_matrix = PixelMatrixConfiguration(
            client, owned_columns=self._zeroed_columns
        )
        self.counter_key = counter_key
        self.settings = noise_settings
        self.shot_executor = shot_executor or MGPDGetShotExecutor()
        self.status_callback = status_callback
        self.bad_pixels = normalize_bad_pixel_map(bad_pixel_map)
        self._base_pixel_configs = (
            build_standard_characterization_pixel_configs(
                digital_counting_enabled=True
            )
            if base_pixel_configs is None
            else load_base_pixel_configs(base_pixel_configs)
        )
        disabled_defaults = build_standard_characterization_pixel_configs(
            digital_counting_enabled=False
        )
        self._base_pixel_configs = {
            coordinate: noise_baseline_pixel_word(raw, bad=coordinate in self.bad_pixels)
            for coordinate, raw in self._base_pixel_configs.items()
        }
        for coordinate in self.bad_pixels:
            self._base_pixel_configs[coordinate] = enforce_disabled_pixel(
                self._base_pixel_configs.get(coordinate, disabled_defaults[coordinate])
            )
        self._current_pixel_configs = dict(self._base_pixel_configs)
        self._decoder = LFSRDecoder(noise_settings.counter_mode_bits)
        self._initialization_fclk_mhz = STANDARD_CHARACTERIZATION_FCLK_MHZ
        self._standard_initialization_complete = False
        self._upo_state_uncertain = False
        self._global_field_state: dict[str, int] = {}

    def initialize_standard_configuration(
        self,
        pixels: Sequence[tuple[int, int]],
        *,
        fclk_mhz: int = STANDARD_CHARACTERIZATION_FCLK_MHZ,
        progress_callback: Callable[[str, float], None] | None = None,
        eo_overrides: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Establish the reproducible global and pixel state before a test.

        The other 16x32 half is first staged with literal 32-bit zero words.
        The full project-owned 16x32 half is then disabled with ``PX_MASK=0``.
        Only the selected test pixels are then loaded from the test baseline,
        whose built-in default uses ``PX_MASK=1``. OMR bytes, including
        polarity, are neither written nor modified by this sequence.
        """

        def report(message: str, percent: float) -> None:
            if progress_callback is not None:
                progress_callback(message, percent)

        # Validate the complete logical EO request before the first hardware
        # command. Direct users of the backend therefore get the same fail-fast
        # guarantee as the public workflow.
        from .parameters import validate_eo_overrides
        overrides = validate_eo_overrides(eo_overrides, run_scurve=False)
        self.validate_pixels(pixels)
        report("Инициализация ASIC: установка FCLK", 0.0)
        if not self.client.set_fclk(int(fclk_mhz)):
            raise RuntimeError(f"failed to set FCLK={fclk_mhz} MHz")
        report("Инициализация ASIC: загрузка global defaults", 10.0)
        if not self.cfg.set_default():
            raise RuntimeError("failed to load EO_cfg.DEFAULT_REGISTERS")

        report("Инициализация ASIC: проверка global readback", 20.0)
        verified_registers = self.cfg.read_registers(EO_cfg.DEFAULT_REGISTERS)
        mismatches = {
            address: {
                "expected": int(expected),
                "readback": int(verified_registers[address]),
            }
            for address, expected in EO_cfg.DEFAULT_REGISTERS.items()
            if int(verified_registers[address]) != int(expected)
        }
        if mismatches:
            first_address = min(mismatches)
            values = mismatches[first_address]
            raise RuntimeError(
                "EO default readback mismatch at "
                f"0x{first_address:04X}: expected 0x{values['expected']:02X}, "
                f"read 0x{values['readback']:02X}"
            )

        for name, value in overrides.items():
            if not self.cfg.set_data(name, value) or self.cfg.get_data(name) != value:
                raise RuntimeError(f"EO override write/readback failed: {name}={value}")
        if overrides:
            report("EO параметры: " + ", ".join(f"{k}={v}" for k, v in overrides.items()), 25.0)

        disabled_configs = build_standard_characterization_pixel_configs(
            digital_counting_enabled=False
        )
        disabled_raw = next(iter(disabled_configs.values()))
        zeroed_bucket = -1

        def report_zeroed(
            current: int, total: int, _row: int, _column: int
        ) -> None:
            nonlocal zeroed_bucket
            bucket = min(4, (4 * current) // max(total, 1))
            if bucket > zeroed_bucket:
                zeroed_bucket = bucket
                report(
                    f"Инициализация PX: другая половина 0x00000000, {current}/{total}",
                    30.0 + 15.0 * current / max(total, 1),
                )

        zeroed_count = self._zeroed_matrix.set_owned_half(
            0x00000000, progress_callback=report_zeroed
        )
        disabled_bucket = -1

        def report_disabled(
            current: int, total: int, _row: int, _column: int
        ) -> None:
            nonlocal disabled_bucket
            bucket = min(4, (4 * current) // max(total, 1))
            if bucket > disabled_bucket:
                disabled_bucket = bucket
                report(
                    f"Инициализация PX: MASK=0, {current}/{total}",
                    45.0 + 20.0 * current / max(total, 1),
                )

        staged_count = self.matrix.set_owned_half(
            disabled_raw,
            progress_callback=report_disabled,
        )
        # GET_SHOT commits the staged full matrix. A separate WRITE_TO_CHIP
        # acknowledges acceptance only and could overlap UPO's next operation.
        self._current_pixel_configs = dict(disabled_configs)

        selected_baseline = {
            coordinate: self._base_pixel_configs[coordinate]
            for coordinate in pixels
        }
        selected_bucket = -1

        def report_selected(
            current: int, total: int, _row: int, _column: int
        ) -> None:
            nonlocal selected_bucket
            bucket = min(4, (4 * current) // max(total, 1))
            if bucket > selected_bucket:
                selected_bucket = bucket
                report(
                    f"Инициализация PX: тестовые пиксели {current}/{total}",
                    65.0 + 35.0 * current / max(total, 1),
                )

        self.restore_pixel_configs(
            selected_baseline,
            progress_callback=report_selected,
        )
        self._initialization_fclk_mhz = int(fclk_mhz)
        self._global_field_state = {
            name: int(value) for name, value in EO_cfg.DEFAULT_FIELD_VALUES.items()
        }
        self._global_field_state.update(overrides)
        self._standard_initialization_complete = True

        return {
            "fclk_mhz": int(fclk_mhz),
            "fclk_acknowledged_no_readback_command": True,
            "global_configuration_source": "EO_cfg.DEFAULT_REGISTERS",
            "global_logical_defaults": dict(EO_cfg.DEFAULT_FIELD_VALUES),
            "eo_overrides": overrides,
            "eo_override_readback_verified": True,
            "global_register_count": len(EO_cfg.DEFAULT_REGISTERS),
            "global_register_readback_verified": True,
            "zeroed_unowned_pixel_count": zeroed_count,
            "zeroed_unowned_columns": list(self._zeroed_columns),
            "zeroed_unowned_rows": list(range(MATRIX_ROWS)),
            "zeroed_unowned_pixel_raw_hex": "0x00000000",
            "zeroed_unowned_policy": (
                "literal 32-bit zero words at initialization and reconnect; "
                "not included in acquisition or owned pixel restore"
            ),
            "standard_owned_pixel_count": staged_count,
            "standard_owned_pixel_raw_hex": f"0x{disabled_raw:08X}",
            "standard_owned_pixel_fields": dict(
                STANDARD_CHARACTERIZATION_PIXEL_FIELDS
            ),
            "standard_pixel_digital_state": (
                "PX_MASK=0 disables digital counting"
            ),
            "selected_test_pixel_count": len(selected_baseline),
            "selected_test_pixels_loaded_after_standard_disable": True,
            "pixel_commit_policy": "stage_only_until_GET_SHOT",
            "selected_test_pixel_mask_policy": (
                "PX_MASK=1 for selected good pixels, 0 for bad pixels; PX_TST_EN=0"
            ),
            "pixel_write_verification": (
                "MGPDLab command acknowledgement; no per-pixel readback exists"
            ),
            "omr_written": False,
            "polarity_modified": False,
            "permanently_disabled_pixel_count": len(self.bad_pixels),
            "bad_pixel_policy": "PX_MASK=0 and PX_TST_EN=0 on every pixel write",
        }

    def active_pixels(
        self, pixels: Sequence[tuple[int, int]]
    ) -> tuple[tuple[int, int], ...]:
        active = tuple(coordinate for coordinate in pixels if coordinate not in self.bad_pixels)
        if not active:
            raise ValueError("all requested pixels are excluded by bad_pixel_map")
        return active

    def _masked_pixel_word(self, coordinate: tuple[int, int], raw: int) -> int:
        return enforce_disabled_pixel(raw) if coordinate in self.bad_pixels else raw

    def validate_pixels(self, pixels: Sequence[tuple[int, int]]) -> None:
        missing = [coordinate for coordinate in pixels if coordinate not in self._base_pixel_configs]
        if missing:
            column, row = missing[0]
            raise ValueError(
                f"base pixel configuration is missing {len(missing)} selected pixel(s); "
                f"first missing coordinate is Col={column} Row={row}"
            )
        for column, row in pixels:
            self.matrix.validate_owned_pixel(row=row, col=column)

    def initial_configuration_snapshot(self) -> dict[str, Any]:
        register_cache = self.cfg.read_registers()
        omr_addresses = (
            MGPDClient.OMR_BYTE_0_ADDRESS,
            MGPDClient.OMR_BYTE_1_ADDRESS,
            MGPDClient.OMR_BYTE_2_ADDRESS,
            MGPDClient.OMR_BYTE_3_ADDRESS,
        )
        omr_bytes: dict[int, int] = {}
        for address in omr_addresses:
            value = self.client.read_byte(address)
            if value is None:
                raise RuntimeError(f"failed to read OMR byte at 0x{address:04X}")
            omr_bytes[address] = int(value)
        omr0 = omr_bytes[MGPDClient.OMR_BYTE_0_ADDRESS]
        omr1 = omr_bytes[MGPDClient.OMR_BYTE_1_ADDRESS]
        omr2 = omr_bytes[MGPDClient.OMR_BYTE_2_ADDRESS]
        omr3 = omr_bytes[MGPDClient.OMR_BYTE_3_ADDRESS]
        return {
            "physical_registers": {
                f"0x{address:04X}": value for address, value in register_cache.items()
            },
            "logical_fields": self.cfg.get_all_data(register_cache=register_cache),
            "operation_mode_register": {
                "bytes": {
                    f"0x{address:04X}": value for address, value in omr_bytes.items()
                },
                "mode_read": (omr0 & MGPDClient.OMR_MODE_READ_MASK) >> 5,
                "mode_cnt": int(bool(omr1 & MGPDClient.OMR_MODE_CNT_MASK)),
                "crw_mode": int(bool(omr1 & MGPDClient.OMR_CRW_MODE_MASK)),
                "pol_ctrl": int(bool(omr2 & MGPDClient.OMR_POL_CTRL_MASK)),
                "pol_sw": int(bool(omr3 & MGPDClient.OMR_POL_SW_MASK)),
                "polarity_interpretation": (
                    "software POL_SW is selected"
                    if omr2 & MGPDClient.OMR_POL_CTRL_MASK
                    else "POL_SW is not selected; effective polarity depends on the alternate control path"
                ),
            },
            "counter_key": self.counter_key,
            "counter_mode_bits": self.settings.counter_mode_bits,
            "lfsr_decoding": self.settings.decode_lfsr,
            "lfsr_direction": self.settings.lfsr_direction,
            "get_shot_omr_preconfiguration": self.settings.configure_get_shot_omr,
            "mode_read": self.settings.mode_read,
            "crw_mode": self.settings.crw_mode,
        }

    def base_pixel_rows(self, pixels: Sequence[tuple[int, int]]) -> list[dict[str, Any]]:
        rows = []
        for column, row in pixels:
            raw = self._base_pixel_configs[(column, row)]
            rows.append(
                {
                    "column": column,
                    "row": row,
                    "raw_pixel_config_hex": f"0x{raw:08X}",
                    **PIXEL_CODEC.unpack(raw),
                }
            )
        return rows

    def configure_window(self, spec: WindowSpec, upper_non_limiting_code: int) -> None:
        # Keep only the tested comparator free for scanning. Record each
        # acknowledged write so reconnect restores the same fixed thresholds.
        fixed = spec.fixed_threshold_codes(upper_non_limiting_code)
        for name, code in fixed.items():
            if not self.cfg.set_data(name, code):
                raise RuntimeError(f"failed to set fixed threshold {name}={code}")
            self._global_field_state[name] = code
        message = (
            f"Окно {spec.name}: свип {spec.threshold_dac}; фиксированные пороги: "
            + ", ".join(f"{name}={code}" for name, code in fixed.items())
        )
        logger.info(message)
        if self.status_callback is not None:
            self.status_callback(message)

    def set_threshold(self, spec: WindowSpec, code: int) -> None:
        if not self.cfg.set_data(spec.threshold_dac, int(code)):
            raise RuntimeError(f"failed to set {spec.threshold_dac}={code}")
        self._global_field_state[spec.threshold_dac] = int(code)

    def program_trim_map(
        self,
        spec: WindowSpec,
        pixels: Sequence[tuple[int, int]],
        trim_map: Mapping[tuple[int, int], int],
    ) -> dict[tuple[int, int], int]:
        self.validate_pixels(pixels)
        staged: dict[tuple[int, int], int] = {}
        for column, row in pixels:
            if (column, row) not in trim_map:
                raise ValueError(f"trim map is missing Col={column} Row={row}")
            trim = int(trim_map[(column, row)])
            if not 0 <= trim <= 31:
                raise ValueError(f"trim at Col={column} Row={row} is outside 0..31")
            current_raw = self._current_pixel_configs[(column, row)]
            fields = PIXEL_CODEC.unpack(current_raw)
            fields[spec.pixel_trim_field] = trim
            updated_raw = self._masked_pixel_word((column, row), PIXEL_CODEC.pack(fields))
            staged[(row, column)] = updated_raw

        self.matrix.set_pixels(staged)

        for (row, column), raw in staged.items():
            self._current_pixel_configs[(column, row)] = raw
        return {(column, row): PIXEL_CODEC.extract(raw, spec.pixel_trim_field) for (column, row), raw in self._current_pixel_configs.items() if (column, row) in pixels}

    def current_trim_map(
        self,
        spec: WindowSpec,
        pixels: Sequence[tuple[int, int]],
    ) -> dict[tuple[int, int], int]:
        return {
            coordinate: PIXEL_CODEC.extract(
                self._current_pixel_configs[coordinate], spec.pixel_trim_field
            )
            for coordinate in pixels
        }

    def snapshot_pixel_configs(
        self, pixels: Sequence[tuple[int, int]]
    ) -> dict[tuple[int, int], int]:
        """Return exact cached 32-bit words for later restoration."""

        self.validate_pixels(pixels)
        return {coordinate: self._current_pixel_configs[coordinate] for coordinate in pixels}

    def restore_pixel_configs(
        self,
        pixel_configs: Mapping[tuple[int, int], int],
        *,
        progress_callback: Callable[[int, int, int, int], None] | None = None,
        commit: bool = False,
    ) -> None:
        """Restore pixel words, with the permanent bad-pixel mask taking priority."""

        self.validate_pixels(tuple(pixel_configs))
        staged: dict[tuple[int, int], int] = {}
        for (column, row), raw in pixel_configs.items():
            PIXEL_CODEC.validate_raw(int(raw))
            staged[(row, column)] = self._masked_pixel_word((column, row), int(raw))
        self.matrix.set_pixels(staged, progress_callback=progress_callback)
        if commit and not self.matrix.write_to_chip():
            raise RuntimeError("SET_PIXEL_CFG WRITE_TO_CHIP failed while restoring pixels")
        for (row, column), raw in staged.items():
            self._current_pixel_configs[(column, row)] = raw

    def program_scurve_pixel_configuration(
        self,
        pixels: Sequence[tuple[int, int]],
        *,
        gain_map: Mapping[tuple[int, int], int],
        active_injection_pixels: Sequence[tuple[int, int]],
    ) -> list[dict[str, Any]]:
        """Apply GAIN and documented test fields while preserving all trims."""

        self.validate_pixels(pixels)
        unknown_active = set(active_injection_pixels) - set(pixels)
        if unknown_active:
            coordinate = sorted(unknown_active)[0]
            raise ValueError(
                f"active injection pixel Col={coordinate[0]} Row={coordinate[1]} "
                "is outside the selected pixel set"
            )
        active = set(active_injection_pixels) - set(self.bad_pixels)
        staged: dict[tuple[int, int], int] = {}
        rows: list[dict[str, Any]] = []
        for column, row in pixels:
            coordinate = (column, row)
            if coordinate not in gain_map and coordinate not in self.bad_pixels:
                raise ValueError(f"GAIN map is missing Col={column} Row={row}")
            gain = int(gain_map.get(coordinate, PIXEL_CODEC.extract(
                self._current_pixel_configs[coordinate], "PX_GAIN"
            )))
            if not 0 <= gain <= 31:
                raise ValueError(f"GAIN at Col={column} Row={row} is outside 0..31")
            fields = PIXEL_CODEC.unpack(self._current_pixel_configs[coordinate])
            fields.update(
                {
                    "PX_GAIN": gain,
                    "PX_SHT": 2,
                    "PX_MASK": int(coordinate not in self.bad_pixels),
                    "PX_SH_EN": 0,
                    "PX_TST_EN": int(coordinate in active),
                    "PX_BUF_NEN": 1,
                }
            )
            raw = self._masked_pixel_word(coordinate, PIXEL_CODEC.pack(fields))
            staged[(row, column)] = raw
            rows.append(
                {
                    "column": column,
                    "row": row,
                    "active_injection_pixel": coordinate in active,
                    "raw_pixel_config_hex": f"0x{raw:08X}",
                    **fields,
                }
            )
        self.matrix.set_pixels(staged)
        for (row, column), raw in staged.items():
            self._current_pixel_configs[(column, row)] = raw
        return rows

    def program_noise_pixel_configuration(self, pixels: Sequence[tuple[int, int]]) -> None:
        """Explicitly disable injection before every noise stage, preserving trims/GAIN."""

        configs = {}
        for coordinate in pixels:
            fields = PIXEL_CODEC.unpack(self._current_pixel_configs[coordinate])
            fields.update(PX_TST_EN=0, PX_MASK=int(coordinate not in self.bad_pixels),
                          PX_SH_EN=0, PX_BUF_NEN=1, PX_SHT=2)
            configs[coordinate] = PIXEL_CODEC.pack(fields)
        self.restore_pixel_configs(configs)

    def configure_test_pulse_amplitude(self, pulse_amplitude: Any) -> dict[str, Any]:
        """Program native REF1/REF2 codes and preserve calibrated levels."""

        if not isinstance(pulse_amplitude, Mapping):
            return {
                "configuration_method": "external_or_native_label_only",
                "pulse_amplitude": pulse_amplitude,
            }
        fields = ("DAC_TST_REF1", "DAC_TST_REF2")
        supplied = [name for name in fields if name in pulse_amplitude]
        if supplied and len(supplied) != len(fields):
            raise ValueError(
                "pulse amplitude mapping must supply both DAC_TST_REF1 and DAC_TST_REF2"
            )
        if not supplied:
            return {
                "configuration_method": "mapping_without_native_ref_codes",
                "pulse_amplitude": dict(pulse_amplitude),
            }
        programmed: dict[str, int] = {}
        for name in fields:
            value = pulse_amplitude[name]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1023:
                raise ValueError(f"{name} must be an integer in 0..1023")
            programmed[name] = int(value)
        minimum_code = pulse_amplitude.get("minimum_reference_code")
        if minimum_code is not None:
            minimum_code = int(minimum_code)
            if any(value < minimum_code for value in programmed.values()):
                raise ValueError(
                    f"selected REF code is below minimum_reference_code={minimum_code}"
                )
        maximum_code = pulse_amplitude.get("maximum_reference_code")
        if maximum_code is not None:
            maximum_code = int(maximum_code)
            if any(value > maximum_code for value in programmed.values()):
                raise ValueError(
                    f"selected REF code is above maximum_reference_code={maximum_code}"
                )
        calibrated_levels: dict[str, Any] = {}
        if "ref1_voltage_v" in pulse_amplitude or "ref2_voltage_v" in pulse_amplitude:
            if not all(name in pulse_amplitude for name in ("ref1_voltage_v", "ref2_voltage_v")):
                raise ValueError(
                    "calibrated pulse amplitude must supply both ref1_voltage_v and ref2_voltage_v"
                )
            ref1_voltage = float(pulse_amplitude["ref1_voltage_v"])
            ref2_voltage = float(pulse_amplitude["ref2_voltage_v"])
            if not all(math.isfinite(v) for v in (ref1_voltage, ref2_voltage)) or not ref1_voltage > ref2_voltage:
                raise ValueError("physical REF levels must satisfy V_REF1 > V_REF2")
            calibrated_levels = {
                "ref1_voltage_v": ref1_voltage,
                "ref2_voltage_v": ref2_voltage,
                "voltage_step_v": ref1_voltage - ref2_voltage,
                "ref_voltage_order": "V_REF1_above_V_REF2",
            }
        for name, value in programmed.items():
            if not self.cfg.set_data(name, value):
                raise RuntimeError(f"failed to set {name}={value}")
            self._global_field_state[name] = int(value)
        return {
            "configuration_method": "Configuration.set_data_native_REF_codes",
            **programmed,
            **calibrated_levels,
            "ref_voltage_lut_status": (
                "calibrated_levels_validated"
                if calibrated_levels
                else "not_supplied_native_codes_preserved"
            ),
        }

    def _decode_selected_counter(self, raw_value: int) -> tuple[int | None, bool]:
        mask = (1 << self.settings.counter_mode_bits) - 1
        selected = int(raw_value) & mask
        if not self.settings.decode_lfsr:
            return selected, True
        decoded = self._decoder.decode(selected, direction=self.settings.lfsr_direction)
        if decoded is None:
            return None, False
        return int(decoded), True

    @staticmethod
    def _contains_transport_error(error: BaseException) -> bool:
        visited: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, (OSError, TimeoutError, ConnectionError)):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _report_status(self, message: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(message)
        except Exception as error:
            logger.warning("Status callback failed: %s", error)

    def _execute_shot_with_recovery(
        self,
        request: ShotRequest,
    ) -> tuple[ShotExecutionResult | int | None, list[dict[str, Any]]]:
        recovery_events: list[dict[str, Any]] = []
        for acquisition_attempt in range(self.settings.upo_reconnect_attempts + 1):
            try:
                self._upo_state_uncertain = True
                logger.debug("GET_SHOT begin: %s/%s, thread=%s",
                             request.measurement_kind, request.acquisition_type,
                             threading.current_thread().name)
                result = self.shot_executor.execute(self.client, request)
                self._upo_state_uncertain = False
                logger.debug("GET_SHOT complete; pixel readout permitted")
                return result, recovery_events
            except BaseException as error:
                retryable = self._contains_transport_error(error) or (
                    "MGPDLab GET_SHOT" in str(error)
                    or "GET_SHOT failed" in str(error)
                )
                if (
                    not retryable
                    or (isinstance(self.shot_executor, KeysightBurstShotExecutor)
                        and (self.shot_executor.upo_command_in_flight or
                             self.shot_executor._active_trigger_thread is not None))
                    or acquisition_attempt >= self.settings.upo_reconnect_attempts
                ):
                    raise
                retry_number = acquisition_attempt + 1
                delay = max(
                    self.settings.upo_reconnect_backoff_s
                    * (2 ** acquisition_attempt),
                    float(request.shutter_duration_s or 0.0) + 0.1,
                )
                logger.warning(
                    "GET_SHOT acquisition failed; waiting %.3f s, reconnecting "
                    "and repeating acquisition %d/%d: %s",
                    delay,
                    retry_number,
                    self.settings.upo_reconnect_attempts,
                    error,
                )
                self._report_status(
                    f"Сбой GET_SHOT: новая попытка {retry_number}/"
                    f"{self.settings.upo_reconnect_attempts} после ожидания "
                    f"{delay:.3f} с"
                )
                time.sleep(delay)
                reconnect_attempt = self.client.reconnect()
                self._restore_programmed_state_after_reconnect()
                self._report_status(
                    f"УПО переподключено, конфигурация ASIC/PX восстановлена; "
                    f"попытка соединения {reconnect_attempt}"
                )
                recovery_events.append(
                    {
                        "timestamp_utc": datetime.now(timezone.utc)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z"),
                        "failed_acquisition_attempt": acquisition_attempt + 1,
                        "retry_number": retry_number,
                        "configured_retry_limit": self.settings.upo_reconnect_attempts,
                        "wait_before_retry_s": delay,
                        "reconnect_attempt_used": reconnect_attempt,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "failed_shot_data_discarded": True,
                    }
                )
        raise RuntimeError("unreachable acquisition retry state")

    def _restore_programmed_state_after_reconnect(self) -> None:
        """Reapply the last acknowledged test state before retrying GET_SHOT."""

        if isinstance(self.shot_executor, UpoPwmShotExecutor):
            # PWM is generated by UPO hardware and may continue after a lost TCP
            # response. Stop it before any register or pixel restoration.
            self.shot_executor.recover_safe_state(self.client)
        if not self._standard_initialization_complete:
            return
        if not self.client.set_fclk(self._initialization_fclk_mhz):
            raise RuntimeError("failed to restore FCLK after UPO reconnect")
        if not self.cfg.set_default():
            raise RuntimeError("failed to restore EO defaults after UPO reconnect")
        for name, value in self._global_field_state.items():
            if EO_cfg.DEFAULT_FIELD_VALUES.get(name) == value:
                continue
            if not self.cfg.set_data(name, value):
                raise RuntimeError(
                    f"failed to restore {name}={value} after UPO reconnect"
                )
        staged = {
            (row, column): self._masked_pixel_word((column, row), raw)
            for (column, row), raw in self._current_pixel_configs.items()
        }
        # Recreate both halves in UPO virtual memory after reconnect. The next
        # GET_SHOT performs the only full-matrix transfer to the chip.
        self._zeroed_matrix.set_owned_half(0x00000000)
        self.matrix.set_pixels(staged)
        for (row, column), raw in staged.items():
            self._current_pixel_configs[(column, row)] = raw
        logger.warning(
            "KIPIX restored and PX staged after reconnect; commit occurs in GET_SHOT"
        )

    @property
    def safe_for_pixel_cleanup(self) -> bool:
        return (
            not self._upo_state_uncertain
            and bool(self.client.connected)
            and not bool(getattr(self.shot_executor, "ctrl_pwm_active", False))
        )

    def acquire(
        self,
        pixels: Sequence[tuple[int, int]],
        request: ShotRequest,
    ) -> tuple[list[dict[str, Any]], ShotExecutionResult]:
        pixels = self.active_pixels(pixels)
        self.validate_pixels(pixels)
        executor_result, recovery_events = self._execute_shot_with_recovery(request)
        if isinstance(executor_result, ShotExecutionResult):
            shot_result = executor_result
        elif executor_result is None:
            shot_result = ShotExecutionResult(
                requested_injections=request.n_injections,
                programmed_injections=None,
                actual_injections=None,
                injections_for_analysis=None,
                injection_count_source="legacy_executor_returned_none",
                details={"executor": type(self.shot_executor).__name__},
            )
        elif isinstance(executor_result, int) and not isinstance(executor_result, bool):
            shot_result = ShotExecutionResult(
                requested_injections=request.n_injections,
                programmed_injections=executor_result,
                actual_injections=executor_result,
                injections_for_analysis=executor_result,
                injection_count_source="legacy_callback_reported_actual_count",
                details={"executor": type(self.shot_executor).__name__},
            )
        else:
            raise RuntimeError("ShotExecutor returned an unsupported result type")
        if recovery_events:
            shot_result = ShotExecutionResult(
                requested_injections=shot_result.requested_injections,
                programmed_injections=shot_result.programmed_injections,
                actual_injections=shot_result.actual_injections,
                injections_for_analysis=shot_result.injections_for_analysis,
                injection_count_source=shot_result.injection_count_source,
                details={
                    **dict(shot_result.details),
                    "upo_acquisition_attempt_count": len(recovery_events) + 1,
                    "upo_recovery_events": recovery_events,
                },
            )
        if request.test_pulses:
            if shot_result.injections_for_analysis is None:
                raise RuntimeError("test-pulse shot did not report an injection denominator")
            if shot_result.injections_for_analysis != request.n_injections:
                raise RuntimeError(
                    f"requested {request.n_injections} injections, executor reported "
                    f"{shot_result.injections_for_analysis} for analysis"
                )

        samples: list[dict[str, Any]] = []
        for column, row in pixels:
            error_text = ""
            result = None
            try:
                result = self.client.get_pixel(row=row, col=column)
                if result is None:
                    raise RuntimeError("GET_PIXEL returned no data")
            except Exception as error:
                error_text = str(error)
                if not self.settings.continue_after_pixel_read_error:
                    raise

            if result is None:
                samples.append(
                    {
                        "column": column,
                        "row": row,
                        "raw_counter_word_hex": "",
                        "counter_low_raw": "",
                        "counter_mid_raw": "",
                        "counter_high_raw": "",
                        "selected_counter_key": self.counter_key,
                        "selected_counter_raw": "",
                        "selected_count": "",
                        "counter_saturated": False,
                        "counter_overflow_behavior": "stops_at_maximum_no_wrap",
                        "maximum_decoded_count": (1 << self.settings.counter_mode_bits) - 2,
                        "counter_decode_valid": False,
                        "measurement_valid": False,
                        "measurement_error": error_text,
                    }
                )
                continue

            selected_raw = int(result[self.counter_key])
            selected_count, decode_valid = self._decode_selected_counter(selected_raw)
            maximum_decoded_count = (1 << self.settings.counter_mode_bits) - 2
            counter_saturated = bool(
                decode_valid and selected_count == maximum_decoded_count
            )
            samples.append(
                {
                    "column": column,
                    "row": row,
                    "raw_counter_word_hex": f"0x{int(result['raw']):016X}",
                    "counter_low_raw": int(result["low"]),
                    "counter_mid_raw": int(result["mid"]),
                    "counter_high_raw": int(result["high"]),
                    "selected_counter_key": self.counter_key,
                    "selected_counter_raw": selected_raw,
                    "selected_count": selected_count if selected_count is not None else "",
                    "counter_saturated": counter_saturated,
                    "counter_overflow_behavior": "stops_at_maximum_no_wrap",
                    "maximum_decoded_count": maximum_decoded_count,
                    "counter_decode_valid": decode_valid,
                    "measurement_valid": bool(decode_valid),
                    "measurement_error": "" if decode_valid else "invalid LFSR counter state",
                }
            )
        return samples, shot_result
