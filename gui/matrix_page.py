from __future__ import annotations

from collections import Counter

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from pixel_matrix import (
    DEFAULT_PIXEL_CONFIG,
    MATRIX_ROWS,
    OWNED_COLUMNS,
    PIXEL_CODEC,
)
from .styles import current_theme_colors
from .widgets import Card


# Continuous maps are intentionally defined locally rather than depending on a
# matplotlib runtime inside the Matrix page. The points below are compact
# approximations of familiar scientific colour maps and are sufficient for the
# 16x32 diagnostic heatmap.
CONTINUOUS_COLORMAPS: dict[str, tuple[str, ...]] = {
    "Viridis": ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"),
    "Cividis": ("#00224e", "#35456c", "#666970", "#a59c74", "#fee838"),
    "Plasma": ("#0d0887", "#7e03a8", "#cc4778", "#f89540", "#f0f921"),
    "Turbo": ("#30123b", "#4667d8", "#1ac7c2", "#a4fc3c", "#f9ba38", "#e2442f", "#7a0403"),
    "Grayscale": ("#171717", "#f2f2f2"),
}

# Categorical colours are deliberately independent of the selected continuous
# colour map. Groups represent equality of the complete 32-bit PX word, not a
# numeric ordering.
GROUP_COLORS = (
    "#4c78a8",
    "#f58518",
    "#54a24b",
    "#e45756",
    "#72b7b2",
    "#b279a2",
    "#ff9da6",
    "#9d755d",
    "#bab0ac",
    "#8f63b8",
    "#2e9fdf",
    "#d4a72c",
)


def _display_field_name(name: str) -> str:
    return name[3:] if name.startswith("PX_") else name


def _interpolate_color(stops: tuple[str, ...], position: float) -> QColor:
    if not stops:
        return QColor("#808080")
    if len(stops) == 1:
        return QColor(stops[0])

    t = max(0.0, min(float(position), 1.0))
    scaled = t * (len(stops) - 1)
    index = min(int(scaled), len(stops) - 2)
    local_t = scaled - index

    a = QColor(stops[index])
    b = QColor(stops[index + 1])
    return QColor(
        round(a.red() + (b.red() - a.red()) * local_t),
        round(a.green() + (b.green() - a.green()) * local_t),
        round(a.blue() + (b.blue() - a.blue()) * local_t),
    )


