"""Indexed history, linked time cursors, comparisons and trial notes."""
import json
import math
from pathlib import Path
import sqlite3

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .project import write_json
from .theme import ACCENT, BORDER, INK, MUTED
from .widgets import ThinSplitter
from .analysis_job import preview_points
from .outcomes import OUTCOME_LABELS


class HistoryQuery(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, primary, comparison, selections, group, start, end, page, anomalies, parent):
        super().__init__(parent)
        self.arguments = primary, comparison, selections, group, start, end, page, anomalies

    def run(self):
        primary, comparison, selections, group, start, end, page, anomalies = self.arguments
        try:
            with sqlite3.connect(primary[0].as_uri()+"?mode=ro", uri=True) as db:
                catalog = [(s, p, g, json.loads(text)["unit"]) for s, p, g, text in db.execute(
                    "SELECT source,parameter,grp,payload FROM samples GROUP BY source,parameter,grp ORDER BY source,parameter")]
                groups = sorted({r[2] for r in catalog if r[2]})
                group = group or (groups[0] if groups else "")
                low, high = db.execute("SELECT min(t),max(t) FROM samples WHERE grp=?", (group,)).fetchone()
                anchor = low or 0
                duration = max(0, ((high or anchor) - anchor) / 1e9)
                if end <= start:
                    start, end = 0, max(duration, .001)
                clause = " AND outcome NOT IN ('consistent','response_observed')" if anomalies else ""
                total = db.execute("SELECT count(*) FROM events WHERE json_extract(payload,'$.time_group')=? AND t BETWEEN ? AND ?" + clause,
                                   (group,anchor + round(start*1e9), anchor + round(end*1e9))).fetchone()[0]
                events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.time_group')=? AND t BETWEEN ? AND ?" + clause +
                    " ORDER BY t,seq LIMIT 100 OFFSET ?", (group,anchor + round(start*1e9), anchor + round(end*1e9), page*100))]
                relation_catalog = [(i, json.loads(text)) for i, text in db.execute("SELECT id,payload FROM relations GROUP BY id")]
            lanes = []
            for selection in selections[:4]:
                kind, source, parameter, selected_group, unit = selection
                series = []
                for entry in [primary] + ([comparison] if comparison else []):
                    with sqlite3.connect(entry[0].as_uri()+"?mode=ro", uri=True) as db:
                        origin = db.execute("SELECT min(t) FROM samples WHERE grp=?", (group,)).fetchone()[0]
                        if origin is None:
                            continue
                        query = ("SELECT seq,t,value,usable,payload FROM samples WHERE source=? AND parameter=? AND grp=? AND (t BETWEEN ? AND ? OR t IS NULL) ORDER BY seq"
                                 if kind == "parameter" else "SELECT seq,t,value,usable,payload FROM relations WHERE id=? AND json_extract(payload,'$.time_group')=? AND (t BETWEEN ? AND ? OR t IS NULL) ORDER BY seq")
                        args = ((source, parameter, selected_group) if kind == "parameter" else (source,group)) + (origin+round(start*1e9), origin+round(end*1e9))
                        count = 0
                        def rows():
                            nonlocal count
                            for row in db.execute(query,args):
                                count += 1
                                yield row
                        points = preview_points(rows(),start,end,origin)
                        series.append({"label": entry[2], "points": points, "archive": entry[1], "count": count})
                lanes.append({"name": parameter, "unit": unit, "series": series})
            self.result.emit({"catalog": catalog, "groups": groups, "group": group, "duration": duration, "anchor": anchor,
                              "start": start, "end": end, "events": events, "event_total": total, "relations": relation_catalog, "lanes": lanes})
        except (sqlite3.Error, OSError, ValueError, KeyError) as error:
            self.failed.emit("历史查询失败：" + str(error))


