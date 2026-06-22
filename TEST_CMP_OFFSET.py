import time

from configuration import Configuration
from mgpd import MGPDClient
import oscilloscope_cfg
import MO_cfg
from generator_cfg import TwoChannelGenerator


def TEST_CMP_OFFSET(
        client: MGPDClient,
        trigger_source: int = 3,
        trigger_level_v: float = 0.5,
        trigger_slope: str = 'POS',
        average_count: int = 4,
        time_scale_s: float = 10e-3,
        time_offset_s: float = 0,

        CH_SIN: int = 2,
        CH_THD: int = 1,
        
        DAC_CMP_BIAS_LSB: int = 350,
        DAC_CMP_BIAS_P: int = 700,
        DAC_CMP_VC5: int = 470,
        TRIM_CODE: list[int] = [0x0F, 0x1E, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0x87, 0x96, 0xA5, 0xB4, 0xC3, 0xD2, 0xE1, 0xF0],

        IS_ALT_CMP: bool = False,
        PREP_DELAY: float = 10,
        AVG_DELAY: float = 6,
):  
    rm, osc = oscilloscope_cfg.prepare_oscilloscope_frame(
        osc_address=None,
        idn_substring="DSO9104H",

        channels=(1, 2, 3),

        trigger_enabled=True,
        trigger_source=trigger_source,
        trigger_level_v=trigger_level_v,
        trigger_slope=trigger_slope,

        average_count=average_count,

        time_scale_s=time_scale_s,
        time_offset_s=time_offset_s,

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
        channel=CH_THD,
        shape='DC',
        frequency_hz=1,
        offset_v=0.5,
        amplitude_v=0,
        enable_after_config=True
    )
    gen.configure_channel(
        channel=CH_SIN,
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

    cfg.set_data("DAC_CMP_BIAS_LSB", DAC_CMP_BIAS_LSB) 
    cfg.set_data("DAC_CMP_BIAS_P", DAC_CMP_BIAS_P)
    cfg.set_data("DAC_CMP_VC5", DAC_CMP_VC5)
    cfg.set_data("DAC_BUF50_CALIB", 0)
    cfg.set_data("CSYS_REF2_MUX", 0)

    cfg.set_data("CMPM_TR", 135)

    time.sleep(PREP_DELAY)
    
    for code in TRIM_CODE:
        cfg.set_data("CMPM_TR", code)
        print(f"Sended trim code {code}")
        time.sleep(AVG_DELAY)
        
        if IS_ALT_CMP:
            oscilloscope_cfg.save_oscilloscope_csv(osc, [1,2,3], f'./csv_data_CMP_ALT_VB5_{DAC_CMP_BIAS_P}_LSB_{DAC_CMP_BIAS_LSB}_VC5_{DAC_CMP_VC5}/', f'{code}.csv')
        else:
            oscilloscope_cfg.save_oscilloscope_csv(osc, [1,2,3], f'./csv_data_CMP_VB5_{DAC_CMP_BIAS_P}_LSB_{DAC_CMP_BIAS_LSB}_VC5_{DAC_CMP_VC5}/', f'{code}.csv')
