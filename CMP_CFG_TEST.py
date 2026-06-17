import time

from mgpd import MGPDClient
import osc_v2
from gen import TwoChannelGenerator

def write_trim_code(val: int, client: MGPDClient):
    low5 = val & 0x1F
    high3 = (val >> 5) & 0x07

    low_reg = low5 << 3
    print(f"Value: {val:08b}", end='|')
    print(f"low_reg: {low_reg:08b}", end='|')
    print(f"high_reg: {high3:08b}")

    client.write_byte(high3, 0x8021)
    client.write_byte(low_reg, 0x8020)
    return


if __name__ == "__main__":
    with MGPDClient() as client:
        rm, osc = osc_v2.prepare_oscilloscope_frame(
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

        # gen.disable_channel(1)
        # gen.disable_channel(2)

        #------------------------------------------------------------------------------------
        # Начало теста
        #------------------------------------------------------------------------------------

        client.write_byte(0b00000111, 0x8016)
        # client.write_byte(0x10, 0x8017) # my CMP AMUX out
        client.write_byte(0x20, 0x8017)   # alternative CMP AMUX out


        time.sleep(10)
        for i in range(0, 256):
            write_trim_code(i, client)
            time.sleep(6)
            osc_v2.save_oscilloscope_csv(osc, [1,2,3], f'./csv_data_CMP_v2/', f'{i}.csv')