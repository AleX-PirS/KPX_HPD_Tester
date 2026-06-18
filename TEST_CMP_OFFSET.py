import time

from configuration import Configuration
from mgpd import MGPDClient
import oscilloscope_cfg
import MO_cfg
from generator_cfg import TwoChannelGenerator

IS_ALT_CMP = False
PREP_DELAY = 10
AVG_DELAY = 6

if __name__ == "__main__":
    with MGPDClient() as client:
        rm, osc = oscilloscope_cfg.prepare_oscilloscope_frame(
            osc_address=None,
            idn_substring="DSO9104H",

            channels=(1, 2, 3),

            trigger_enabled=True,
            trigger_source=3,
            trigger_level_v=0.5,
            trigger_slope="POS",

            average_count=4,

            time_scale_s=10e-3,
            time_offset_s=0,

            voltage_scale_v=0.26,
            voltage_offset_v=0.0,

            input_modes={
                1: "DC",
                2: "DC50",
                3: "DC50",
            },

            waveform_points=10000,
        )
    
        gen = TwoChannelGenerator()   
        gen.configure_channel(
            channel=1,
            shape='DC',
            frequency_hz=1,
            offset_v=0.5,
            amplitude_v=0,
            enable_after_config=True
        )
        gen.configure_channel(
            channel=2,
            shape='SIN',
            frequency_hz=2e2,
            offset_v=0.5,
            amplitude_v=0.95,
            enable_after_config=True
        )


        cfg = Configuration(client, MO_cfg.DEFAULT_REGISTERS, MO_cfg.AMUX_SIGNALS, MO_cfg.REGS_FIELDS, MO_cfg.AMUX_MAP)
        if IS_ALT_CMP:
            cfg.set_amux("CMP1_OUT")
        else:
            cfg.set_amux("CMP0_OUT")

        cfg.set_data("DAC_CMP_BIAS_LSB", 350)
        cfg.set_data("DAC_CMP_BIAS_P", 700)
        cfg.set_data("DAC_CMP_VC5", 470)
        cfg.set_data("DAC_BUF50_CALIB", 0)
        cfg.set_data("CSYS_REF2_MUX", 0)

        cfg.set_data("CMPM_TR", 135)

        time.sleep(PREP_DELAY)
        
        for code in range(0, 256):
            cfg.set_data("CMPM_TR", code)
            print(f"Sended trim code {code}")
            time.sleep(AVG_DELAY)
            
            if IS_ALT_CMP:
                oscilloscope_cfg.save_oscilloscope_csv(osc, [1,2,3], f'./csv_data_CMP_ALT/', f'{code}.csv')
            else:
                oscilloscope_cfg.save_oscilloscope_csv(osc, [1,2,3], f'./csv_data_CMP/', f'{code}.csv')            