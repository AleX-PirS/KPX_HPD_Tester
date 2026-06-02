import time

from mgpd import MGPDClient
from dac_cfg import DacConfiguration

with MGPDClient() as client:
    cfg = DacConfiguration()
    
    cfg.set_dac_and_mux("DAC_CMP_L", 0)
    for i in range (0, 1024):
        time.sleep(0.5)
        cfg.set_dac("DAC_CMP_L", i)
        if cfg.write_to_client(client):
            print(f"Sended cfg to DAC_CMP_L, val:{i}")
    