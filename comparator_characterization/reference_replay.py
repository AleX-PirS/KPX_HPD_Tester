"""Validate and replay exact, previously used REF pairs without a new LUT plan."""
from __future__ import annotations

import math
from typing import Sequence

from .calibration import ReferencePairSelection


def replay_reference_amplitudes(selections: Sequence[ReferencePairSelection]) -> tuple[dict, ...]:
    if not selections:
        raise ValueError("replay_reference_selections must not be empty")
    amplitudes, steps = [], set()
    for pair in selections:
        if not isinstance(pair, ReferencePairSelection):
            raise TypeError("REF replay requires ReferencePairSelection objects")
        for code in (pair.ref1_code, pair.ref2_code):
            if not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 1023:
                raise ValueError("replayed REF codes must be integers in 0..1023")
        step = float(pair.actual_voltage_step_v)
        if not math.isfinite(step) or step <= 0 or step in steps:
            raise ValueError("replayed REF steps must be distinct, finite and positive")
        known = [math.isfinite(float(value)) for value in (pair.ref1_voltage_v, pair.ref2_voltage_v)]
        if any(known) and not all(known):
            raise ValueError("replayed REF levels must both be known or both be unavailable")
        if all(known) and not math.isclose(pair.ref1_voltage_v - pair.ref2_voltage_v, step,
                                         rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("replayed REF levels do not match the stored positive step")
        amplitude = pair.to_pulse_amplitude()
        amplitude.update(reference_code_policy="explicit_manual", reference_pair_replayed=True)
        if not all(known):
            amplitude["manual_equivalent_voltage_step"] = True
        amplitudes.append(amplitude)
        steps.add(step)
    return tuple(amplitudes)
