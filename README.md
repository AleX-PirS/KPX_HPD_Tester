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
- `Update all`: stage the current editor PX word into all 512 project-owned pixels (Cols 16..31).
- `Write zeros`: stage `0x00000000` into all 1024 physical pixel entries in UPO virtual memory, wait 0.1 s after the final update, then issue `SET_PIXEL_CFG WRITE_TO_CHIP`.
- `Write`: first stages all current `Local edit` pixels, then immediately sends `SET_PIXEL_CFG WRITE_TO_CHIP`.
- `Update all` and `Write` execute immediately without confirmation dialogs.

## Matrix JSON configuration files

The **Matrix** page provides `Save config` and `Load config` below the 32-bit PX
editor controls. Save opens a Windows `Save As` dialog in `configs/` by default
and writes all 512 project-owned pixels as readable named fields rather than as
packed hexadecimal words.

For each pixel, saving uses the first known value in this order: Local edit,
staged UPO value, then value written to the chip in the current GUI session.
Saving is disabled while any pixel remains unknown. Loading validates the file
format, all coordinates, duplicate/missing pixels and every PX field range,
then places the complete configuration in local memory only. It does not send
commands to UPO or the chip. A loaded value matching the known UPO value keeps
its staged/written status; when UPO is unknown, a value matching the known chip
value keeps its written status. Only differing or previously unknown values are
marked `Local edit`; use `Update` or `Write` afterward when required.


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

## Matrix sweep GUI

The `Matrix sweep` page is placed directly below `Visualize` in the sidebar.
It contains independent `Global settings` and `Sweep settings` editors for the
32-bit PX word, one oscilloscope-channel selector, a settling delay and the same
optional `FCLK OFF during capture` behavior as AMUX sweep.

The project-owned matrix half (Cols 16..31, Rows 0..31) can be selected. Selection
uses desktop-style modifiers: normal click replaces the selection, Ctrl+click
toggles individual pixels, Shift+click selects the rectangle between the anchor
and clicked pixel, and Ctrl+Shift adds such a rectangle. An empty explicit
selection means all 512 project-owned pixels.

At sweep start the complete enabled matrix is initialized once with Global settings and
committed to the chip. For each captured pixel the previous sweep pixel is
restored to Global settings and only the current pixel is changed to Sweep
settings before the next commit. Thus each capture has exactly one Sweep pixel
and all other project-owned pixels at Global settings without re-sending 512 identical
SET_PIXEL_CFG commands for every point. After completion (or best-effort cleanup
on error), the complete enabled matrix is returned to Global settings.

Matrix sweep temporarily selects `TST_IN` on AMUX and restores the exact previous
TEST_MUX value afterward. Individual waveforms and `combined.csv` are stored in
`temp/matrix_sweep/`; this directory is overwritten on each new sweep. As in
AMUX sweep, waveform `x_origin` is ignored and a relative time axis is used;
`x_increment` must remain unchanged. The resulting plot and combined CSV can be
saved from the page with Save As dialogs.

## Matrix grouped editing and parameter view

The Matrix page shows the project-owned Col=16..31 half. Both matrix views share desktop-style selection: click replaces the selection, Ctrl+click toggles pixels, Shift+click selects a rectangle from the anchor, and Ctrl+Shift+click adds a rectangle. `Apply local` copies the current editor value to the selected pixels locally; `Update` then stages all Local edits in UPO.

The left Parameter view can show `Groups` (complete 32-bit configuration groups) or one PX field as a dynamically normalized heatmap. Uniform fields are muted in the Field selector, varying fields are emphasized, and `Show only varying fields` hides uniform fields. Available continuous maps are Viridis, Cividis, Plasma, Turbo and Grayscale. Hold Alt while hovering either matrix to inspect a pixel without interfering with selection.

The matrix field formerly named `PX_REG` is now named `PX_MASK` throughout the project; its bit position and default value are unchanged.

## v9 GUI / FCLK updates

- CTRL PWM GUI defaults: 100 kHz, 5000 ns.
- Oscilloscope GUI defaults: edge slope NEG; CH1..CH4 input mode DC / 1 MΩ.
- `DAC_CSA_VB2` and `DAC_CSA_VB2_TR` keep their signal names but are shown under the `Shaper` filter group.
- If the GUI knows FCLK is OFF and chip constants are written (`Apply changes` or `Load defaults`), the controller temporarily restores the last known non-zero FCLK, performs the register writes, and returns FCLK to 0. Pixel-matrix writes keep their existing explicit FCLK guard.
- `Visualize` is now the only visualization sidebar page. The oscilloscope screenshot control stays above an internal `AMUX sweep` / `Matrix sweep` tab selector, and both sweep modes share the large preview and Save figure / Save CSV controls.

