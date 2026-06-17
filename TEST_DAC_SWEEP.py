import time
import csv

from configuration import Configuration
from mgpd import MGPDClient
import oscilloscope_cfg
import MO_cfg

AVG_OSC_DELAY = 0.4
SWITCH_DAC_DELAY = 10

DAC_TO_TEST = [
    "DAC_CMP_H",
    "DAC_CMP_M",
    "DAC_CMP_L",
    "DAC_CSA_VC1",
    "DAC_CSA_VC2",
    "DAC_CMP_VC5",
    "DAC_CSA_VB1",
    "DAC_CMP_BIAS_P",
    "DAC_CMP_BIAS_LSB",
    "DAC_CSA_RES_FB",
    "DAC_CSYS_REF2",
    "DAC_PBUF_VB6",
    "DAC_BUF_LB",
    "DAC_BUF_HB",
]

def save_to_csv(scan_data: dict[int, float], dac_name: str) -> str:
    with open(f"./DAC_csv_data/{dac_name}.csv", 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["DAC_code", "Measured_value, V"])
        for code, value in scan_data.items():
            writer.writerow([code, value])
    
    print(f"Saved scan data for {dac_name}.csv")
    return f"{dac_name}.csv"

if __name__ == "__main__":
    with MGPDClient() as client:
        cfg = Configuration(client, MO_cfg.DEFAULT_REGISTERS, MO_cfg.AMUX_SIGNALS, MO_cfg.REGS_FIELDS, MO_cfg.AMUX_MAP)
        osc = oscilloscope_cfg.prepare_oscilloscope_frame(
            channels=(1),
            trigger_enabled=False,
            average_count=8,
            time_scale_s=5e-5,
            time_offset_s=0,
            voltage_scale_v=0.26,
            input_modes={
                1: "DC",
            },
            waveform_points=10000,
        )
        
        for DAC_name in DAC_TO_TEST:
            scan = dict()
            
            cfg.set_amux(DAC_name)
            print(f"DAC source: {DAC_name}")
            time.sleep(SWITCH_DAC_DELAY)

            for code in range (0, 1024):
                if cfg.set_data(DAC_name, code):
                    print(f"{DAC_name} at {code}.")
                time.sleep(AVG_OSC_DELAY)

                osc_data = oscilloscope_cfg.read_dc_level(osc, 1)
                scan[code] = osc_data

            save_to_csv(scan, DAC_name)