class HistoryPlot(QWidget):
    selected = Signal(object, object)
    zoomed = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(210)
        self.setMouseTracking(True)
        self.lanes = []
        self.start, self.end, self.cursor = 0, 1, None

    def set_data(self, result):
        self.lanes, self.start, self.end = result["lanes"], result["start"], result["end"]
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.lanes:
            self.cursor = self.start + max(0, min(1,(event.position().x()-72)/max(1,self.width()-96))) * (self.end-self.start)
            lane = self.lanes[min(len(self.lanes)-1, max(0, int(event.position().y()/max(1,(self.height()-28)/len(self.lanes)))))]
            points = [(abs(point[0]-self.cursor), point, series) for series in lane["series"] for point in series["points"]]
            if points:
                _, point, series = min(points, key=lambda p:p[0])
                self.selected.emit(json.loads(point[3]), series["archive"])
            self.update()

    def wheelEvent(self, event):
        if self.lanes:
            center = self.cursor if self.cursor is not None else (self.start+self.end)/2
            width = (self.end-self.start) * (.65 if event.angleDelta().y() > 0 else 1.5)
            self.zoomed.emit(max(0,center-width/2), max(.000001,center+width/2))
            event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = painter.font(); font.setPixelSize(12); painter.setFont(font)
        painter.fillRect(self.rect(), QColor("white"))
        if not self.lanes:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "勾选左侧参数或关系 · 最多同时查看4项 · 点击曲线定位原始报文")
            return
        height = (self.height()-28)/len(self.lanes)
        for index, lane in enumerate(self.lanes):
            top = index*height
            area = QRectF(72, top+28, max(1,self.width()-96), max(1,height-48))
            valid = [p[1] for s in lane["series"] for p in s["points"] if p[2] and p[1] is not None and math.isfinite(p[1])]
            low, high = (min(valid),max(valid)) if valid else (0,1)
            pad = max((high-low)*.1, abs(high)*.001, .001)
            low -= pad; high += pad
            painter.setPen(QColor(INK))
            painter.drawText(QRectF(8,top+2,self.width()-16,22), f"{lane['name']} / {lane['unit']}" +
                " · " + " / ".join(s["label"] + f" ({s['count']}点)" for s in lane["series"]))
            painter.setPen(QPen(QColor(BORDER),1))
            for i in range(3):
                y = area.top()+area.height()*i/2
                painter.drawLine(QPointF(area.left(),y),QPointF(area.right(),y))
                painter.setPen(QColor(MUTED))
                painter.drawText(QRectF(0,y-9,64,20), Qt.AlignmentFlag.AlignRight, f"{high-(high-low)*i/2:.4g}")
                painter.setPen(QPen(QColor(BORDER),1))
            for series_index, series in enumerate(lane["series"]):
                path = QPainterPath(); connected = False; epoch = None
                for t, value, usable, text,sequence in series["points"]:
                    record = json.loads(text)
                    identity = record["trigger"] if "trigger" in record else record
                    current_epoch = identity.get("generation"), identity.get("origin_generation"), identity.get("recording_session_id")
                    if not usable or value is None:
                        connected = False; continue
                    x = area.left() + (t-self.start)/max(1e-9,self.end-self.start)*area.width()
                    y = area.bottom()-(value-low)/(high-low)*area.height()
                    if connected and current_epoch == epoch: path.lineTo(x,y)
                    else: path.moveTo(x,y)
                    connected = True; epoch = current_epoch
                painter.setPen(QPen(QColor(ACCENT if series_index == 0 else "#687982"),1.7,
                                    Qt.PenStyle.SolidLine if series_index == 0 else Qt.PenStyle.DashLine))
                painter.drawPath(path)
            if self.cursor is not None:
                x = area.left()+(self.cursor-self.start)/max(1e-9,self.end-self.start)*area.width()
                painter.setPen(QPen(QColor(MUTED),1,Qt.PenStyle.DashLine))
                painter.drawLine(QPointF(x,area.top()),QPointF(x,area.bottom()))
        painter.setPen(QColor(MUTED))
        painter.drawText(QRectF(72,self.height()-25,self.width()-96,24), f"{self.start:.6f} s" + "     —     " + f"{self.end:.6f} s · 各记录同一时基首点为0；滚轮缩放")


