import time
import csv
from pathlib import Path

from mgpd import MGPDClient
from oscilloscope_cfg import Oscilloscope
from configuration import Configuration
import EO_cfg

AVG_OSC_DELAY = 0.1
SWITCH_DAC_DELAY = 0.5

DAC_TO_TEST = [
    "DAC_TST_REF2",
    "DAC_TST_REF1",
    "DAC_CMP_A",
    "DAC_CMP_B",
    "DAC_CMP_C",
    "DAC_CMP_D",
    "DAC_CMP_BIAS_LSB",
    "DAC_CMP_VB5",
    "DAC_CMP_VC5",
    "DAC_SH_VB4",
    "DAC_SH_VB3",
    "DAC_CSA_VB2",
    "DAC_CSA_VB1",
    "DAC_CSA_RES_FB",
    "DAC_CSA_VREF",
    "DAC_SH_VC4",
    "DAC_SH_VC3",
    "DAC_CSA_VC2",
    "DAC_CSA_VC1",
    "DAC_PFB",
    "DAC_BUF_HB",
    "DAC_BUF_LB",
]

def check_DAC(cfg: Configuration, osc: Oscilloscope, name: str) -> dict:
    scan = dict()
    
    cfg.set_amux(name)
    cfg.set_data(name, 0)
    print(f"DAC Source: {name}")
    
    time.sleep(SWITCH_DAC_DELAY)
    for code in range(0, 128):
        cfg.set_data(name, code)
        time.sleep(AVG_OSC_DELAY)
        osc_data = osc.read_dc_level(1)
        scan[code] = osc_data
        print(f"{name} at {code} = {osc_data:3f}")
    return scan


def save_to_csv(scan_data: dict[int, float], dac_name: str) -> str:
    out_file = Path("EO_all/csv_data_DAC") / f"{dac_name}.csv"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    with open(out_file, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["DAC_code", "Measured_value, V"])
        for code, value in scan_data.items():
            writer.writerow([code, value])

    print(f"Saved scan data for {dac_name}.csv")
    return out_file.name

with MGPDClient() as client:
    cfg = Configuration(
        client=client,
        AMUX_MAP=EO_cfg.AMUX_MAP,
        AMUX_SIGNALS=EO_cfg.AMUX_SIGNALS,
        DEFAULT_REGISTERS=EO_cfg.DEFAULT_REGISTERS, 
        REGS_FIELDS=EO_cfg.REGS_FIELDS,
    )

    cfg.set_default()
    
    osc = Oscilloscope()
    osc.configure_frame(
        channels=(1,),
        trigger_enabled=False,
        average_count=2,
        time_scale_s=50e-9,
        voltage_scale_v=0.25,
        input_modes={
            1: "DC",
        },
    )
    
    for DAC_name in DAC_TO_TEST:
        data = check_DAC(cfg, osc, DAC_name)
        save_to_csv(data, DAC_name)