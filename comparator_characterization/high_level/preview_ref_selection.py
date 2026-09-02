"""Без подключения к стенду показать выбранные REF1/REF2 пары."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from comparator_characterization import (
    load_reference_dac_calibrations,
    select_reference_dac_pairs,
)

from comparator_characterization.high_level import characterization_config as config


def main() -> None:
    calibrations = load_reference_dac_calibrations(
        config.reference_calibration_files(),
        voltage_unit=config.REFERENCE_LUT_VOLTAGE_UNIT,
    )
    selections = select_reference_dac_pairs(
        calibrations["DAC_TST_REF1"],
        calibrations["DAC_TST_REF2"],
        config.injection_voltage_steps_v(),
        minimum_reference_code=config.MINIMUM_REFERENCE_CODE,
        maximum_reference_code=config.MAXIMUM_REFERENCE_CODE,
        minimum_reference_voltage_v=config.MINIMUM_REFERENCE_VOLTAGE_V,
        preferred_reference_common_mode_v=config.PREFERRED_REFERENCE_COMMON_MODE_V,
        common_mode_step_error_slack_v=(
            config.REFERENCE_COMMON_MODE_STEP_ERROR_SLACK_V
        ),
        maximum_reference_step_error_v=config.MAXIMUM_REFERENCE_STEP_ERROR_V,
    )
    table = pd.DataFrame(
        {
            "requested_step_mV": 1000 * item.requested_voltage_step_v,
            "actual_step_mV": 1000 * item.actual_voltage_step_v,
            "error_mV": 1000 * item.voltage_step_error_v,
            "REF1_code": item.ref1_code,
            "REF1_voltage_V": item.ref1_voltage_v,
            "REF2_code": item.ref2_code,
            "REF2_voltage_V": item.ref2_voltage_v,
            "common_mode_V": item.reference_common_mode_v,
            "common_mode_target_V": item.selected_common_mode_target_v,
        }
        for item in selections
    )
    print(table.to_string(index=False))


if __name__ == "__main__":
    main()
