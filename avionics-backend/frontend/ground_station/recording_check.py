"""Qt/process acceptance for volume rotation, crash discovery and recovered evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from .analysis_job import run_analysis
from .project import write_json
from .recording import archive_identity
from .window import MainWindow


def main(arguments=None):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin-dir", type=Path, default=Path(__file__).resolve().parents[2]/"bin")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    output = args.output.resolve() / ("run-"+uuid4().hex[:8])
    if output.drive.upper() == "C:": raise ValueError("验收数据请选择D/E盘。")
    output.mkdir(parents=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(output)
    app = QApplication(["AEROBUS-recording-check"]); app.setStyle("Fusion")
    if app.platformName() == "offscreen" and os.name == "nt":
        for font in ("msyh.ttc", "segoeui.ttf"):
            assert QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"])/"Fonts"/font)) >= 0
        app.setFont(QFont("Microsoft YaHei UI",9))
    errors = []
    original_hook = sys.excepthook
    sys.excepthook = lambda typ,value,tb: errors.append(str(value))
    windows = []
    receipt = {"success":False, "frozen":bool(getattr(sys,"frozen",False)), "platform":app.platformName(), "checks":[], "screenshots":[]}

    def wait(predicate, seconds=20):
        deadline = time.monotonic()+seconds
        while not predicate():
            if errors: raise AssertionError(errors[-1])
            if time.monotonic() > deadline:
                raise AssertionError("录制验收等待超时："+"\n".join(w.message.text()+"\n"+w.log.toPlainText() for w in windows)[-4000:])
            QTest.qWait(20); time.sleep(.005)
        if errors: raise AssertionError(errors[-1])

    def command(*arguments):
        return subprocess.run([str(args.bin_dir/"bus_analyze.exe"),*map(str,arguments)],
            capture_output=True, encoding="utf-8", check=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout

    def screenshot(window, name):
        window.resize(1160,780); QTest.qWait(160)
        assert window.grab().save(str(output/name))
        receipt["screenshots"].append(name)

    try:
        template = Path(__file__).resolve().parents[2]/"config/desktop-demo.ini"
        configuration = output/"分卷测试.ini"
        configuration.write_text(template.read_text(encoding="utf-8")+
            "\n[recording]\nvolume_bytes=4096\nvolume_seconds=1\nwarning_free_bytes=9223372036854775807\n",encoding="utf-8")
        window = MainWindow(args.bin_dir, output/"中文 数据", configuration)
        windows.append(window); window.show()
        window.load_configuration(configuration)
        window.start_demo(); wait(lambda:window.capture_button.isEnabled())
        QTest.mouseClick(window.capture_button,Qt.MouseButton.LeftButton)
        wait(lambda:window.parameter_model.rowCount()==4)
        page = window.relations_page
        for i,sample in enumerate(page.input_model.rows):
            if sample["source"] in ("sensor_a","sensor_b"):
                page.input_model.setData(page.input_model.index(i,0),Qt.CheckState.Checked.value,Qt.ItemDataRole.CheckStateRole)
        page.name.setText("跨卷温差"); page.formula.setText("A-B"); page.unit.setText("degC"); page.window_ms.setValue(100)
        page.create_relation()
        wait(lambda:window.latest_snapshot.get("recorded",0)>=150 and window.latest_snapshot["storage"]["volumes"]>=3)
        assert window.latest_snapshot["storage"]["low_space"] and "容量预警" in window.capacity_status.text()
        window.navigate(0); screenshot(window,"volume-capacity-warning.png")
        window.end_session()
        wait(lambda:not window.client.active and window.relation_process.state()==QProcess.ProcessState.NotRunning)
        archive = window.archive
        sealed = json.loads(command("--inspect",archive))
        identity = archive_identity(archive)
        assert sealed["volumes"]>=3 and sealed["file_sha256"]==identity["file_sha256"]
        assert sealed["records"]==window.last_metadata["recorded"]
        for file in [archive,*archive.parent.glob(archive.name+".part*.avbus")]:
            assert file.stat().st_size<=4096
        offline = output/"offline"
        command("--analyze",Path(str(archive)+".analysis/configuration.ini"),archive,offline)
        for filename in ("parameters.jsonl","events.jsonl"):
            assert (Path(str(archive)+".analysis")/filename).read_bytes()==(offline/filename).read_bytes(), filename
        original_analysis = window.project.resource(window.current_run["analysis"])
        rebuilt = run_analysis(offline,window.project.resource(window.current_run["relations"]),output/"formula-reference",archive)
        original_summary = json.loads((original_analysis/"summary.json").read_text(encoding="utf-8"))
        assert rebuilt["results"]>0 and (original_analysis/original_summary["result_file"]).read_bytes()==(output/"formula-reference"/rebuilt["result_file"]).read_bytes()
        index = sealed["records"]//2
        raw = json.loads(command("--record",archive,index))
        assert raw["record_index"]==index and raw["recording_session_id"]==sealed["recording_session_id"]
        receipt["checks"].append({"name":"size-time-capacity-and-cross-volume-analysis","volumes":sealed["volumes"],
                                  "records":sealed["records"],"formula_results":rebuilt["results"],"raw_record":index})

        window.start_capture()
        wait(lambda:window.client.active and window.latest_snapshot.get("recorded",0)>=150 and window.latest_snapshot["storage"]["volumes"]>=3)
        original_run = window.current_run
        interrupted = window.archive
        window.client.process.kill()
        wait(lambda:not window.client.active and window.relation_process.state()==QProcess.ProcessState.NotRunning and
             window.recording_mode is None and not window.pending_recording_checks)
        assert window.last_metadata is None and original_run["status"]=="待恢复"
        check = original_run["recovery_check"]
        assert check["status"]=="incomplete" and check["archive"]["records"]>0
        inputs = [interrupted,*sorted(interrupted.parent.glob(interrupted.name+".part*.avbus"))]
        before = {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
        project_path = window.project.path
        window.close(); wait(lambda:not window.isVisible())
        reopened = MainWindow(args.bin_dir,output/"reopened",configuration)
        windows.append(reopened); reopened.show(); reopened.open_project(project_path)
        wait(lambda:reopened.recording_mode is None and not reopened.pending_recording_checks)
        assert reopened.project.document["runs"][-1]["status"]=="待恢复"
        reopened.navigate(5); wait(lambda:reopened.history_page.recover_button.isEnabled())
        screenshot(reopened,"interrupted-record-check.png")
        QTest.mouseClick(reopened.history_page.recover_button,Qt.MouseButton.LeftButton)
        wait(lambda:reopened.recording_mode is None and reopened.project.document["runs"][-1]["status"]=="已恢复")
        recovered_run = reopened.project.document["runs"][-1]
        recovered_archive = reopened.project.resource(recovered_run["archive"])
        recovered = json.loads(command("--inspect",recovered_archive))
        assert recovered["recovered"] and recovered["records"]==check["archive"]["records"]
        assert recovered["recording_session_id"]==check["archive"]["recording_session_id"]
        assert before=={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
        result_directory = reopened.project.resource(recovered_run["analysis"])
        summary = json.loads((result_directory/"summary.json").read_text(encoding="utf-8"))
        assert summary["status"]=="recovered" and summary["results"]>0 and summary["samples"]==recovered["records"]
        assert "中断恢复" in (result_directory/"report.html").read_text(encoding="utf-8")
        history = reopened.history_page
        wait(lambda:history.catalog_loaded and not history.query_active)
        history.parameters.item(0).setCheckState(Qt.CheckState.Checked)
        for i in range(history.parameters.count()):
            if history.parameters.item(i).text().startswith("ƒ"):
                history.parameters.item(i).setCheckState(Qt.CheckState.Checked); break
        wait(lambda:not history.query_active and len(history.plot.lanes)==2)
        screenshot(reopened,"recovered-history.png")
        series = history.plot.lanes[0]["series"][0]
        evidence = json.loads(series["points"][-1][3])
        history.point_selected(evidence,series["archive"]); history.show_selected()
        wait(lambda:hasattr(reopened,"evidence_dialog") and reopened.evidence_dialog.isVisible())
        screenshot(reopened.evidence_dialog,"recovered-raw-evidence.png")
        reopened.evidence_dialog.close()
        reopened.export_run(result_directory)
        wait(lambda:reopened.export_process.state()==QProcess.ProcessState.NotRunning)
        assert (result_directory/"parameters.csv").exists() and (result_directory/"relations.csv").exists()
        reopened.edit_recording_options()
        screenshot(reopened,"recording-options.png")
        assert reopened.config_editor.fields["volume_bytes"].text()=="4096"
        receipt["checks"].append({"name":"process-kill-reopen-recover-history-formula-raw-export", "source_volumes":len(inputs),
            "recovered_records":recovered["records"],"formula_results":summary["results"],"original_files_unchanged":True,
            "recovery_status":summary["status"],"recovery_archive":str(recovered_archive)})
        receipt["build"] = json.loads(command("--version"))
        receipt["success"] = True
    finally:
        for window in windows:
            if window.client.active and not window.client.closing: window.end_session()
        deadline = time.monotonic()+20
        while any(window.client.active or window.recording_mode or window.relation_process.state()!=QProcess.ProcessState.NotRunning or
                  window.history_page.query_active for window in windows) and time.monotonic()<deadline:
            QTest.qWait(20); time.sleep(.005)
        for window in windows: window.close()
        QTest.qWait(100)
        sys.excepthook = original_hook
        receipt["qt_errors"] = errors
        write_json(output/"receipt.json",receipt)
        print(json.dumps({"receipt":str(output/"receipt.json"),"success":receipt["success"]},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