class HistoryPage(QWidget):
    evidence_requested = Signal(object, object)
    export_requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.project = None
        self.query = None
        self.event_page = 0
        self.selected_evidence = None
        self.catalog_loaded = False
        self.query_pending = False
        self.context_generation = 0
        layout = QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        toolbar = QHBoxLayout()
        self.runs = QComboBox(); self.compare = QComboBox(); self.group = QComboBox()
        self.runs.setToolTip("选择项目内的一次采集；完整数据从磁盘索引读取。")
        self.compare.setToolTip("可与另一次采集比较；每次记录在所选时基的首个样本对齐为0秒，不推断跨试验绝对同步。")
        for text,widget in (("采集记录",self.runs),("比较",self.compare),("时基",self.group)):
            toolbar.addWidget(QLabel(text)); toolbar.addWidget(widget,1)
        self.refresh_button = QPushButton("刷新"); self.refresh_button.clicked.connect(self.reload)
        toolbar.addWidget(self.refresh_button); layout.addLayout(toolbar)
        self.status = QLabel("打开项目后选择采集记录。")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs,1)
        workspace = QWidget(); work = QVBoxLayout(workspace); work.setContentsMargins(0,12,0,0)
        ranges = QHBoxLayout()
        self.start = QDoubleSpinBox(); self.end = QDoubleSpinBox()
        for spin in (self.start,self.end): spin.setRange(0,1e9); spin.setDecimals(6); spin.setSuffix(" s")
        for spin in (self.start,self.end): spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons); spin.setMaximumWidth(180)
        self.start.setToolTip("所选时基首个样本之后的秒数；只筛选显示范围，完整数据保留。")
        self.end.setToolTip("结束相对时间，须大于开始时间；“全时段”恢复完整范围。")
        self.apply_button = QPushButton("应用范围"); self.apply_button.clicked.connect(lambda:self.zoom(self.start.value(),self.end.value()))
        self.full_button = QPushButton("全时段"); self.full_button.clicked.connect(self.full_range)
        self.raw_button = QPushButton("查看选中点报文"); self.raw_button.setEnabled(False); self.raw_button.clicked.connect(self.show_selected)
        for w in (QLabel("相对时间"),self.start,self.end,self.apply_button,self.full_button,self.raw_button): ranges.addWidget(w)
        work.addLayout(ranges)
        splitter = ThinSplitter(Qt.Orientation.Horizontal)
        self.parameters = QListWidget(); self.parameters.setMinimumWidth(220); self.parameters.setMaximumWidth(320)
        self.parameters.setToolTip("选择最多4项参数或自定义关系；各曲线独立纵轴并标明单位。")
        splitter.addWidget(self.parameters)
        right = QWidget(); right_layout = QVBoxLayout(right); right_layout.setContentsMargins(0,0,0,0)
        self.plot = HistoryPlot(); right_layout.addWidget(self.plot,2)
        event_controls = QHBoxLayout()
        self.anomalies = QCheckBox("仅异常"); self.anomalies.setChecked(True)
        self.previous = QPushButton("上一页"); self.next = QPushButton("下一页")
        self.event_count = QLabel()
        for w in (QLabel("规则事件 · 双击定位"),self.anomalies,self.event_count,self.previous,self.next): event_controls.addWidget(w)
        right_layout.addLayout(event_controls)
        self.events = QTableWidget(0,4); self.events.setHorizontalHeaderLabels(["规则","结果","相对时间 / s","原始记录"])
        self.events.setShowGrid(False)
        self.events.verticalHeader().hide(); self.events.horizontalHeader().setStretchLastSection(True)
        self.events.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.events.setMinimumHeight(145); right_layout.addWidget(self.events,1)
        splitter.addWidget(right); splitter.setSizes([240,900]); work.addWidget(splitter,1)
        self.tabs.addTab(workspace,"时间轴与异常")
        notes = QWidget(); form = QFormLayout(notes)
        self.run_label = QLineEdit(); self.notes = QPlainTextEdit(); self.steps = QPlainTextEdit()
        self.run_label.setToolTip("为这一次采集起名，用于记录列表和报告；原始文件身份保持不变。")
        self.steps.setToolTip("每行一个项目试验步骤，保存到项目并写入本次报告；不自动控制真实设备。")
        self.notes.setToolTip("本次工况、人工观察和复核结论；会保存到本次采集记录。")
        self.steps.setPlaceholderText("每行一个试验步骤；随项目保存，采集时形成快照。")
        self.notes.setPlaceholderText("填写本次采集工况、异常原因和人工复核结论。")
        self.save_notes_button = QPushButton("保存步骤与备注"); self.save_notes_button.clicked.connect(self.save_notes)
        self.export_button = QPushButton("导出报告与CSV"); self.export_button.clicked.connect(self.request_export)
        self.export_button.setToolTip("完整分析结束后导出HTML报告和全部参数、事件、关系CSV；报告可在浏览器打印为PDF。")
        form.addRow("本次采集名称",self.run_label); form.addRow("项目试验步骤",self.steps); form.addRow("本次采集备注",self.notes)
        actions = QHBoxLayout(); actions.addWidget(self.save_notes_button); actions.addWidget(self.export_button)
        form.addRow(actions); self.tabs.addTab(notes,"试验备注与报告")
        self.runs.currentIndexChanged.connect(self.reload)
        self.compare.currentIndexChanged.connect(self.load_query)
        self.group.currentIndexChanged.connect(self.group_changed)
        self.parameters.itemChanged.connect(self.load_query)
        self.plot.selected.connect(self.point_selected); self.plot.zoomed.connect(self.zoom)
        self.events.cellDoubleClicked.connect(self.event_selected)
        self.anomalies.toggled.connect(self.full_range)
        self.previous.clicked.connect(lambda:self.change_page(-1)); self.next.clicked.connect(lambda:self.change_page(1))

    def set_project(self, project, select_latest=False):
        selected = self.runs.currentData()
        self.project = project
        for combo in (self.runs,self.compare): combo.blockSignals(True); combo.clear()
        self.compare.addItem("不比较",None)
        for run in project.document["runs"]:
            self.runs.addItem(run["label"] + " · " + run["status"],run)
            if (project.resource(run["analysis"]) / "history.sqlite").exists(): self.compare.addItem(run["label"],run)
        if select_latest and self.runs.count(): self.runs.setCurrentIndex(self.runs.count()-1)
        elif selected:
            for i in range(self.runs.count()):
                if self.runs.itemData(i)["id"] == selected["id"]: self.runs.setCurrentIndex(i)
        for combo in (self.runs,self.compare): combo.blockSignals(False)
        self.reload()

    def entry(self, run):
        return self.project.resource(run["analysis"]) / "history.sqlite", self.project.resource(run["archive"]), run["label"]

    def reload(self):
        self.context_generation += 1
        self.group.blockSignals(True); self.group.clear(); self.group.blockSignals(False)
        self.catalog_loaded = False; self.event_page = 0
        self.parameters.blockSignals(True); self.parameters.clear(); self.parameters.blockSignals(False)
        self.start.setValue(0); self.end.setValue(0)
        run = self.runs.currentData()
        if run:
            self.run_label.setText(run["label"]); self.notes.setPlainText(run["notes"])
            self.steps.setPlainText("\n".join(self.project.document["steps"]))
        self.load_query()

    def group_changed(self):
        self.context_generation += 1
        self.event_page = 0
        self.catalog_loaded = False
        self.start.setValue(0); self.end.setValue(0)
        self.load_query()

    def full_range(self):
        self.event_page = 0; self.start.setValue(0); self.end.setValue(0); self.load_query()

    def zoom(self,start,end):
        self.event_page = 0
        self.start.setValue(start); self.end.setValue(end); self.load_query()

    def change_page(self,delta):
        self.event_page += delta; self.load_query()

    def load_query(self):
        run = self.runs.currentData()
        if not run or not self.project:
            return
        if self.query is not None and self.query.isRunning():
            self.query_pending = True
            return
        if self.end.value() <= self.start.value() and (self.start.value() > 0 or self.end.value() > 0):
            self.status.setText("结束时间须大于开始时间。")
            return
        entry = self.entry(run)
        if not entry[0].exists():
            self.status.setText("本次记录的完整分析尚未生成；采集开始后自动建立索引，封存后生成报告。")
            return
        selected = [self.parameters.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.parameters.count())
                    if self.parameters.item(i).checkState() == Qt.CheckState.Checked]
        if len(selected) > 4:
            self.status.setText("最多同时显示4项，请取消其他勾选。")
            return
        comparison = self.compare.currentData()
        self.query = HistoryQuery(entry,self.entry(comparison) if comparison else None,selected,self.group.currentText(),
                                  self.start.value(),self.end.value(),self.event_page,self.anomalies.isChecked(),self)
        generation = self.context_generation
        self.query.result.connect(lambda result:self.show_result(result) if generation == self.context_generation else None)
        self.query.failed.connect(self.status.setText)
        self.query.finished.connect(self.query_finished)
        self.query.start(); self.status.setText("正在查询完整历史…")

    def query_finished(self):
        if self.query_pending:
            self.query_pending = False
            self.load_query()

    def show_result(self,result):
        self.group.blockSignals(True)
        self.group.clear(); self.group.addItems(result["groups"]); self.group.setCurrentText(result["group"]); self.group.blockSignals(False)
        if not self.catalog_loaded:
            self.parameters.blockSignals(True); self.parameters.clear()
            for source,parameter,group,unit in result["catalog"]:
                if group != result["group"]: continue
                item = QListWidgetItem(parameter + "\n" + source + " · " + unit,self.parameters)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole,("parameter",source,parameter,group,unit))
            for identifier,row in result["relations"]:
                item = QListWidgetItem("ƒ " + row["name"] + " / " + row["unit"],self.parameters)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole,("relation",identifier,row["name"],result["group"],row["unit"]))
            self.parameters.blockSignals(False); self.catalog_loaded = True
        self.start.setValue(result["start"]); self.end.setValue(result["end"])
        self.plot.set_data(result)
        self.event_rows = result["events"]; self.anchor = result["anchor"]
        self.events.setRowCount(len(self.event_rows))
        for i,row in enumerate(self.event_rows):
            values = [row["rule"],OUTCOME_LABELS[row["outcome"]],f"{(row['time_ns']-self.anchor)/1e9:.6f}",
                      ", ".join(f"{e['source']} / #{e['record_index']}" for e in row["evidence"])]
            for j,value in enumerate(values): self.events.setItem(i,j,QTableWidgetItem(value))
        self.events.resizeColumnsToContents()
        self.event_count.setText(f"{result['event_total']}条 · 第{self.event_page+1}页")
        self.previous.setEnabled(self.event_page > 0); self.next.setEnabled((self.event_page+1)*100 < result["event_total"])
        self.status.setText(f"完整历史时长 {result['duration']:.6f} s · 曲线保留显示桶内极值与质量断点，完整点可导出CSV。")

    def point_selected(self,evidence,archive):
        if "trigger" in evidence:
            evidence = evidence["trigger"]
        self.selected_evidence = evidence,archive
        self.raw_button.setEnabled(True)
        self.status.setText(f"已定位：{evidence['source']} · 原始记录 #{evidence['record_index']} · 点击“查看选中点报文”。")

    def show_selected(self):
        self.evidence_requested.emit(*self.selected_evidence)

    def event_selected(self,row,column):
        event = self.event_rows[row]
        self.plot.cursor = (event["time_ns"]-self.anchor)/1e9; self.plot.update()
        if event["evidence"]:
            self.point_selected(event["evidence"][0],self.entry(self.runs.currentData())[1])
            self.show_selected()

    def save_notes(self):
        run = self.runs.currentData()
        if not run:
            return
        run["label"],run["notes"] = self.run_label.text().strip() or run["label"],self.notes.toPlainText()
        self.project.document["steps"] = [line.strip() for line in self.steps.toPlainText().splitlines() if line.strip()]
        for stored in self.project.document["runs"]:
            if stored["id"] == run["id"]: stored.update(run)
        self.project.save()
        write_json(self.project.resource(run["archive"]).parent / "run.json",{**run,"steps":self.project.document["steps"]})
        self.status.setText("试验步骤与备注已保存；重新导出报告会包含当前备注。")

    def request_export(self):
        run = self.runs.currentData()
        if run:
            self.save_notes()
            self.export_requested.emit(self.project.resource(run["analysis"]))
