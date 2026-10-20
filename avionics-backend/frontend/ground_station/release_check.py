"""Repeatable acceptance through the real Qt event loop and packaged executables."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
from unittest.mock import patch

from PySide6.QtCore import QPoint, QProcess, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidget, QTabWidget

from .jobs import start_analysis_process
from .project import read_shared, write_json
from .window import MainWindow


def main(arguments=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin-dir",type=Path,default=Path(__file__).resolve().parents[2]/"bin")
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args(arguments)
    output = args.output.resolve() / ("run-"+uuid4().hex[:8])
    if output.drive.upper() == "C:":
        raise ValueError("验收数据请选择D/E盘。")
    output.mkdir(parents=True,exist_ok=False)
    os.environ["TEMP"] = os.environ["TMP"] = str(output)
    app = QApplication(["AEROBUS-release-check"])
    app.setStyle("Fusion")
    if app.platformName() == "offscreen" and os.name == "nt":
        for font in ("msyh.ttc","segoeui.ttf"):
            assert QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"])/"Fonts"/font)) >= 0
        app.setFont(QFont("Microsoft YaHei UI",9))
    errors = []
    original_hook = sys.excepthook
    sys.excepthook = lambda typ,value,tb:errors.append(str(value))
    windows = []
    receipt = {"success":False,"frozen":bool(getattr(sys,"frozen",False)),"platform":app.platformName(),"checks":[],"screenshots":[]}

    def wait(predicate,seconds=20):
        deadline = time.monotonic()+seconds
        while not predicate():
            if errors: raise AssertionError(errors[-1])
            if time.monotonic() > deadline: raise AssertionError("Qt验收等待超时")
            QTest.qWait(20)
            time.sleep(.005)  # QTest.qWait alone can starve Python QThread work.
        if errors: raise AssertionError(errors[-1])

    def digest(path):
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream,"sha256").hexdigest()

    def command(arguments):
        env = os.environ.copy(); env["PATH"] = str(args.bin_dir)+os.pathsep+env["PATH"]
        return subprocess.run([str(args.bin_dir/"bus_analyze.exe"),*map(str,arguments)],capture_output=True,
                              check=True,env=env,creationflags=subprocess.CREATE_NO_WINDOW)

    def screenshot(window,name,width=1440,height=900):
        if app.platformName() != "offscreen":
            screen = window.screen().availableGeometry()
            width,height = min(width,screen.width()-20),min(height,screen.height()-48)
        window.resize(width,height); QTest.qWait(70)
        assert window.grab().save(str(output/name))
        receipt["screenshots"].append({"file":name,"width":window.width(),"height":window.height()})

    try:
        configuration = Path(__file__).resolve().parents[2]/"config"/"desktop-demo.ini"
        receipt["build"] = json.loads(command(["--version"]).stdout)
        window = MainWindow(args.bin_dir,output/"项目数据",configuration)
        windows.append(window); window.show(); QTest.qWait(60)
        window.start_demo(); wait(lambda:window.capture_button.isEnabled())
        assert window.latest_snapshot["recorded"] == 0
        for source in ("sensor_a","sensor_b"):
            window.client.send("replace",source,str(args.bin_dir/"bus_simulated.dll"),"period_ms=1;initial_milli=20000;step_milli=1")
        wait(lambda:not window.client.pending and not window.client.queue)
        window.start_capture(); wait(lambda:window.parameter_model.rowCount()==4)
        page = window.relations_page
        for i,sample in enumerate(page.input_model.rows):
            if sample["source"] in ("sensor_a","sensor_b"):
                page.input_model.setData(page.input_model.index(i,0),Qt.CheckState.Checked.value,Qt.ItemDataRole.CheckStateRole)
        for name,formula,unit in (("完整温差","A-B","degC"),("温差条件","abs(A-B)<=0.25",""),("除零诊断","A/(B-B)","")):
            page.name.setText(name); page.formula.setText(formula); page.unit.setText(unit); page.window_ms.setValue(50)
            page.create_relation()
        assert len(page.engine.relations)==3
        summary_path = window.project.resource(window.current_run["analysis"])/"summary.json"
        wait(lambda:window.latest_complete is not None and window.latest_complete["samples"]>1400)
        window.client.poll.stop()
        wait(lambda:window.client.pending is None and not window.client.queue)
        before = window.latest_complete["samples"]
        wait(lambda:window.latest_complete["samples"]>before+800)
        window.client.poll.start(); wait(lambda:window.display_omitted>0)
        window.end_session(); wait(lambda:not window.client.active)
        wait(lambda:window.relation_process.state()==QProcess.ProcessState.NotRunning)
        assert window.current_run["relation_status"]=="complete",window.message.text()
        analysis = window.project.resource(window.current_run["analysis"])
        summary = json.loads(read_shared(analysis/"summary.json"))
        assert summary["status"]=="complete" and summary["samples"]==window.last_metadata["samples"]
        assert summary["results"]>512 and summary["results_sha256"]==digest(analysis/summary["result_file"])
        with closing(sqlite3.connect(analysis/"history.sqlite")) as db:
            assert db.execute("SELECT count(*) FROM samples").fetchone()[0]==summary["samples"]
            assert db.execute("SELECT count(*) FROM relations").fetchone()[0]==summary["results"]
            assert db.execute("SELECT count(*) FROM relations WHERE usable=0").fetchone()[0]>0
        receipt["checks"].append({"name":"complete_relations_despite_display_omission","samples":summary["samples"],"results":summary["results"],"display_omitted":window.display_omitted})
        window.navigate(5); history = window.history_page
        wait(lambda:history.catalog_loaded and not history.query_active)
        for i in range(min(2,history.parameters.count())): history.parameters.item(i).setCheckState(Qt.CheckState.Checked)
        wait(lambda:not history.query_active and not history.query_pending and len(history.plot.lanes)==2)
        assert len(history.plot.lanes)==2
        full_count = sum(s["count"] for lane in history.plot.lanes for s in lane["series"])
        full_end = history.end.value()
        half = history.end.value()/2
        history.end.setValue(half); history.load_query()
        wait(lambda:not history.query_active and not history.query_pending and history.plot.end<=half+.000001)
        assert history.plot.end <= half+.000001
        assert sum(s["count"] for lane in history.plot.lanes for s in lane["series"]) < full_count
        history.start.setValue(history.end.value()+1)
        history.load_query()
        assert "结束时间须大于开始时间" in history.status.text()
        history.full_range()
        wait(lambda:not history.query_active and not history.query_pending and abs(history.plot.end-full_end)<=.000001)
        QTest.mouseClick(history.plot,Qt.MouseButton.LeftButton,pos=QPoint(history.plot.width()//2,history.plot.height()//4))
        assert history.raw_button.isEnabled() and history.selected_evidence[0]["record_index"]>0
        for size in ((1160,780),(1440,900),(1920,1080)):
            screenshot(window,f"history-{size[0]}x{size[1]}.png",*size)
        history.run_label.setText("首轮联调")
        history.steps.setPlainText("检查输入与时间基准\n执行采集与温差判定\n复核异常原始报文")
        history.notes.setPlainText("试验备注 <script>不得执行</script>；使用软件合成输入。")
        history.save_notes()
        with patch("ground_station.window.QDesktopServices.openUrl"):
            window.export_run(analysis); wait(lambda:window.export_process.state()==QProcess.ProcessState.NotRunning)
        assert (analysis/"parameters.csv").read_text(encoding="utf-8-sig").count("\n")==summary["samples"]+1
        report = (analysis/"report.html").read_text(encoding="utf-8")
        assert "&lt;script&gt;" in report and "<script>" not in report and summary["results_sha256"] in report
        receipt["checks"].append({"name":"history_ranges_notes_report_and_full_csv","status":"passed"})
        raw = json.loads(command(["--record",window.archive,1]).stdout)
        assert raw["record_index"]==1 and bytes.fromhex(raw["payload_hex"])
        window.request_raw_record({"record_index":1},window.archive)
        wait(lambda:hasattr(window,"evidence_dialog"))
        screenshot(window.evidence_dialog,"raw-record.png",900,650); window.evidence_dialog.close()
        offline = output/"offline"
        command(["--analyze",str(window.archive)+".analysis/configuration.ini",window.archive,offline])
        process = QProcess(window)
        start_analysis_process(process,["--input",str(offline),"--relations",str(window.project.resource(window.current_run["relations"])),
            "--output",str(offline/"workbench"),"--archive",str(window.archive)])
        wait(lambda:process.state()==QProcess.ProcessState.NotRunning,30)
        assert process.exitCode()==0,bytes(process.readAllStandardError()).decode("utf-8",errors="replace")
        offline_summary = json.loads((offline/"workbench/summary.json").read_text(encoding="utf-8"))
        assert offline_summary["results_sha256"]==summary["results_sha256"]
        for name in ("parameters.jsonl","events.jsonl"):
            assert digest(offline/name)==digest(Path(str(window.archive)+".analysis")/name)
        receipt["checks"].append({"name":"online_offline_byte_identity","sha256":summary["results_sha256"]})
        window.save_project(); project_file = window.project.path
        first_digest = digest(window.archive)
        copied = output/"复制后的项目根目录"
        shutil.copytree(window.data_dir,copied)
        second = MainWindow(args.bin_dir,output/"另一个工作区",configuration)
        windows.append(second); second.show(); second.open_project(copied/project_file.name)
        assert len(second.project.document["runs"])==1 and len(second.relations_page.engine.relations)==3
        assert len(second.source_configs)==4 and not second.client.active
        assert second.configuration_path.is_relative_to(copied)
        assert second.capture_button.isEnabled()
        original_run = second.project.document["runs"][0]
        assert digest(second.project.resource(original_run["archive"]))==first_digest
        second.start_capture(); wait(lambda:second.latest_snapshot.get("recorded",0)>100)
        second.end_session(); wait(lambda:not second.client.active)
        wait(lambda:second.relation_process.state()==QProcess.ProcessState.NotRunning)
        assert len(second.project.document["runs"])==2
        assert digest(second.project.resource(original_run["archive"]))==first_digest
        second.navigate(5); h = second.history_page
        wait(lambda:h.catalog_loaded and not h.query_active)
        h.parameters.item(0).setCheckState(Qt.CheckState.Checked); wait(lambda:not h.query_active)
        h.compare.setCurrentIndex(1)
        wait(lambda:not h.query_active and not h.query_pending and bool(h.plot.lanes) and len(h.plot.lanes[0]["series"])==2)
        assert len(h.plot.lanes[0]["series"])==2
        screenshot(second,"project-reopened-comparison.png")
        receipt["checks"].append({"name":"portable_project_reopen_and_second_capture","runs":2,"original_archive_unchanged":True})
        window.start_plugin(window.plugin_center.cards["mil1553b"].definition)
        wait(lambda:window.capture_button.isEnabled()); window.start_capture()
        wait(lambda:window.parameter_model.rowCount()==7)
        sample = next(row for row in window.parameter_model.rows if not row["parameter"].startswith("mil1553b."))
        window.end_session(); wait(lambda:not window.client.active)
        wait(lambda:window.relation_process.state()==QProcess.ProcessState.NotRunning)
        window.request_raw_record(sample,window.archive)
        wait(lambda:window.raw_process.state()==QProcess.ProcessState.NotRunning and window.evidence_dialog.isVisible())
        tabs = window.evidence_dialog.findChild(QTabWidget)
        assert tabs.count()==3
        tabs.setCurrentIndex(1)
        field_table = tabs.widget(1).findChild(QTableWidget)
        for row in range(field_table.rowCount()):
            for column in range(4):
                assert field_table.columnWidth(column) >= field_table.fontMetrics().horizontalAdvance(field_table.item(row,column).text())+22
        screenshot(window.evidence_dialog,"1553b-record-fields.png",1000,680)
        window.evidence_dialog.close()
        receipt["checks"].append({"name":"1553b_raw_record_fields_and_parameter_provenance","status":"passed"})
        old_project = window.project.path
        old_name = window.project.document["name"]
        old_runs = len(window.project.document["runs"])
        window.project_name.setText("独立项目B")
        window.new_project(); wait(lambda:window.client.is_ready)
        assert window.project.path != old_project
        saved_old = json.loads(old_project.read_text(encoding="utf-8"))
        assert saved_old["name"]==old_name and len(saved_old["runs"])==old_runs
        assert window.latest_snapshot.get("recorded",0)==0
        window.end_session(); wait(lambda:not window.client.active)
        wait(lambda:window.relation_process.state()==QProcess.ProcessState.NotRunning)
        receipt["checks"].append({"name":"new_project_preserves_previous_project_identity","status":"passed"})
        receipt["success"] = True
        write_json(output/"receipt.json",receipt)
        print(json.dumps({"success":True,"checks":len(receipt["checks"]),"output":str(output)},ensure_ascii=False))
    finally:
        for window in windows:
            if window.client.active: window.end_session(); wait(lambda:not window.client.active)
            wait(lambda:window.relation_process.state()==QProcess.ProcessState.NotRunning)
            window.close(); wait(lambda:not window.isVisible())
        sys.excepthook = original_hook
        if not receipt["success"]: write_json(output/"receipt.json",receipt)


if __name__ == "__main__":
    main()
