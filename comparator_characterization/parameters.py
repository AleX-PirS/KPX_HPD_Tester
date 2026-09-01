"""Validated logical KIPIX fields; never raw OMR/ICR/DCR addresses."""
from __future__ import annotations

from collections.abc import Mapping
from numbers import Integral

import EO_cfg


def validate_eo_overrides(values: Mapping[str, int] | None, *, run_scurve: bool) -> dict[str, int]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise TypeError("eo_overrides must be a field -> integer mapping")
    # Thresholds belong to the inner scan/non-limiting policy, REF codes to LUT
    # selection. Reject conflicts rather than silently overwriting user values.
    managed = {f"DAC_CMP_{letter}" for letter in "ABCD"}
    if run_scurve:
        managed.update(("DAC_TST_REF1", "DAC_TST_REF2"))
    result = {}
    for name, value in values.items():
        if name not in EO_cfg.REGS_FIELDS:
            raise ValueError(f"unknown EO_cfg.REGS_FIELDS parameter: {name!r}")
        if name in managed:
            raise ValueError(f"{name} is managed by the threshold/REF scan, not the outer EO sweep")
        fields = EO_cfg.REGS_FIELDS[name]
        if any(not 0x8000 <= address <= 0x803B for address, _, _ in fields):
            raise ValueError(f"{name} is outside KIPIX")
        maximum = (1 << sum(width for _, _, width in fields)) - 1
        if isinstance(value, bool) or not isinstance(value, Integral) or not 0 <= value <= maximum:
            raise ValueError(f"{name} must be an integer in 0..{maximum}")
        result[name] = int(value)
    return result
