# Chip Test Stand GUI

Windows-oriented PyQt6 GUI over the existing chip test-stand drivers.

## Compatibility principle

The GUI is an additional layer. The existing direct usage remains valid:

```python
from mgpd import MGPDClient
from configuration import Configuration
from oscilloscope_cfg import Oscilloscope
from generator_cfg import TwoChannelGenerator
```

Existing automated tests do not need to import PyQt or use `StandController`.
The only additions to the drivers are backward-compatible optional tracing hooks,
and `Configuration` gained read/decode helpers (`get_data`, `get_all_data`,
`read_registers`, `get_amux`).

## Run on Windows

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

MGPDLab must be running separately before connecting to the chip.

## GUI workflow

1. Open **Connections** and connect devices manually.
2. On chip connection the GUI performs one complete physical-register read and
   decodes all logical fields.
3. In **Chip**, edit values locally. Changed rows are highlighted. `Apply changes`
   writes only modified logical fields and an AMUX change, if any.
4. `Load defaults` writes `EO_cfg.DEFAULT_REGISTERS` and then re-reads the chip.
5. **Oscilloscope** exposes channel coupling, common/per-channel scale and offset,
   trigger, averaging, DC measurement, CSV capture and screenshot capture.
6. **Generator** controls the two independent source channels.
7. Low-level MGPD/SCPI TX/RX can be enabled in the log panel; it is hidden by
   default.

## Future automated-test integration

Keep test algorithms as normal Python functions using the drivers directly.
A future `Tests` GUI page can submit those functions to the existing serialized
hardware worker without changing their measurement logic.

## GUI theme

The default theme is selected in `main.py`:

```python
APP_THEME = "dark"   # or "light"
```

It can also be overridden at launch without editing the file:

```powershell
python main.py --theme dark
python main.py --theme light
```

The light theme is intentionally neutral rather than pure white/high-contrast.

## Chip table before connection

The register table is always scrollable. Before the chip is connected the
`Value` column is read-only. After connection the GUI reads the chip and enables
editing of logical fields. Hardware writes still occur only through
`Apply changes`.


## v5 table scrolling fix

The register table contains an internal non-data bottom scroll guard. It is ignored by filtering, Apply, dirty-value detection and chip readback, but guarantees that the final real register row can be scrolled fully above the open Log panel.


## Visualization page

The GUI contains a **Visualize** page with two hardware-assisted tools:

- **Oscilloscope screen** - captures the current display into `temp/oscilloscope_screen.png` and shows it directly in the GUI. The file is overwritten on every refresh.
- **AMUX sweep** - switches only the selected `EO_cfg.AMUX_SIGNALS`, waits the configured settling delay, reads one selected oscilloscope channel without changing the oscilloscope setup, stores temporary raw CSV files under `temp/amux_sweep/`, and creates `combined.csv` plus an in-GUI plot. The previous AMUX state is restored after completion or failure.

`temp/` is runtime-only and ignored by Git. The displayed screenshot/figure and the combined sweep CSV can be exported with Save dialogs.


## Matrix / standalone test pixel separation

- Matrix pixel configuration uses independent `PX_*` fields and `SET_PIXEL_CFG`.
- `TEST_CONF_*` registers at `0x8038..0x803B` configure only the standalone test pixel.
- Matrix code does not read, write, or derive its field definitions from those SPI registers.
- `Clear local edits` discards GUI-only matrix edits that were not staged to MGPDLab virtual memory.

## Matrix editing additions

- `Update` stages one selected pixel.
- `Update` stages every pixel currently marked `Local edit`, preserving each pixel's individual 32-bit PX word.
- `Send RAW to selected` accepts a complete hexadecimal 32-bit PX word and stages it directly for the selected pixel.
- Matrix cells show a small `T` marker when `PX_TST_EN=1` and `B` when the active-low `PX_BUF_NEN=0`.

## FCLK / CTRL GUI controls

- The FCLK frequency combo contains only non-zero supported frequencies. `FCLK` has a separate ON/OFF toggle; OFF sends `SET_FCLK 0` and ON uses the selected frequency.
- CTRL static mode has a direct 0/1 toggle. PWM has a separate ON/OFF toggle. Because MGPDLab has no dedicated PWM-OFF command, turning PWM off returns CTRL to the last known static state (0 if unknown).
- AMUX sweep can optionally use `FCLK OFF during capture`: AMUX is selected with clock running, FCLK is forced to 0, the settling delay is applied, the waveform is captured, then FCLK is restored before the next AMUX selection. Unknown FCLK is established/restored as 100 MHz.

## GUI instrument defaults

Oscilloscope GUI defaults: CH1 only, DC 50 ohm coupling selected for all channels, averaging OFF, trigger OFF, trigger level 0.2 V, time scale 250 ns/div, common vertical scale 0.2 V/div, common vertical offset 0.4 V. Generator channel frequency defaults to 100 kHz.


### Matrix GUI action semantics (v4)

- `Load default`: load PX software defaults into the selected editor only.
- `Clear local edits`: discard all edits not yet staged in UPO.
- `Update`: stage all pixels currently marked `Local edit` in UPO.
- `Update all`: stage the current editor PX word into all 512 owned pixels (Cols 16..31).
- `Write`: first stages all current `Local edit` pixels, then immediately sends `SET_PIXEL_CFG WRITE_TO_CHIP`.
- `Update all` and `Write` execute immediately without confirmation dialogs.


## OMR chip-control helpers

`MGPDClient` includes opt-in read-modify-write helpers for selected Operation Mode Register bits:

```python
client.set_puf_mode(0)       # OMR[20]
client.set_puf_mode(1)

client.set_win_dis_mode(0)   # OMR[9]
client.set_win_dis_mode(1)

client.set_polarity(0)       # OMR[24] = 0, OMR[19] POL_CTRL = 1
client.set_polarity(1)       # OMR[24] = 1, OMR[19] POL_CTRL = 1
```

These functions are not called automatically by connection, GUI setup, or other configuration code. Each operation first reads the affected byte and then changes only its masked bit(s), preserving all unrelated OMR fields.
