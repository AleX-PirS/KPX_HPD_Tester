import time

from mgpd import MGPDClient
from dac_cfg import DacConfiguration
from osc import OscilloscopeDCLevelLogger

DAC_CODE_DELAY = 0.02
AVG_OSC_DELAY = 1

DAC_TO_TEST = [
    "DAC_CSYS_REF2",
    "DAC_BUF_LB",
    "DAC_BUF_HB",
    "DAC_PBUF_VB6",
    "DAC_CSA_VC2",
    "DAC_CSA_VC1",
    "DAC_CSA_VB1",
    "DAC_CSA_RES_FB",
    "DAC_CMP_L",
    "DAC_CMP_M",
    "DAC_CMP_H",
    "DAC_CMP_VC5",
    "DAC_CMP_BIAS_P",
    "DAC_CMP_BIAS_LSB",
]

def check_DAC(cfg: DacConfiguration, client: MGPDClient, logger: OscilloscopeDCLevelLogger, name: str) -> dict:
    scan = dict()
    
    cfg.set_dac_and_mux(name, 0)
    if cfg.write_to_client(client):
        print(f"DAC Source: {name}")
        
    for code in range (0, 1024):
        time.sleep(DAC_CODE_DELAY)
        cfg.set_dac(name, code)
        if cfg.write_to_client(client):
            print(f"{name} at {code}")
        time.sleep(AVG_OSC_DELAY)

        osc_data = logger.save()
        scan[code] = osc_data
    return scan

with MGPDClient() as client:
    cfg = DacConfiguration()
    logger = OscilloscopeDCLevelLogger()
    
    for DAC_name in DAC_TO_TEST:
        data = check_DAC(cfg, client, logger, DAC_name)

