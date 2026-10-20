from collections import deque
from configparser import Error as ConfigurationError
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from PySide6.QtCore import QProcess, QSize, QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
    QStackedWidget, QStatusBar, QTableView, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from .backend import BackendClient, read_jsonl_tail, writable_data_path
from .configuration_editor import ConfigurationEditor
from . import __version__
from .models import EventModel, ParameterModel, TrendPlot
from .plugin_center import PluginCenter, ReplaySetupDialog
from .relations_page import RelationsPage
from .theme import ACTIVE, ALERT_STYLE, INK, MUTED, NEGATIVE, RUNNING_STYLE, STYLE
from .widgets import ThinSplitter
from .ui_assets import ASSET_DIR, artwork, icon
from .project import Project, remember_project, write_json
from .jobs import start_analysis_process
from .history_page import HistoryPage
from .evidence import EvidenceDialog


def label(text, name=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def button(text, callback, primary=False, icon_name=None):
    widget = QPushButton(text)
    if icon_name:
        widget.setIcon(icon(icon_name, light=primary))
        widget.setIconSize(QSize(18, 18))
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
    view.horizontalHeader().setMinimumSectionSize(80)
    view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    return view


class SourceDialog(QDialog):
    def __init__(self, bin_dir, values=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("替换采集插件" if values else "添加采集源")
        self.resize(620, 250)
        form = QFormLayout(self)
        self.source = QLineEdit(values[0] if values else "sensor_a")
        self.source.setReadOnly(values is not None)
        self.source.setToolTip("这一路采集数据入口的唯一名称；同一插件可以创建多路采集源，名称需与字典中的 source 对应。")
        suffix = ".dll" if os.name == "nt" else ".so"
        self.plugin = QLineEdit(values[1] if values else str(bin_dir / ("bus_simulated" + suffix)))
        self.config = QLineEdit(values[2] if values else "period_ms=20;initial_milli=20000;step_milli=10")
        row = QHBoxLayout()
        row.addWidget(self.plugin)
        self.plugin_browse = button("浏览", self.browse, icon_name="browse")
        row.addWidget(self.plugin_browse)
        form.addRow("采集源名称（ID）", self.source)
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
        self.configuration_path = configuration.resolve()
        self.demo_configuration = Path(__file__).resolve().parents[2] / "config" / "desktop-demo.ini"
        self.plugin_definitions = json.loads(Path(__file__).with_name("plugins.json").read_text(encoding="utf-8"))
        suffix = ".dll" if os.name == "nt" else ".so"
        for definition in self.plugin_definitions:
            definition["path"] = str(bin_dir / (definition["library"] + suffix))
        self.data_dir = writable_data_path(str(data_dir))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = self.data_dir
        self.project = None
        self.current_run = None
        self.restore_sources = []
        self.capture_after_ready = False
        self.relation_process = QProcess(self)
        self.relation_buffer = bytearray()
        self.latest_complete = None
        self.relation_process.readyReadStandardOutput.connect(self.read_complete_relations)
        self.relation_process.finished.connect(self.relations_finished)
        self.relation_process.errorOccurred.connect(lambda error: self.on_error("完整关系分析进程：" + self.relation_process.errorString()))
        self.export_process = QProcess(self)
        self.export_process.finished.connect(self.export_finished)
        self.export_process.errorOccurred.connect(lambda error:self.on_error("导出进程："+self.export_process.errorString()))
        self.raw_process = QProcess(self)
        self.raw_process.finished.connect(self.raw_finished)
        self.raw_process.errorOccurred.connect(lambda error:self.on_error("报文读取进程："+self.raw_process.errorString()))
        self.offline_relation_process = QProcess(self)
        self.offline_relation_process.finished.connect(self.offline_relations_finished)
        self.offline_relation_process.errorOccurred.connect(lambda error:self.on_error("离线关系分析进程："+self.offline_relation_process.errorString()))
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
        self.archive_root = None
        self.demo_pending = False
        self.session_is_demo = False
        self.close_pending = False
        self.busy = False
        self.configuration_customized = False
        self.configuration_candidate = None
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
        self.configuration_process = QProcess(self)
        self.configuration_process.setProcessEnvironment(self.client.process.processEnvironment())
        self.configuration_process.finished.connect(self.configuration_validated)
        self.configuration_process.errorOccurred.connect(self.configuration_validation_error)
        self.setWindowTitle(f"AEROBUS v{__version__} · 地面总线试验与分析工作台")
        self.setWindowIcon(icon("monitor"))
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
        root.setObjectName("appBackground")
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
        QSvgRenderer(str(ASSET_DIR / "aerobus.svg")).render(painter)
        painter.end()
        self.brand_mark.setPixmap(mark)
        self.brand_mark.setFixedSize(28, 28)
        identity.addWidget(self.brand_mark)
        identity.addWidget(label("AEROBUS", "brand"))
        identity.addStretch()
        navigation.addLayout(identity)
        navigation.addWidget(label("地面总线试验", "muted"))
        navigation.addSpacing(28)
        self.monitor_nav = button("监测工作台", lambda: self.navigate(0), icon_name="monitor")
        self.offline_nav = button("离线分析", lambda: self.navigate(1), icon_name="archive")
        self.plugins_nav = button("插件中心", lambda: self.navigate(2), icon_name="plugins")
        self.relations_nav = button("数据关联", lambda: self.navigate(3), icon_name="relations")
        self.project_nav = button("项目设置", lambda: self.navigate(4), icon_name="settings")
        self.history_nav = button("历史与报告", lambda:self.navigate(5), icon_name="archive")
        self.project_nav.setToolTip("设置项目名称和根目录，在表单中编辑解析字典、分析规则和时间处理配置。")
        for nav in (self.plugins_nav, self.monitor_nav, self.relations_nav, self.history_nav, self.offline_nav, self.project_nav):
            nav.setCheckable(True)
            nav.setIconSize(QSize(20, 20))
            navigation.addWidget(nav)
        navigation.addStretch()
        navigation.addWidget(label("原始归档 · 参数分析", "muted"))
        navigation.addWidget(label(f"v{__version__}", "muted"))
        self.capture_controls = QFrame()
        self.capture_controls.setObjectName("captureControls")
        controls = QVBoxLayout(self.capture_controls)
        controls.setContentsMargins(0, 12, 0, 0)
        controls.setSpacing(8)
        self.badge = label("等待中", "badge")
        self.badge.setToolTip("当前项目的实际状态；详细回执可在日志和离线分析中查看。")
        controls.addWidget(self.badge)
        self.new_button = button("新建项目", self.new_project, icon_name="new_session")
        self.new_button.setToolTip("使用项目设置中的名称和已保存配置创建新项目；已有归档保留。运行中的项目需先结束并封存。")
        self.capture_button = button("开始采集", self.start_capture, True, icon_name="capture_start")
        self.end_button = button("结束并封存", self.end_session, icon_name="capture_stop")
        self.end_button.setToolTip("停止本项目全部采集源，写完剩余数据并完成归档；等待显示“已封存”。")
        for action in (self.new_button, self.capture_button, self.end_button):
            controls.addWidget(action)
        navigation.addWidget(self.capture_controls)
        layout.addWidget(rail)
        body = QWidget()
        body.setObjectName("frame")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 8, 8, 8)
        body_layout.setSpacing(0)
        sheet = QFrame()
        sheet.setObjectName("sheet")
        sheet_layout = QVBoxLayout(sheet)
        sheet_layout.setContentsMargins(24, 24, 24, 20)
        sheet_layout.setSpacing(24)
        title_row = QHBoxLayout()
        self.heading = label("插件中心", "heading")
        title_row.addWidget(self.heading)
        title_row.addStretch()
        sheet_layout.addLayout(title_row)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.build_monitor())
        self.pages.addWidget(self.build_offline(configuration))
        self.plugin_center = PluginCenter(self.plugin_definitions)
        self.plugin_center.start_requested.connect(self.start_plugin)
        self.plugin_center.stop_requested.connect(self.stop_plugin)
        self.pages.addWidget(self.plugin_center)
        self.relations_page = RelationsPage(self.event_model, self.data_dir / "relations.json")
        self.relations_page.definitions_changed.connect(self.relations_changed)
        self.pages.addWidget(self.relations_page)
        self.project_page = self.build_project_settings(configuration)
        self.pages.addWidget(self.project_page)
        self.history_page = HistoryPage()
        self.history_page.evidence_requested.connect(self.request_raw_record)
        self.history_page.export_requested.connect(self.export_run)
        self.pages.addWidget(self.history_page)
        sheet_layout.addWidget(self.pages, 1)
        self.message = label("就绪 · 先启用插件，再点击左下角“开始采集”。", "muted")
        self.message.setWordWrap(True)
        sheet_layout.addWidget(self.message)
        body_layout.addWidget(sheet, 1)
        layout.addWidget(body, 1)
        self.setCentralWidget(root)
        self.status_bar = QStatusBar()
        self.status_bar.setSizeGripEnabled(False)
        self.status_bar.setContentsMargins(20, 0, 16, 0)
        self.recording_status = label("等待中", "muted")
        self.archive_path = QLineEdit()
        self.archive_path.setObjectName("archivePath")
        self.archive_path.setReadOnly(True)
        self.archive_path.setPlaceholderText("新建项目后显示相对根目录的归档路径")
        self.status_bar.addPermanentWidget(self.recording_status)
        self.status_bar.addPermanentWidget(label("· 自动保存为：", "muted"))
        self.status_bar.addPermanentWidget(self.archive_path, 1)
        self.setStatusBar(self.status_bar)
        self.update_relative_paths()
        self.navigate(2)

    def build_project_settings(self, configuration):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.project_tabs = QTabWidget()
        basics = QWidget()
        basic_layout = QVBoxLayout(basics)
        basic_layout.setContentsMargins(0, 16, 0, 0)
        basic_layout.setSpacing(16)
        project_actions = QHBoxLayout()
        self.open_project_button = button("打开项目",self.browse_project,icon_name="folder")
        self.open_project_button.setToolTip("打开 .aerobus.json 项目文档，恢复配置、关系、采集来源与历史记录；不会自动开始采集。")
        self.save_project_button = button("保存项目",lambda:self.save_project(notify=True),icon_name="settings")
        self.save_project_button.setToolTip("保存当前项目设置和页面布局；每次采集、关系与配置修改也会保存对应记录。")
        self.recent_projects = QComboBox()
        self.recent_projects.setToolTip("最近打开的项目；项目文档与内部资源使用相对路径，可整体复制项目根目录。")
        self.recent_projects.activated.connect(self.open_recent_project)
        for action in (self.open_project_button,self.save_project_button,self.recent_projects): project_actions.addWidget(action)
        basic_layout.addLayout(project_actions)
        self.update_recent_projects()
        settings, fields = card()
        fields.addWidget(label("项目与数据", "section"))
        name_label = label("项目名称")
        name_label.setToolTip("为本次测试起一个便于识别的名称；新建项目时用于生成归档文件名。")
        fields.addWidget(name_label)
        self.project_name = QLineEdit("地面试验")
        self.project_name.setToolTip("例如：1553B联调。新建项目时生成“名称_日期_时间_编号.avbus”；项目运行期间锁定名称。")
        self.project_name.setMinimumHeight(38)
        fields.addWidget(self.project_name)
        fields.addWidget(label("配置模板"))
        self.preset_selector = QComboBox()
        self.preset_selector.addItem("当前配置", str(configuration))
        self.preset_selector.addItem("合成演示", str(self.demo_configuration))
        self.preset_selector.addItem("1553B软件验证", str(Path(__file__).resolve().parents[2] / "config" / "mil1553b-demo.ini"))
        self.preset_selector.setToolTip("选择要编辑的配置模板。切换到“字典与规则”可直接修改参数和规则，保存后用于新项目。")
        self.preset_selector.activated.connect(lambda index: self.load_configuration(self.preset_selector.itemData(index)))
        fields.addWidget(self.preset_selector)
        config_row = QHBoxLayout()
        self.config_path = QLineEdit(configuration.name)
        self.config_path.setReadOnly(True)
        self.config_path.setToolTip("当前配置；根目录内显示相对路径，悬停查看完整路径。\n" + str(configuration))
        self.config_button = button("导入配置", self.browse_configuration, icon_name="browse")
        self.config_button.setToolTip("导入已有配置后在“字典与规则”页直接编辑，无需在外部修改文件。")
        config_row.addWidget(self.config_path, 1)
        config_row.addWidget(self.config_button)
        fields.addLayout(config_row)
        storage_label = label("项目根目录")
        storage_label.setToolTip("本项目配置和归档的根目录，可输入或选择 D/E 盘目录；运行期间锁定。下方路径均以此目录为基准。")
        fields.addWidget(storage_label)
        storage_row = QHBoxLayout()
        self.storage_label = QLineEdit(str(self.data_dir))
        self.storage_label.setMinimumHeight(38)
        self.storage_label.setToolTip("输入项目根目录后按回车或移开焦点应用；根目录须位于 D 或 E 盘，项目运行期间锁定。")
        self.storage_label.editingFinished.connect(self.apply_root_directory)
        self.storage_button = button("选择根目录", self.browse_storage, icon_name="browse")
        self.storage_button.setToolTip("选择项目根目录；归档、配置和分析输出保存在其中，下方显示相对路径。")
        self.open_storage_button = button("打开目录", self.open_data_directory, icon_name="folder")
        self.open_storage_button.setToolTip("在文件资源管理器中打开项目根目录。")
        storage_row.addWidget(self.storage_label, 1)
        storage_row.addWidget(self.storage_button)
        storage_row.addWidget(self.open_storage_button)
        fields.addLayout(storage_row)
        self.relative_paths = label("归档：./项目名称_日期_时间_编号.avbus\n配置：config/\n分析：./归档文件名.avbus.analysis/", "muted")
        self.relative_paths.setWordWrap(True)
        self.relative_paths.setToolTip("这些是相对于项目根目录的路径；底部在新建项目后显示实际归档的相对路径。")
        fields.addWidget(self.relative_paths)
        basic_layout.addWidget(settings)
        help_text = label("在“字典与规则”页直接编辑并保存配置。启用插件后，点击左下角“开始采集”。", "muted")
        help_text.setWordWrap(True)
        basic_layout.addWidget(help_text)
        basic_layout.addStretch()
        self.project_tabs.addTab(basics, "项目与目录")
        editor_page = QWidget()
        editor_layout = QVBoxLayout(editor_page)
        editor_layout.setContentsMargins(0, 16, 0, 0)
        self.config_editor = ConfigurationEditor(configuration)
        self.config_editor.dirty_changed.connect(self.configuration_edited)
        editor_layout.addWidget(self.config_editor, 1)
        actions = QHBoxLayout()
        self.config_feedback = label("已载入", "muted")
        self.config_feedback.setWordWrap(True)
        actions.addWidget(self.config_feedback, 1)
        self.reload_configuration_button = button("放弃修改", self.reload_configuration)
        self.reload_configuration_button.setToolTip("重新载入当前已保存配置，放弃尚未保存的表单修改。")
        self.save_configuration_button = button("保存配置", self.save_configuration, True, icon_name="settings")
        self.save_configuration_button.setToolTip("校验全部参数、引用和规则，成功后保存到根目录的 config/，用于下一项目；原模板保留。")
        actions.addWidget(self.reload_configuration_button)
        actions.addWidget(self.save_configuration_button)
        editor_layout.addLayout(actions)
        self.project_tabs.addTab(editor_page, "字典与规则")
        layout.addWidget(self.project_tabs, 1)
        return page

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
        splitter = ThinSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(24)
        left, content = card()
        heading = QHBoxLayout()
        source_heading = label("插件采集源", "section")
        source_heading.setToolTip("插件提供采集能力，采集源是插件创建的具体数据入口。一个插件可以创建多路源，单独停止某一路不影响其他源。")
        heading.addWidget(source_heading)
        heading.addStretch()
        self.add_button = button("添加源", self.add_source, icon_name="add_source")
        heading.addWidget(self.add_button)
        content.addLayout(heading)
        self.sources = QTableWidget(0, 3)
        self.sources.setHorizontalHeaderLabels(["采集源", "状态", "已记录"])
        self.sources.horizontalHeaderItem(0).setToolTip("每一路采集源的唯一名称；选择一行，在下方查看所属插件。合成演示插件包含四个采集源。")
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
        self.replace_button = button("替换插件", self.replace_source, icon_name="replace_source")
        self.remove_button = button("移除源", lambda: self.source_action("remove"))
        self.add_button.setToolTip("使用插件创建一个新的采集数据入口；同一插件可以创建多路源。")
        self.start_button.setToolTip("只启动当前选中的这一采集源；左下角“开始采集”会启动所有已启用且就绪的来源。")
        self.stop_button.setToolTip("只停止当前选中的这一采集源，同一插件下的其他来源继续运行。")
        self.replace_button.setToolTip("更换当前采集源使用的插件及配置，保留逻辑源名称；新来源进入就绪状态。")
        self.remove_button.setToolTip("从当前项目移除选中的采集源，已经保存的原始数据仍保留。")
        for widget, row, column in [(self.start_button, 0, 0), (self.stop_button, 0, 1),
                                    (self.replace_button, 1, 0), (self.remove_button, 1, 1)]:
            actions.addWidget(widget, row, column)
        content.addLayout(actions)
        self.source_note = label("插件提供采集能力，一个插件可创建多路采集源。\n选择一行查看所属插件与运行状态。", "muted")
        self.source_note.setWordWrap(True)
        self.source_note.setMinimumHeight(76)
        content.addWidget(self.source_note)
        splitter.addWidget(left)
        right = ThinSplitter(Qt.Orientation.Vertical)
        right.setHandleWidth(24)
        trend_card, trend = card()
        trend.setContentsMargins(16, 12, 16, 12)
        trend_title = QHBoxLayout()
        trend_title.addWidget(label("参数趋势", "section"))
        trend_title.addStretch()
        self.trend_selector = QComboBox()
        self.trend_selector.setMinimumWidth(220)
        self.trend_selector.currentIndexChanged.connect(self.select_trend)
        trend_title.addWidget(self.trend_selector)
        trend.addLayout(trend_title)
        self.plot = TrendPlot()
        self.plot.setMinimumHeight(124)
        trend.addWidget(self.plot, 1)
        self.display_note = label("显示最近 512 个样本；完整参数与事件保存在项目目录。", "muted")
        trend.addWidget(self.display_note)
        right.addWidget(trend_card)
        tabs_card, tabs_content = card()
        tabs_content.setContentsMargins(16, 12, 16, 12)
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
            row.addWidget(button("浏览", callback, icon_name="browse"))
            form.addRow(title, row)
        form.addRow("新输出目录", self.offline_output)
        content.addLayout(form)
        actions = QHBoxLayout()
        self.inspect_button = button("校验归档完整性", lambda: self.start_offline("inspect"), icon_name="inspect")
        self.analyze_button = button("执行离线分析", lambda: self.start_offline("analyze"), icon_name="analyze")
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
        self.offline_results = QStackedWidget()
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        self.offline_empty = QLabel()
        self.offline_empty.setPixmap(artwork("empty_archive").scaled(260, 120,
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        empty_layout.addWidget(self.offline_empty, alignment=Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(label("选择已保存的 .avbus 归档，校验完整性或重新分析。", "muted"),
                               alignment=Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch()
        self.offline_results.addWidget(empty)
        self.offline_results.addWidget(self.offline_tabs)
        result_content.addWidget(self.offline_results, 1)
        layout.addWidget(results_card, 1)
        return page

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.monitor_nav.setChecked(index == 0)
        self.offline_nav.setChecked(index == 1)
        self.plugins_nav.setChecked(index == 2)
        self.relations_nav.setChecked(index == 3)
        self.project_nav.setChecked(index == 4)
        self.history_nav.setChecked(index == 5)
        self.heading.setText(["总线监测工作台", "离线归档分析", "插件中心", "数据关联工作区", "项目设置", "历史与试验报告"][index])
        if index == 5 and self.project:
            self.history_page.set_project(self.project)

    def browse_configuration(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入字典与规则", str(self.configuration_path), "配置 (*.ini)")
        if path:
            self.load_configuration(path)

    def load_configuration(self, path, customized=True):
        try:
            self.config_editor.load_file(path)
        except (OSError, UnicodeError, ConfigurationError, ValueError) as error:
            self.on_error("配置载入失败：" + str(error))
            return
        self.configuration_path = Path(path).resolve()
        self.configuration_customized = customized
        self.config_feedback.setText("已载入 · 可在此编辑")
        self.update_relative_paths()

    def reload_configuration(self):
        try:
            self.config_editor.load_file(self.configuration_path)
        except (OSError, UnicodeError, ConfigurationError, ValueError) as error:
            self.on_error("配置重新载入失败：" + str(error))
            return
        self.config_feedback.setText("已放弃修改")

    def configuration_edited(self, dirty):
        self.config_feedback.setText("有未保存的修改" if dirty else "已保存")
        self.on_active(self.client.active)

    def save_configuration(self):
        if not self.apply_root_directory():
            return
        try:
            text = self.config_editor.serialize()
            directory = self.data_dir / "config"
            directory.mkdir(parents=True, exist_ok=True)
            self.configuration_candidate = directory / ("draft-" + uuid4().hex + ".ini")
            self.configuration_candidate.write_text(text, encoding="utf-8")
        except (OSError, ValueError) as error:
            self.config_feedback.setText("保存失败：" + str(error))
            self.on_error(self.config_feedback.text())
            return
        self.config_feedback.setText("校验中")
        suffix = ".exe" if os.name == "nt" else ""
        self.configuration_process.start(str(self.bin_dir / ("bus_analyze" + suffix)),
                                         ["--validate-config", str(self.configuration_candidate)])
        self.on_active(self.client.active)

    def configuration_validated(self, code, exit_status):
        error = bytes(self.configuration_process.readAllStandardError()).decode("utf-8")
        self.configuration_process.readAllStandardOutput()
        candidate = self.configuration_candidate
        self.configuration_candidate = None
        if code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
            candidate.unlink()
            self.config_feedback.setText("配置无效：" + error.strip())
            self.on_error(self.config_feedback.text())
        else:
            path = candidate.with_name("project-" + candidate.stem.removeprefix("draft-") + ".ini")
            try:
                candidate.rename(path)
            except OSError as failure:
                self.config_feedback.setText("配置保存失败：" + str(failure))
                self.on_error(self.config_feedback.text())
            else:
                self.configuration_path = self.config_editor.path = path
                self.configuration_customized = True
                self.config_editor.set_dirty(False)
                self.config_feedback.setText("已保存")
                self.update_relative_paths()
                self.set_project_state("等待中", "配置已通过后端校验，下一项目使用当前保存版本。")
                self.message.setText("配置已保存，可新建项目。")
                self.message.show()
        self.on_active(self.client.active)
        if self.close_pending:
            self.close()

    def configuration_validation_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.configuration_candidate.unlink()
            self.configuration_candidate = None
            self.config_feedback.setText("无法启动配置校验：" + self.configuration_process.errorString())
            self.on_error(self.config_feedback.text())
            self.on_active(self.client.active)
            if self.close_pending:
                self.close()

    def apply_root_directory(self):
        try:
            text = self.storage_label.text().strip()
            if not text:
                raise ValueError("项目根目录不能为空。")
            directory = writable_data_path(text)
            directory.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as error:
            self.on_error(str(error))
            return False
        changed = directory != self.data_dir
        self.data_dir = directory
        if changed and self.project is not None:
            self.save_project()
            self.project = None
            self.current_run = None
        self.storage_label.setText(str(directory))
        self.storage_label.setToolTip("项目根目录；运行期间锁定。\n" + str(directory))
        self.relations_page.definitions_path = directory / "relations.json"
        self.update_relative_paths()
        if changed:
            self.set_project_state("等待中", "根目录已更新，新项目使用当前目录。")
        return True

    def update_relative_paths(self):
        configuration = self.configuration_path
        display = configuration.relative_to(self.data_dir).as_posix() if configuration.is_relative_to(self.data_dir) else "根目录外 / " + configuration.name
        self.config_path.setText(display)
        self.config_path.setToolTip("配置位置；根目录内显示相对路径。\n" + str(configuration))
        if self.archive is not None and self.archive_root == self.data_dir:
            self.archive_path.setText(self.archive.relative_to(self.data_dir).as_posix())
            self.archive_path.setToolTip(str(self.archive))
        else:
            self.archive_path.clear()
            self.archive_path.setToolTip("根目录：" + str(self.data_dir))

    def browse_storage(self):
        path = QFileDialog.getExistingDirectory(self, "选择 D / E 盘项目根目录", str(self.data_dir))
        if path:
            self.storage_label.setText(path)
            self.apply_root_directory()

    def open_data_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))

    def new_project(self):
        if self.project:
            self.save_project()
        self.project = None
        self.current_run = None
        self.new_session()

    def new_session(self):
        if self.client.active:
            return
        if not self.apply_root_directory():
            return
        try:
            if self.relation_process.state() != QProcess.ProcessState.NotRunning:
                raise ValueError("请等待上一轮完整关系分析完成。")
            if self.config_editor.dirty:
                raise ValueError("请先在项目设置中保存字典与规则。")
            configuration = self.configuration_path
            if not configuration.is_file():
                raise ValueError("请先选择有效的字典与规则配置。")
            name = self.project_name.text().strip()
            if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " ")):
                raise ValueError("项目名称为空或含有文件名不支持的字符。")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if self.project is None:
                self.project = Project.create(self.data_dir,name,configuration,self.relations_page.engine.document())
                self.relations_page.definitions_path = self.project.resource(self.project.document["relations"])
                remember_project(self.workspace_dir / "recent-projects.json",self.project.path)
                self.update_recent_projects()
            else:
                self.project.set_configuration(configuration)
                self.restore_sources = self.project.sources(self.bin_dir) if self.capture_after_ready else []
            self.current_run = self.project.new_run()
            configuration = self.project.resource(self.project.document["configuration"])
            write_json(self.project.resource(self.current_run["archive"]).parent / "run.json",{**self.current_run,"steps":self.project.document["steps"]})
            self.archive = self.project.resource(self.current_run["archive"])
            self.archive_root = self.data_dir
            self.parameter_model.clear()
            self.event_model.clear()
            self.samples.clear()
            self.source_configs.clear()
            self.disabled_plugins = set(self.project.document["disabled_plugins"])
            self.latest_snapshot = {"sources": []}
            self.relations_page.reset_session()
            self.trend_selector.clear()
            self.plot.select(None, [])
            self.sources.setRowCount(0)
            self.last_snapshot_time = self.last_counts = None
            self.last_metadata = None
            self.display_omitted = 0
            self.display_events_omitted = 0
            self.session_is_demo = self.demo_pending or self.config_editor.matches_plugin(self.plugin_center.cards["demo"].definition)
            for value in self.metric_values:
                value.setText("0")
            self.badge.setStyleSheet("")
            self.client.start(self.archive, configuration)
            self.start_complete_relations()
            self.set_project_state("准备中", "正在创建项目及原始归档。")
            self.update_relative_paths()
            self.message.setText("正在创建：" + self.archive.name)
            self.message.show()
            self.message.setToolTip(str(self.archive))
            self.navigate(4)
        except (OSError, ValueError) as error:
            self.demo_pending = False
            self.pending_plugin_sources.clear()
            self.plugin_action_pending = False
            self.on_error(str(error))

    def start_demo(self):
        if self.configuration_customized and not self.config_editor.matches_plugin(self.plugin_center.cards["demo"].definition):
            raise ValueError("当前配置已自定义。请在项目设置中选择合成演示模板，再编辑和保存；已有修改不会被覆盖。")
        if not self.configuration_customized:
            self.load_configuration(self.demo_configuration, customized=False)
        self.demo_pending = True
        self.new_session()

    def refresh_plugins(self):
        self.plugin_center.refresh(self.source_configs, self.latest_snapshot, self.client.active,
            self.client.is_ready, self.busy or self.plugin_action_pending or self.config_editor.dirty or
            self.configuration_process.state() != QProcess.ProcessState.NotRunning, self.client.closing and self.client.active,
            self.disabled_plugins)
        ready = any(source["status"]["state"] in {"configured", "stopped"}
                    for key, sources in self.plugin_center.sources_by_plugin.items()
                    if key not in self.disabled_plugins for source in sources)
        self.capture_button.setEnabled(ready and self.client.is_ready and not self.busy
                                       and not self.plugin_action_pending and not self.client.closing)
        if self.project and not self.client.active and self.relation_process.state() == QProcess.ProcessState.NotRunning:
            self.capture_button.setEnabled(bool(self.project.document["sources"]) and not self.config_editor.dirty)
        self.capture_button.setToolTip("启动本项目中已启用且就绪的采集源，开始接收数据并自动保存。" if ready else
            "启用所需插件或添加采集源后，才能开始采集。正在采集或已停用的来源不会重复启动。")

    def start_capture(self):
        if not self.client.active:
            self.capture_after_ready = True
            self.new_session()
            return
        self.plugin_action_pending = True
        for key, sources in self.plugin_center.sources_by_plugin.items():
            if key not in self.disabled_plugins:
                for source in sources:
                    if source["status"]["state"] in {"configured", "stopped"}:
                        self.client.send("start", source["id"])
        self.message.setText("正在开始采集…")
        self.set_project_state("启动中", "正在启动已启用且就绪的采集源。")
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
            if definition["mode"] == "preset":
                configuration = Path(__file__).resolve().parents[2] / "config" / definition["configuration"]
                if (self.client.active or self.configuration_customized) and not self.config_editor.matches_plugin(definition):
                    raise ValueError("当前配置没有此插件对应的参数；请在项目设置选择其配套模板后编辑和保存。运行中的项目需先封存。")
                sources = [(preset["source"], definition["path"], preset["config"]) for preset in definition["presets"]]
                for source, plugin, config in sources:
                    if source in self.source_configs and os.path.normcase(str(Path(self.source_configs[source][1]).resolve())) != os.path.normcase(str(Path(plugin).resolve())):
                        raise ValueError("预设来源 ID 已被其他插件使用：" + source)
                self.plugin_action_pending = True
                if self.client.active:
                    for source, plugin, config in sources:
                        if source not in self.source_configs:
                            self.client.send("add", source, plugin, config)
                else:
                    if not self.configuration_customized:
                        self.load_configuration(configuration, customized=False)
                    self.pending_plugin_sources.extend(sources)
                    self.new_session()
            elif definition["mode"] == "demo":
                if self.client.active and not self.session_is_demo:
                    raise ValueError("请先结束当前试验项目，再启动使用配套字典的演示插件。")
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
                self.message.setText("正在准备：" + definition["name"] + "。就绪后请点击左下角“开始采集”。")
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
        self.message.setText("正在停用：" + definition["name"] + "。已记录数据保留在当前项目中。")
        self.refresh_plugins()

    def on_ready(self, message):
        self.current_run["build"] = message["build"]
        self.project.save()
        write_json(self.archive.parent / "run.json",{**self.current_run,"steps":self.project.document["steps"]})
        self.set_project_state("等待中", "项目已就绪，启用插件后点击左下角“开始采集”。")
        self.log_message("项目已创建：" + message["archive"])
        self.log_message(f"后端版本 {message['build']['version']} · {message['build']['revision']}")
        self.message.setText("项目已就绪；启用插件后，点击左下角“开始采集”。")
        if self.demo_pending:
            self.demo_pending = False
            self.queue_demo_sources()
        for source, plugin, config in self.pending_plugin_sources:
            self.client.send("add", source, plugin, config)
        self.pending_plugin_sources.clear()
        queued = {item[2][0] for item in self.client.queue if item[1] == "add"}
        if self.client.pending and self.client.pending[1] == "add": queued.add(self.client.pending[2][0])
        for source,plugin,config in self.restore_sources:
            if source not in queued:
                self.client.send("add",source,plugin,config)
        self.restore_sources.clear()
        self.on_busy(self.busy)

    def end_session(self):
        self.client.send("quit")
        self.set_project_state("封存中", "正在停止采集源、写完剩余数据并封存归档。")
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
            if command in {"add","replace","remove"} and self.project:
                self.project.save_sources(self.source_configs,self.bin_dir,self.disabled_plugins)
        else:
            self.on_error(command + " 失败：" + payload)

    def on_snapshot(self, snapshot):
        self.latest_snapshot = snapshot
        if self.capture_after_ready and not self.client.queue and self.source_configs:
            self.capture_after_ready = False
            QTimer.singleShot(0,self.start_capture)
        if not self.client.queue:
            self.plugin_action_pending = False
        now = time.monotonic()
        rate = 0
        if self.last_counts:
            rate = (snapshot["recorded"] - self.last_counts[1]) / (now - self.last_counts[0])
        self.last_counts = now, snapshot["recorded"]
        self.last_snapshot_time = now
        if not snapshot["healthy"]:
            self.set_project_state("异常", snapshot["failure"], "alert")
        elif not snapshot["analysis_healthy"]:
            self.set_project_state("异常", snapshot["analysis_failure"], "alert")
        elif snapshot["capture_degraded"]:
            self.set_project_state("已降级", "采集存在故障或丢弃，请检查源状态及日志。", "alert")
        elif self.client.closing:
            self.set_project_state("封存中", "正在停止采集源并排空保存队列。")
        elif any(source["status"]["state"] == "running" for source in snapshot["sources"]):
            self.set_project_state("采集中", "原始数据正在自动保存到当前归档。", "running")
        elif any(source["status"]["state"] == "stopped" for source in snapshot["sources"]):
            self.set_project_state("已暂停", "当前来源已停止；已启用的来源可以再次开始采集。")
        else:
            self.set_project_state("等待中", "启用插件后，点击左下角“开始采集”。")
        if self.message.text() == "正在开始采集…" and any(
                source["status"]["state"] == "running" for source in snapshot["sources"]):
            self.message.setText("采集中 · 原始归档：" + self.archive.name)
            self.message.hide()
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
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, self.source_configs[source["id"]][1])
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
            self.message.setText(snapshot["failure"])
            self.message.show()
        elif not snapshot["analysis_healthy"]:
            self.message.setText(snapshot["analysis_failure"])
            self.message.show()
        self.refresh_plugins()

    def select_trend(self, index):
        self.plot.select(self.trend_selector.itemData(index) if index >= 0 else None, self.samples)

    def on_sealed(self, metadata):
        self.last_metadata = metadata
        if self.current_run:
            write_json(self.archive.parent / "capture-result.json",metadata)
            self.current_run["status"] = ("已封存" if metadata["capture_status"] == "complete" and metadata["analysis_status"] == "complete" else "已降级") if metadata["recording_healthy"] else "异常"
            self.current_run["capture_status"] = metadata["capture_status"]
            self.current_run["analysis_status"] = metadata["analysis_status"]
            self.current_run["sources"] = list(self.project.document["sources"])
            self.save_project()
            self.relation_process.write(b"finish\n")
            self.relation_process.closeWriteChannel()
        healthy = metadata["recording_healthy"]
        complete = healthy and metadata["capture_status"] == "complete" and metadata["analysis_status"] == "complete"
        state = "已封存" if complete else "已降级" if healthy else "异常"
        self.set_project_state(state, f"原始记录健康：{healthy}；采集：{metadata['capture_status']}；分析：{metadata['analysis_status']}。", "running" if complete else "alert")
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
        self.message.setText(f"已保存 {metadata['recorded']:,} 帧，拒绝 {metadata['dropped']:,} 帧；回执：{self.archive.name}.session.json")
        self.message.show()
        self.log_message("项目结束，已收到封存回执：" + metadata["capture_status"] + " / " + metadata["analysis_status"])
        self.offline_archive.setText(str(self.archive))
        self.offline_config.setText(str(self.archive) + ".analysis/configuration.ini")
        self.offline_output.setText(str(self.data_dir / ("离线分析_" + uuid4().hex[:8])))

    def on_active(self, active):
        if not active:
            self.plugin_action_pending = False
        editable = not active and self.configuration_process.state() == QProcess.ProcessState.NotRunning and self.relation_process.state() == QProcess.ProcessState.NotRunning
        for widget in (self.storage_button, self.storage_label, self.project_name, self.config_editor):
            widget.setEnabled(editable)
        for widget in (self.new_button, self.config_button, self.preset_selector):
            widget.setEnabled(editable and not self.config_editor.dirty)
        for widget in (self.open_project_button,self.recent_projects,self.save_project_button):
            widget.setEnabled(editable and self.relation_process.state() == QProcess.ProcessState.NotRunning)
        self.save_configuration_button.setEnabled(editable and self.config_editor.dirty)
        self.reload_configuration_button.setEnabled(editable and self.config_editor.dirty)
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
            plugin = Path(self.sources.item(self.sources.currentRow(), 0).data(Qt.ItemDataRole.UserRole)).resolve()
            owner = next((definition["name"] for definition in self.plugin_definitions
                          if Path(definition["path"]).resolve() == plugin), plugin.name)
            self.source_note.setText(f"所属插件：{owner}\n采集源：{selected} · 代次 {status['generation']}\n"
                                     f"发出 {status['emitted']:,} · 拒绝 {status['rejected']:,} · 错误 {status['errors']:,}\n"
                                     + status["failure"])
        else:
            self.source_note.setText("插件提供采集能力，一个插件可创建多路采集源。\n选择一行查看所属插件与运行状态。")

    def check_freshness(self):
        if self.client.active and self.last_snapshot_time and not self.client.closing:
            age = time.monotonic() - self.last_snapshot_time
            if age > 2:
                self.set_project_state("状态超时", f"后端 {age:.0f} 秒未刷新，保存状态待确认。", "alert")

    def set_project_state(self, state, detail, tone=None):
        if self.project and self.current_run and self.client.active and self.current_run["status"] != state:
            self.current_run["status"] = state
            self.project.save()
        self.badge.setText(state)
        self.recording_status.setText(state)
        self.badge.setToolTip(detail)
        self.recording_status.setToolTip(detail)
        self.badge.setStyleSheet(ALERT_STYLE if tone == "alert" else RUNNING_STYLE if tone == "running"
                                else f"background:{ACTIVE};color:{MUTED};")

    def log_message(self, text):
        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {text}")

    def on_error(self, text):
        self.message.setText(text)
        self.message.show()
        self.log_message("错误 · " + text)
        if not self.client.active:
            self.set_project_state("异常", text, "alert")

    def on_exit(self, code):
        if self.archive is not None and self.last_metadata is None:
            self.set_project_state("未封存", "采集已停止，但没有收到最终封存回执。", "alert")
            if self.current_run:
                self.current_run["status"] = "未封存"
                self.save_project()
                self.relation_process.write(b"finish\n")
                self.relation_process.closeWriteChannel()
        if self.close_pending:
            self.close()

    def show_parameter(self, index):
        source_index = self.parameter_proxy.mapToSource(index)
        self.show_evidence(self.parameter_model.rows[source_index.row()])

    def show_evidence(self, evidence):
        if self.archive is not None and self.last_metadata is not None and evidence.get("record_index"):
            self.request_raw_record(evidence,self.archive)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("数据与证据溯源")
        dialog.resize(780, 580)
        layout = QVBoxLayout(dialog)
        heading = QHBoxLayout()
        image = QLabel()
        image.setPixmap(icon("raw_packet").pixmap(48, 48))
        heading.addWidget(image)
        heading.addWidget(label("参数与原始记录证据", "section"), 1)
        layout.addLayout(heading)
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
            self.offline_results.setCurrentIndex(1)
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
            self.offline_results.setCurrentIndex(0)

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
            if mode == "analyze":
                self.start_offline_relations()
            self.offline_detail.setPlainText(json.dumps(summary, ensure_ascii=False, indent=2))
            self.offline_status.setText("校验通过，封存摘要与数据一致。" if mode == "inspect" else "完成 · 表格显示末尾数据，完整结果已写入输出目录。")
            if mode == "inspect":
                self.offline_tabs.setCurrentIndex(2)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            self.offline_summary = None
            self.offline_summary_label.setText("操作失败 · 结果未通过校验")
            self.offline_results.setCurrentIndex(0)
            self.offline_status.setText("失败：" + str(error))
            self.log_message("离线分析失败：" + str(error))

    def update_recent_projects(self):
        self.recent_projects.clear()
        self.recent_projects.addItem("最近项目",None)
        registry = self.workspace_dir / "recent-projects.json"
        if registry.exists():
            for path in json.loads(registry.read_text(encoding="utf-8")):
                self.recent_projects.addItem(Path(path).stem,path)

    def browse_project(self):
        path,_ = QFileDialog.getOpenFileName(self,"打开AEROBUS项目",str(self.workspace_dir),"AEROBUS项目 (*.aerobus.json)")
        if path:
            self.open_project(path)

    def open_recent_project(self,index):
        path = self.recent_projects.itemData(index)
        if path:
            self.open_project(path)

    def open_project(self,path):
        try:
            if self.client.active or self.relation_process.state() != QProcess.ProcessState.NotRunning:
                raise ValueError("请先结束采集并等待完整分析保存。")
            if self.config_editor.dirty:
                raise ValueError("请先保存或放弃当前配置修改。")
            project = Project.open(writable_data_path(str(path)))
            if self.project:
                self.save_project()
            self.project = project
            self.current_run = None
            self.data_dir = project.root
            self.storage_label.setText(str(project.root))
            self.project_name.setText(project.document["name"])
            self.load_configuration(project.resource(project.document["configuration"]))
            self.relations_page.authoritative = False
            self.relations_page.load_definitions(project.resource(project.document["relations"]))
            self.source_configs = {row[0]:row for row in project.sources(self.bin_dir)}
            self.disabled_plugins = set(project.document["disabled_plugins"])
            self.latest_snapshot = {"sources":[]}
            self.archive = self.archive_root = None
            self.parameter_model.clear(); self.event_model.clear(); self.samples.clear()
            self.sources.setRowCount(0); self.trend_selector.clear(); self.plot.select(None,[])
            remember_project(self.workspace_dir / "recent-projects.json",project.path)
            self.update_recent_projects()
            layout = project.document["layout"]
            for splitter,sizes in zip(self.findChildren(ThinSplitter),layout.get("splitters",[])):
                splitter.setSizes(sizes)
            self.history_page.set_project(project,select_latest=True)
            self.navigate(layout.get("page",5))
            self.set_project_state("等待中","项目已打开，点击开始采集会创建新一轮记录；已有记录保留。")
            self.on_active(False)
        except (OSError,ValueError,KeyError,ConfigurationError) as error:
            self.on_error("项目打开失败："+str(error))

    def save_project(self,notify=False):
        if not self.project:
            return
        try:
            self.project.document["name"] = self.project_name.text().strip()
            self.project.document["layout"] = {"page":self.pages.currentIndex(),
                "splitters":[s.sizes() for s in self.findChildren(ThinSplitter)]}
            if not self.client.active and not self.config_editor.dirty:
                self.project.set_configuration(self.configuration_path)
            self.project.save_sources(self.source_configs,self.bin_dir,self.disabled_plugins)
            if notify:
                self.message.setText("项目已保存："+self.project.path.name)
        except OSError as error:
            self.on_error("项目保存失败："+str(error))

    def relations_changed(self):
        if self.project and self.current_run:
            target = self.project.resource(self.current_run["relations"])
            temporary = target.with_suffix(".json.tmp")
            temporary.write_bytes(self.relations_page.definitions_path.read_bytes())
            temporary.replace(target)
            if not self.client.active and self.relation_process.state() == QProcess.ProcessState.NotRunning:
                self.start_complete_relations(follow=False)

    def start_complete_relations(self,follow=True):
        self.relation_buffer.clear()
        self.latest_complete = None
        output = self.project.resource(self.current_run["analysis"])
        if output.exists():
            relative = str(Path(self.current_run["analysis"]).with_name("analysis-"+uuid4().hex[:8])).replace("\\","/")
            self.current_run["analysis"] = relative
            output = self.project.resource(relative)
            self.project.save()
        self.relations_page.authoritative = True
        arguments = ["--input",str(self.archive)+".analysis","--relations",str(self.project.resource(self.current_run["relations"])),
                     "--output",str(output),"--archive",str(self.archive)]
        if follow:
            arguments.append("--follow")
        start_analysis_process(self.relation_process,arguments)

    def read_complete_relations(self):
        self.relation_buffer.extend(bytes(self.relation_process.readAllStandardOutput()))
        latest = None
        while b"\n" in self.relation_buffer:
            line,_,remaining = self.relation_buffer.partition(b"\n")
            self.relation_buffer = bytearray(remaining)
            latest = json.loads(line)
        if latest is not None:
            self.latest_complete = latest
            self.relations_page.apply_complete_state(latest)
            self.relations_page.feedback.setText(f"完整参数计算：{latest['samples']:,}样本 / {latest['results']:,}结果 · " +
                ("已完整保存" if latest["status"] == "complete" else "输入不完整" if latest["status"] == "partial" else "持续保存中"))

    def relations_finished(self,code,status):
        if code != 0 or status != QProcess.ExitStatus.NormalExit:
            error = "完整关系分析失败："+bytes(self.relation_process.readAllStandardError()).decode("utf-8",errors="replace")
            self.log_message(error)
            if self.last_metadata is not None:
                self.message.setText(error)
            if self.current_run:
                self.current_run["relation_status"] = "failed"
                if self.last_metadata is not None:
                    self.current_run["status"] = "已降级"
                    self.set_project_state("已降级","原始记录已结束，完整关系计算失败；请查看日志。","alert")
                write_json(self.archive.parent / "analysis-failure.json",{"error":error,"exit_code":code})
                self.project.save()
        else:
            self.read_complete_relations()
            self.current_run["relation_status"] = self.latest_complete["status"]
            self.project.save()
            self.history_page.set_project(self.project,select_latest=True)
            self.log_message("完整关系结果、历史索引与报告已保存。")
        self.on_active(self.client.active)
        if self.close_pending:
            self.close()

    def start_offline_relations(self):
        archive = Path(self.offline_archive.text()).resolve()
        recorded = archive.parent / "relations.json"
        definitions = recorded if recorded.exists() else self.relations_page.definitions_path
        if not definitions.exists():
            write_json(self.analysis_output / "relations.json",self.relations_page.engine.document())
            definitions = self.analysis_output / "relations.json"
        self.offline_relation_output = self.analysis_output / "workbench"
        start_analysis_process(self.offline_relation_process,["--input",str(self.analysis_output),"--relations",str(definitions),
            "--output",str(self.offline_relation_output),"--archive",str(archive)])
        self.offline_status.setText("参数重算完成，正在计算完整自定义关系与历史索引…")

    def offline_relations_finished(self,code,status):
        if code != 0 or status != QProcess.ExitStatus.NormalExit:
            self.on_error("离线关系复算失败："+bytes(self.offline_relation_process.readAllStandardError()).decode("utf-8",errors="replace"))
            self.offline_status.setText("离线关系复算失败；请查看日志。")
        else:
            summary = json.loads((self.offline_relation_output / "summary.json").read_text(encoding="utf-8"))
            self.offline_status.setText(f"完成 · 完整自定义关系 {summary['results']:,} 条；报告位于输出目录 workbench/。")
        if self.close_pending:
            self.close()

    def export_run(self,directory):
        if self.export_process.state() != QProcess.ProcessState.NotRunning:
            self.on_error("上一份报告仍在导出。")
            return
        if self.relation_process.state() != QProcess.ProcessState.NotRunning:
            self.on_error("完整分析尚在运行，请等待保存完成。")
            return
        summary = directory / "summary.json"
        if not summary.exists() or json.loads(summary.read_text(encoding="utf-8"))["status"] == "running":
            self.on_error("请先结束采集，等待完整分析完成后导出。")
            return
        self.export_directory = directory
        self.history_page.status.setText("正在导出完整CSV与带证据的试验报告…")
        start_analysis_process(self.export_process,["--output",str(directory),"--export"])

    def export_finished(self,code,status):
        if code != 0 or status != QProcess.ExitStatus.NormalExit:
            self.on_error("报告导出失败："+bytes(self.export_process.readAllStandardError()).decode("utf-8",errors="replace"))
        else:
            self.history_page.status.setText("报告和完整CSV已导出："+str(self.export_directory))
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.export_directory / "report.html")))
        if self.close_pending:
            self.close()

    def request_raw_record(self,evidence,archive):
        if self.raw_process.state() != QProcess.ProcessState.NotRunning:
            self.on_error("正在读取上一条原始记录。")
            return
        self.raw_evidence = evidence
        self.raw_archive = Path(archive)
        self.message.setText("正在校验归档并读取原始记录 #"+str(evidence["record_index"])+"…")
        self.raw_process.setProcessEnvironment(self.client.process.processEnvironment())
        self.raw_process.start(str(self.bin_dir / ("bus_analyze.exe" if os.name == "nt" else "bus_analyze")),
                               ["--record",str(archive),str(evidence["record_index"])])

    def raw_finished(self,code,status):
        if code != 0 or status != QProcess.ExitStatus.NormalExit:
            self.on_error("原始记录读取失败："+bytes(self.raw_process.readAllStandardError()).decode("utf-8",errors="replace"))
        else:
            record = json.loads(bytes(self.raw_process.readAllStandardOutput()))
            expected_identity = self.raw_evidence.get("recording_session_id")
            if expected_identity is not None and record["recording_session_id"] != expected_identity:
                self.on_error("原始归档与所选分析结果的会话身份不一致，请检查项目文件。")
                return
            self.evidence_dialog = EvidenceDialog(record,self.raw_evidence,self,Path(str(self.raw_archive)+".analysis/configuration.ini"))
            self.evidence_dialog.show()

    def showEvent(self, event):
        super().showEvent(event)
        if QApplication.platformName() == "offscreen":
            return
        available = self.screen().availableGeometry()
        frame = self.frameGeometry()
        self.resize(min(self.width(), available.width() - (frame.width() - self.width())),
                    min(self.height(), available.height() - (frame.height() - self.height())))

    def closeEvent(self, event):
        if self.configuration_process.state() != QProcess.ProcessState.NotRunning:
            self.close_pending = True
            event.ignore()
            return
        if self.offline_mode or self.offline_relation_process.state() != QProcess.ProcessState.NotRunning:
            self.message.setText("离线分析正在运行；完成后可关闭窗口。")
            event.ignore()
        elif self.client.active:
            self.close_pending = True
            self.end_session()
            event.ignore()
        elif self.relation_process.state() != QProcess.ProcessState.NotRunning or self.export_process.state() != QProcess.ProcessState.NotRunning:
            self.close_pending = True
            self.message.setText("正在保存完整分析与报告，完成后关闭。")
            event.ignore()
        elif self.history_page.query is not None and self.history_page.query.isRunning():
            self.close_pending = True
            self.history_page.query.finished.connect(self.close)
            event.ignore()
        else:
            if self.project:
                self.save_project()
            event.accept()
