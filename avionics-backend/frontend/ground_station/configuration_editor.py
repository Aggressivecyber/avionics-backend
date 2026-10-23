"""Form editor for the configuration consumed by the backend parser."""
from configparser import ConfigParser
from io import StringIO
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QScrollArea, QVBoxLayout, QWidget)

from .ui_assets import icon
from .widgets import ThinSplitter, MotionButton as QPushButton


TITLES = {"dictionary": "字典信息", "parameter": "参数", "protocol": "协议输入",
          "consistency": "一致性规则", "response": "响应规则", "limit": "参数上限规则", "clock": "时钟映射",
          "processing": "处理设置", "pipeline": "队列预算", "capture": "采集超时"}
GROUPS = [("解析字典", ("dictionary", "parameter", "protocol")),
          ("分析规则", ("consistency", "response", "limit")),
          ("时钟与处理", ("clock", "processing", "pipeline", "capture"))]
DEFAULTS = {
    "dictionary": {"schema": "2", "version": "project-v1"},
    "parameter": {"source": "sensor_a", "protocol": "65535", "channel": "0",
        "bit_offset": "0", "bit_width": "64", "byte_order": "little", "type": "signed",
        "scale": "1", "unit": "unit", "max_age_ms": "100"},
    "protocol": {"channel": "0", "protocol": "1553", "selection": "auto",
        "capture_representation": "mil1553b.monitor-v1"},
    "consistency": {"left_source": "sensor_a", "left_parameter": "temperature_a",
        "right_source": "sensor_b", "right_parameter": "temperature_b", "tolerance": "0.25", "window_ms": "5"},
    "response": {"command_source": "control", "command_parameter": "command", "response_source": "feedback",
        "response_parameter": "position", "command_threshold": "0.5", "response_threshold": "0.5", "max_delay_ms": "30"},
    "limit": {"input_source": "scenario", "input_parameter": "temperature", "upper": "25"},
    "clock": {"domain": "1", "group": "host", "offset_ns": "0", "uncertainty_ns": "0"},
    "processing": {"allowed_lateness_ms": "20", "max_parameter_states": "4096", "max_reorder_samples": "8192"},
    "pipeline": {"queue_frames": "4096", "source_frames": "1024", "analysis_frames": "4096",
        "queue_bytes": "67108864", "source_bytes": "16777216", "analysis_bytes": "67108864", "max_sources": "256"},
    "capture": {"command_timeout_ms": "5000", "stop_timeout_ms": "1000"},
}
OPTIONAL = {"parameter": ("offset", "sequence_step", "valid_bit", "enum_values", "rt_address", "subaddress", "direction"),
            "protocol": ("source",)}