class HeatmapScale(QWidget):
    """Compact vertical colour scale for the selected PX field."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._minimum = 0
        self._maximum = 0
        self._stops = CONTINUOUS_COLORMAPS["Viridis"]
        self.setMinimumWidth(58)
        self.setMaximumWidth(76)
        self.setMinimumHeight(220)

    def set_scale(self, minimum: int, maximum: int, colormap_name: str):
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._stops = CONTINUOUS_COLORMAPS[colormap_name]
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        c = current_theme_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        top = 28.0
        bottom = 28.0
        bar_width = 18.0
        bar_height = max(self.height() - top - bottom, 1.0)
        left = max((self.width() - bar_width) / 2.0, 4.0)
        rect = QRectF(left, top, bar_width, bar_height)

        # Highest field value is at the top of the colour bar, matching the
        # conventional vertical heatmap legend orientation.
        gradient = QLinearGradient(0, rect.bottom(), 0, rect.top())
        if len(self._stops) == 1:
            gradient.setColorAt(0.0, QColor(self._stops[0]))
            gradient.setColorAt(1.0, QColor(self._stops[0]))
        else:
            for index, stop in enumerate(self._stops):
                gradient.setColorAt(index / (len(self._stops) - 1), QColor(stop))

        painter.fillRect(rect, QBrush(gradient))
        border_pen = QPen(QColor(c["input_border"]))
        border_pen.setWidthF(1.0)
        painter.setPen(border_pen)
        painter.drawRect(rect)

        painter.setPen(QColor(c["muted"]))
        painter.drawText(
            QRectF(0, 2, self.width(), 20),
            Qt.AlignmentFlag.AlignCenter,
            str(self._maximum),
        )
        painter.drawText(
            QRectF(0, self.height() - 22, self.width(), 20),
            Qt.AlignmentFlag.AlignCenter,
            str(self._minimum),
        )
        painter.end()


class OwnedMatrixMap(QWidget):
    """Interactive view of only the project-owned 16x32 matrix half."""

    pixel_clicked = pyqtSignal(int, int, object)  # row, col, keyboard modifiers

    def __init__(self, mode: str, parent=None):
        super().__init__(parent)
        if mode not in ("heatmap", "status"):
            raise ValueError("mode must be 'heatmap' or 'status'")
        self.mode = mode
        self._color_provider = None
        self._marker_provider = None
        self._hover_provider = None
        self._selection_provider = None
        self._active_provider = None
        self.setMinimumSize(270, 460)
        self.setMouseTracking(True)
        self.setToolTip("")

    def set_color_provider(self, provider):
        self._color_provider = provider
        self.update()

    def set_marker_provider(self, provider):
        self._marker_provider = provider
        self.update()

    def set_hover_provider(self, provider):
        self._hover_provider = provider

    def set_selection_provider(self, provider):
        self._selection_provider = provider
        self.update()

    def set_active_provider(self, provider):
        self._active_provider = provider
        self.update()

    @staticmethod
    def _visual_row(row: int) -> int:
        """Map logical Row=0 to the bottom and Row=31 to the top."""
        return MATRIX_ROWS - 1 - int(row)

    @staticmethod
    def _logical_row(visual_row: int) -> int:
        return MATRIX_ROWS - 1 - int(visual_row)

    def _geometry(self):
        left = 38.0
        top = 27.0
        right = 8.0
        bottom = 8.0
        columns = len(OWNED_COLUMNS)
        available_w = max(self.width() - left - right, 1.0)
        available_h = max(self.height() - top - bottom, 1.0)
        cell = min(available_w / columns, available_h / MATRIX_ROWS)
        grid_w = cell * columns
        grid_h = cell * MATRIX_ROWS
        x0 = left + max((available_w - grid_w) / 2.0, 0.0)
        y0 = top + max((available_h - grid_h) / 2.0, 0.0)
        return x0, y0, cell, grid_w, grid_h

    def _pixel_at(self, position) -> tuple[int, int] | None:
        x0, y0, cell, grid_w, grid_h = self._geometry()
        x = position.x()
        y = position.y()
        if not (x0 <= x < x0 + grid_w and y0 <= y < y0 + grid_h):
            return None
        local_col = int((x - x0) // cell)
        visual_row = int((y - y0) // cell)
        row = self._logical_row(visual_row)
        col = min(OWNED_COLUMNS) + local_col
        if col not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
            return None
        return row, col

    def paintEvent(self, event):
        super().paintEvent(event)
        c = current_theme_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        x0, y0, cell, grid_w, grid_h = self._geometry()
        selection = (
            set(self._selection_provider())
            if self._selection_provider is not None
            else set()
        )
        active = self._active_provider() if self._active_provider is not None else None

        fallback = QColor(c["table_alt"])
        for row in range(MATRIX_ROWS):
            for local_col, col in enumerate(OWNED_COLUMNS):
                rect = QRectF(
                    x0 + local_col * cell,
                    y0 + self._visual_row(row) * cell,
                    cell,
                    cell,
                )
                color = fallback
                if self._color_provider is not None:
                    provided = self._color_provider(row, col)
                    if isinstance(provided, QColor):
                        color = provided
                    elif provided:
                        color = QColor(provided)
                painter.fillRect(rect, color)

        # Selection is an overlay, not a replacement colour, so both heatmap
        # information and configuration status remain readable.
        if selection:
            overlay = QColor(c["selection"])
            overlay.setAlpha(95)
            painter.setPen(Qt.PenStyle.NoPen)
            for row, col in selection:
                if col not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
                    continue
                local_col = col - min(OWNED_COLUMNS)
                rect = QRectF(
                    x0 + local_col * cell + 0.6,
                    y0 + self._visual_row(row) * cell + 0.6,
                    max(cell - 1.2, 1.0),
                    max(cell - 1.2, 1.0),
                )
                painter.fillRect(rect, overlay)

        grid_pen = QPen(QColor(c["table_grid"]))
        grid_pen.setWidthF(0.7)
        painter.setPen(grid_pen)
        for index in range(len(OWNED_COLUMNS) + 1):
            x = x0 + index * cell
            painter.drawLine(QPointF(x, y0), QPointF(x, y0 + grid_h))
        for index in range(MATRIX_ROWS + 1):
            y = y0 + index * cell
            painter.drawLine(QPointF(x0, y), QPointF(x0 + grid_w, y))

        # T/B markers are useful on the status map but intentionally omitted
        # from the heatmap to keep parameter contrast visually clean.
        if self.mode == "status" and self._marker_provider is not None and cell >= 7.0:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            marker_font = QFont(self.font())
            marker_font.setPixelSize(max(5, min(8, int(cell * 0.50))))
            marker_font.setBold(True)
            painter.setFont(marker_font)
            painter.setPen(QColor(c["text_strong"]))
            for row in range(MATRIX_ROWS):
                for col in OWNED_COLUMNS:
                    tst_enabled, buf_marker = self._marker_provider(row, col)
                    if not tst_enabled and not buf_marker:
                        continue
                    local_col = col - min(OWNED_COLUMNS)
                    x = x0 + local_col * cell
                    y = y0 + self._visual_row(row) * cell
                    half = cell / 2.0
                    if tst_enabled:
                        painter.drawText(
                            QRectF(x + 0.5, y, half, cell),
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                            "T",
                        )
                    if buf_marker:
                        painter.drawText(
                            QRectF(x + half - 0.5, y, half - 0.5, cell),
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                            "B",
                        )
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)

        if active is not None:
            row, col = active
            if col in OWNED_COLUMNS and 0 <= row < MATRIX_ROWS:
                local_col = col - min(OWNED_COLUMNS)
                rect = QRectF(
                    x0 + local_col * cell + 1.0,
                    y0 + self._visual_row(row) * cell + 1.0,
                    max(cell - 2.0, 1.0),
                    max(cell - 2.0, 1.0),
                )
                pen = QPen(QColor(c["text_strong"]))
                pen.setWidthF(max(1.8, min(cell * 0.18, 3.5)))
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect)

        painter.setPen(QColor(c["muted"]))
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, min(9.0, cell * 0.55)))
        painter.setFont(font)
        for col in (0, 4, 8, 12, 15):
            local_col = col - min(OWNED_COLUMNS)
            center_x = x0 + (local_col + 0.5) * cell
            painter.drawText(
                QRectF(center_x - 18, y0 - 24, 36, 18),
                Qt.AlignmentFlag.AlignCenter,
                str(col),
            )
        for row in (0, 8, 16, 24, 31):
            center_y = y0 + (self._visual_row(row) + 0.5) * cell
            painter.drawText(
                QRectF(x0 - 34, center_y - 9, 29, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(row),
            )
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return super().mousePressEvent(event)
        pixel = self._pixel_at(event.position())
        if pixel is None:
            return
        row, col = pixel
        self.pixel_clicked.emit(row, col, event.modifiers())

    def mouseMoveEvent(self, event):
        # Normal hover stays silent because hover tooltips interfere with pixel
        # selection. Hold Alt explicitly to inspect a pixel configuration.
        modifiers = event.modifiers()
        if not bool(modifiers & Qt.KeyboardModifier.AltModifier):
            QToolTip.hideText()
            return super().mouseMoveEvent(event)

        pixel = self._pixel_at(event.position())
        if pixel is None or self._hover_provider is None:
            QToolTip.hideText()
            return super().mouseMoveEvent(event)

        text = self._hover_provider(*pixel)
        if text:
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        return super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)


class MatrixPage(QWidget):
    """Editor and visual analyser for project-owned Col=0..15 pixels."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self._values: dict[tuple[int, int], int] = {}
        self._upo_values: dict[tuple[int, int], int] = {}
        self._chip_values: dict[tuple[int, int], int] = {}
        self._local_dirty: set[tuple[int, int]] = set()
        self._local_baseline: dict[tuple[int, int], int] = {}
        self._updating_editor = False
        self._connected = False

        self._selection: set[tuple[int, int]] = {(0, min(OWNED_COLUMNS))}
        self._anchor: tuple[int, int] | None = (0, min(OWNED_COLUMNS))
        self._active_coord: tuple[int, int] = (0, min(OWNED_COLUMNS))
        self._field_selector_refreshing = False
        self._group_cache: dict[int, dict] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 20)
        root.setSpacing(10)

        title = QLabel("Pixel matrix")
        title.setObjectName("Title")
        root.addWidget(title)

        subtitle = QLabel(
            "Project-owned matrix half, Col=0..15. Use Ctrl/Shift selection for grouped local edits; "
            "the two maps show parameter differences and write status independently."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(5)
        root.addWidget(split, 1)

        # ----------------------------------------------------------- heatmap
        heat_card = Card("Parameter view")
        heat_card.setMinimumWidth(420)

        # Keep both matrix cards aligned: their header areas have the same
        # height and the matrix/legend bodies therefore start on one level.
        heat_header = QWidget()
        heat_header.setFixedHeight(122)
        heat_header_layout = QVBoxLayout(heat_header)
        heat_header_layout.setContentsMargins(0, 0, 0, 0)
        heat_header_layout.setSpacing(6)

        heat_controls = QGridLayout()
        heat_controls.setHorizontalSpacing(8)
        heat_controls.setVerticalSpacing(6)
        self.field_filter = QComboBox()
        self.colormap = QComboBox()
        self.colormap.addItems(list(CONTINUOUS_COLORMAPS))
        self.colormap.setCurrentText("Viridis")
        self.only_varying = QCheckBox("Show only varying fields")
        heat_controls.addWidget(QLabel("Field"), 0, 0)
        heat_controls.addWidget(self.field_filter, 0, 1)
        heat_controls.addWidget(QLabel("Colormap"), 1, 0)
        heat_controls.addWidget(self.colormap, 1, 1)
        heat_controls.addWidget(self.only_varying, 2, 0, 1, 2)
        heat_header_layout.addLayout(heat_controls)

        self.heat_summary = QLabel()
        self.heat_summary.setObjectName("Muted")
        heat_header_layout.addWidget(self.heat_summary)
        heat_card.layout_.addWidget(heat_header)

        heat_body = QHBoxLayout()
        heat_body.setContentsMargins(0, 0, 0, 0)
        heat_body.setSpacing(8)

        self.heatmap_map = OwnedMatrixMap("heatmap")
        self.heatmap_map.set_color_provider(self.heatmap_color)
        self.heatmap_map.set_selection_provider(self.selected_coordinates)
        self.heatmap_map.set_active_provider(self.current_coordinate)
        self.heatmap_map.set_hover_provider(self.pixel_hover_text)
        self.heatmap_map.pixel_clicked.connect(self._matrix_clicked)
        heat_body.addWidget(self.heatmap_map, 1)

        # All Parameter-view legends live beside the matrix. This keeps the
        # matrix height independent of the number of configuration groups.
        heat_legend_panel = QWidget()
        heat_legend_panel.setFixedWidth(160)
        heat_legend_layout = QVBoxLayout(heat_legend_panel)
        heat_legend_layout.setContentsMargins(2, 0, 0, 0)
        heat_legend_layout.setSpacing(6)

        self.heat_scale = HeatmapScale()
        heat_legend_layout.addWidget(self.heat_scale, 1, Qt.AlignmentFlag.AlignHCenter)

        self.group_legend = QLabel()
        self.group_legend.setObjectName("Muted")
        self.group_legend.setWordWrap(True)
        self.group_legend.setTextFormat(Qt.TextFormat.RichText)
        self.group_legend.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self.group_legend_scroll = QScrollArea()
        self.group_legend_scroll.setWidgetResizable(True)
        self.group_legend_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.group_legend_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.group_legend_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.group_legend_scroll.setWidget(self.group_legend)
        heat_legend_layout.addWidget(self.group_legend_scroll, 1)

        heat_body.addWidget(heat_legend_panel)
        heat_card.layout_.addLayout(heat_body, 1)
        split.addWidget(heat_card)

        # ------------------------------------------------------------ status
        status_card = Card("Configuration status")
        status_card.setMinimumWidth(420)

        status_header = QWidget()
        status_header.setFixedHeight(122)
        status_header_layout = QVBoxLayout(status_header)
        status_header_layout.setContentsMargins(0, 0, 0, 0)
        status_header_layout.setSpacing(6)

        selected_row = QHBoxLayout()
        self.coord_label = QLabel("Col=0 Row=0")
        self.coord_label.setObjectName("SectionTitle")
        self.selection_label = QLabel("Selected: 1")
        self.selection_label.setObjectName("Muted")
        selected_row.addWidget(self.coord_label)
        selected_row.addStretch(1)
        selected_row.addWidget(self.selection_label)
        status_header_layout.addLayout(selected_row)

        self.pixel_status = QLabel("Unknown - protocol provides no per-pixel readback")
        self.pixel_status.setObjectName("Muted")
        self.pixel_status.setWordWrap(True)
        status_header_layout.addWidget(self.pixel_status)

        selection_note = QLabel(
            "Click: one pixel. Ctrl: add/remove. Shift: rectangle. "
            "Ctrl+Shift: add rectangle. Alt+hover: inspect."
        )
        selection_note.setObjectName("Muted")
        selection_note.setWordWrap(True)
        status_header_layout.addWidget(selection_note)
        status_card.layout_.addWidget(status_header)

        status_body = QHBoxLayout()
        status_body.setContentsMargins(0, 0, 0, 0)
        status_body.setSpacing(8)

        self.status_map = OwnedMatrixMap("status")
        self.status_map.set_color_provider(self.status_color)
        self.status_map.set_marker_provider(self.pixel_markers)
        self.status_map.set_selection_provider(self.selected_coordinates)
        self.status_map.set_active_provider(self.current_coordinate)
        self.status_map.set_hover_provider(self.pixel_hover_text)
        self.status_map.pixel_clicked.connect(self._matrix_clicked)
        status_body.addWidget(self.status_map, 1)

        status_legend_panel = QWidget()
        status_legend_panel.setFixedWidth(160)
        status_legend_layout = QVBoxLayout(status_legend_panel)
        status_legend_layout.setContentsMargins(2, 0, 0, 0)
        status_legend_layout.setSpacing(8)

        status_legend_title = QLabel("Status")
        status_legend_title.setObjectName("SectionTitle")
        status_legend_layout.addWidget(status_legend_title)

        self.legend_unknown = QLabel("Unknown")
        self.legend_unknown.setObjectName("MatrixLegendUnknown")
        self.legend_local = QLabel("Local edit")
        self.legend_local.setObjectName("MatrixLegendLocal")
        self.legend_staged = QLabel("Staged in UPO")
        self.legend_staged.setObjectName("MatrixLegendStaged")
        self.legend_written = QLabel("Written to chip")
        self.legend_written.setObjectName("MatrixLegendWritten")
        status_legend_layout.addWidget(self.legend_unknown)
        status_legend_layout.addWidget(self.legend_local)
        status_legend_layout.addWidget(self.legend_staged)
        status_legend_layout.addWidget(self.legend_written)

        marker_legend = QLabel("T: TST_EN=1\nB: BUF_NEN=0")
        marker_legend.setObjectName("Muted")
        marker_legend.setWordWrap(True)
        status_legend_layout.addSpacing(6)
        status_legend_layout.addWidget(marker_legend)
        status_legend_layout.addStretch(1)

        status_body.addWidget(status_legend_panel)
        status_card.layout_.addLayout(status_body, 1)
        split.addWidget(status_card)

        # --------------------------------------------------------- right tools
        controls = QWidget()
        controls.setMinimumWidth(350)
        controls.setMaximumWidth(470)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 8, 0)
        controls_layout.setSpacing(12)

        field_card = Card("32-bit PX configuration")
        fields_grid = QGridLayout()
        fields_grid.setHorizontalSpacing(10)
        field_card.layout_.addLayout(fields_grid)

        self.field_edits: dict[str, QSpinBox] = {}
        field_names = list(PIXEL_CODEC.field_names)
        split_index = (len(field_names) + 1) // 2
        for index, name in enumerate(field_names):
            column_group = 0 if index < split_index else 1
            form_key = f"form_{column_group}"
            if not hasattr(self, form_key):
                form = QFormLayout()
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                setattr(self, form_key, form)
                fields_grid.addLayout(form, 0, column_group)
            else:
                form = getattr(self, form_key)

            width = PIXEL_CODEC.width(name)
            edit = QSpinBox()
            edit.setRange(0, (1 << width) - 1)
            edit.setToolTip(f"{_display_field_name(name)} range: 0..{(1 << width) - 1}")
            edit.valueChanged.connect(self._editor_changed)
            getattr(self, form_key).addRow(_display_field_name(name), edit)
            self.field_edits[name] = edit

        raw_row = QHBoxLayout()
        raw_row.addWidget(QLabel("Raw 32-bit value"))
        self.raw_value = QLineEdit("0x00000000")
        self.raw_value.setPlaceholderText("0x00000000")
        self.raw_value.setMaxLength(10)
        self.raw_value.setToolTip(
            "Complete 32-bit PX word in hexadecimal, for example 0x1234ABCD."
        )
        self.send_raw = QPushButton("Send RAW to selected")
        self.send_raw.setToolTip(
            "Stage this exact 32-bit word for the active pixel in MGPDLab virtual memory."
        )
        raw_row.addWidget(self.raw_value, 1)
        raw_row.addWidget(self.send_raw)
        field_card.layout_.addLayout(raw_row)

        editor_buttons = QGridLayout()
        self.load_defaults = QPushButton("Load default")
        self.load_defaults.setObjectName("NeutralButton")
        self.load_defaults.setToolTip("Load software PX defaults into the active pixel editor.")

        self.stage_local = QPushButton("Update")
        self.stage_local.setObjectName("PrimaryButton")
        self.stage_local.setToolTip(
            "Stage every pixel currently marked Local edit, preserving each pixel's own value."
        )

        self.apply_local = QPushButton("Apply local")
        self.apply_local.setToolTip(
            "Copy the current editor configuration to every selected pixel as Local edit. "
            "No hardware command is sent."
        )

        self.clear_local = QPushButton("Clear local edits")
        self.clear_local.setObjectName("DangerButton")
        self.clear_local.setToolTip(
            "Discard all PX edits that have not been sent to MGPDLab virtual memory."
        )

        editor_buttons.addWidget(self.load_defaults, 0, 0, 1, 3)
        editor_buttons.addWidget(self.stage_local, 1, 0)
        editor_buttons.addWidget(self.apply_local, 1, 1)
        editor_buttons.addWidget(self.clear_local, 1, 2)
        field_card.layout_.addLayout(editor_buttons)
        controls_layout.addWidget(field_card)

        operations = Card("Matrix operations")
        note = QLabel(
            "Update all sends the current editor value to all 512 owned pixels. "
            "Write first stages all Local edits, then commits the virtual matrix to the chip."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        operations.layout_.addWidget(note)

        op_buttons = QHBoxLayout()
        self.stage_all = QPushButton("Update all")
        self.write_chip = QPushButton("Write")
        self.write_chip.setObjectName("PrimaryButton")
        op_buttons.addWidget(self.stage_all)
        op_buttons.addWidget(self.write_chip)
        operations.layout_.addLayout(op_buttons)

        self.progress = QProgressBar()
        self.progress.setRange(0, MATRIX_ROWS * len(OWNED_COLUMNS))
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        operations.layout_.addWidget(self.progress)

        self.write_zeros = QPushButton("Write zeros")
        self.write_zeros.setObjectName("NeutralButton")
        self.write_zeros.setToolTip(
            "Stage 0x00000000 into all 1024 pixels of MGPDLab virtual matrix memory, "
            "wait 0.1 s, then send WRITE_TO_CHIP."
        )
        operations.layout_.addWidget(self.write_zeros)

        commit_note = QLabel(
            "Normal Matrix operations modify only Col=0..15 in MGPDLab virtual memory. "
            "Write zeros is the explicit exception: it stages zero into all 1024 virtual pixels, "
            "waits 0.1 s, and then commits the complete virtual matrix to the chip."
        )
        commit_note.setObjectName("Muted")
        commit_note.setWordWrap(True)
        operations.layout_.addWidget(commit_note)
        controls_layout.addWidget(operations)
        controls_layout.addStretch(1)

        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setWidget(controls)
        controls_scroll.setMinimumWidth(360)
        controls_scroll.setMaximumWidth(490)
        split.addWidget(controls_scroll)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([455, 455, 410])

        # ------------------------------------------------------------ signals
        self.load_defaults.clicked.connect(self.load_defaults_to_editor)
        self.apply_local.clicked.connect(self.apply_editor_to_selection)
        self.clear_local.clicked.connect(self.clear_local_edits)
        self.field_filter.currentIndexChanged.connect(self._heatmap_mode_changed)
        self.colormap.currentIndexChanged.connect(self._heatmap_mode_changed)
        self.only_varying.toggled.connect(self._refresh_field_selector)

        self.reset_session()
        self.set_connected(False)

    # ---------------------------------------------------------------- state

    def set_connected(self, connected: bool):
        self._connected = bool(connected)
        for widget in (
            self.stage_local,
            self.send_raw,
            self.stage_all,
            self.write_chip,
            self.write_zeros,
        ):
            widget.setEnabled(self._connected)

    def reset_session(self):
        self._values = {
            (row, col): DEFAULT_PIXEL_CONFIG
            for row in range(MATRIX_ROWS)
            for col in OWNED_COLUMNS
        }
        self._upo_values.clear()
        self._chip_values.clear()
        self._local_dirty.clear()
        self._local_baseline = dict(self._values)
        self._selection = {(0, min(OWNED_COLUMNS))}
        self._anchor = (0, min(OWNED_COLUMNS))
        self._active_coord = (0, min(OWNED_COLUMNS))
        self.progress.setRange(0, MATRIX_ROWS * len(OWNED_COLUMNS))
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        self.select_pixel(*self._active_coord, preserve_selection=True)
        self._refresh_field_selector()
        self._refresh_visualization()

    def current_coordinate(self) -> tuple[int, int]:
        return self._active_coord

    def selected_coordinates(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted(self._selection))

    def current_raw_value(self) -> int:
        values = {name: edit.value() for name, edit in self.field_edits.items()}
        return PIXEL_CODEC.pack(values)

    def raw_input_value(self) -> int:
        text = self.raw_value.text().strip().replace("_", "")
        if not text:
            raise ValueError("RAW pixel configuration is empty")
        try:
            raw = int(text[2:] if text.lower().startswith("0x") else text, 16)
        except ValueError as error:
            raise ValueError(
                "RAW pixel configuration must be a 32-bit hexadecimal value such as 0x1234ABCD"
            ) from error
        PIXEL_CODEC.validate_raw(raw)
        return raw

    def local_edits(self) -> dict[tuple[int, int], int]:
        return {
            coord: self._values[coord]
            for coord in sorted(self._local_dirty)
        }

    def pixel_markers(self, row: int, col: int) -> tuple[bool, bool]:
        raw = self._values.get((row, col), DEFAULT_PIXEL_CONFIG)
        tst = PIXEL_CODEC.extract(raw, "PX_TST_EN") == 1
        buf = PIXEL_CODEC.extract(raw, "PX_BUF_NEN") == 0
        return tst, buf

    def pixel_state(self, row: int, col: int) -> str:
        coord = (row, col)
        if coord in self._local_dirty:
            return "local"
        if coord in self._upo_values:
            if self._chip_values.get(coord) == self._upo_values[coord]:
                return "written"
            return "staged"
        return "unknown"

    def status_color(self, row: int, col: int) -> QColor:
        c = current_theme_colors()
        colors = {
            "unknown": QColor(c["table_alt"]),
            "local": QColor(c["warning"]),
            "staged": QColor(c["primary"]),
            "written": QColor(c["ok"]),
        }
        return colors[self.pixel_state(row, col)]

    def _status_text(self, row: int, col: int) -> str:
        state = self.pixel_state(row, col)
        if state == "local":
            return "Local edit - not sent to MGPDLab virtual memory"
        if state == "staged":
            return "Staged in MGPDLab virtual memory - not yet committed to chip"
        if state == "written":
            return "Written to chip in this GUI session"
        return "Unknown - protocol provides no per-pixel readback"

    # ------------------------------------------------------------- selection

    def _matrix_clicked(self, row: int, col: int, modifiers):
        clicked = (row, col)
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        if shift and self._anchor is not None:
            anchor_row, anchor_col = self._anchor
            r0, r1 = sorted((anchor_row, row))
            c0, c1 = sorted((anchor_col, col))
            rectangle = {
                (r, c)
                for r in range(r0, r1 + 1)
                for c in range(c0, c1 + 1)
                if c in OWNED_COLUMNS
            }
            if ctrl:
                self._selection.update(rectangle)
            else:
                self._selection = rectangle
            # Shift preserves the original anchor, like a desktop selection.
        elif ctrl:
            if clicked in self._selection:
                self._selection.remove(clicked)
            else:
                self._selection.add(clicked)
            self._anchor = clicked
        else:
            self._selection = {clicked}
            self._anchor = clicked

        self.select_pixel(row, col, preserve_selection=True)
        self._refresh_visualization()

    def select_pixel(self, row: int, col: int, preserve_selection: bool = False):
        if col not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
            return

        self._active_coord = (row, col)
        if not preserve_selection:
            self._selection = {(row, col)}
            self._anchor = (row, col)

        raw = self._values[(row, col)]
        fields = PIXEL_CODEC.unpack(raw)
        self._updating_editor = True
        try:
            for name, value in fields.items():
                self.field_edits[name].setValue(value)
        finally:
            self._updating_editor = False
        self._refresh_selected_status()
        self.heatmap_map.update()
        self.status_map.update()

    def _editor_changed(self):
        if self._updating_editor:
            return
        coord = self.current_coordinate()
        raw = self.current_raw_value()
        self._set_local_value(coord, raw)
        self._refresh_selected_status()
        self._refresh_field_selector()
        self._refresh_visualization()

    def _set_local_value(self, coord: tuple[int, int], raw: int):
        self._values[coord] = raw
        baseline = self._local_baseline.get(coord, DEFAULT_PIXEL_CONFIG)
        if baseline == raw:
            self._local_dirty.discard(coord)
        else:
            self._local_dirty.add(coord)

    def apply_editor_to_selection(self):
        """Copy current editor value to the complete current selection locally."""
        raw = self.current_raw_value()
        targets = self._selection or {self._active_coord}
        for coord in targets:
            self._set_local_value(coord, raw)
        self._refresh_selected_status()
        self._refresh_field_selector()
        self._refresh_visualization()

    def load_defaults_to_editor(self):
        fields = PIXEL_CODEC.default_fields()
        self._updating_editor = True
        try:
            for name, value in fields.items():
                self.field_edits[name].setValue(value)
        finally:
            self._updating_editor = False
        self._editor_changed()

    def clear_local_edits(self):
        """Discard every PX edit not yet staged to MGPDLab virtual memory."""
        for coord in tuple(self._local_dirty):
            self._values[coord] = self._local_baseline.get(coord, DEFAULT_PIXEL_CONFIG)
        self._local_dirty.clear()
        self.select_pixel(*self.current_coordinate(), preserve_selection=True)
        self._refresh_field_selector()
        self._refresh_visualization()

    def _refresh_selected_status(self):
        row, col = self.current_coordinate()
        raw = self._values[(row, col)]
        self.raw_value.setText(f"0x{raw:08X}")
        self.coord_label.setText(f"Col={col} Row={row}")
        self.selection_label.setText(f"Selected: {len(self._selection)}")
        self.pixel_status.setText(self._status_text(row, col))

    # --------------------------------------------------------------- heatmap

    def _field_variability(self) -> dict[str, set[int]]:
        variability = {name: set() for name in PIXEL_CODEC.field_names}
        for raw in self._values.values():
            fields = PIXEL_CODEC.unpack(raw)
            for name, value in fields.items():
                variability[name].add(value)
        return variability

    def _refresh_field_selector(self):
        if self._field_selector_refreshing:
            return
        self._field_selector_refreshing = True
        try:
            previous = self.field_filter.currentData()
            if previous is None:
                previous = "Groups"

            variability = self._field_variability()
            show_only_varying = self.only_varying.isChecked()
            c = current_theme_colors()

            self.field_filter.blockSignals(True)
            self.field_filter.clear()
            self.field_filter.addItem("Groups", "Groups")

            model = self.field_filter.model()
            if isinstance(model, QStandardItemModel):
                item = model.item(0)
                if item is not None:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)

            for name in PIXEL_CODEC.field_names:
                values = variability[name]
                varying = len(values) > 1
                if show_only_varying and not varying:
                    continue
                self.field_filter.addItem(_display_field_name(name), name)
                row = self.field_filter.count() - 1
                if isinstance(model, QStandardItemModel):
                    item = model.item(row)
                    if item is not None:
                        color = c["input_focus"] if varying else c["muted"]
                        item.setForeground(QBrush(QColor(color)))
                        font = item.font()
                        font.setBold(varying)
                        item.setFont(font)
                        if varying:
                            item.setToolTip(f"{len(values)} different values")
                        else:
                            value = next(iter(values)) if values else "?"
                            item.setToolTip(f"Uniform: {value}")

            index = self.field_filter.findData(previous)
            if index < 0:
                index = 0
            self.field_filter.setCurrentIndex(index)
            self.field_filter.blockSignals(False)
        finally:
            self._field_selector_refreshing = False
        self._heatmap_mode_changed()

    def _heatmap_mode_changed(self):
        mode = self.field_filter.currentData()
        if mode is None:
            mode = "Groups"
        groups = mode == "Groups"
        self.colormap.setEnabled(not groups)
        self.heat_scale.setVisible(not groups)
        self.group_legend_scroll.setVisible(groups)

        if groups:
            self._build_group_cache()
            count = len(self._group_cache)
            self.heat_summary.setText(
                f"{count} unique configuration{'s' if count != 1 else ''} across 512 working values"
            )
            self._refresh_group_legend()
        else:
            values = [
                PIXEL_CODEC.extract(raw, mode)
                for raw in self._values.values()
            ]
            minimum = min(values) if values else 0
            maximum = max(values) if values else 0
            unique_count = len(set(values))
            if minimum == maximum:
                self.heat_summary.setText(f"Uniform: {minimum}")
            else:
                self.heat_summary.setText(
                    f"Range: {minimum}..{maximum}   |   {unique_count} values"
                )
            self.heat_scale.set_scale(minimum, maximum, self.colormap.currentText())
            self.group_legend.clear()

        self.heatmap_map.update()

    def _build_group_cache(self):
        counts = Counter(self._values.values())
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        self._group_cache = {}
        for index, (raw, count) in enumerate(ordered, start=1):
            color = QColor(GROUP_COLORS[(index - 1) % len(GROUP_COLORS)])
            # More than 12 groups are still deterministic: cycle hue slightly
            # instead of silently producing identical colours forever.
            if index > len(GROUP_COLORS):
                hue = int((index * 137.508) % 360)
                color = QColor.fromHsv(hue, 155, 220)
            self._group_cache[raw] = {
                "index": index,
                "count": count,
                "color": color,
            }

    def _refresh_group_legend(self):
        if not self._group_cache:
            self.group_legend.clear()
            return

        reference_raw = min(
            self._group_cache,
            key=lambda raw: self._group_cache[raw]["index"],
        )
        reference_fields = PIXEL_CODEC.unpack(reference_raw)
        ordered = sorted(
            self._group_cache.items(),
            key=lambda item: item[1]["index"],
        )

        lines = []
        for raw, info in ordered:
            color = info["color"].name()
            group_name = f"G{info['index']}"
            count = info["count"]
            if raw == reference_raw:
                detail = "Reference"
            else:
                fields = PIXEL_CODEC.unpack(raw)
                differences = [
                    f"{_display_field_name(name)} {reference_fields[name]}→{fields[name]}"
                    for name in PIXEL_CODEC.field_names
                    if fields[name] != reference_fields[name]
                ]
                if len(differences) > 3:
                    hidden = len(differences) - 3
                    differences = differences[:3] + [f"+{hidden} more"]
                detail = ", ".join(differences) if differences else "Same as reference"
            lines.append(
                f"<span style='color:{color}; font-size:14px;'>■</span> "
                f"<b>{group_name}</b> {count} px - {detail}"
            )

        lines.append("<br><i>Alt+hover a pixel for full configuration</i>")
        self.group_legend.setText("<br>".join(lines))

    def heatmap_color(self, row: int, col: int) -> QColor:
        raw = self._values[(row, col)]
        mode = self.field_filter.currentData() or "Groups"
        if mode == "Groups":
            if raw not in self._group_cache:
                self._build_group_cache()
            return self._group_cache[raw]["color"]

        values = [PIXEL_CODEC.extract(value, mode) for value in self._values.values()]
        minimum = min(values) if values else 0
        maximum = max(values) if values else 0
        current = PIXEL_CODEC.extract(raw, mode)
        if maximum == minimum:
            position = 0.5
        else:
            position = (current - minimum) / (maximum - minimum)
        return _interpolate_color(CONTINUOUS_COLORMAPS[self.colormap.currentText()], position)

    def pixel_hover_text(self, row: int, col: int) -> str:
        raw = self._values[(row, col)]
        fields = PIXEL_CODEC.unpack(raw)
        if not self._group_cache or raw not in self._group_cache:
            self._build_group_cache()
        group = self._group_cache.get(raw)
        group_text = ""
        if group is not None:
            group_text = f"Group G{group['index']} ({group['count']} px)\n"

        field_text = "\n".join(
            f"{_display_field_name(name)}={fields[name]}"
            for name in PIXEL_CODEC.field_names
        )
        return (
            f"Col={col} Row={row}\n"
            f"Status: {self.pixel_state(row, col)}\n"
            f"{group_text}"
            f"{field_text}"
        )

    def _refresh_visualization(self):
        self._build_group_cache()
        mode = self.field_filter.currentData() or "Groups"
        if mode == "Groups":
            self._refresh_group_legend()
        else:
            values = [PIXEL_CODEC.extract(raw, mode) for raw in self._values.values()]
            minimum = min(values) if values else 0
            maximum = max(values) if values else 0
            unique_count = len(set(values))
            self.heat_summary.setText(
                f"Uniform: {minimum}"
                if minimum == maximum
                else f"Range: {minimum}..{maximum}   |   {unique_count} values"
            )
            self.heat_scale.set_scale(minimum, maximum, self.colormap.currentText())

        self._refresh_selected_status()
        self.heatmap_map.update()
        self.status_map.update()

    # ----------------------------------------------------------- worker results

    def set_busy(self, busy: bool):
        busy = bool(busy)
        enabled = self._connected and not busy
        self.stage_local.setEnabled(enabled)
        self.send_raw.setEnabled(enabled)
        self.stage_all.setEnabled(enabled)
        self.write_chip.setEnabled(enabled)
        self.write_zeros.setEnabled(enabled)
        self.heatmap_map.setEnabled(not busy)
        self.status_map.setEnabled(not busy)
        for edit in self.field_edits.values():
            edit.setEnabled(not busy)
        self.load_defaults.setEnabled(not busy)
        self.apply_local.setEnabled(not busy)
        self.clear_local.setEnabled(not busy)
        self.raw_value.setEnabled(not busy)
        self.field_filter.setEnabled(not busy)
        self.colormap.setEnabled(not busy and (self.field_filter.currentData() != "Groups"))
        self.only_varying.setEnabled(not busy)

    def apply_selected_stage_result(self, result: dict):
        row = int(result["row"])
        col = int(result["col"])
        value = int(result["value"])
        coord = (row, col)
        self._values[coord] = value
        self._upo_values[coord] = value
        self._local_baseline[coord] = value
        self._local_dirty.discard(coord)
        if self.current_coordinate() == coord:
            self.select_pixel(row, col, preserve_selection=True)
        self._refresh_field_selector()
        self._refresh_visualization()

    def apply_local_stage_result(self, result: dict):
        pixels = result.get("pixels", [])
        for item in pixels:
            row = int(item["row"])
            col = int(item["col"])
            value = int(item["value"])
            coord = (row, col)
            self._values[coord] = value
            self._upo_values[coord] = value
            self._local_baseline[coord] = value
            self._local_dirty.discard(coord)
        count = int(result.get("count", len(pixels)))
        self.progress.setValue(count)
        self.progress.setFormat(f"Staged {count} local edit(s) in UPO")
        self.select_pixel(*self.current_coordinate(), preserve_selection=True)
        self._refresh_field_selector()
        self._refresh_visualization()

    def apply_bulk_stage_result(self, result: dict):
        value = int(result["value"])
        for row in range(MATRIX_ROWS):
            for col in OWNED_COLUMNS:
                coord = (row, col)
                self._values[coord] = value
                self._upo_values[coord] = value
                self._local_baseline[coord] = value
                self._local_dirty.discard(coord)
        self.progress.setValue(self.progress.maximum())
        self.progress.setFormat(f"Staged {result['count']} / {result['count']} pixels in UPO")
        self.select_pixel(*self.current_coordinate(), preserve_selection=True)
        self._refresh_field_selector()
        self._refresh_visualization()

    def apply_commit_result(self, ok: bool):
        if not ok:
            return
        for coord, value in self._upo_values.items():
            self._chip_values[coord] = value
        self.progress.setFormat("Matrix WRITE_TO_CHIP accepted by UPO")
        self._refresh_visualization()

    def apply_external_global_state(self, raw_config: int):
        """Synchronize GUI state after another workflow rewrites owned pixels."""
        PIXEL_CODEC.validate_raw(raw_config)
        for row in range(MATRIX_ROWS):
            for col in OWNED_COLUMNS:
                coord = (row, col)
                self._values[coord] = raw_config
                self._upo_values[coord] = raw_config
                self._chip_values[coord] = raw_config
                self._local_baseline[coord] = raw_config
        self._local_dirty.clear()
        self.select_pixel(*self.current_coordinate(), preserve_selection=True)
        self._refresh_field_selector()
        self._refresh_visualization()

    def set_matrix_progress(self, current: int, total: int, row: int, col: int):
        self.progress.setRange(0, total)
        self.progress.setValue(current)
        self.progress.setFormat(f"Staging {current}/{total} - Col={col} Row={row}")
