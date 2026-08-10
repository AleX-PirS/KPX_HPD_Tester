import csv
import time
from pathlib import Path
from statistics import fmean, pstdev

from mgpd import MGPDClient
from oscilloscope_cfg import Oscilloscope
from configuration import Configuration
import EO_cfg
from generator_cfg import TwoChannelGenerator


def TEST_CMP_OFFSET(
    client: MGPDClient,

    # Normal comparator measurement
    trigger_source: int = 2,
    trigger_level_v: float = 0.5,
    trigger_slope: str = "POS",
    average_count: int = 2,
    time_scale_s: float = 10e-3,
    time_offset_s: float = 0,

    voltage_scale_v: float = 0.2,
    voltage_offset_v: float = 0.2,
    

    # Generator
    CH_SIN: int = 1,

    # Oscilloscope channel connected to analog TEST_MUX output
    CH_AMUX: int = 1,

    # Comparator DACs
    DAC_CMP_D: int = 512,
    DAC_CMP_BIAS_LSB: int = 512,
    DAC_CMP_BIAS_P: int = 512,
    DAC_CMP_VC5: int = 512,

    # Threshold voltage measurement
    THRESHOLD_AVERAGE_COUNT: int = 32,
    THRESHOLD_SETTLE_DELAY: float = 5.0,
    THRESHOLD_VOLTAGE_SCALE_V: float = 0.2,

    TEST_NAME: str = "CMP",
    PREP_DELAY: float = 2.0,
    AVG_DELAY: float = 0.5,
):
    # ------------------------------------------------------------
    # Output directory
    # ------------------------------------------------------------

    output_dir = Path(
        f"./csv_data_{TEST_NAME}"
        f"_CMPD_{DAC_CMP_D}"
        f"_VB5_{DAC_CMP_BIAS_P}"
        f"_LSB_{DAC_CMP_BIAS_LSB}"
        f"_VC5_{DAC_CMP_VC5}"
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    osc = Oscilloscope()
    gen = TwoChannelGenerator()

    cfg = Configuration(
        client,
        EO_cfg.DEFAULT_REGISTERS,
        EO_cfg.AMUX_SIGNALS,
        EO_cfg.REGS_FIELDS,
        EO_cfg.AMUX_MAP,
    )

    try:
        # ============================================================
        # 1. CONFIGURE COMPARATOR
        # ============================================================

        cfg.set_data("DAC_CMP_BIAS_LSB", DAC_CMP_BIAS_LSB)
        cfg.set_data("DAC_CMP_VB5", DAC_CMP_BIAS_P)
        cfg.set_data("DAC_CMP_VC5", DAC_CMP_VC5)

        # Internal comparator threshold
        cfg.set_data("DAC_CMP_D", DAC_CMP_D)

        # Initial trim
        cfg.set_data("TEST_CONF_CMPD_TR", 0)

        time.sleep(PREP_DELAY)

        # ============================================================
        # 2. MEASURE REAL DAC_CMP_D VOLTAGE
        # ============================================================

        print("\n----- DAC_CMP_D threshold measurement -----")

        # Route DAC_CMP_D to analog test output
        cfg.set_amux("DAC_CMP_D")

        gen.configure_channel(
            channel=CH_SIN,
            shape="SIN",
            frequency_hz=2e2,
            offset_v=0.5,
            amplitude_v=0.98,
            enable_after_config=True,
        )

        # For DC threshold measurement trigger is not required.
        # Use stronger averaging than during comparator sweep.
        osc.configure_frame(
            channels=(CH_AMUX,),

            trigger_enabled=False,

            average_count=THRESHOLD_AVERAGE_COUNT,

            time_scale_s=1e-3,
            time_offset_s=0,

            voltage_scale_v=THRESHOLD_VOLTAGE_SCALE_V,
            voltage_offset_v=0.0,

            input_modes={
                CH_AMUX: "DC",
            },

            waveform_points=20000,
        )

        # Give the DAC, AMUX and oscilloscope averaging time to settle
        time.sleep(THRESHOLD_SETTLE_DELAY)

        threshold_waveform, x_origin, x_increment = osc.read_waveform(CH_AMUX)

        threshold_voltage_v = fmean(threshold_waveform)
        threshold_std_v = pstdev(threshold_waveform)

        print(f"DAC_CMP_D code: {DAC_CMP_D}")
        print(f"Measured threshold: {threshold_voltage_v:.6f} V")
        print(f"Waveform std:       {threshold_std_v:.6f} V")

        # ------------------------------------------------------------
        # Save actual threshold waveform
        # ------------------------------------------------------------

        threshold_waveform_path = output_dir / "threshold_measurement.csv"

        with threshold_waveform_path.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.writer(file)

            writer.writerow([
                "time_s",
                "DAC_CMP_D_voltage_v",
            ])

            for index, voltage in enumerate(threshold_waveform):
                time_s = x_origin + index * x_increment

                writer.writerow([
                    time_s,
                    voltage,
                ])

        # ------------------------------------------------------------
        # Save test metadata
        # ------------------------------------------------------------

        metadata_path = output_dir / "test_info.csv"

        with metadata_path.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.writer(file)

            writer.writerow([
                "parameter",
                "value",
            ])

            writer.writerow([
                "DAC_CMP_D_code",
                DAC_CMP_D,
            ])

            writer.writerow([
                "DAC_CMP_D_voltage_v",
                threshold_voltage_v,
            ])

            writer.writerow([
                "DAC_CMP_D_voltage_std_v",
                threshold_std_v,
            ])

            writer.writerow([
                "DAC_CMP_BIAS_LSB",
                DAC_CMP_BIAS_LSB,
            ])

            writer.writerow([
                "DAC_CMP_VB5",
                DAC_CMP_BIAS_P,
            ])

            writer.writerow([
                "DAC_CMP_VC5",
                DAC_CMP_VC5,
            ])

            writer.writerow([
                "threshold_average_count",
                THRESHOLD_AVERAGE_COUNT,
            ])

        print(f"Threshold waveform saved: {threshold_waveform_path}")
        print(f"Test metadata saved:      {metadata_path}")

        # ============================================================
        # 3. SWITCH AMUX BACK TO COMPARATOR SIGNAL
        # ============================================================

        cfg.set_amux("TST_CMPD")

        time.sleep(PREP_DELAY)

        # ============================================================
        # 4. CONFIGURE GENERATOR
        # ============================================================

        # Only comparator input signal is generated externally now.
        # gen.configure_channel(
        #     channel=CH_SIN,
        #     shape="SIN",
        #     frequency_hz=2e2,
        #     offset_v=0.5,
        #     amplitude_v=0.95,
        #     enable_after_config=True,
        # )

        # ============================================================
        # 5. CONFIGURE OSCILLOSCOPE FOR COMPARATOR TEST
        # ============================================================

        osc.configure_frame(
            channels=(1, 2),

            trigger_enabled=True,
            trigger_source=trigger_source,
            trigger_level_v=trigger_level_v,
            trigger_slope=trigger_slope,

            average_count=average_count,

            voltage_offset_v=voltage_offset_v,
            voltage_scale_v=voltage_scale_v,


            time_scale_s=time_scale_s,
            time_offset_s=time_offset_s,

            input_modes={
                1: "DC",
                2: "DC50",
            },

            waveform_points=20000,
        )

        time.sleep(PREP_DELAY)

        # ============================================================
        # 6. CMPD TRIM SWEEP
        # ============================================================

        print("\n----- CMPD trim sweep -----")

        for code in range(32):
            cfg.set_data("TEST_CONF_CMPD_TR", code)

            print(
                f"Sent trim code {code:02d}, "
                f"VTH={threshold_voltage_v:.6f} V"
            )

            time.sleep(AVG_DELAY)

            osc.save_oscilloscope_csv(
                [1, 2],
                output_dir,
                f"{code}.csv",
            )

    finally:
        # ============================================================
        # 7. CLEANUP
        # ============================================================

        try:
            gen.disable_channel(CH_SIN)
        except Exception:
            pass

        try:
            gen.close()
        except Exception:
            pass

        try:
            osc.close()
        except Exception:
            pass