## GET_SHOT / GET_PIXEL and OMR helpers

`MGPDClient` also supports the MGPDLab v2.0+ acquisition commands:

```python
from mgpd import MGPDClient

client = MGPDClient(
    configure_omr_before_get_shot=False,  # default: trust MGPDLab/UPO settings
).connect()

ok = client.get_shot()
pixel = client.get_pixel(row=5, col=17)
if pixel is not None:
    print(pixel["raw_hex"], pixel["low"], pixel["mid"], pixel["high"])
```

The following OMR helpers use READ_BYTE -> masked modification -> WRITE_BYTE and
preserve all unrelated OMR bits:

```python
client.set_mode_cnt(0)       # OMR[10]: 0=16-bit, 1=8-bit counters
client.set_mode_read(0b010)  # OMR[7:5], raw 3-bit MODE_READ code
client.set_crw_mode(0)       # OMR[11]: 0=sequential, 1=CRW
```

For optional pre-configuration before `GET_SHOT`:

```python
client.configure_omr_before_get_shot = True
client.get_shot(mode_cnt=0, mode_read=0b010, crw_mode=0)
```

The module-level default is `CONFIGURE_OMR_BEFORE_GET_SHOT_DEFAULT = False`.
DCR and ICR are deliberately not changed by this Python pre-configuration.

## Характеризация компараторов

Простые запускаемые файлы для noise scan, эквализации, полного trim-sweep,
S-кривых, сравнения режимов инжекции и повторного построения графиков находятся
в `comparator_characterization/high_level/`. Все пользовательские пути и
параметры собраны в
`comparator_characterization/high_level/characterization_config.py`.

Полное русскоязычное руководство находится в
`COMPARATOR_CHARACTERIZATION.md`. Начните с команды
`python -m comparator_characterization.high_level.preview_ref_selection`,
которая показывает автоматически выбранные коды REF1/REF2 без подключения к
стенду.

Верхнеуровневые аппаратные тесты перед измерением устанавливают FCLK 50 МГц,
загружают `EO_cfg.DEFAULT_REGISTERS` и стандартно отключают цифровой счет всех
512 принадлежащих пикселей через `PX_MASK=0`. Цифровая часть включается через
`PX_MASK=1` только у выбранных тестом пикселей. Консольный статус дублируется в
`experiment.log`; transient timeout УПО вызывает ограниченное переподключение,
а noise scan умеет завершать пустой хвост после уже найденной активной области.
MGPDLab documents `GET_SHOT` itself as beginning with its own "Load settings"
operation, so its stored UPO/GUI OMR settings should be kept consistent with any
Python pre-configuration.

## Matrix operation / counter readout

The GUI includes a dedicated **Matrix operation** page for MGPDLab `GET_SHOT` and `GET_PIXEL` commands.

- The displayed/read project matrix range is Rows `0..31`, Cols `16..31` (512 pixels).
- `Get shot` sends `GET_SHOT`. The optional checkbox enables direct read-modify-write of only `OMR.MODE_CNT`, `OMR.MODE_READ` and `OMR.CRW_MODE` immediately before the command. DCR and ICR are not directly written by this page.
- `Read selected` calls `GET_PIXEL` for the pixel selected on the counter map.
- `Read all` calls `GET_PIXEL` for all 512 project-owned pixels and updates the heatmap/progress incrementally.
- The counter selector switches the heatmap and histogram between Low, Mid and High 16-bit words returned by MGPDLab. Values are displayed exactly as returned; no physical counter decoding is applied.
- `GET_SHOT` itself is implemented by MGPDLab/UPO and may internally load its configured register image as documented by the vendor.

## Matrix operation histogram and application icon

The Matrix operation page includes a **Show all** checkbox. When enabled, the
Low, Mid and High counter distributions are overlaid on the same histogram with
transparency; the counter selector continues to control the matrix heatmap.

The GUI application/window icon is stored in `gui/app_icon.svg`. On Windows the
launcher also assigns a stable AppUserModelID so the running application uses
this icon instead of the generic Python process icon.

## Характеризация и эквализация компараторов

Высокоуровневый пакет поддерживает AB/CMP_B, BC/CMP_C и CD/CMP_D, сохраняет
отдельный raw CSV для каждого acquisition, допускает resume и изменяет только
выбранное 5-битное trim-поле. Реализованы paired-background S-кривые,
аппаратный burst Keysight 81150A/81160A и сравнение `all`, `tile_2x2`,
`tile_4x4`, `tile_8x8`. Все таблицы и рисунки повторно строятся без стенда.

Настройка, порядок запуска и описание выходных данных приведены в
[COMPARATOR_CHARACTERIZATION.md](COMPARATOR_CHARACTERIZATION.md).
