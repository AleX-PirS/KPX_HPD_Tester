"""Без подключения к стенду показать выбранные REF1/REF2 пары."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from comparator_characterization import (
    load_reference_dac_calibrations,
    plan_reference_dac_pairs,
)

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    config.validate_configuration("ref_preview")
    mode = str(config.REFERENCE.mode).strip().lower()
    if mode == "manual":
        table = pd.DataFrame(
            [
                {
                    "mode": "manual",
                    "equivalent_step_mV": config.REFERENCE.manual_step_mv,
                    "REF1_code": config.REFERENCE.manual_ref1,
                    "REF2_code": config.REFERENCE.manual_ref2,
                    "REF_LUT_used": False,
                }
            ]
        )
        print(table.to_string(index=False))
        return
    if mode != "lut":
        raise ValueError("REFERENCE_MODE must be 'lut' or 'manual'")
    calibrations = load_reference_dac_calibrations(
        config.reference_calibration_files(),
        voltage_unit=config.REFERENCE.lut_voltage_unit,
    )
    plan = plan_reference_dac_pairs(
        calibrations["DAC_TST_REF1"],
        calibrations["DAC_TST_REF2"],
        config.injection_voltage_steps_v(),
        minimum_reference_code=config.REFERENCE.code_limits[0],
        maximum_reference_code=config.REFERENCE.code_limits[1],
        minimum_reference_voltage_v=config.metadata.SCURVE.minimum_reference_voltage_v,
        maximum_reference_step_error_v=config.metadata.SCURVE.maximum_reference_step_error_v,
    )
    selections = plan.selections
    table = pd.DataFrame(
        {
            "requested_step_mV": 1000 * item.requested_voltage_step_v,
            "actual_step_mV": 1000 * item.actual_voltage_step_v,
            "error_mV": 1000 * item.voltage_step_error_v,
            "REF1_code": item.ref1_code,
            "REF1_voltage_V": item.ref1_voltage_v,
            "REF2_code": item.ref2_code,
            "REF2_voltage_V": item.ref2_voltage_v,
            "REF1_fixed_for_all_steps": item.ref1_shared_across_amplitudes,
        }
        for item in selections
    )
    print(table.to_string(index=False))
    unavailable = pd.DataFrame(plan.availability)
    unavailable = unavailable[~unavailable["realizable"].astype(bool)]
    if not unavailable.empty:
        print("\nНедостижимые ступеньки, которые будут исключены из теста:")
        columns = [
            "requested_voltage_step_v",
            "status",
            "global_minimum_achievable_step_error_v",
        ]
        print(unavailable[columns].to_string(index=False))


if __name__ == "__main__":
    main()