LABELS = {
    "input_source": "采集源名称", "input_parameter": "参数名称", "upper": "参数上限",
    "schema": "配置格式版本", "version": "字典版本名称", "source": "采集源名称", "protocol": "协议编号",
    "channel": "通道", "bit_offset": "起始位", "bit_width": "位宽", "byte_order": "字节顺序",
    "type": "数据类型", "scale": "换算系数", "offset": "换算偏移", "unit": "单位",
    "max_age_ms": "参数有效期 / ms", "sequence_step": "序号步长", "valid_bit": "有效标志位",
    "enum_values": "枚举值与名称", "rt_address": "RT 地址", "subaddress": "子地址", "direction": "传输方向",
    "selection": "协议选择方式", "capture_representation": "输入记录格式", "domain": "时钟域编号",
    "group": "共享时基组", "offset_ns": "时间偏移 / ns", "uncertainty_ns": "时间不确定度 / ns",
    "left_source": "左侧采集源", "left_parameter": "左侧参数", "right_source": "右侧采集源",
    "right_parameter": "右侧参数", "tolerance": "允许差值", "window_ms": "时间对齐窗口 / ms",
    "command_source": "指令采集源", "command_parameter": "指令参数", "response_source": "反馈采集源",
    "response_parameter": "反馈参数", "command_threshold": "指令触发阈值", "response_threshold": "反馈响应阈值",
    "max_delay_ms": "最长响应时间 / ms", "allowed_lateness_ms": "允许到达乱序 / ms",
    "max_parameter_states": "参数状态数量上限", "max_reorder_samples": "重排样本数量上限",
    "queue_frames": "总记录队列 / 帧", "source_frames": "单源记录队列 / 帧", "analysis_frames": "分析队列 / 帧",
    "queue_bytes": "总记录队列 / 字节", "source_bytes": "单源记录队列 / 字节", "analysis_bytes": "分析队列 / 字节",
    "max_sources": "采集源数量上限", "command_timeout_ms": "采集命令超时 / ms", "stop_timeout_ms": "停止超时 / ms",
}
HELP = {
    "input_source": "被检查参数所属的采集源，需要与解析字典对应。",
    "input_parameter": "需要检查上限的参数名称。",
    "upper": "严格大于上限时记录首次越限；持续越限不重复发出事件。连续有效输入恢复至上限或以下时记录恢复事件。",
    "source": "填采集源的名称，与插件创建的入口对应，例如 sensor_a。",
    "protocol": "软件合成记录使用 65535；1553B 监控记录使用 1553。",
    "bit_offset": "从报文数据区第 0 位开始计数，指定参数从哪里读取。",
    "bit_width": "读取多少位；整数为 1–64 位，float64 为对齐的 64 位。",
    "scale": "工程值 = 原始数值 × 换算系数 + 换算偏移。",
    "offset": "工程值 = 原始数值 × 换算系数 + 换算偏移；空白时为 0。",
    "max_age_ms": "参数参与判断的有效时间，单位毫秒，必须大于 0。",
    "valid_bit": "可选。报文中的有效标志位位置；留空表示没有此标志。",
    "enum_values": "仅 enum 类型使用，例如 0:关闭,1:开启。",
    "rt_address": "1553B 参数必填，RT 地址为 0–31。",
    "subaddress": "1553B 参数必填，普通数据子地址为 1–30。",
    "direction": "1553B 的 RT 接收或发送方向；其他协议留空。",
    "group": "只把已确认共享时基的源放在同一组；这个设置不会自动同步设备。",
    "tolerance": "两参数差值的允许范围，使用参数的工程单位，必须不小于 0。",
    "window_ms": "两参数的时间必须在此窗口内才参与比较，单位毫秒，必须大于 0。",
    "max_delay_ms": "指令触发后允许反馈响应的最长时间，单位毫秒，必须大于 0。",
    "allowed_lateness_ms": "允许数据晚到并重排的时间范围；不会改写原始采集时间。",
    "schema": "后端当前使用配置格式 2。",
    "version": "给当前字典版本起一个名称，便于在归档和分析记录中识别。",
    "channel": "数据入口的通道编号；需要与插件提交的通道一致。",
    "byte_order": "little 表示小端，低字节在前；big 表示大端，高字节在前。按设备报文定义选择。",
    "type": "signed：有符号整数；unsigned：无符号整数；float64：双精度浮点；bool：布尔；enum：枚举。",
    "unit": "解码后的工程单位，例如 degC、V、ratio；按设备参数含义填写。",
    "sequence_step": "期望的报文序号步长，用于检查序号连续性；0 表示不检查。",
    "selection": "auto 自动选择输入协议，manual 按指定协议处理；当前协议输入支持 1553B。",
    "capture_representation": "插件提交的记录格式；当前 1553B 软件监控输入使用 mil1553b.monitor-v1。",
    "domain": "来源使用的时钟域编号，需要与插件提交的时钟域一致。",
    "offset_ns": "把来源时间映射到共享时基时使用的偏移，单位纳秒，可为负值。",
    "uncertainty_ns": "时间映射的不确定度上限，单位纳秒，参与时间对齐判断。",
    "left_source": "左侧参数所属的采集源名称，必须与解析字典匹配。",
    "left_parameter": "左侧参数在解析字典中的名称。",
    "right_source": "右侧参数所属的采集源名称，必须与解析字典匹配。",
    "right_parameter": "右侧参数在解析字典中的名称。",
    "command_source": "指令参数所属的采集源名称，必须与解析字典匹配。",
    "command_parameter": "用作指令的参数名称，必须已在解析字典中定义。",
    "response_source": "反馈参数所属的采集源名称，必须与解析字典匹配。",
    "response_parameter": "用作反馈的参数名称，必须已在解析字典中定义。",
    "command_threshold": "判定指令触发的阈值，使用指令参数的工程单位。",
    "response_threshold": "判定反馈响应的阈值，使用反馈参数的工程单位。",
    "max_parameter_states": "最多保留多少个来源与参数组合的当前状态，必须覆盖字典定义。",
    "max_reorder_samples": "时间重排队列可保留的样本数量上限。",
    "queue_frames": "原始记录总队列的帧数上限。",
    "source_frames": "每个采集源可占用的原始记录队列帧数上限。",
    "analysis_frames": "分析队列的帧数上限；独立于原始记录队列。",
    "queue_bytes": "原始记录总队列的字节预算。",
    "source_bytes": "每个采集源可占用的原始记录队列字节预算。",
    "analysis_bytes": "分析队列的字节预算。",
    "max_sources": "当前项目最多允许的采集源数量。",
    "command_timeout_ms": "等待插件采集命令完成的最长时间，单位毫秒。",
    "stop_timeout_ms": "等待插件停止的最长时间，单位毫秒。",
}
CHOICES = {"byte_order": ("little", "big"), "type": ("signed", "unsigned", "float64", "bool", "enum"),
           "direction": ("", "receive", "transmit"), "selection": ("auto", "manual")}


