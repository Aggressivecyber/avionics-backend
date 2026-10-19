from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from PySide6.QtCore import QProcess, QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
    QSplitter, QStackedWidget, QTableView, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from .backend import BackendClient, read_jsonl_tail, writable_data_path
from .models import EventModel, ParameterModel, TrendPlot
from .plugin_center import PluginCenter, ReplaySetupDialog
from .relations_page import RelationsPage
from .theme import ACTIVE, ALERT_STYLE, INK, MUTED, NEGATIVE, RUNNING_STYLE, STYLE


def label(text, name=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def card():
    widget = QFrame()
    widget.setObjectName("card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(12)
    return widget, layout


def table_view(model):
    view = QTableView()
    view.setModel(model)
    view.setShowGrid(False)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(36)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return view


class SourceDialog(QDialog):
    def __init__(self, bin_dir, values=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("替换采集插件" if values else "添加采集源")
        self.resize(620, 250)
        form = QFormLayout(self)
        self.source = QLineEdit(values[0] if values else "sensor_a")
        self.source.setReadOnly(values is not None)
        suffix = ".dll" if os.name == "nt" else ".so"
        self.plugin = QLineEdit(values[1] if values else str(bin_dir / ("bus_simulated" + suffix)))
        self.config = QLineEdit(values[2] if values else "period_ms=20;initial_milli=20000;step_milli=10")
        row = QHBoxLayout()
        row.addWidget(self.plugin)
        self.plugin_browse = button("浏览", self.browse)
        row.addWidget(self.plugin_browse)
        form.addRow("逻辑源 ID", self.source)
        form.addRow("插件文件", row)
        form.addRow("插件配置", self.config)
        form.addRow(label("源 ID 使用字母、数字、下划线或连字符。硬件参数由所选插件定义。", "muted"))
        self.error = label("")
        self.error.setWordWrap(True)
        form.addRow(self.error)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(self.validate)
        actions.rejected.connect(self.reject)
        form.addRow(actions)

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择采集插件", self.plugin.text(), "插件 (*.dll *.so *.dylib)")
        if path:
            self.plugin.setText(path)

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.source.text()):
            self.error.setText("源 ID 必须为 1–64 个字母、数字、下划线或连字符。")
        elif not Path(self.plugin.text()).is_file():
            self.error.setText("插件文件不存在。")
        elif any(c in self.config.text() for c in "\r\n\0"):
            self.error.setText("插件配置必须为单行文本。")
        else:
            self.accept()

    def values(self):
        return self.source.text(), self.plugin.text(), self.config.text()


class MainWindow(QMainWindow):
    def __init__(self, bin_dir: Path, data_dir: Path, configuration: Path):
        super().__init__()
        self.bin_dir = bin_dir
        self.demo_configuration = Path(__file__).resolve().parents[2] / "config" / "desktop-demo.ini"
        self.plugin_definitions = json.loads(Path(__file__).with_name("plugins.json").read_text(encoding="utf-8"))
        suffix = ".dll" if os.name == "nt" else ".so"
        for definition in self.plugin_definitions:
            definition["path"] = str(bin_dir / (definition["library"] + suffix))
        self.data_dir = writable_data_path(str(data_dir))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.client = BackendClient(bin_dir, self)
        self.client.ready.connect(self.on_ready)
        self.client.snapshot.connect(self.on_snapshot)
        self.client.command_finished.connect(self.on_command)
        self.client.sealed.connect(self.on_sealed)
        self.client.active_changed.connect(self.on_active)
        self.client.busy_changed.connect(self.on_busy)
        self.client.failed.connect(self.on_error)
        self.client.exited.connect(self.on_exit)
        self.samples = deque(maxlen=512)
        self.source_configs = {}
        self.latest_snapshot = {"sources": []}
        self.pending_plugin_sources = []
        self.disabled_plugins = set()
        self.plugin_action_pending = False
        self.archive = None
        self.demo_pending = False
        self.session_is_demo = False
        self.close_pending = False
        self.busy = False
        self.last_snapshot_time = None
        self.last_counts = None
        self.display_omitted = 0
        self.display_events_omitted = 0
        self.last_metadata = None
        self.offline_process = QProcess(self)
        self.offline_process.setProcessEnvironment(self.client.process.processEnvironment())
        self.offline_process.finished.connect(self.offline_finished)
        self.offline_process.errorOccurred.connect(self.offline_error)
        self.offline_process.readyReadStandardOutput.connect(self.read_offline_stdout)
        self.offline_process.readyReadStandardError.connect(self.read_offline_stderr)
        self.offline_mode = None
        self.offline_stdout = bytearray()
        self.offline_stderr = bytearray()
        self.offline_summary = None
        self.setWindowTitle("AEROBUS v0.4.2 · 插件与数据关联工作台")
        self.resize(1440, 920)
        self.setMinimumSize(1160, 780)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(ACTIVE))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(INK))
        self.setPalette(palette)
        self.setStyleSheet(STYLE)
        self.build_ui(configuration)
        self.freshness = QTimer(self)
        self.freshness.setInterval(500)
        self.freshness.timeout.connect(self.check_freshness)
        self.freshness.start()
        self.on_active(False)

    def build_ui(self, configuration):
        # At 1440 px: 228 sidebar + 8 outer inset + 1204 sheet; sheet gutters are 24 px each.
        root = QWidget()
        root.setObjectName("frame")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(228)
        navigation = QVBoxLayout(rail)
        navigation.setContentsMargins(20, 20, 16, 24)
        navigation.setSpacing(8)
        identity = QHBoxLayout()
        self.brand_mark = QLabel()
        mark = QPixmap(28, 28)
        mark.fill(Qt.GlobalColor.transparent)
        painter = QPainter(mark)
        QSvgRenderer(str(Path(__file__).with_name("assets") / "aerobus.svg")).render(painter)
        painter.end()
        self.brand_mark.setPixmap(mark)
        self.brand_mark.setFixedSize(28, 28)
        identity.addWidget(self.brand_mark)
        identity.addWidget(label("AEROBUS", "brand"))
        identity.addStretch()
        navigation.addLayout(identity)
        navigation.addWidget(label("地面总线试验", "muted"))
        navigation.addSpacing(28)
        self.monitor_nav = button("监测工作台", lambda: self.navigate(0))
        self.offline_nav = button("离线分析", lambda: self.navigate(1))
        self.plugins_nav = button("插件中心", lambda: self.navigate(2))
        self.relations_nav = button("数据关联", lambda: self.navigate(3))
        for nav in (self.plugins_nav, self.monitor_nav, self.relations_nav, self.offline_nav):
            nav.setCheckable(True)
            navigation.addWidget(nav)
        navigation.addStretch()
        navigation.addWidget(label("原始归档 · 参数分析", "muted"))
        navigation.addWidget(label("v0.4.2", "muted"))
        layout.addWidget(rail)
        body = QWidget()
        body.setObjectName("frame")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 8)
        body_layout.setSpacing(0)
        toolbar = QFrame()
        toolbar.setFixedHeight(56)
        actions = QHBoxLayout(toolbar)
        actions.setContentsMargins(24, 8, 0, 8)
        actions.setSpacing(12)
        actions.addWidget(label("试验", "muted"))
        self.session_name = QLineEdit("地面试验")
        self.session_name.setFixedWidth(168)
        self.session_name.setToolTip("试验名称用于本次会话的归档文件名。")
        actions.addWidget(self.session_name)
        actions.addStretch()
        self.badge = label("会话未开始", "badge")
        self.badge.setMaximumWidth(240)
        self.badge.setWordWrap(True)
        actions.addWidget(self.badge)
        self.new_button = button("新建会话", self.new_session)
        self.capture_button = button("开始采集", self.start_capture, True)
        self.end_button = button("结束并封存", self.end_session)
        for action in (self.new_button, self.capture_button, self.end_button):
            actions.addWidget(action)
        body_layout.addWidget(toolbar)
        sheet = QFrame()
        sheet.setObjectName("sheet")
        sheet_layout = QVBoxLayout(sheet)
        sheet_layout.setContentsMargins(24, 24, 24, 20)
        sheet_layout.setSpacing(24)
        title_row = QHBoxLayout()
        self.heading = label("插件中心", "heading")
        title_row.addWidget(self.heading)
        title_row.addStretch()
        self.config_toggle = button("试验配置", lambda: self.config_dialog.exec())
        self.config_toggle.setToolTip("查看字典、规则与数据目录。")
        title_row.addWidget(self.config_toggle)
        sheet_layout.addLayout(title_row)
        self.config_panel, session = card()
        session_row = QHBoxLayout()
        self.config_path = QLineEdit(str(configuration))
        self.config_path.setReadOnly(True)
        self.config_path.setToolTip(str(configuration))
        self.config_button = button("字典 / 规则", self.browse_configuration)
        session_row.addWidget(self.config_path, 1)
        session_row.addWidget(self.config_button)
        session.addLayout(session_row)
        path_row = QHBoxLayout()
        path_row.addWidget(label("数据目录", "muted"))
        self.storage_label = QLineEdit(str(self.data_dir))
        self.storage_label.setReadOnly(True)
        self.storage_label.setToolTip(str(self.data_dir))
        path_row.addWidget(self.storage_label, 1)
        self.storage_button = button("选择目录", self.browse_storage)
        path_row.addWidget(self.storage_button)
        path_row.addWidget(button("打开目录", self.open_data_directory))
        session.addLayout(path_row)
        self.config_dialog = QDialog(self)
        self.config_dialog.setWindowTitle("试验配置 · 字典与数据目录")
        self.config_dialog.resize(780, 230)
        settings = QVBoxLayout(self.config_dialog)
        settings.addWidget(self.config_panel)
        close_settings = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_settings.rejected.connect(self.config_dialog.reject)
        settings.addWidget(close_settings)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.build_monitor())
        self.pages.addWidget(self.build_offline(configuration))
        self.plugin_center = PluginCenter(self.plugin_definitions)
        self.plugin_center.start_requested.connect(self.start_plugin)
        self.plugin_center.stop_requested.connect(self.stop_plugin)
        self.pages.addWidget(self.plugin_center)
        self.relations_page = RelationsPage(self.event_model, self.data_dir / "relations.json")
        self.pages.addWidget(self.relations_page)
        sheet_layout.addWidget(self.pages, 1)
        self.message = label("就绪 · 先启用插件，再点击顶部“开始采集”。", "muted")
        self.message.setWordWrap(True)
        sheet_layout.addWidget(self.message)
        body_layout.addWidget(sheet, 1)
        layout.addWidget(body, 1)
        self.setCentralWidget(root)
        self.navigate(2)

    def build_monitor(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        metrics_panel = QFrame()
        metrics_panel.setObjectName("tray")
        metrics = QHBoxLayout(metrics_panel)
        metrics.setContentsMargins(20, 12, 20, 12)
        metrics.setSpacing(24)
        self.metric_values = []
        self.metric_notes = []
        for title, note in [("已记录帧", "原始记录"), ("参数样本", "在线解析"),
                            ("关联事件", "规则输出"), ("采集拒绝", "累计拒绝帧")]:
            content = QVBoxLayout()
            content.setSpacing(4)
            content.addWidget(label(title, "muted"))
            value = label("0", "metricLead" if not self.metric_values else "metric")
            sub = label(note, "muted")
            content.addWidget(value)
            content.addWidget(sub)
            self.metric_values.append(value)
            self.metric_notes.append(sub)
            metrics.addLayout(content, 1)
        layout.addWidget(metrics_panel)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(24)
        left, content = card()
        heading = QHBoxLayout()
        heading.addWidget(label("采集源", "section"))
        heading.addStretch()
        self.add_button = button("添加源", self.add_source)
        heading.addWidget(self.add_button)
        content.addLayout(heading)
        self.sources = QTableWidget(0, 3)
        self.sources.setHorizontalHeaderLabels(["逻辑源", "状态", "已记录"])
        self.sources.verticalHeader().hide()
        self.sources.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sources.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sources.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sources.setShowGrid(False)
        self.sources.verticalHeader().setDefaultSectionSize(36)
        self.sources.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.sources.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.sources.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.sources.horizontalHeaderItem(2).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sources.itemSelectionChanged.connect(self.update_source_actions)
        self.sources.setMinimumWidth(260)
        content.addWidget(self.sources, 1)
        actions = QGridLayout()
        self.start_button = button("启动源", lambda: self.source_action("start"))
        self.stop_button = button("停止源", lambda: self.source_action("stop"))
        self.replace_button = button("替换插件", self.replace_source)
        self.remove_button = button("移除源", lambda: self.source_action("remove"))
        for widget, row, column in [(self.start_button, 0, 0), (self.stop_button, 0, 1),
                                    (self.replace_button, 1, 0), (self.remove_button, 1, 1)]:
            actions.addWidget(widget, row, column)
        content.addLayout(actions)
        self.source_note = label("选择采集源查看状态。\n合成演示不代表已连接实际硬件。", "muted")
        self.source_note.setWordWrap(True)
        self.source_note.setMinimumHeight(55)
        content.addWidget(self.source_note)
        splitter.addWidget(left)
        right = QSplitter(Qt.Orientation.Vertical)
        right.setHandleWidth(24)
        trend_card, trend = card()
        trend_title = QHBoxLayout()
        trend_title.addWidget(label("参数趋势", "section"))
        trend_title.addStretch()
        self.trend_selector = QComboBox()
        self.trend_selector.setMinimumWidth(220)
        self.trend_selector.currentIndexChanged.connect(self.select_trend)
        trend_title.addWidget(self.trend_selector)
        trend.addLayout(trend_title)
        self.plot = TrendPlot()
        trend.addWidget(self.plot, 1)
        self.display_note = label("显示最近 512 个样本；完整参数与事件保存在会话目录。", "muted")
        trend.addWidget(self.display_note)
        right.addWidget(trend_card)
        tabs_card, tabs_content = card()
        tabs = QTabWidget()
        self.monitor_tabs = tabs
        self.parameter_model = ParameterModel(self)
        self.parameter_proxy = QSortFilterProxyModel(self)
        self.parameter_proxy.setSourceModel(self.parameter_model)
        self.parameter_proxy.setFilterKeyColumn(-1)
        self.parameter_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        parameters = QWidget()
        parameter_layout = QVBoxLayout(parameters)
        parameter_layout.setContentsMargins(0, 4, 0, 0)
        search = QLineEdit()
        search.setPlaceholderText("搜索参数、来源、单位或质量标记…")
        search.textChanged.connect(self.parameter_proxy.setFilterFixedString)
        parameter_layout.addWidget(search)
        self.parameter_view = table_view(self.parameter_proxy)
        self.parameter_view.doubleClicked.connect(self.show_parameter)
        parameter_layout.addWidget(self.parameter_view)
        tabs.addTab(parameters, "实时参数")
        self.event_model = EventModel(self)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        tabs.addTab(self.log, "操作日志")
        tabs_content.addWidget(tabs)
        right.addWidget(tabs_card)
        right.setSizes([240, 330])
        splitter.addWidget(right)
        splitter.setSizes([300, 900])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        return page

    def build_offline(self, configuration):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        inputs, content = card()
        content.addWidget(label("离线归档分析", "section"))
        form = QFormLayout()
        self.offline_archive = QLineEdit()
        self.offline_archive.setPlaceholderText("选择已封存的 .avbus 记录")
        self.offline_config = QLineEdit(str(configuration))
        self.offline_output = QLineEdit()
        for title, field, callback in [("归档文件", self.offline_archive, self.browse_archive),
                                       ("字典 / 规则", self.offline_config, self.browse_offline_config)]:
            row = QHBoxLayout()
            row.addWidget(field)
            row.addWidget(button("浏览", callback))
            form.addRow(title, row)
        form.addRow("新输出目录", self.offline_output)
        content.addLayout(form)
        actions = QHBoxLayout()
        self.inspect_button = button("校验归档完整性", lambda: self.start_offline("inspect"))
        self.analyze_button = button("执行离线分析", lambda: self.start_offline("analyze"))
        actions.addWidget(self.inspect_button)
        actions.addWidget(self.analyze_button)
        actions.addStretch()
        self.offline_status = label("选择归档后可重新运行当前规则。输出目录必须为新目录。", "muted")
        content.addLayout(actions)
        content.addWidget(self.offline_status)
        layout.addWidget(inputs)
        results_card, result_content = card()
        self.offline_summary_label = label("等待分析结果", "section")
        result_content.addWidget(self.offline_summary_label)
        self.offline_tabs = QTabWidget()
        self.offline_parameter_model = ParameterModel(self)
        self.offline_event_model = EventModel(self)
        self.offline_parameter_view = table_view(self.offline_parameter_model)
        self.offline_parameter_view.doubleClicked.connect(
            lambda index: self.show_evidence(self.offline_parameter_model.rows[index.row()]))
        self.offline_event_view = table_view(self.offline_event_model)
        self.offline_event_view.doubleClicked.connect(
            lambda index: self.show_evidence(self.offline_event_model.rows[index.row()]))
        self.offline_detail = QPlainTextEdit()
        self.offline_detail.setReadOnly(True)
        self.offline_tabs.addTab(self.offline_event_view, "关联事件")
        self.offline_tabs.addTab(self.offline_parameter_view, "参数")
        self.offline_tabs.addTab(self.offline_detail, "完整性 / 分析回执")
        result_content.addWidget(self.offline_tabs, 1)
        layout.addWidget(results_card, 1)
        return page

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.monitor_nav.setChecked(index == 0)
        self.offline_nav.setChecked(index == 1)
        self.plugins_nav.setChecked(index == 2)
        self.relations_nav.setChecked(index == 3)
        self.heading.setText(["总线监测工作台", "离线归档分析", "插件中心", "数据关联工作区"][index])

    def browse_configuration(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择字典与规则", self.config_path.text(), "配置 (*.ini)")
        if path:
            self.config_path.setText(path)
            self.config_path.setToolTip(path)

    def browse_storage(self):
        path = QFileDialog.getExistingDirectory(self, "选择 D / E 盘数据目录", str(self.data_dir))
        if path:
            try:
                self.data_dir = writable_data_path(path)
                self.relations_page.definitions_path = self.data_dir / "relations.json"
                self.storage_label.setText(str(self.data_dir))
                self.storage_label.setToolTip(str(self.data_dir))
            except ValueError as error:
                self.on_error(str(error))

    def open_data_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))

    def new_session(self):
        if self.client.active:
            return
        try:
            configuration = Path(self.config_path.text()).resolve()
            if not configuration.is_file():
                raise ValueError("请先选择有效的字典与规则配置。")
            name = self.session_name.text().strip()
            if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " ")):
                raise ValueError("试验名称为空或含有文件名不支持的字符。")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.archive = writable_data_path(str(self.data_dir / f"{name}_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}.avbus"))
            self.parameter_model.clear()
            self.event_model.clear()
            self.samples.clear()
            self.source_configs.clear()
            self.disabled_plugins.clear()
            self.latest_snapshot = {"sources": []}
            self.relations_page.reset_session()
            self.trend_selector.clear()
            self.plot.select(None, [])
            self.sources.setRowCount(0)
            self.last_snapshot_time = self.last_counts = None
            self.last_metadata = None
            self.display_omitted = 0
            self.display_events_omitted = 0
            self.session_is_demo = self.demo_pending
            for value in self.metric_values:
                value.setText("0")
            self.badge.setStyleSheet("")
            self.client.start(self.archive, configuration)
            self.badge.setText("正在创建会话…")
            self.message.setText("正在创建：" + self.archive.name)
            self.message.setToolTip(str(self.archive))
            self.navigate(0)
        except (OSError, ValueError) as error:
            self.demo_pending = False
            self.pending_plugin_sources.clear()
            self.plugin_action_pending = False
            self.on_error(str(error))

    def start_demo(self):
        self.config_path.setText(str(self.demo_configuration))
        self.config_path.setToolTip(str(self.demo_configuration))
        self.demo_pending = True
        self.new_session()

    def refresh_plugins(self):
        self.plugin_center.refresh(self.source_configs, self.latest_snapshot, self.client.active,
            self.client.is_ready, self.busy or self.plugin_action_pending, self.client.closing and self.client.active,
            self.disabled_plugins)
        ready = any(source["status"]["state"] in {"configured", "stopped"}
                    for key, sources in self.plugin_center.sources_by_plugin.items()
                    if key not in self.disabled_plugins for source in sources)
        self.capture_button.setEnabled(ready and self.client.is_ready and not self.busy
                                       and not self.plugin_action_pending and not self.client.closing)
        self.capture_button.setToolTip("开始已启用且就绪的采集源。" if ready else
            "请先启用插件或添加采集源；正在采集或已停用的来源不会重复启动。")

    def start_capture(self):
        self.plugin_action_pending = True
        for key, sources in self.plugin_center.sources_by_plugin.items():
            if key not in self.disabled_plugins:
                for source in sources:
                    if source["status"]["state"] in {"configured", "stopped"}:
                        self.client.send("start", source["id"])
        self.message.setText("正在开始采集…")
        self.navigate(0)
        self.refresh_plugins()

    def queue_demo_sources(self):
        definition = self.plugin_center.cards["demo"].definition
        plugin = definition["path"]
        normalized = os.path.normcase(str(Path(plugin).resolve()))
        for preset in definition["presets"]:
            source = preset["source"]
            if source in self.source_configs and os.path.normcase(str(Path(self.source_configs[source][1]).resolve())) != normalized:
                raise ValueError("演示来源 ID 已被其他插件使用：" + source)
        for preset in definition["presets"]:
            source = preset["source"]
            if source not in self.source_configs:
                self.client.send("add", source, plugin, preset["config"])

    def start_plugin(self, definition):
        try:
            if definition["mode"] == "demo":
                if self.client.active and not self.session_is_demo:
                    raise ValueError("请先结束当前试验会话，再启动使用配套字典的演示插件。")
                self.plugin_action_pending = True
                if self.client.active:
                    self.queue_demo_sources()
                else:
                    self.start_demo()
            else:
                existing = self.plugin_center.sources_by_plugin[definition["id"]]
                if self.client.active and existing:
                    self.disabled_plugins.discard(definition["id"])
                else:
                    if definition["mode"] == "replay":
                        dialog = ReplaySetupDialog(self.data_dir, self)
                        if dialog.exec() != QDialog.DialogCode.Accepted:
                            return
                        args = (dialog.source.text(), definition["path"], f"path={dialog.archive.text()};speed={dialog.speed.value()}")
                    else:
                        dialog = SourceDialog(self.bin_dir, parent=self)
                        dialog.setWindowTitle("启用插件 · " + definition["name"])
                        dialog.plugin.setText(definition["path"])
                        dialog.plugin.setReadOnly(True)
                        dialog.plugin_browse.setEnabled(False)
                        if dialog.exec() != QDialog.DialogCode.Accepted:
                            return
                        args = dialog.values()
                    self.plugin_action_pending = True
                    if not self.client.active:
                        self.pending_plugin_sources.append(args)
                        self.new_session()
                    else:
                        self.client.send("add", *args)
            if self.client.active:
                self.disabled_plugins.discard(definition["id"])
                self.message.setText("正在准备：" + definition["name"] + "。就绪后请点击顶部“开始采集”。")
                self.navigate(0)
            self.refresh_plugins()
        except ValueError as error:
            self.plugin_action_pending = False
            self.on_error(str(error))
            self.refresh_plugins()

    def stop_plugin(self, definition):
        self.plugin_action_pending = True
        self.disabled_plugins.add(definition["id"])
        for source in self.plugin_center.sources_by_plugin[definition["id"]]:
            if source["status"]["state"] != "stopped":
                self.client.send("stop", source["id"])
        self.message.setText("正在停用：" + definition["name"] + "。已记录数据保留在当前会话中。")
        self.refresh_plugins()

    def on_ready(self, message):
        self.log_message("会话已创建：" + message["archive"])
        self.log_message(f"后端版本 {message['build']['version']} · {message['build']['revision']}")
        self.message.setText("会话已就绪；请点击“开始采集”。归档：" + self.archive.name)
        if self.demo_pending:
            self.demo_pending = False
            self.queue_demo_sources()
            self.badge.setText("合成演示 · 等待开始采集")
        else:
            self.badge.setText("会话已就绪 · 等待采集源")
        for source, plugin, config in self.pending_plugin_sources:
            self.client.send("add", source, plugin, config)
        self.pending_plugin_sources.clear()
        self.on_busy(self.busy)

    def end_session(self):
        self.client.send("quit")
        self.badge.setText("正在停止采集并封存…")
        self.end_button.setEnabled(False)
        self.update_source_actions()

    def selected_source(self):
        row = self.sources.currentRow()
        return self.sources.item(row, 0).text() if row >= 0 else None

    def add_source(self):
        dialog = SourceDialog(self.bin_dir, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.client.send("add", *dialog.values())

    def replace_source(self):
        source = self.selected_source()
        if source:
            dialog = SourceDialog(self.bin_dir, self.source_configs[source], self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.client.send("replace", *dialog.values())

    def source_action(self, action):
        source = self.selected_source()
        if source:
            self.client.send(action, source)

    def on_command(self, command, arguments, ok, payload):
        if command == "snapshot":
            if not ok:
                self.on_error("状态读取失败：" + payload)
            return
        if ok:
            if command in {"add", "replace"}:
                self.source_configs[arguments[0]] = tuple(arguments)
            elif command == "remove":
                del self.source_configs[arguments[0]]
            self.log_message(command + " " + (str(arguments[0]) if arguments else "") + " · 完成")
        else:
            self.on_error(command + " 失败：" + payload)

    def on_snapshot(self, snapshot):
        self.latest_snapshot = snapshot
        if not self.client.queue:
            self.plugin_action_pending = False
        now = time.monotonic()
        rate = 0
        if self.last_counts:
            rate = (snapshot["recorded"] - self.last_counts[1]) / (now - self.last_counts[0])
        self.last_counts = now, snapshot["recorded"]
        self.last_snapshot_time = now
        if self.message.text() == "正在开始采集…" and any(
                source["status"]["state"] == "running" for source in snapshot["sources"]):
            self.message.setText("采集中 · 原始归档：" + self.archive.name)
        for value, count in zip(self.metric_values,
                                [snapshot["recorded"], snapshot["samples"], snapshot["results"], snapshot["dropped"]]):
            value.setText(f"{count:,}")
        self.metric_notes[0].setText(f"{rate:,.0f} 帧 / s")
        self.metric_notes[1].setText(f"有效 {snapshot['usable']:,} · 质量拒绝 {snapshot['quality_rejected']:,}")
        self.metric_notes[2].setText(f"分析游标 {snapshot['analysis_cursor']:,}")
        self.metric_notes[3].setText(f"记录队列 {snapshot['queued_bytes'] / 1024:.1f} KiB")
        selected = self.selected_source()
        self.sources.blockSignals(True)
        self.sources.setRowCount(len(snapshot["sources"]))
        states = {"loaded": "已加载", "configured": "已配置", "running": "运行中", "stopped": "已停止", "faulted": "故障"}
        for row, source in enumerate(snapshot["sources"]):
            status = source["status"]
            values = [source["id"], states[status["state"]], f"{source['recorded']:,}"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(json.dumps(source, ensure_ascii=False, indent=2))
                if col == 1:
                    item.setForeground(QColor(NEGATIVE if status["state"] == "faulted" else INK))
                elif col == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.sources.setItem(row, col, item)
            if source["id"] == selected:
                self.sources.selectRow(row)
        self.sources.blockSignals(False)
        self.update_source_actions()
        self.samples.extend(snapshot["parameters"])
        self.parameter_model.update_samples(snapshot["parameters"])
        self.event_model.append(snapshot["events"])
        self.relations_page.feed(snapshot["parameters"], snapshot["display_samples_omitted"])
        self.plot.add(snapshot["parameters"])
        known = {self.trend_selector.itemData(i) for i in range(self.trend_selector.count())}
        for sample in self.parameter_model.rows:
            key = sample["source"], sample["parameter"]
            if key not in known:
                self.trend_selector.addItem(f"{key[0]} / {key[1]}", key)
                known.add(key)
        self.display_omitted += snapshot["display_samples_omitted"]
        self.display_events_omitted += snapshot["display_events_omitted"]
        self.display_note.setText(f"趋势最近 512 点 · 界面跳过 {self.display_omitted:,} 样本 / {self.display_events_omitted:,} 事件 · 完整结果写入文件")
        self.display_note.setToolTip(f"参数表最多 4096 项；超过表容量的更新：{self.parameter_model.omitted:,}。事件表保留最近 200 条。")
        if not snapshot["healthy"]:
            self.badge.setText("原始记录故障 · 查看日志")
            self.message.setText(snapshot["failure"])
        elif not snapshot["analysis_healthy"]:
            self.badge.setText("分析不完整 · 原始记录仍在采集")
            self.message.setText(snapshot["analysis_failure"])
        elif snapshot["capture_degraded"]:
            self.badge.setText("采集降级 · 请查看源状态及日志")
        else:
            running = sum(source["status"]["state"] == "running" for source in snapshot["sources"])
            self.badge.setText(f"{'合成演示' if self.session_is_demo else '正在记录'} · {running} 个源运行中" if running else
                              f"{'合成演示' if self.session_is_demo else '会话已就绪'} · 等待开始采集")
        degraded = not snapshot["healthy"] or not snapshot["analysis_healthy"] or snapshot["capture_degraded"]
        self.badge.setStyleSheet(ALERT_STYLE if degraded else RUNNING_STYLE if any(
            source["status"]["state"] == "running" for source in snapshot["sources"]) else "")
        self.refresh_plugins()

    def select_trend(self, index):
        self.plot.select(self.trend_selector.itemData(index) if index >= 0 else None, self.samples)

    def on_sealed(self, metadata):
        self.last_metadata = metadata
        healthy = metadata["recording_healthy"]
        complete = healthy and metadata["capture_status"] == "complete" and metadata["analysis_status"] == "complete"
        self.badge.setText("已封存 · 采集与分析完整" if complete else "会话结束 · 请检查降级 / 故障回执")
        self.badge.setStyleSheet("" if complete else ALERT_STYLE)
        self.metric_values[0].setText(f"{metadata['recorded']:,}")
        self.metric_values[1].setText(f"{metadata['samples']:,}")
        self.metric_values[2].setText(f"{metadata['results']:,}")
        self.metric_values[3].setText(f"{metadata['dropped']:,}")
        self.metric_notes[0].setText("原始记录已封存" if healthy else "原始记录发生故障")
        self.metric_notes[1].setText(f"有效 {metadata['usable']:,} · 质量拒绝 {metadata['quality_rejected']:,}")
        self.metric_notes[2].setText(f"最终分析游标 {metadata['analysis_cursor']:,}")
        for row in range(self.sources.rowCount()):
            self.sources.item(row, 1).setText("已停止")
        try:
            final_samples = read_jsonl_tail(Path(str(self.archive) + ".analysis") / "parameters.jsonl")
            final_events = read_jsonl_tail(Path(str(self.archive) + ".analysis") / "events.jsonl", 200)
            self.parameter_model.update_samples(final_samples)
            self.event_model.clear()
            self.event_model.append(final_events)
            self.samples.clear()
            self.samples.extend(final_samples)
            self.select_trend(self.trend_selector.currentIndex())
            if metadata["analysis_status"] == "complete":
                first_index = metadata["samples"] - len(final_samples)
                unseen = [sample for index, sample in enumerate(final_samples, first_index + 1) if index > self.client.sample_cursor]
                self.relations_page.feed(unseen)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.log_message("封存后的显示数据读取失败：" + str(error))
        self.message.setText(f"已结束：记录 {metadata['recorded']:,} 帧，拒绝 {metadata['dropped']:,} 帧；回执 {self.archive}.session.json")
        self.log_message("会话结束，已收到封存回执：" + metadata["capture_status"] + " / " + metadata["analysis_status"])
        self.offline_archive.setText(str(self.archive))
        self.offline_config.setText(str(self.archive) + ".analysis/configuration.ini")
        self.offline_output.setText(str(self.data_dir / ("离线分析_" + uuid4().hex[:8])))

    def on_active(self, active):
        if not active:
            self.plugin_action_pending = False
        for widget in (self.new_button, self.config_button, self.storage_button, self.session_name):
            widget.setEnabled(not active)
        self.end_button.setEnabled(active and not self.client.closing)
        self.update_source_actions()
        self.refresh_plugins()

    def on_busy(self, busy):
        self.busy = busy
        self.update_source_actions()
        self.refresh_plugins()

    def update_source_actions(self):
        enabled = self.client.active and self.client.is_ready and not self.client.closing and not self.busy
        self.add_button.setEnabled(enabled)
        selected = self.selected_source()
        for widget in (self.start_button, self.stop_button, self.replace_button, self.remove_button):
            widget.setEnabled(enabled and selected is not None)
        if any(key in self.disabled_plugins and any(source["id"] == selected for source in sources)
               for key, sources in self.plugin_center.sources_by_plugin.items()):
            self.start_button.setEnabled(False)
        if selected:
            source = json.loads(self.sources.item(self.sources.currentRow(), 0).toolTip())
            status = source["status"]
            self.source_note.setText(f"{selected} · 代次 {status['generation']}\n"
                                     f"发出 {status['emitted']:,} · 拒绝 {status['rejected']:,} · 错误 {status['errors']:,}\n"
                                     + status["failure"])

    def check_freshness(self):
        if self.client.active and self.last_snapshot_time and not self.client.closing:
            age = time.monotonic() - self.last_snapshot_time
            if age > 2:
                self.badge.setText(f"等待后端响应 · {age:.0f} s 未刷新")
                self.badge.setStyleSheet(ALERT_STYLE)

    def log_message(self, text):
        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {text}")

    def on_error(self, text):
        self.message.setText(text)
        self.log_message("错误 · " + text)
        if not self.client.active:
            self.badge.setText("操作失败 · 查看日志")
        self.badge.setStyleSheet(ALERT_STYLE)

    def on_exit(self, code):
        if self.close_pending:
            self.close()

    def show_parameter(self, index):
        source_index = self.parameter_proxy.mapToSource(index)
        self.show_evidence(self.parameter_model.rows[source_index.row()])

    def show_evidence(self, evidence):
        dialog = QDialog(self)
        dialog.setWindowTitle("数据与证据溯源")
        dialog.resize(780, 580)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(evidence, ensure_ascii=False, indent=2))
        layout.addWidget(text)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        dialog.exec()

    def browse_archive(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择已封存归档", str(self.data_dir), "总线归档 (*.avbus)")
        if path:
            self.offline_archive.setText(path)
            self.offline_output.setText(str(self.data_dir / ("离线分析_" + uuid4().hex[:8])))

    def browse_offline_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择字典与规则", self.offline_config.text(), "配置 (*.ini)")
        if path:
            self.offline_config.setText(path)

    def start_offline(self, mode):
        if self.offline_mode:
            return
        try:
            archive = Path(self.offline_archive.text()).resolve()
            if not archive.is_file():
                raise ValueError("请选择已存在的归档文件。")
            arguments = ["--inspect", str(archive)]
            if mode == "analyze":
                configuration = Path(self.offline_config.text()).resolve()
                if not configuration.is_file():
                    raise ValueError("分析配置不存在。")
                output = writable_data_path(self.offline_output.text())
                if output.exists() or not self.offline_output.text().strip():
                    raise ValueError("请选择一个尚不存在的新输出目录。")
                output.parent.mkdir(parents=True, exist_ok=True)
                self.analysis_output = output
                arguments = ["--analyze", str(configuration), str(archive), str(output)]
            self.offline_mode = mode
            self.offline_summary = None
            self.offline_stdout.clear()
            self.offline_stderr.clear()
            self.offline_parameter_model.clear()
            self.offline_event_model.clear()
            self.offline_detail.clear()
            self.offline_summary_label.setText("正在校验归档…" if mode == "inspect" else "正在执行分析…")
            self.inspect_button.setEnabled(False)
            self.analyze_button.setEnabled(False)
            self.offline_status.setText("正在校验归档…" if mode == "inspect" else "正在执行离线分析…")
            suffix = ".exe" if os.name == "nt" else ""
            self.offline_process.start(str(self.bin_dir / ("bus_analyze" + suffix)), arguments)
        except (OSError, ValueError) as error:
            self.offline_status.setText(str(error))
            self.on_error(str(error))

    def read_offline_stdout(self):
        self.offline_stdout.extend(bytes(self.offline_process.readAllStandardOutput()))

    def read_offline_stderr(self):
        self.offline_stderr.extend(bytes(self.offline_process.readAllStandardError()))

    def offline_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.offline_mode = None
            self.inspect_button.setEnabled(True)
            self.analyze_button.setEnabled(True)
            self.offline_status.setText("分析程序无法启动：" + self.offline_process.errorString())

    def offline_finished(self, exit_code, exit_status):
        mode = self.offline_mode
        self.offline_mode = None
        self.inspect_button.setEnabled(True)
        self.analyze_button.setEnabled(True)
        self.read_offline_stdout()
        self.read_offline_stderr()
        try:
            if exit_code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
                raise ValueError(self.offline_stderr.decode("utf-8", errors="replace").strip() or f"分析进程退出：{exit_code}")
            if mode == "inspect":
                summary = json.loads(self.offline_stdout.decode("utf-8"))
                self.offline_summary_label.setText(f"归档校验通过 · {summary['records']:,} 帧")
            else:
                summary = json.loads((self.analysis_output / "summary.json").read_text(encoding="utf-8"))
                self.offline_parameter_model.clear()
                self.offline_event_model.clear()
                self.offline_parameter_model.update_samples(read_jsonl_tail(self.analysis_output / "parameters.jsonl"))
                self.offline_event_model.append(read_jsonl_tail(self.analysis_output / "events.jsonl", 200))
                self.offline_summary_label.setText(f"分析完成 · {summary['records']:,} 帧 / {summary['samples']:,} 样本 / {summary['results']:,} 事件")
            self.offline_summary = summary
            self.offline_detail.setPlainText(json.dumps(summary, ensure_ascii=False, indent=2))
            self.offline_status.setText("校验通过，封存摘要与数据一致。" if mode == "inspect" else "完成 · 表格显示末尾数据，完整结果已写入输出目录。")
            if mode == "inspect":
                self.offline_tabs.setCurrentIndex(2)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            self.offline_summary = None
            self.offline_summary_label.setText("操作失败 · 结果未通过校验")
            self.offline_status.setText("失败：" + str(error))
            self.log_message("离线分析失败：" + str(error))

    def closeEvent(self, event):
        if self.offline_mode:
            self.message.setText("离线分析正在运行；完成后可关闭窗口。")
            event.ignore()
        elif self.client.active:
            self.close_pending = True
            self.end_session()
            event.ignore()
        else:
            event.accept()
