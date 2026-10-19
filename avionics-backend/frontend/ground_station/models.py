from collections import deque
import json
import math

from PySide6.QtCore import QAbstractTableModel, QLineF, QModelIndex, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


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
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            values = [row["parameter"], row["source"], display_value(row["value"]), row["unit"],
                      "有效" if row["usable"] else " / ".join(row["quality"]),
                      str(row["record_index"]), str(row["generation"])]
            return values[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 4:
            return QColor("#12846e" if row["usable"] else "#bc6635")
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
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return [row["rule"], row["outcome"], f"{row['time_ns'] / 1e9:.6f}",
                    display_value(row["metric"]), str(len(row["evidence"]))][index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return QColor("#12846e" if row["outcome"] in {"consistent", "response_ok"} else "#bc6635")
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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(175)
        self.key = None
        self.epoch = None
        self.points = deque(maxlen=512)
        self.unit = ""
        self.time_origin = None
        self.boolean_axis = False

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
        painter.fillRect(self.rect(), QColor("#fcfdff"))
        area = QRectF(68, 20, max(1, self.width() - 91), max(1, self.height() - 58))
        painter.setPen(QPen(QColor("#e5ebf2"), 1))
        for i in range(5):
            y = area.top() + area.height() * i / 4
            painter.drawLine(QLineF(area.left(), y, area.right(), y))
        valid = [point for point in self.points if point is not None]
        if not valid:
            painter.setPen(QColor("#8492a5"))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "选择参数后显示趋势 · 等待有效数据")
            return
        x0, x1 = min(p[0] for p in valid), max(p[0] for p in valid)
        low, high = min(p[1] for p in valid), max(p[1] for p in valid)
        padding = max((high - low) * .12, abs(high) * .002, .001)
        low -= padding
        high += padding
        if self.boolean_axis:
            low, high = 0.0, 1.0
        painter.setPen(QColor("#738197"))
        for i in range(5):
            value = high - (high - low) * i / 4
            axis_label = ("true / 1" if i == 0 else "false / 0" if i == 4 else "") if self.boolean_axis else f"{value:.4g}"
            painter.drawText(QRectF(2, area.top() + area.height() * i / 4 - 9, 58, 20),
                             Qt.AlignmentFlag.AlignRight, axis_label)
        time_origin = x0 if self.time_origin is None else self.time_origin
        begin_time, end_time = (x0 - time_origin) / 1e9, (x1 - time_origin) / 1e9
        painter.drawText(QRectF(area.left(), area.bottom() + 10, area.width(), 20),
                         Qt.AlignmentFlag.AlignLeft, f"{begin_time:.3f} s     →     {end_time:.3f} s    ·    {self.unit}")
        path = QPainterPath()
        connected = False
        markers = []
        for point in self.points:
            if point is None:
                connected = False
                continue
            x = area.left() + (point[0] - x0) / max(x1 - x0, 1) * area.width()
            y = area.bottom() - (point[1] - low) / (high - low) * area.height()
            markers.append(QPointF(x, y))
            if connected:
                path.lineTo(x, y)
            else:
                path.moveTo(x, y)
            connected = True
        painter.setPen(QPen(QColor("#168fbd"), 2.2))
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#168fbd"))
        for marker in markers:
            painter.drawEllipse(marker, 1.8, 1.8)