class ConfigurationEditor(QWidget):
    dirty_changed = Signal(bool)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.dirty = False
        self.fields = {}
        self.section = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        note = QLabel("选择左侧参数或规则，在右侧修改。准备阶段可编辑，保存后用于下次采集；采集开始后请先结束并封存再编辑。")
        note.setWordWrap(True)
        note.setObjectName("muted")
        root.addWidget(note)
        self.group = QComboBox()
        self.group.addItems([group[0] for group in GROUPS])
        self.group.setToolTip("解析字典定义数据怎么读；分析规则定义如何判断；时钟与处理定义时间映射和资源预算。")
        root.addWidget(self.group)
        splitter = ThinSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(24)
        left = QWidget()
        listing = QVBoxLayout(left)
        listing.setContentsMargins(0, 0, 0, 0)
        self.entries = QListWidget()
        self.entries.setMinimumWidth(200)
        listing.addWidget(self.entries, 1)
        actions = QHBoxLayout()
        self.add_button = QPushButton("添加")
        self.add_button.setIcon(icon("add_source"))
        self.add_button.setToolTip("添加参数、协议输入、分析规则或时钟设置，然后修改右侧字段。")
        self.remove_button = QPushButton("删除所选")
        self.remove_button.setToolTip("删除选中的配置项；被规则引用的参数删除后，需要同步修改规则才能通过校验。")
        actions.addWidget(self.add_button)
        actions.addWidget(self.remove_button)
        listing.addLayout(actions)
        splitter.addWidget(left)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("configurationScroll")
        self.scroll.setWidgetResizable(True)
        self.form_widget = QWidget()
        self.form_widget.setObjectName("configurationFields")
        self.form = QFormLayout(self.form_widget)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.form.setVerticalSpacing(10)
        self.scroll.setWidget(self.form_widget)
        splitter.addWidget(self.scroll)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        self.group.currentIndexChanged.connect(self.refresh_entries)
        self.entries.currentItemChanged.connect(self.show_section)
        self.add_button.clicked.connect(self.add_menu)
        self.remove_button.clicked.connect(self.remove_section)
        self.load_file(path)

    def load_file(self, path):
        document = ConfigParser(interpolation=None, delimiters=("=",), empty_lines_in_values=False)
        document.optionxform = str
        document.read_string(Path(path).read_text(encoding="utf-8-sig"))
        if document.defaults():
            raise ValueError("配置不支持 DEFAULT 继承，请把字段写入对应配置项。")
        self.document = document
        self.path = Path(path).resolve()
        self.section = None
        self.refresh_entries()
        self.set_dirty(False)

    def set_dirty(self, dirty):
        self.dirty = dirty
        self.dirty_changed.emit(dirty)

    def refresh_entries(self):
        self.entries.blockSignals(True)
        self.entries.clear()
        kinds = GROUPS[self.group.currentIndex()][1]
        for section in self.document.sections():
            kind, _, name = section.partition(" ")
            if kind in kinds:
                item = QListWidgetItem(TITLES[kind] + (" · " + name if name else ""))
                item.setData(Qt.ItemDataRole.UserRole, section)
                self.entries.addItem(item)
        self.entries.blockSignals(False)
        if self.entries.count():
            self.entries.setCurrentRow(1 if self.group.currentIndex() == 0 and self.entries.count() > 1 else 0)
        else:
            self.show_section(None)

    def show_section(self, item, previous=None):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields.clear()
        self.section = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not item:
            self.form.addRow(QLabel("还没有配置项，点击“添加”创建。"))
            return
        kind, _, name = self.section.partition(" ")
        self.name = QLineEdit(name or TITLES[kind])
        self.name.setReadOnly(not bool(name))
        self.name.setToolTip("参数或规则的标识名称。修改参数名称后，也要修改引用它的规则。")
        self.name.textChanged.connect(lambda text: self.set_dirty(True))
        self.name.editingFinished.connect(self.rename_section)
        self.form.addRow("名称", self.name)
        values = self.document[self.section]
        keys = list(dict.fromkeys([*DEFAULTS[kind], *OPTIONAL.get(kind, ()), *values]))
        for key in keys:
            value = values.get(key, "")
            if key in CHOICES:
                field = QComboBox()
                field.setEditable(True)
                field.addItems(CHOICES[key])
                field.setCurrentText(value)
                field.currentTextChanged.connect(lambda text, k=key: self.edit_field(k, text))
            else:
                field = QLineEdit(value)
                field.textChanged.connect(lambda text, k=key: self.edit_field(k, text))
            field.setMinimumHeight(34)
            tip = HELP.get(key, LABELS.get(key, key) + "；填入与当前设备报文和分析要求对应的值。")
            field.setToolTip(tip)
            caption = QLabel(LABELS.get(key, key))
            caption.setToolTip(tip)
            self.fields[key] = field
            self.form.addRow(caption, field)

    def edit_field(self, key, text):
        kind = self.section.partition(" ")[0]
        if not text and key in OPTIONAL.get(kind, ()):
            self.document.remove_option(self.section, key)
        else:
            self.document.set(self.section, key, text)
        self.set_dirty(True)

    def rename_section(self):
        kind, _, current = self.section.partition(" ")
        if not current or self.name.text() == current:
            return True
        name = self.name.text().strip()
        section = kind + " " + name
        if not name or any(c in name for c in "[]\r\n") or self.document.has_section(section):
            self.error.setText("名称不能为空、重复或包含方括号与换行。")
            return False
        values = dict(self.document[self.section])
        self.document.remove_section(self.section)
        self.document[section] = values
        self.section = section
        item = self.entries.currentItem()
        item.setData(Qt.ItemDataRole.UserRole, section)
        item.setText(TITLES[kind] + " · " + name)
        self.error.clear()
        self.set_dirty(True)
        return True

    def add_menu(self):
        menu = QMenu(self)
        for kind in GROUPS[self.group.currentIndex()][1]:
            if kind in {"dictionary", "processing", "pipeline", "capture"} and self.document.has_section(kind):
                continue
            action = menu.addAction(TITLES[kind])
            action.triggered.connect(lambda checked=False, k=kind: self.add_section(k))
        menu.exec(self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))

    def add_section(self, kind):
        section = kind
        if kind not in {"dictionary", "processing", "pipeline", "capture"}:
            index = 1
            while self.document.has_section(f"{kind} new_{index}"):
                index += 1
            section = f"{kind} new_{index}"
        self.document[section] = dict(DEFAULTS[kind])
        self.refresh_entries()
        for row in range(self.entries.count()):
            if self.entries.item(row).data(Qt.ItemDataRole.UserRole) == section:
                self.entries.setCurrentRow(row)
                break
        self.set_dirty(True)

    def remove_section(self):
        if self.section:
            self.document.remove_section(self.section)
            self.refresh_entries()
            self.set_dirty(True)

    def serialize(self):
        if self.section and not self.rename_section():
            raise ValueError(self.error.text())
        output = StringIO()
        self.document.write(output)
        return output.getvalue()

    def matches_plugin(self, definition):
        sources = {preset["source"] for preset in definition["presets"]} | {"*"}
        return any(section.startswith("parameter ")
                   and self.document[section].get("protocol") == str(definition["protocol"])
                   and self.document[section].get("source") in sources
                   for section in self.document.sections())
