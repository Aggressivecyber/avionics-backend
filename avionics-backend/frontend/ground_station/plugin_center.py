import os
from pathlib import Path
import re

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget)


class PluginCard(QFrame):
    start_requested = Signal(object)
    stop_requested = Signal(object)
    details_requested = Signal(object)

    def __init__(self, definition, parent=None):
        super().__init__(parent)
        self.definition = definition
        self.setObjectName("card")
        self.setMinimumWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        top = QHBoxLayout()
        category = QLabel(definition["category"])
        category.setObjectName("muted")
        top.addWidget(category)
        top.addStretch()
        self.status = QLabel("未启动")
        self.status.setObjectName("badge")
        top.addWidget(self.status)
        layout.addLayout(top)
        name = QLabel(definition["name"])
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setObjectName("section")
        layout.addWidget(name)
        summary = QLabel(definition["summary"])
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setWordWrap(True)
        summary.setMinimumHeight(54)
        layout.addWidget(summary)
        self.runtime = QLabel("点击启动后进入监测工作台。")
        self.runtime.setObjectName("muted")
        self.runtime.setWordWrap(True)
        layout.addWidget(self.runtime)
        actions = QHBoxLayout()
        self.start_button = QPushButton("启动")
        self.start_button.setObjectName("primary")
        self.stop_button = QPushButton("停用")
        self.details_button = QPushButton("查看详情")
        self.start_button.clicked.connect(lambda: self.start_requested.emit(definition))
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(definition))
        self.details_button.clicked.connect(lambda: self.details_requested.emit(definition))
        for action in (self.start_button, self.stop_button, self.details_button):
            actions.addWidget(action)
        layout.addLayout(actions)


class PluginDetailsDialog(QDialog):
    def __init__(self, definition, states, parent=None):
        super().__init__(parent)
        self.setWindowTitle(definition["name"] + " · 功能详情")
        self.resize(750, 560)
        layout = QVBoxLayout(self)
        title = QLabel(definition["name"])
        title.setObjectName("heading")
        title.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(title)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(definition["description"] + "\n\n功能\n" +
            "\n".join("• " + item for item in definition["functions"]) + "\n\n配置参数\n" +
            definition["parameters"] + "\n\n使用说明\n" + definition["notes"] +
            "\n\n插件文件\n" + definition["path"] + "\n\n当前采集源\n" + states)
        layout.addWidget(self.text)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)


class ReplaySetupDialog(QDialog):
    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle("启动归档回放插件")
        self.resize(650, 230)
        form = QFormLayout(self)
        self.source = QLineEdit("replay")
        self.archive = QLineEdit()
        self.data_dir = data_dir
        path = QHBoxLayout()
        path.addWidget(self.archive)
        browse = QPushButton("选择归档")
        browse.clicked.connect(self.browse)
        path.addWidget(browse)
        self.speed = QSpinBox()
        self.speed.setRange(0, 1000)
        self.speed.setValue(1)
        self.speed.setSpecialValueText("尽快回放")
        form.addRow("逻辑源 ID", self.source)
        form.addRow("原始归档", path)
        form.addRow("速度倍数", self.speed)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择已封存原始归档", str(self.data_dir), "总线归档 (*.avbus)")
        if path:
            self.archive.setText(path)

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.source.text()):
            self.error.setText("逻辑源 ID 须为 1–64 个字母、数字、下划线或连字符。")
        elif not Path(self.archive.text()).is_file():
            self.error.setText("请选择已有的 .avbus 归档。")
        elif any(c in self.archive.text() for c in ";\r\n\0"):
            self.error.setText("归档路径不能包含分号、换行或空字符。")
        else:
            self.accept()


class PluginCenter(QWidget):
    start_requested = Signal(object)
    stop_requested = Signal(object)

    def __init__(self, definitions, parent=None):
        super().__init__(parent)
        self.cards = {}
        self.sources_by_plugin = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("选择插件启动采集；停用仅停止该插件的来源。点击详情查看功能、配置参数和当前状态。")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("pluginGrid")
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(0, 10, 0, 10)
        self.grid.setSpacing(16)
        self.grid.setRowStretch(20, 1)
        for definition in definitions:
            self.add_definition(definition)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

    def add_definition(self, definition):
        card = PluginCard(definition)
        card.start_requested.connect(self.start_requested)
        card.stop_requested.connect(self.stop_requested)
        card.details_requested.connect(self.show_details)
        index = len(self.cards)
        self.cards[definition["id"]] = card
        self.grid.addWidget(card, index // 2, index % 2)

    def show_details(self, definition):
        sources = self.sources_by_plugin.get(definition["id"], [])
        states = "\n".join(f"{source['id']}：{source['status']['state']} · " + source["status"]["failure"] for source in sources) or "当前没有活动来源。"
        dialog = PluginDetailsDialog(definition, states, self)
        dialog.exec()

    def refresh(self, configurations, snapshot, active, ready, busy, closing):
        paths = {os.path.normcase(str(Path(card.definition["path"]).resolve())): card.definition for card in self.cards.values()}
        for source, path, config in configurations.values():
            normalized = os.path.normcase(str(Path(path).resolve()))
            if normalized not in paths:
                definition = {"id": normalized, "name": Path(path).stem, "path": path,
                    "category": "自定义 · 当前会话", "summary": "此插件的功能说明尚未登记。可在插件说明文件中补充用途和配置。",
                    "description": "已由当前会话加载的自定义采集插件。功能需要由插件作者登记，界面不会从文件名推断用途。",
                    "functions": ["当前用于逻辑源：" + source], "parameters": config,
                    "notes": "在 plugins.json 中登记插件名称、功能和参数后，可显示完整说明。",
                    "mode": "custom", "presets": []}
                self.add_definition(definition)
                paths[normalized] = definition
        self.sources_by_plugin = {key: [] for key in self.cards}
        if active:
            for source in snapshot["sources"]:
                if source["id"] in configurations:
                    path = os.path.normcase(str(Path(configurations[source["id"]][1]).resolve()))
                    self.sources_by_plugin[paths[path]["id"]].append(source)
        for key, card in self.cards.items():
            sources = self.sources_by_plugin[key]
            running = sum(source["status"]["state"] == "running" for source in sources)
            faulted = any(source["status"]["state"] == "faulted" for source in sources)
            exists = Path(card.definition["path"]).is_file()
            card.status.setText("文件缺失" if not exists else "故障" if faulted else f"运行中 · {running} 个源" if running else "已停用" if sources else "未启动")
            card.status.setStyleSheet("background:#fff0e6;color:#b15c2c;" if faulted or not exists else "")
            card.runtime.setText("；".join(f"{source['id']} · 已记录 {source['recorded']} 帧" for source in sources) or "点击启动后进入监测工作台。")
            available = exists and not busy and not closing and (not active or ready)
            card.start_button.setEnabled(available and not running)
            card.stop_button.setEnabled(available and bool(sources) and (running > 0 or faulted))
