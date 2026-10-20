"""Native raw-record inspector, with the captured bits kept visible."""
import json
from configparser import ConfigParser
import hashlib
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)


class EvidenceDialog(QDialog):
    def __init__(self, record, evidence, parent=None, configuration=None):
        super().__init__(parent)
        self.setWindowTitle("原始报文与参数溯源")
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        title = QLabel(f"{record['source']} · 通道 {record['channel']} · 原始记录 #{record['record_index']}")
        title.setObjectName("section")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"协议 {record['protocol']} · 代次 {record['generation']} · 采集时间 {record['capture_time_ns']} ns"))
        field = None
        if configuration is not None and configuration.exists() and evidence.get("parameter"):
            content = configuration.read_bytes()
            if hashlib.sha256(content).hexdigest() == evidence["configuration_sha256"]:
                document = ConfigParser(interpolation=None)
                document.read_string(content.decode("utf-8"))
                section = "parameter " + evidence["parameter"]
                if document.has_section(section):
                    field = document[section]
                    layout.addWidget(QLabel(f"参数 {evidence['parameter']} · 起始位 {field['bit_offset']} · 位宽 {field['bit_width']} · 换算与字节序见配置快照"))
            else:
                layout.addWidget(QLabel("配置快照摘要不匹配，参数字段高亮已停止；原始归档继续独立校验。"))
        tabs = QTabWidget()
        raw = QPlainTextEdit()
        raw.setReadOnly(True)
        payload = bytes.fromhex(record["payload_hex"])
        raw.setPlainText("\n".join(f"{offset:06x}  " + " ".join(f"{b:02x}" for b in payload[offset:offset+16])
                                    for offset in range(0, len(payload), 16)))
        tabs.addTab(raw, "原始十六进制")
        if payload[:8] == b"M1553B01" and len(payload) >= 12 and len(payload) == 12 + payload[9] * 4:
            page = QWidget()
            content = QVBoxLayout(page)
            content.addWidget(QLabel("1553B监控封装 · " + ("A总线" if payload[8] == 0 else "B总线") +
                                    " · 同步与校验为捕获观测值"))
            words = [(int.from_bytes(payload[i:i+2], "little"), payload[i+2], payload[i+3]) for i in range(12, len(payload), 4)]
            messages = record["protocol_messages"]
            command_indices = set()
            status_indices = set()
            if messages:
                metadata = messages[0]["metadata"]
                command_indices = {0,1} if "receive_command" in metadata and "transmit_command" in metadata else {0}
                status_indices = {int(key.removeprefix("status_word_")) for key in metadata if key.startswith("status_word_")}
                content.addWidget(QLabel("后端解析结果："+messages[0]["kind"]))
            if record["diagnostics"]:
                diagnostic = QLabel("解析诊断："+"；".join(record["diagnostics"])); diagnostic.setWordWrap(True); content.addWidget(diagnostic)
            fields = QTableWidget(len(words), 5)
            fields.setHorizontalHeaderLabels(["字序", "捕获值", "同步类型", "奇校验位", "解释"])
            fields.verticalHeader().hide()
            data_word = 0
            for i, (word, sync, parity) in enumerate(words):
                highlighted = field is not None and sync == 1 and data_word*16 < int(field["bit_offset"])+int(field["bit_width"]) and (data_word+1)*16 > int(field["bit_offset"])
                if i in command_indices:
                    sa = (word >> 5) & 31
                    explanation = f"命令：RT {(word>>11)&31} · {'发送' if word&0x400 else '接收'} · 子地址 {sa} · " + (f"模式码 {word&31}" if sa in (0,31) else f"字数 {(word&31) or 32}")
                elif i in status_indices:
                    flags = [("消息错误",0x400),("请求服务",0x100),("广播已接收",0x10),("忙",8),("子系统标志",4),("动态总线控制接受",2),("终端标志",1)]
                    explanation = f"状态：RT {(word>>11)&31} · " + ("、".join(name for name, mask in flags if word & mask) or "状态标志为0")
                else:
                    explanation = f"数据字 · 无符号整数 {word}" if sync == 1 else "命令/状态同步；解析器未确认字角色"
                for col, text in enumerate((str(i+1), f"0x{word:04X}", "命令/状态" if sync == 0 else "数据", str(parity), explanation)):
                    item = QTableWidgetItem(text)
                    if highlighted:
                        item.setBackground(QColor("#edf5f5"))
                    fields.setItem(i, col, item)
                if sync == 1:
                    data_word += 1
            fields.resizeColumnsToContents()
            fields.horizontalHeader().setStretchLastSection(True)
            content.addWidget(fields)
            content.addWidget(QLabel("浅色行标出选中参数涉及的数据字；位偏移、位宽和换算取自本次采集的配置快照。"))
            tabs.addTab(page, "1553B字与状态")
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(json.dumps({"原始记录": record, "选中参数或事件": evidence}, ensure_ascii=False, indent=2))
        tabs.addTab(details, "身份与分析证据")
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
