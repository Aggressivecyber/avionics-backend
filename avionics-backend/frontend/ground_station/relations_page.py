import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QTableView, QTableWidget, QTableWidgetItem, QTabWidget,
    QSizePolicy, QVBoxLayout, QWidget)

from .models import ParameterModel, TrendPlot, display_value
from .relations import RelationEngine
from .widgets import ThinSplitter


class InputModel(ParameterModel):
    selection_changed = Signal()
    headers = ["参数（勾选）", "来源", "当前值", "单位", "质量", "时间 / s", "记录索引"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.checked = set()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        sample = self.rows[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if (sample["source"], sample["parameter"]) in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            return [sample["parameter"], sample["source"], display_value(sample["value"]), sample["unit"],
                    "有效" if sample["usable"] else " / ".join(sample["quality"]),
                    "—" if sample["time_ns"] is None else f"{sample['time_ns'] / 1e9:.6f}", str(sample["record_index"])][index.column()]
        return super().data(index, role)

    def flags(self, index):
        flags = super().flags(index)
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
            return False
        row = self.rows[index.row()]
        key = row["source"], row["parameter"]
        if value == Qt.CheckState.Checked.value:
            self.checked.add(key)
        else:
            self.checked.discard(key)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()
        return True

    def bindings(self):
        selected = [row for row in self.rows if (row["source"], row["parameter"]) in self.checked]
        return [{"alias": chr(65 + i) if i < 26 else f"X{i+1}", "source": row["source"],
                 "parameter": row["parameter"], "unit": row["unit"]} for i, row in enumerate(selected)]

    def clear(self):
        self.checked.clear()
        super().clear()
        self.selection_changed.emit()


class RelationsPage(QWidget):
    def __init__(self, event_model, definitions_path, parent=None):
        super().__init__(parent)
        self.engine = RelationEngine()
        self.definitions_path = definitions_path
        if definitions_path.exists():
            document = json.loads(definitions_path.read_text(encoding="utf-8"))
            if document["schema"] != 1:
                raise ValueError("不支持的关系配置版本：" + str(definitions_path))
            for item in document["relations"]:
                self.engine.add(item["name"], item["formula"], item["bindings"], item["unit"], item["window_ns"])
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        workspace = ThinSplitter(Qt.Orientation.Horizontal)
        workspace.setHandleWidth(24)
        controls = ThinSplitter(Qt.Orientation.Vertical)
        controls.setHandleWidth(24)
        controls.setMinimumWidth(360)
        left = QFrame()
        left.setObjectName("card")
        inputs = QVBoxLayout(left)
        inputs.setContentsMargins(16, 12, 16, 12)
        inputs.setSpacing(8)
        title = QLabel("原始参数序列 · 可选择多路")
        title.setObjectName("section")
        inputs.addWidget(title)
        note = QLabel("数据来自已解码参数，每个点保留原始报文位置。")
        self.input_note = note
        note.setWordWrap(True)
        note.setObjectName("muted")
        inputs.addWidget(note)
        self.input_model = InputModel(self)
        self.input_model.selection_changed.connect(self.update_bindings)
        self.input_view = QTableView()
        self.input_view.setModel(self.input_model)
        self.input_view.setShowGrid(False)
        self.input_view.setWordWrap(False)
        self.input_view.verticalHeader().hide()
        self.input_view.verticalHeader().setDefaultSectionSize(32)
        self.input_view.setMinimumHeight(132)
        self.input_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.input_view.horizontalHeader().setStretchLastSection(True)
        self.input_view.doubleClicked.connect(lambda index: self.show_record(self.input_model.rows[index.row()]))
        inputs.addWidget(self.input_view, 1)
        self.bindings_text = QPlainTextEdit()
        self.bindings_text.setReadOnly(True)
        self.bindings_text.setFixedHeight(76)
        self.bindings_text.setPlaceholderText("勾选参数后，会在这里分配 A、B、C… 变量。")
        inputs.addWidget(self.bindings_text)
        controls.addWidget(left)
        editor = QFrame()
        editor.setObjectName("card")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(16, 12, 16, 12)
        editor_layout.setSpacing(8)
        editor_title = QLabel("定义关系")
        editor_title.setObjectName("section")
        editor_layout.addWidget(editor_title)
        form = QGridLayout()
        form.setSpacing(8)
        form.setColumnStretch(1, 2)
        form.setColumnStretch(3, 1)
        self.name = QLineEdit()
        self.name.setPlaceholderText("例如：双路温差")
        self.formula = QLineEdit()
        self.formula.setPlaceholderText("例如：A-B、A/B、A+B>C、abs(A-B)<=0.25")
        self.unit = QLineEdit()
        self.unit.setMaxLength(32)
        self.unit.setPlaceholderText("输出单位，可留空；条件结果为 true / false")
        self.window_ms = QDoubleSpinBox()
        self.window_ms.setRange(0, 60000)
        self.window_ms.setDecimals(3)
        self.window_ms.setValue(50)
        self.window_ms.setSuffix(" ms")
        for field in (self.name, self.formula, self.unit):
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for row, column, text, field in ((0, 0, "名称", self.name), (0, 2, "对齐窗口", self.window_ms),
                                         (1, 0, "公式", self.formula), (1, 2, "单位", self.unit)):
            caption = QLabel(text)
            caption.setObjectName("muted")
            form.addWidget(caption, row, column)
            form.addWidget(field, row, column + 1)
        editor_layout.addLayout(form)
        self.create_button = QPushButton("创建并显示关系")
        self.create_button.setToolTip("支持 + - * /、比较、and/or/not、abs()；关系定义自动保存。")
        self.create_button.clicked.connect(self.create_relation)
        editor_layout.addWidget(self.create_button)
        self.feedback = QLabel("等待参数。启用插件并点击“开始采集”后，勾选输入。")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.setObjectName("muted")
        editor_layout.addWidget(self.feedback)
        controls.addWidget(editor)
        controls.setSizes([350, 230])
        workspace.addWidget(controls)
        right = QFrame()
        right.setObjectName("card")
        results = QVBoxLayout(right)
        results.setContentsMargins(16, 12, 16, 12)
        results.setSpacing(8)
        heading = QHBoxLayout()
        result_title = QLabel("已命名关系")
        result_title.setObjectName("section")
        heading.addWidget(result_title)
        heading.addStretch()
        self.remove_button = QPushButton("删除所选关系")
        self.remove_button.clicked.connect(self.remove_relation)
        heading.addWidget(self.remove_button)
        results.addLayout(heading)
        self.relationships = QTableWidget(0, 5)
        self.relationships.setHorizontalHeaderLabels(["名称", "公式", "最近有效值", "状态", "相对时间 / s"])
        self.relationships.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.relationships.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.relationships.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.relationships.setShowGrid(False)
        self.relationships.setWordWrap(False)
        self.relationships.verticalHeader().hide()
        self.relationships.verticalHeader().setDefaultSectionSize(36)
        self.relationships.setMinimumHeight(100)
        self.relationships.setMaximumHeight(190)
        self.relationships.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.relationships.horizontalHeader().setMinimumSectionSize(80)
        for column in (2, 4):
            self.relationships.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            self.relationships.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.relationships.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.relationships.setColumnWidth(3, 80)
        self.relationships.itemSelectionChanged.connect(self.render_plot)
        self.relationships.doubleClicked.connect(self.show_relation_evidence)
        results.addWidget(self.relationships)
        self.relation_title = QLabel("时间趋势")
        self.relation_title.setObjectName("section")
        self.relation_title.setWordWrap(True)
        self.relation_title.setTextFormat(Qt.TextFormat.PlainText)
        results.addWidget(self.relation_title)
        self.time_label = QLabel("时间：—")
        self.time_label.setObjectName("muted")
        self.time_label.setTextFormat(Qt.TextFormat.PlainText)
        self.time_label.setWordWrap(True)
        results.addWidget(self.time_label)
        self.plot = TrendPlot()
        self.plot.setMinimumHeight(150)
        results.addWidget(self.plot, 1)
        self.details = QLabel("条件值在曲线上以 0 / 1 表示。输入无效或超出对齐窗口时曲线断开。")
        self.details.setWordWrap(True)
        self.details.setObjectName("muted")
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        results.addWidget(self.details)
        workspace.addWidget(right)
        workspace.setSizes([400, 730])
        workspace.setStretchFactor(1, 1)
        self.tabs.addTab(workspace, "自定义关系")
        self.rule_view = QTableView()
        self.rule_view.setModel(event_model)
        self.rule_view.setShowGrid(False)
        self.rule_view.verticalHeader().hide()
        self.rule_view.verticalHeader().setDefaultSectionSize(36)
        self.rule_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rule_view.doubleClicked.connect(lambda index: self.show_record(event_model.rows[index.row()]))
        self.tabs.addTab(self.rule_view, "后端规则事件 · 双击溯源")
        root.addWidget(self.tabs)
        self.refresh()

    def update_bindings(self):
        self.bindings_text.setPlainText("\n".join(f"{b['alias']} = {b['source']} / {b['parameter']} [{b['unit']}]"
                                                for b in self.input_model.bindings()))

    def create_relation(self):
        try:
            relation = self.engine.add(self.name.text().strip(), self.formula.text(), self.input_model.bindings(),
                                       self.unit.text(), round(self.window_ms.value() * 1e6))
        except ValueError as error:
            self.feedback.setText(str(error))
            return
        self.feedback.setText("已创建：" + relation.name + "。正在等待满足时间与质量要求的新输入。")
        try:
            self.save_definitions()
        except OSError as error:
            self.engine.remove(relation)
            self.feedback.setText("关系未创建，配置保存失败：" + str(error))
            return
        self.refresh()
        self.relationships.selectRow(len(self.engine.relations) - 1)

    def remove_relation(self):
        row = self.relationships.currentRow()
        if row >= 0:
            relation = self.engine.relations[row]
            try:
                self.save_definitions(exclude=relation)
            except OSError as error:
                self.feedback.setText("删除失败，配置未更新：" + str(error))
                return
            self.engine.remove(relation)
            self.refresh()

    def feed(self, samples, omitted=0):
        self.input_model.update_samples(samples)
        self.input_note.setText(f"{self.input_model.rowCount()} 路已解码参数 · 双击追溯原始报文")
        self.engine.feed(samples, omitted)
        self.refresh()

    def reset_session(self):
        self.input_model.clear()
        self.engine.reset()
        self.refresh()

    def save_definitions(self, exclude=None):
        document = {"schema": 1, "relations": [{"name": r.name, "formula": r.formula, "unit": r.unit,
                     "bindings": r.bindings, "window_ns": r.window_ns} for r in self.engine.relations if r is not exclude]}
        temporary = self.definitions_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.definitions_path)

    def refresh(self):
        selected = self.relationships.currentRow()
        self.relationships.blockSignals(True)
        self.relationships.setRowCount(len(self.engine.relations))
        for row, relation in enumerate(self.engine.relations):
            elapsed = "—" if relation.origin_ns is None or relation.last_time_ns is None else f"{(relation.last_time_ns - relation.origin_ns) / 1e9:.6f}"
            values = [relation.name, relation.formula, display_value(relation.last_value), relation.status, elapsed]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column in (2, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.relationships.setItem(row, column, item)
        if selected >= 0 and self.engine.relations:
            self.relationships.selectRow(min(selected, len(self.engine.relations) - 1))
        self.relationships.blockSignals(False)
        self.remove_button.setEnabled(self.relationships.currentRow() >= 0)
        self.render_plot()

    def render_plot(self):
        row = self.relationships.currentRow()
        if row < 0:
            self.plot.select(None, [])
            self.relation_title.setText("时间趋势")
            self.time_label.setText("时间：—")
            return
        relation = self.engine.relations[row]
        self.relation_title.setText(f"{relation.name} · {relation.formula}" + (f" / {relation.unit}" if relation.unit else ""))
        self.time_label.setText(f"时基：{relation.time_group or '等待输入'} · 有效结果时间："
                               + ("—" if relation.last_time_ns is None else f"{relation.last_time_ns / 1e9:.6f} s"))
        points = [{"source": "relation", "parameter": relation.id, "generation": 0, "origin_session_id": None,
                   "time_group": relation.time_group, "usable": value is not None, "time_ns": timestamp,
                   "value": value, "unit": relation.unit} for timestamp, value in relation.history]
        self.plot.select(("relation", relation.id), points)
        self.plot.time_origin = relation.origin_ns
        self.plot.boolean_axis = any(type(value) is bool for _, value in relation.history)
        self.details.setText("变量映射：" + "；".join(f"{b['alias']}={b['source']}/{b['parameter']}" for b in relation.bindings)
                             + f"\n对齐窗口 {relation.window_ns / 1e6:g} ms · 横轴相对首个有效结果 · 最近 512 点")

    def show_record(self, record):
        dialog = QDialog(self)
        dialog.setWindowTitle("原始参数与关系证据")
        dialog.resize(740, 540)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(record, ensure_ascii=False, indent=2))
        layout.addWidget(text)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        dialog.exec()

    def show_relation_evidence(self, index):
        relation = self.engine.relations[index.row()]
        self.show_record({"name": relation.name, "formula": relation.formula, "bindings": relation.bindings,
                          "latest_valid_value": relation.last_value, "result_time_ns": relation.last_time_ns,
                          "status": relation.status, "latest_valid_evidence": relation.evidence})
