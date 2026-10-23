"""Native Qt setup and evidence-linked acceptance, reached from the plugin center."""
import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (QAbstractSpinBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QComboBox, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .scenarios import DEFAULT, NAMES, validate
from .theme import INK, MUTED, NEGATIVE
from .widgets import MotionButton


class ScenarioTimeline(QWidget):
    def __init__(self):
        super().__init__()
        self.spec = dict(DEFAULT)
        self.setMinimumHeight(170)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, right = 110, self.width()-48
        duration = self.spec["duration_ms"]
        painter.setPen(QColor(MUTED))
        for i in range(5):
            x = left+(right-left)*i/4
            painter.drawText(int(x-20), 164, f"{duration*i/4000:g} s")
        for row, (name, at, text) in enumerate((
            ("温度", self.spec["trigger_ms"], "越限" if self.spec["kind"] == "limit" else "正常"),
            ("指令", self.spec["trigger_ms"], "发出"),
            ("反馈", self.spec["trigger_ms"]+self.spec["response_ms"], "到达"))):
            y = 24+row*40
            painter.setPen(QColor(INK)); painter.drawText(0, y+4, name)
            painter.setPen(QPen(QColor("#d9dfe2"), 2)); painter.drawLine(left, y, right, y)
            if row == 0 and self.spec["kind"] != "limit":
                painter.setPen(QColor(MUTED)); painter.drawText(left+8, y-7, f"正常 {self.spec['baseline_milli']/1000:g} °C")
                continue
            x = left+(right-left)*min(at, duration)/duration
            color = NEGATIVE if (row == 0 and self.spec["kind"] == "limit") or (row == 2 and self.spec["response_ms"] > self.spec["deadline_ms"]) else "#087e8b"
            painter.setPen(QPen(QColor(color), 3)); painter.drawLine(int(x), y-6, int(x), y+6)
            painter.drawText(min(int(x+7), right-70), y+19 if row == 2 else y-7, text+f" {at/1000:g} s")
            if row == 2:
                deadline = self.spec["trigger_ms"]+self.spec["deadline_ms"]
                end = left+(right-left)*min(deadline,duration)/duration
                painter.setPen(QPen(QColor(MUTED),1)); painter.drawLine(int(end),y-12,int(end),y+5)
                painter.drawText(min(int(end+6),right-75),y-16,f"截止 {deadline/1000:g} s")


class ScenarioSetupDialog(QDialog):
    def __init__(self, spec=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("场景验证 · 配置并准备")
        self.resize(900, 680)
        root = QVBoxLayout(self); root.setContentsMargins(24, 20, 24, 20); root.setSpacing(12)
        title = QLabel("场景验证"); title.setObjectName("heading"); root.addWidget(title)
        note = QLabel("保存并准备会使用配套字典与两条验收公式；已有采集记录保留。准备完成后，点击左下角“开始采集”。")
        note.setWordWrap(True); note.setObjectName("muted"); root.addWidget(note)
        self.kind = QComboBox()
        for key, name in NAMES.items(): self.kind.addItem(name, key)
        self.kind.setToolTip("正常运行应无异常；越限和超时场景应准确检出预设异常。检出预期异常也属于验收通过。")
        root.addWidget(self.kind)
        self.fields = {}
        grid = QGridLayout(); grid.setHorizontalSpacing(24); grid.setVerticalSpacing(12)
        settings = (
            ("period_ms", "采样间隔 / ms", "两条原始报文之间的模拟时间间隔。"),
            ("baseline_milli", "正常温度 / °C", "温度参数的正常值，只用于软件验证。"),
            ("duration_ms", "场景时长 / ms", "包含起点和终点。达到预定帧数后自动结束、封存并验收。"),
            ("limit_milli", "温度上限 / °C", "温度严格大于此值时触发越限事件；等于上限仍为正常。"),
            ("trigger_ms", "指令与异常触发 / ms", "相对场景起点的时刻；指令变为1，越限场景同时改变温度。"),
            ("high_milli", "越限温度 / °C", "参数越限场景的异常值，需要高于温度上限。"),
            ("response_ms", "反馈延迟 / ms", "指令发出后，反馈从0变为1的延迟。时刻需对齐采样间隔。"),
            ("deadline_ms", "最长响应时间 / ms", "反馈在此期限内到达（含边界）为正常；之后到达为响应超时。"))
        for i, (key, text, tip) in enumerate(settings):
            column = QVBoxLayout(); column.setSpacing(6)
            caption = QLabel(text); caption.setToolTip(tip); column.addWidget(caption)
            field = QDoubleSpinBox() if key.endswith("milli") else QSpinBox()
            field.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            if key.endswith("milli"):
                field.setDecimals(3); field.setRange(-1000, 1000)
            else:
                field.setRange(1, 1000 if key == "period_ms" else 600000)
            field.setToolTip(tip); column.addWidget(field)
            grid.addLayout(column, i//2, i%2)
            self.fields[key] = field
        root.addLayout(grid)
        preview = QLabel("预期时间轴 · 软件模拟时钟"); preview.setObjectName("section"); root.addWidget(preview)
        self.timeline = ScenarioTimeline(); root.addWidget(self.timeline)
        self.feedback = QLabel(); self.feedback.setWordWrap(True); root.addWidget(self.feedback)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存并准备")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.save_button.setToolTip("保存可复现场景并准备插件；此操作尚不开始采集。")
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); root.addWidget(buttons)
        initial = validate(spec) if spec is not None else dict(DEFAULT)
        self.kind.setCurrentIndex(self.kind.findData(initial["kind"]))
        for key, field in self.fields.items():
            field.setValue(initial[key]/1000 if key.endswith("milli") else initial[key])
            field.valueChanged.connect(self.preview)
        self.kind.currentIndexChanged.connect(self.change_kind)
        self.preview()

    def values(self):
        return {"schema":1, "kind":self.kind.currentData(), **{key:int(round(field.value()*1000)) if key.endswith("milli") else field.value()
                for key, field in self.fields.items()}}

    def change_kind(self):
        period = self.fields["period_ms"].value()
        deadline = self.fields["deadline_ms"].value()
        delay = (deadline//period+2)*period if self.kind.currentData() == "timeout" else max(period, deadline//period*period)
        self.fields["response_ms"].setValue(delay)
        self.preview()

    def preview(self):
        spec = self.values()
        self.timeline.spec = spec; self.timeline.update()
        self.fields["high_milli"].setEnabled(spec["kind"] == "limit")
        try:
            validate(spec)
        except ValueError as error:
            self.feedback.setText(str(error)); self.feedback.setStyleSheet(f"color:{NEGATIVE}")
            self.save_button.setEnabled(False)
        else:
            count = spec["duration_ms"]//spec["period_ms"]+1
            expectation = "1次温度越限，响应正常" if spec["kind"] == "limit" else "1次响应超时，温度正常" if spec["kind"] == "timeout" else "无越限、无超时，响应正常"
            self.feedback.setText(f"预期：{count}帧原始记录 · {count*3}个参数样本 · {expectation}。")
            self.feedback.setStyleSheet(""); self.save_button.setEnabled(True)


class ScenarioResultDialog(QDialog):
    evidence_requested = Signal(object, object)

    def __init__(self, directory, archive, parent=None):
        super().__init__(parent)
        self.directory, self.archive = Path(directory), Path(archive)
        self.result = json.loads((self.directory/"acceptance.json").read_text(encoding="utf-8"))
        self.setWindowTitle("场景验收结果")
        self.resize(1000, 650)
        root = QVBoxLayout(self); root.setContentsMargins(24,24,24,24)
        verdict = "未运行" if self.result["passed"] is None else "验收通过" if self.result["passed"] else "验收未通过"
        title = QLabel(NAMES[self.result["scenario"]["kind"]] + " · " + verdict)
        title.setObjectName("heading"); root.addWidget(title)
        note = QLabel("预期异常被准确识别也属于通过。选择检查项可查看预期、实际结果和对应原始报文。")
        note.setWordWrap(True); root.addWidget(note)
        self.table = QTableWidget(len(self.result["checks"]), 3)
        self.table.setHorizontalHeaderLabels(["检查项", "结论", "实际结果"])
        self.table.setShowGrid(False); self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i, check in enumerate(self.result["checks"]):
            state = "未运行" if check["passed"] is None else "通过" if check["passed"] else "未通过"
            for j, text in enumerate((check["name"], state, check["actual"])):
                item = QTableWidgetItem(text); item.setToolTip(text)
                if j == 1 and check["passed"] is False: item.setForeground(QColor(NEGATIVE))
                self.table.setItem(i,j,item)
        root.addWidget(self.table,1)
        self.detail = QLabel(); self.detail.setWordWrap(True); self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setMinimumHeight(90); root.addWidget(self.detail)
        actions = QHBoxLayout()
        self.raw_button = MotionButton("查看对应原始报文"); self.raw_button.clicked.connect(self.open_evidence)
        report = MotionButton("打开验收报告"); report.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory/"acceptance.html"))))
        close = MotionButton("关闭"); close.clicked.connect(self.close)
        actions.addWidget(self.raw_button); actions.addWidget(report); actions.addStretch(); actions.addWidget(close); root.addLayout(actions)
        self.table.itemSelectionChanged.connect(self.select)
        self.table.cellDoubleClicked.connect(lambda row,column:self.open_evidence())
        self.table.selectRow(0)

    def select(self):
        check = self.result["checks"][self.table.currentRow()]
        self.detail.setText("预期："+check["expected"]+"\n实际："+check["actual"])
        self.raw_button.setEnabled(check["evidence"] is not None)

    def open_evidence(self):
        check = self.result["checks"][self.table.currentRow()]
        if check["evidence"] is not None:
            self.evidence_requested.emit(check["evidence"], self.archive)
