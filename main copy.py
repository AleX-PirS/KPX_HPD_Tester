import time
import csv

from mgpd import MGPDClient
from dac_cfg import DacConfiguration
import osc_v2
from gen import TwoChannelGenerator
from osc import OscilloscopeDCLevelLogger

def save_to_csv(scan_data: dict[int, float], dac_name: str) -> str:
    with open(f"./csv_data/{dac_name}.csv", 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["DAC_code", "Measured_value, V"])
        for code, value in scan_data.items():
            writer.writerow([code, value])
    
    print(f"Saved scan data for {dac_name}.csv")
    return f"{dac_name}.csv"



if __name__ == "__main__":
    rm, osc = osc_v2.prepare_oscilloscope_frame(
        osc_address=None,
        idn_substring="DSO9104H",

        channels=(1, 2, 3),

        trigger_enabled=True,
        trigger_source=3,
        trigger_level_v=0.5,
        trigger_slope="POS",

        average_count=8,

        time_scale_s=1e-6,

        voltage_scale_v=0.25,
        voltage_offset_v=0.0,

        input_modes={
            1: "DC",
            2: "DC50",
            3: "DC50",
        },

        waveform_points=10000,
    )
    
    gen = TwoChannelGenerator()   
    # gen.configure_channel(
    #     channel=1,
    #     shape='DC',
    #     frequency_hz=1,
    #     offset_v=0.3,
    #     amplitude_v=0,
    #     # perec=False,
    #     enable_after_config=True
    # )
    # gen.configure_channel(
    #     channel=2,
    #     shape='SQU',
    #     frequency_hz=100e3,
    #     low_level_v=0,
    #     high_level_v=0.15,
    #     rise_time_s=1e-9,
    #     fall_time_s=1e-9,
    #     enable_after_config=True
    # )

    gen.configure_channel(
        channel=2,
        shape='PULS',
        frequency_hz=100e3,
        offset_v=0,
        amplitude_v=1,
        rise_time_s=1e-9,
        fall_time_s=1e-9,
        pulse_width_s=5e-9,
        enable_after_config=True
    )

    time.sleep(10)

    for i in range(1, 8000, 1):
        # gen.disable_channel(1)
        gen.configure_channel(
            channel=2,
            shape='PULS',
            frequency_hz=10e3,
            offset_v=0,
            amplitude_v=1,
            rise_time_s=1e-9,
            fall_time_s=1e-9,
            pulse_width_s=i*1e-9,
            enable_after_config=True,
        )
        time.sleep(0.25)
        # gen.disable_channel(1)
        # time.sleep(0.5)
        osc_v2.save_oscilloscope_csv(osc, [1, 3], './csv_data/BUF_PULSE', f'{i}.csv')
        # time.sleep(0.01)

    # if KeyboardInterrupt:
    #     gen.disable_channel(1)
    #     gen.disable_channel(2)

    # for i in range(0, 1000, 1):
    #     gen.disable_channel(1)
    #     gen.configure_channel(
    #         channel=2,
    #         shape='SIN',
    #         frequency_hz=1,
    #         offset_v=i/1000,
    #         amplitude_v=0,
    #         perec=False,
    #     )
    #     time.sleep(0.1)
    #     gen.enable_channel(1)
    #     time.sleep(0.5)
    #     osc_data = logger.save()
    #     scan[i] = osc_data
    #     time.sleep(0.01)

    # save_to_csv(scan, 'BUFFER')

    # gen.disable_channel(1)

    # osc_v2.save_oscilloscope_csv(
    #     channels=[1, 2, 3],
    #     filename="exaple.csv",
    #     osc=osc,
    #     output_dir='./csv_data_copy',
    # )