"""Scenario acceptance using real plugin processes, native Qt controls and negative oracles."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from PySide6.QtCore import QProcess, QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from .project import write_json
from .scenario_ui import ScenarioSetupDialog
from .scenarios import DEFAULT, validate, verify_scenario
from .window import MainWindow


def main(arguments=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin-dir", type=Path, default=Path(__file__).resolve().parents[2]/"bin")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    output = args.output.resolve() / ("run-"+uuid4().hex[:8])
    if output.drive.upper() == "C:": raise ValueError("验收数据请选择D/E盘。")
    output.mkdir(parents=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(output)
    app = QApplication(["AEROBUS-scenario-check"]); app.setStyle("Fusion")
    if app.platformName() == "offscreen" and os.name == "nt":
        for font in ("msyh.ttc", "segoeui.ttf"):
            assert QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"])/"Fonts"/font)) >= 0
        app.setFont(QFont("Microsoft YaHei UI", 9))
    errors = []
    original_hook = sys.excepthook
    sys.excepthook = lambda typ,value,tb:errors.append(str(value))
    windows = []
    receipt = {"success":False, "frozen":bool(getattr(sys,"frozen",False)), "platform":app.platformName(), "checks":[], "screenshots":[]}

    def wait(predicate, seconds=15):
        deadline = time.monotonic()+seconds
        while not predicate():
            if errors: raise AssertionError(errors[-1])
            if time.monotonic()>deadline: raise AssertionError("场景验收等待超时："+window.message.text())
            QTest.qWait(20); time.sleep(.005)
        if errors: raise AssertionError(errors[-1])

    def screenshot(widget, name):
        QTest.qWait(200)
        assert widget.grab().save(str(output/name))
        receipt["screenshots"].append(name)

    def configure(spec):
        def edit():
            dialog = app.activeModalWidget()
            assert isinstance(dialog, ScenarioSetupDialog)
            dialog.kind.setCurrentIndex(dialog.kind.findData(spec["kind"]))
            for key,field in dialog.fields.items():
                field.setValue(spec[key]/1000 if key.endswith("milli") else spec[key])
            assert dialog.save_button.isEnabled(), dialog.feedback.text()
            if spec["kind"] == "timeout": screenshot(dialog,"scenario-configuration.png")
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
        window.navigate(2)
        wait(lambda:window.plugin_center.cards["scenario"].configure_button.isEnabled())
        QTimer.singleShot(80, edit)
        QTest.mouseClick(window.plugin_center.cards["scenario"].configure_button, Qt.MouseButton.LeftButton)
        wait(lambda:window.capture_button.isEnabled())
        assert window.latest_snapshot["recorded"] == 0
        assert window.current_run["scenario"] == spec

    try:
        for key,value in (("kind",[]),("kind",None),("period_ms",0),("duration_ms",801),("baseline_milli",float("nan"))):
            try:
                validate({**DEFAULT,key:value})
            except ValueError:
                pass
            else:
                raise AssertionError("invalid scenario was accepted: "+key)
        receipt["checks"].append({"name":"invalid_scenario_input_rejected", "passed":True})
        configuration = Path(__file__).resolve().parents[2]/"config/desktop-demo.ini"
        window = MainWindow(args.bin_dir, output/"项目数据", configuration)
        windows.append(window); window.show(); QTest.qWait(80)
        signatures = {}
        for kind in ("normal", "limit", "timeout", "normal"):
            spec = {**DEFAULT, "kind":kind, "duration_ms":800, "trigger_ms":200,
                    "response_ms":400 if kind == "timeout" else 100}
            configure(spec)
            window.navigate(2); QTest.qWait(200)
            if kind == "normal" and not signatures:
                QTest.mousePress(window.capture_button, Qt.MouseButton.LeftButton)
                QTest.qWait(90)
                assert window.capture_button.press_depth>.9
                screenshot(window,"button-pressed.png")
                QTest.mouseRelease(window.capture_button, Qt.MouseButton.LeftButton)
                assert window.capture_started  # Click action is not delayed by the release animation.
                window.navigate(3); window.navigate(0); window.navigate(2)
                assert window.pages.currentIndex() == 2
                QTest.qWait(220)
                assert window.pages.fading_page is None
                receipt["checks"].append({"name":"button_feedback_and_rapid_page_switch", "passed":True})
            else:
                QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
            wait(lambda:not window.client.active and window.relation_process.state()==QProcess.ProcessState.NotRunning)
            assert window.current_run["relation_status"] == "complete", window.message.text()
            directory = window.project.resource(window.current_run["analysis"])
            result = json.loads((directory/"acceptance.json").read_text(encoding="utf-8"))
            assert result["passed"], [row for row in result["checks"] if not row["passed"]]
            assert window.last_metadata["recorded"] == 41 and window.last_metadata["samples"] == 123
            actual_events = [json.loads(line) for line in Path(str(window.archive)+".analysis/events.jsonl").read_text().splitlines()]
            # Fixed golden answers, independent of the producer and acceptance helper.
            golden = [("response_timeout",1400000000)] if kind == "timeout" else [("response_observed",1300000000)]
            if kind == "limit": golden.append(("parameter_above_limit",1200000000))
            assert sorted((event["outcome"],event["time_ns"]) for event in actual_events) == sorted(golden)
            if kind in signatures: assert result["data_signature"] == signatures[kind]
            signatures[kind] = result["data_signature"]
            receipt["checks"].append({"name":kind, "passed":True, "checks":len(result["checks"]), "data_signature":result["data_signature"]})
            if kind == "limit":
                limit_case = (dict(spec), window.archive, directory,
                              json.loads((directory/"summary.json").read_text(encoding="utf-8")))
                window.navigate(2); QTest.mouseClick(window.plugin_center.cards["scenario"].acceptance_button, Qt.MouseButton.LeftButton)
                dialog = window.scenario_result_dialog
                assert dialog.isVisible()
                dialog.table.selectRow(7)
                QTest.mouseClick(dialog.raw_button, Qt.MouseButton.LeftButton)
                wait(lambda:window.raw_process.state()==QProcess.ProcessState.NotRunning and hasattr(window,"evidence_dialog"))
                assert window.evidence_dialog.isVisible()
                screenshot(window.evidence_dialog,"scenario-raw-evidence.png"); window.evidence_dialog.close()
                screenshot(dialog,"scenario-acceptance.png"); dialog.close()
                for width,height in ((1160,780),(1440,900),(1680,1000)):
                    window.resize(width,height); screenshot(window,f"scenario-plugins-{width}.png")
                env = os.environ.copy(); env["PATH"] = str(args.bin_dir)+os.pathsep+env["PATH"]
                raw = subprocess.run([str(args.bin_dir/"bus_analyze.exe"),"--record",str(window.archive),"11"],
                                     capture_output=True,check=True,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
                record = json.loads(raw.stdout)
                assert record["payload_hex"].replace(" ", "").lower() == "b888000000000000e8030000000000000000000000000000"
                receipt["checks"].append({"name":"fixed_raw_hex_and_clickable_evidence", "passed":True})
        original_project = window.project.path
        archive = window.archive
        directory = window.project.resource(window.current_run["analysis"])
        metadata = json.loads((directory/"summary.json").read_text(encoding="utf-8"))
        # Each mutation must be caught; outputs from the successful runs stay intact.
        for mutation in ("value", "missing", "event_time", "formula", "relation_parameter",
                         "trigger_configuration", "trigger_generation", "partial_event_evidence"):
            case_spec, case_archive, case_directory, case_metadata = (
                limit_case if mutation == "partial_event_evidence" else (spec, archive, directory, metadata))
            case = output/("negative-"+mutation); case.mkdir()
            source = case/"input"; shutil.copytree(Path(str(case_archive)+".analysis"),source)
            analysis = case/"analysis"; analysis.mkdir()
            for name in (case_metadata["definition"],case_metadata["result_file"]): shutil.copy2(case_directory/name,analysis/name)
            if mutation in ("value","missing"):
                path = source/"parameters.jsonl"; rows = [json.loads(line) for line in path.read_text().splitlines()]
                if mutation == "value": rows[0]["value"] = 987
                else: rows.pop()
            elif mutation in ("event_time", "partial_event_evidence"):
                path = source/"events.jsonl"; rows = [json.loads(line) for line in path.read_text().splitlines()]
                if mutation == "event_time": rows[0]["time_ns"] += 1
                else:
                    assert len(rows) > 1
                    rows[0]["evidence"] = []
            else:
                path = analysis/case_metadata["result_file"]; rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                if mutation == "formula": rows[0]["value"] = 999
                elif mutation == "trigger_configuration": rows[0]["trigger"]["configuration_sha256"] = "other-configuration"
                elif mutation == "trigger_generation": rows[0]["trigger"]["generation"] += 1
                else:
                    inputs = [json.loads(line) for line in (source/"parameters.jsonl").read_text().splitlines()]
                    position = next(row for row in inputs if row["parameter"] == "position" and row["record_index"] == rows[0]["trigger"]["record_index"])
                    rows[0]["evidence"] = [{key:position[key] for key in rows[0]["evidence"][0]}]
            path.write_text("".join(json.dumps(row,ensure_ascii=False)+"\n" for row in rows),encoding="utf-8")
            result = verify_scenario(case_spec,source,analysis,case_archive,case_metadata)
            assert not result["passed"], mutation
            if mutation in ("relation_parameter", "trigger_configuration", "trigger_generation", "partial_event_evidence"):
                assert [row["name"] for row in result["checks"] if not row["passed"]] == ["结果到原始报文的关联"]
            receipt["checks"].append({"name":"reject_"+mutation, "passed":True})
        # Opening the portable document restores the exact scenario and keeps old archives.
        opened = MainWindow(args.bin_dir,output/"reopened",configuration); windows.append(opened); opened.show()
        opened.open_project(original_project)
        assert opened.project.document["scenario"] == spec
        opened.navigate(5); wait(lambda:not opened.history_page.query_active)
        assert opened.history_page.acceptance_button.isEnabled()
        QTest.mouseClick(opened.history_page.acceptance_button,Qt.MouseButton.LeftButton)
        assert opened.scenario_result_dialog.result["passed"]; opened.scenario_result_dialog.close()
        receipt["checks"].append({"name":"project_reopen_and_historical_acceptance", "passed":True})
        # Prepared edits seal only the empty run, then prepare the new saved scenario.
        configure(spec)
        changed = {**spec, "baseline_milli":21500}
        configure(changed)
        assert window.project.document["scenario"] == changed and window.latest_snapshot["recorded"] == 0
        empty_run = window.project.document["runs"][-2]
        assert empty_run["scenario_acceptance"] == "not_run"
        window.show_scenario_result(empty_run)
        assert window.scenario_result_dialog.result["passed"] is None and not window.scenario_result_dialog.raw_button.isEnabled()
        window.scenario_result_dialog.close()
        window.start_capture(); wait(lambda:window.latest_snapshot.get("recorded",0)>0)
        window.end_session()
        wait(lambda:not window.client.active and window.relation_process.state()==QProcess.ProcessState.NotRunning)
        directory = window.project.resource(window.current_run["analysis"])
        assert not json.loads((directory/"acceptance.json").read_text(encoding="utf-8"))["passed"]
        receipt["checks"].append({"name":"prepared_edit_and_early_stop_fails_acceptance", "passed":True})
        # Fractional upper-bound equality and exact response deadline, with more than 512 samples.
        boundary = {**DEFAULT, "period_ms":5, "duration_ms":1200, "trigger_ms":200, "response_ms":200,
                    "baseline_milli":1001, "limit_milli":1001, "high_milli":2000}
        configure(boundary); window.start_capture()
        wait(lambda:not window.client.active and window.relation_process.state()==QProcess.ProcessState.NotRunning)
        directory = window.project.resource(window.current_run["analysis"])
        result = json.loads((directory/"acceptance.json").read_text(encoding="utf-8"))
        assert result["passed"], [row for row in result["checks"] if not row["passed"]]
        assert window.last_metadata["samples"] == 723
        receipt["checks"].append({"name":"fractional_upper_and_response_deadline_equality_full_samples", "passed":True})
    finally:
        write_json(output/"receipt.json",receipt)
        for window in windows:
            if window.client.active: window.end_session(); wait(lambda:not window.client.active)
            wait(lambda:window.relation_process.state()==QProcess.ProcessState.NotRunning)
            window.close(); wait(lambda:not window.isVisible())
        sys.excepthook = original_hook
    receipt["success"] = True
    write_json(output/"receipt.json",receipt)
    print(json.dumps({"success":True,"checks":len(receipt["checks"]),"output":str(output)},ensure_ascii=False))


if __name__ == "__main__":
    main()
