from collections import deque
import json
import math

from PySide6.QtCore import QAbstractTableModel, QLineF, QModelIndex, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .theme import ACCENT, BORDER, MUTED, POSITIVE, SURFACE, WARNING
from .ui_assets import artwork


def display_value(value):
    if isinstance(value, dict):
        return f"{value['label']} ({value['code']})"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return "—" if value is None else str(value)


class ParameterModel(QAbstractTableModel):
    headers = ["参数", "来源", "当前值", "单位", "质量", "记录索引", "代次"]
    numeric_columns = {2, 5, 6}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.indices = {}
        self.omitted = 0

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole:
            return (Qt.AlignmentFlag.AlignRight if section in self.numeric_columns else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (Qt.AlignmentFlag.AlignRight if index.column() in self.numeric_columns else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.DisplayRole:
            values = [row["parameter"], row["source"], display_value(row["value"]), row["unit"],
                      "有效" if row["usable"] else " / ".join(row["quality"]),
                      str(row["record_index"]), str(row["generation"])]
            return values[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 4:
            return QColor(POSITIVE if row["usable"] else WARNING)
        if role == Qt.ItemDataRole.ToolTipRole:
            return json.dumps(row, ensure_ascii=False, indent=2)
        return None

    def clear(self):
        self.beginResetModel()
        self.rows.clear()
        self.indices.clear()
        self.omitted = 0
        self.endResetModel()

    def update_samples(self, samples):
        for row in samples:
            key = row["source"], row["parameter"]
            if key in self.indices:
                index = self.indices[key]
                self.rows[index] = row
                self.dataChanged.emit(self.index(index, 0), self.index(index, len(self.headers) - 1))
            elif len(self.rows) < 4096:
                index = len(self.rows)
                self.beginInsertRows(QModelIndex(), index, index)
                self.indices[key] = index
                self.rows.append(row)
                self.endInsertRows()
            else:
                self.omitted += 1


class EventModel(QAbstractTableModel):
    headers = ["规则", "结果", "时间 / s", "度量", "证据数"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole:
            return (Qt.AlignmentFlag.AlignRight if section >= 2 else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (Qt.AlignmentFlag.AlignRight if index.column() >= 2 else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.DisplayRole:
            return [row["rule"], row["outcome"], f"{row['time_ns'] / 1e9:.6f}",
                    display_value(row["metric"]), str(len(row["evidence"]))][index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return QColor(POSITIVE if row["outcome"] in {"consistent", "response_ok"} else WARNING)
        if role == Qt.ItemDataRole.ToolTipRole:
            return json.dumps(row, ensure_ascii=False, indent=2)
        return None

    def clear(self):
        self.beginResetModel()
        self.rows.clear()
        self.endResetModel()

    def append(self, rows):
        if rows:
            self.beginResetModel()
            self.rows = (list(reversed(rows)) + self.rows)[:200]
            self.endResetModel()


class TrendPlot(QWidget):
    def __init__(self, parent=None, empty_artwork="empty_monitor", empty_caption="等待数据 · 先启用插件，再开始采集"):
        super().__init__(parent)
        self.setMinimumHeight(175)
        self.key = None
        self.epoch = None
        self.points = deque(maxlen=512)
        self.unit = ""
        self.time_origin = None
        self.boolean_axis = False
        self.empty_image = artwork(empty_artwork)
        self.empty_caption = empty_caption

    def select(self, key, samples):
        self.key = key
        self.epoch = None
        self.points.clear()
        self.add(samples)

    def add(self, samples):
        for sample in samples:
            if (sample["source"], sample["parameter"]) != self.key:
                continue
            epoch = sample["generation"], sample["origin_session_id"], sample["time_group"]
            if self.epoch != epoch:
                self.points.clear()
                self.epoch = epoch
            value = sample["value"]
            if sample["usable"] and isinstance(value, (int, float)) and math.isfinite(value):
                self.points.append((sample["time_ns"], float(value)))
                self.unit = sample["unit"]
            else:
                self.points.append(None)  # Quality failures visibly break the line.
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        axis_font = painter.font()
        axis_font.setPixelSize(12)
        painter.setFont(axis_font)
        valid = [point for point in self.points if point is not None]
        if not valid:
            image = self.empty_image.scaled(QSize(min(280, self.width() - 32), min(108, self.height() - 34)),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self.width() - image.width()) // 2, (self.height() - 34 - image.height()) // 2, image)
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(0, self.height() - 28, self.width(), 24),
                             Qt.AlignmentFlag.AlignCenter, self.empty_caption)
            return
        area = QRectF(68, 20, max(1, self.width() - 91), max(1, self.height() - 58))
        painter.setPen(QPen(QColor(BORDER), 1))
        for i in range(5):
            y = area.top() + area.height() * i / 4
            painter.drawLine(QLineF(area.left(), y, area.right(), y))
        x0, x1 = min(p[0] for p in valid), max(p[0] for p in valid)
        low, high = min(p[1] for p in valid), max(p[1] for p in valid)
        padding = max((high - low) * .12, abs(high) * .002, .001)
        low -= padding
        high += padding
        if self.boolean_axis:
            low, high = 0.0, 1.0
        painter.setPen(QColor(MUTED))
        for i in range(5):
            value = high - (high - low) * i / 4
            axis_label = ("true / 1" if i == 0 else "false / 0" if i == 4 else "") if self.boolean_axis else f"{value:.4g}"
            painter.drawText(QRectF(2, area.top() + area.height() * i / 4 - 9, 58, 20),
                             Qt.AlignmentFlag.AlignRight, axis_label)
        time_origin = x0 if self.time_origin is None else self.time_origin
        begin_time, end_time = (x0 - time_origin) / 1e9, (x1 - time_origin) / 1e9
        ticks = 5 if area.width() >= 480 else 3
        for i in range(ticks):
            x = area.left() + area.width() * i / (ticks - 1)
            alignment = Qt.AlignmentFlag.AlignLeft if i == 0 else Qt.AlignmentFlag.AlignRight if i == ticks - 1 else Qt.AlignmentFlag.AlignHCenter
            painter.drawText(QRectF(x if i == 0 else x - 80 if i == ticks - 1 else x - 40,
                                   area.bottom() + 10, 80, 20), alignment,
                             f"{begin_time + (end_time - begin_time) * i / (ticks - 1):.3f} s")
        path = QPainterPath()
        connected = False
        markers = []
        run_length = 0
        for point in [*self.points, None]:
            if point is None:
                if run_length == 1:
                    markers.append(last_point)
                connected = False
                run_length = 0
                continue
            x = area.left() + (point[0] - x0) / max(x1 - x0, 1) * area.width()
            y = area.bottom() - (point[1] - low) / (high - low) * area.height()
            last_point = QPointF(x, y)
            run_length += 1
            if connected:
                path.lineTo(x, y)
            else:
                path.moveTo(x, y)
            connected = True
        painter.setPen(QPen(QColor(ACCENT), 2))
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ACCENT))
        for marker in markers:
            painter.drawEllipse(marker, 1.8, 1.8)
