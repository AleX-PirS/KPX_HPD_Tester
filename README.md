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
