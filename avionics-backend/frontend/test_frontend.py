"""Real Qt event-loop / backend integration checks; all evidence stays on D/E."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import time
import traceback
import unittest
from uuid import uuid4

sys.dont_write_bytecode = True
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ground_station.backend import BackendClient, writable_data_path
from ground_station.window import MainWindow, SourceDialog


parser = argparse.ArgumentParser()
parser.add_argument("--bin-dir", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args, remaining = parser.parse_known_args()
args.output = writable_data_path(str(args.output)) / ("run-" + uuid4().hex[:8])
args.output.mkdir(parents=True, exist_ok=False)
app = QApplication(["frontend-tests"])
app.setStyle("Fusion")
if os.name == "nt" and app.platformName() == "offscreen":
    # The offscreen QPA plugin has no Windows system-font database.
    fonts = Path(os.environ["WINDIR"]) / "Fonts"
    for filename in ("msyh.ttc", "segoeui.ttf"):
        if QFontDatabase.addApplicationFont(str(fonts / filename)) < 0:
            raise RuntimeError("Cannot load the installed Windows font for visual QA: " + filename)
    app.setFont(QFont("Microsoft YaHei UI", 9))
plugin_suffix = ".dll" if os.name == "nt" else ".so"
qt_errors = []
sys.excepthook = lambda typ, value, tb: qt_errors.append("".join(traceback.format_exception(typ, value, tb)))
receipt = {"qt_version": app.instance().applicationVersion(), "checks": []}


def wait_until(predicate, timeout=10000):
    deadline = time.monotonic() + timeout / 1000
    while not predicate():
        if qt_errors:
            raise AssertionError(qt_errors[-1])
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for Qt/backend state")
        QTest.qWait(20)
    if qt_errors:
        raise AssertionError(qt_errors[-1])


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class FrontendIntegration(unittest.TestCase):
    def setUp(self):
        self.data_dir = args.output / self._testMethodName / "中文 试验数据"
        self.configuration = Path(__file__).resolve().parents[1] / "config" / "correlation-demo.ini"
        self.window = MainWindow(args.bin_dir, self.data_dir, self.configuration)
        self.window.show()
        QTest.qWait(30)

    def tearDown(self):
        if self.window.client.active and not self.window.client.closing:
            self.window.end_session()
        if self.window.client.active:
            wait_until(lambda: not self.window.client.active, 15000)
        self.window.close()
        QTest.qWait(20)

    def select_source(self, source):
        def exists():
            return any(self.window.sources.item(i, 0).text() == source for i in range(self.window.sources.rowCount()))
        wait_until(exists)
        for i in range(self.window.sources.rowCount()):
            if self.window.sources.item(i, 0).text() == source:
                self.window.sources.selectRow(i)
                return

    def source_state(self, source):
        for i in range(self.window.sources.rowCount()):
            if self.window.sources.item(i, 0).text() == source:
                return self.window.sources.item(i, 1).text()
        return None

    def test_01_live_roundtrip_and_offline(self):
        window = self.window
        QTest.mouseClick(window.demo_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.parameter_model.rowCount() == 4 and window.event_model.rowCount() > 0)
        QTest.qWait(550)
        self.assertEqual(window.sources.rowCount(), 4)
        self.assertGreater(len(window.plot.points), 5)
        self.assertIn("合成演示", window.badge.text())
        self.assertTrue(window.grab().save(str(args.output / "monitor.png")))
        # A stale cursor after actual >600 samples must report bounded-buffer omissions.
        # Use the measured sample count, independent of Windows SDK/timer scheduling.
        wait_until(lambda: window.client.sample_cursor > 600)
        window.client.send("snapshot", 0, 0)
        wait_until(lambda: window.display_omitted > 0)
        copied_plugin = self.data_dir / ("含 空格的插件" + plugin_suffix)
        shutil.copyfile(args.bin_dir / ("bus_simulated" + plugin_suffix), copied_plugin)
        before = next(row["generation"] for row in window.parameter_model.rows if row["source"] == "sensor_a")
        window.client.send("replace", "sensor_a", str(copied_plugin), "period_ms=20;initial_milli=22000;step_milli=10")
        wait_until(lambda: any(row["source"] == "sensor_a" and row["generation"] > before for row in window.parameter_model.rows))
        self.select_source("sensor_a")
        wait_until(lambda: window.stop_button.isEnabled())
        QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("sensor_a") == "已停止")
        QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("sensor_a") == "运行中")
        self.select_source("sensor_b")
        QTest.mouseClick(window.remove_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.sources.rowCount() == 3)
        QTest.mouseClick(window.end_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: not window.client.active)
        metadata = window.last_metadata
        self.assertIsNotNone(metadata)
        self.assertTrue(metadata["recording_healthy"])
        self.assertEqual(metadata["archive"]["integrity"], "sealed")
        self.assertEqual(metadata["accepted"], metadata["recorded"])
        self.assertEqual(metadata["dropped"], 0)
        self.assertEqual(metadata["analysis_status"], "complete")
        self.assertEqual(window.metric_values[1].text().replace(",", ""), str(metadata["samples"]))
        QTest.mouseClick(window.offline_nav, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.inspect_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.offline_mode is None)
        self.assertIsNotNone(window.offline_summary, window.offline_status.text())
        self.assertEqual(window.offline_summary["records"], metadata["recorded"])
        self.assertEqual(window.offline_summary["integrity"], "sealed")
        QTest.mouseClick(window.analyze_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.offline_mode is None)
        self.assertIsNotNone(window.offline_summary, window.offline_status.text())
        online = Path(str(window.archive) + ".analysis")
        hashes = {}
        for name in ("parameters.jsonl", "events.jsonl"):
            self.assertEqual(digest(online / name), digest(window.analysis_output / name))
            hashes[name] = digest(online / name)
        self.assertTrue(window.grab().save(str(args.output / "offline.png")))
        # Corruption must fail visibly, replacing the prior successful result.
        corrupt = self.data_dir / "损坏归档.avbus"
        content = bytearray(window.archive.read_bytes())
        content[-10] ^= 1
        corrupt.write_bytes(content)
        window.offline_archive.setText(str(corrupt))
        QTest.mouseClick(window.inspect_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.offline_mode is None)
        self.assertIsNone(window.offline_summary)
        self.assertIn("失败", window.offline_status.text())
        self.assertIn("失败", window.offline_summary_label.text())
        receipt["checks"].append({"name": self._testMethodName, "status": "passed", "metadata": metadata,
                                   "online_offline_sha256": hashes, "display_omitted": window.display_omitted})

    def test_02_sdk_failure_keeps_ui_responsive(self):
        window = self.window
        QTest.mouseClick(window.new_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.client.is_ready)
        plugin = str(args.bin_dir / ("bus_simulated" + plugin_suffix))
        window.client.send("add", "sensor_a", plugin, "period_ms=20")
        window.client.send("start", "sensor_a")
        wait_until(lambda: window.parameter_model.rowCount() == 1)
        failures = []
        window.client.command_finished.connect(lambda command, arguments, ok, payload: failures.append((command, ok, payload)))
        ticks = []
        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        start = time.monotonic()
        window.client.send("add", "hanging", str(args.bin_dir / ("bus_hang_load" + plugin_suffix)), "")
        wait_until(lambda: any(command == "add" and not ok for command, ok, _ in failures), 10000)
        elapsed = time.monotonic() - start
        timer.stop()
        self.assertGreater(elapsed, 4)
        self.assertGreater(len(ticks), 100)
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertLess(max(gaps), .5)
        window.client.send("add", "bad", str(args.bin_dir / ("bus_fault" + plugin_suffix)), "mode=exception")
        window.client.send("start", "bad")
        wait_until(lambda: self.source_state("bad") == "故障")
        wait_until(lambda: "降级" in window.badge.text())
        self.assertTrue(window.grab().save(str(args.output / "degraded.png")))
        QTest.mouseClick(window.end_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: not window.client.active)
        self.assertEqual(window.last_metadata["capture_status"], "degraded")
        self.assertEqual(window.last_metadata["archive"]["integrity"], "sealed")
        self.assertNotIn("采集与分析完整", window.badge.text())
        receipt["checks"].append({"name": self._testMethodName, "status": "passed",
                                   "blocked_sdk_seconds": elapsed, "gui_timer_ticks": len(ticks),
                                   "gui_timer_max_gap_ms": max(gaps) * 1000,
                                   "gui_timer_p95_gap_ms": statistics.quantiles(gaps, n=100)[94] * 1000,
                                   "metadata": window.last_metadata})

    def test_03_graceful_window_close_and_storage_boundary(self):
        if os.name == "nt":
            with self.assertRaises(ValueError):
                writable_data_path(r"C:\forbidden-test-data")
        dialog = SourceDialog(args.bin_dir, parent=self.window)
        dialog.source.setText("非法源 ID")
        dialog.validate()
        self.assertIn("源 ID", dialog.error.text())
        dialog.reject()
        self.window.start_demo()
        wait_until(lambda: self.window.parameter_model.rowCount() == 4)
        self.window.close()
        wait_until(lambda: not self.window.client.active)
        self.assertIsNotNone(self.window.last_metadata)
        self.assertFalse(self.window.isVisible())
        failed_client = BackendClient(self.data_dir / "missing-bin", self.window)
        failures = []
        failed_client.failed.connect(failures.append)
        failed_client.start(self.data_dir / "never-created.avbus", self.configuration)
        wait_until(lambda: not failed_client.active)
        self.assertIn("无法启动", failures[0])
        self.assertIsNone(failed_client.final_metadata)
        receipt["checks"].append({"name": self._testMethodName, "status": "passed"})

    def test_04_backend_crash_is_not_a_sealed_session(self):
        window = self.window
        window.start_demo()
        wait_until(lambda: window.parameter_model.rowCount() == 4)
        window.client.process.kill()
        wait_until(lambda: not window.client.active)
        self.assertIsNone(window.last_metadata)
        self.assertIn("失败", window.badge.text())
        self.assertIn("未收到封存回执", window.message.text())
        window.offline_archive.setText(str(window.archive))
        window.start_offline("inspect")
        wait_until(lambda: window.offline_mode is None)
        self.assertIsNone(window.offline_summary)
        self.assertIn("失败", window.offline_status.text())
        receipt["checks"].append({"name": self._testMethodName, "status": "passed",
                                   "unsealed_archive": str(window.archive)})


if __name__ == "__main__":
    from PySide6.QtCore import qVersion
    receipt["qt_version"] = qVersion()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FrontendIntegration)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    receipt["success"] = result.wasSuccessful() and not qt_errors
    receipt["qt_callback_errors"] = qt_errors
    (args.output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Verification receipt: " + str(args.output / "receipt.json"))
    raise SystemExit(0 if receipt["success"] else 1)
