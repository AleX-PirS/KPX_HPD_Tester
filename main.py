import time
import csv

from mgpd import MGPDClient
from dac_cfg import DacConfiguration
from osc import OscilloscopeDCLevelLogger

AVG_OSC_DELAY = 0.03
SWITCH_DAC_DELAY = 5

DAC_TO_TEST = [
    "DAC_CMP_L",
    "DAC_CMP_M",
    "DAC_CMP_H",
    "DAC_CSA_VC1",
    "DAC_CSA_VC2",
    "DAC_CMP_VC5",
    "DAC_CSA_VB1",
    "DAC_CMP_BIAS_P",
    "DAC_CMP_BIAS_LSB",
    "DAC_CSA_RES_FB",
    "DAC_CSYS_REF2",
    "DAC_BUF_LB",
    "DAC_BUF_HB",
    "DAC_PBUF_VB6",
]

def check_DAC(cfg: DacConfiguration, client: MGPDClient, logger: OscilloscopeDCLevelLogger, name: str) -> dict:
    scan = dict()
    
    cfg.set_dac_and_mux(name, 0)
    if cfg.write_to_client(client):
        print(f"DAC Source: {name}")
    
    time.sleep(SWITCH_DAC_DELAY)   
    for code in range (0, 1024):
        cfg.set_dac(name, code)
        if cfg.write_to_client(client):
            print(f"{name} at {code}")
        time.sleep(AVG_OSC_DELAY)

        osc_data = logger.save()
        scan[code] = osc_data
    return scan


def save_to_csv(scan_data: dict[int, float], dac_name: str) -> str:
    with open(f"./csv_data/{dac_name}.csv", 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["DAC_code", "Measured_value, V"])
        for code, value in scan_data.items():
            writer.writerow([code, value])
    
    print(f"Saved scan data for {dac_name}.csv")
    return f"{dac_name}.csv"

with MGPDClient() as client:
    cfg = DacConfiguration()
    logger = OscilloscopeDCLevelLogger()
    
    for DAC_name in DAC_TO_TEST:
        data = check_DAC(cfg, client, logger, DAC_name)
        save_to_csv(data, DAC_name)

