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
from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetricsF, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QSplitter, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget

from ground_station.backend import BackendClient, writable_data_path
from ground_station.window import MainWindow, SourceDialog
from ground_station.plugin_center import PluginDetailsDialog, ReplaySetupDialog
from ground_station.ui_assets import ASSET_DIR


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
receipt = {"qt_version": app.instance().applicationVersion(), "checks": [], "layouts": []}


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
        if app.platformName() != "offscreen":
            self.assertTrue(self.window.screen().availableGeometry().contains(self.window.frameGeometry()),
                            (self.window.frameGeometry(), self.window.screen().availableGeometry()))

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

    def capture_layouts(self, stage, pages):
        window = self.window
        original_size, original_page = window.size(), window.pages.currentIndex()
        for requested_width, requested_height in ((1440, 900), (1160, 780), (1920, 1080)):
            width, height = requested_width, requested_height
            if app.platformName() != "offscreen":
                available = window.screen().availableGeometry()
                width = min(width, available.width() - (window.frameGeometry().width() - window.width()))
                height = min(height, available.height() - (window.frameGeometry().height() - window.height()))
            window.resize(width, height)
            for page in pages:
                window.navigate(page)
                QTest.qWait(30)
                self.assertEqual((window.width(), window.height()), (width, height))
                controls = [window.capture_button, window.end_button, window.heading,
                            window.recording_status, window.archive_path]
                if page == 3:
                    controls += [window.relations_page.input_view, window.relations_page.create_button,
                                 window.relations_page.relationships, window.relations_page.plot]
                    self.assertFalse(window.relations_page.input_view.geometry().intersects(
                        window.relations_page.bindings_text.geometry()))
                    table = window.relations_page.relationships
                    for row in range(table.rowCount()):
                        self.assertGreaterEqual(table.columnWidth(0),
                            QFontMetricsF(table.font()).horizontalAdvance(table.item(row, 0).text()) + 12)
                if page == 1:
                    for field in (window.offline_archive, window.offline_config, window.offline_output):
                        self.assertGreaterEqual(field.height(), 32)
                if page == 0:
                    self.assertFalse(window.plot.geometry().intersects(window.display_note.geometry()))
                    table = window.parameter_view
                    for row in range(window.parameter_proxy.rowCount()):
                        self.assertGreaterEqual(table.columnWidth(0), QFontMetricsF(table.font()).horizontalAdvance(
                            window.parameter_proxy.index(row, 0).data()) + 12)
                for control in controls:
                    self.assertTrue(control.isVisible())
                    bounds = control.rect().translated(control.mapTo(window, QPoint(0, 0)))
                    self.assertTrue(window.rect().contains(bounds), (stage, page, width, height, bounds))
                screenshot = f"{stage}-page-{page}-{width}x{height}.png"
                self.assertTrue(window.grab().save(str(args.output / screenshot)))
                receipt["layouts"].append({"stage": stage, "page": page, "width": width,
                                           "height": height, "requested_width": requested_width,
                                           "requested_height": requested_height, "screenshot": screenshot})
        window.resize(original_size)
        window.navigate(original_page)

    def test_01_live_roundtrip_and_offline(self):
        window = self.window
        QTest.mouseClick(window.plugin_center.cards["demo"].start_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.capture_button.isEnabled())
        QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
        # Rule events depend on real cross-process timing and the configured 5 ms rule window.
        wait_until(lambda: window.parameter_model.rowCount() == 4)
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
        self.capture_layouts("offline", (1,))
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
        wait_until(lambda: self.window.capture_button.isEnabled())
        QTest.mouseClick(self.window.capture_button, Qt.MouseButton.LeftButton)
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
        wait_until(lambda: window.capture_button.isEnabled())
        QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.parameter_model.rowCount() == 4)
        window.client.process.kill()
        wait_until(lambda: not window.client.active)
        self.assertIsNone(window.last_metadata)
        self.assertIn("失败", window.badge.text())
        self.assertIn("未收到封存回执", window.message.text())
        self.assertIn("未确认封存", window.recording_status.text())
        window.offline_archive.setText(str(window.archive))
        window.start_offline("inspect")
        wait_until(lambda: window.offline_mode is None)
        self.assertIsNone(window.offline_summary)
        self.assertIn("失败", window.offline_status.text())
        receipt["checks"].append({"name": self._testMethodName, "status": "passed",
                                   "unsealed_archive": str(window.archive)})

    def test_05_plugin_cards_and_named_relationships(self):
        window = self.window
        self.assertEqual(window.pages.currentIndex(), 2)
        self.assertEqual(window.recording_status.text(), "未开始采集")
        self.assertEqual(window.archive_path.text(), "")
        self.assertFalse(hasattr(window, "demo_button"))
        self.assertFalse(window.brand_mark.pixmap().isNull())
        mark = window.brand_mark.pixmap().toImage()
        self.assertTrue(any(mark.pixelColor(x, y).alpha() for x in range(mark.width()) for y in range(mark.height())))
        self.assertEqual(QFontInfo(window.parameter_view.font()).family(), "Microsoft YaHei UI")
        metrics = QFontMetricsF(window.parameter_view.font())
        self.assertLess(max(metrics.horizontalAdvance(c) for c in "0123456789") -
                        min(metrics.horizontalAdvance(c) for c in "0123456789"), 0.01)
        for page_index in range(4):
            window.navigate(page_index)
            self.assertTrue(window.capture_button.isVisible())
            self.assertFalse(window.capture_button.isEnabled())
        window.navigate(2)
        self.assertFalse(any("关联" in window.monitor_tabs.tabText(i) for i in range(window.monitor_tabs.count())))
        self.assertTrue(window.grab().save(str(args.output / "plugins.png")))
        def inspect_settings():
            self.assertIs(QApplication.activeModalWidget(), window.config_dialog)
            self.assertTrue(window.config_path.isVisible())
            self.assertTrue(window.storage_label.isVisible())
            self.assertTrue(window.config_dialog.grab().save(str(args.output / "settings.png")))
            window.config_dialog.reject()
        QTimer.singleShot(80, inspect_settings)
        QTest.mouseClick(window.config_toggle, Qt.MouseButton.LeftButton)
        demo = window.plugin_center.cards["demo"]
        inspected = []
        def inspect_details():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, PluginDetailsDialog)
            self.assertIn("功能", dialog.text.toPlainText())
            self.assertIn("数据关联", dialog.text.toPlainText())
            self.assertIn("开始采集", dialog.text.toPlainText())
            self.assertTrue(dialog.grab().save(str(args.output / "plugin-details.png")))
            inspected.append(True)
            dialog.accept()
        QTimer.singleShot(80, inspect_details)
        QTest.mouseClick(demo.details_button, Qt.MouseButton.LeftButton)
        self.assertTrue(inspected)
        QTest.mouseClick(demo.start_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.sources.rowCount() == 4 and window.capture_button.isEnabled())
        self.assertEqual(window.pages.currentIndex(), 0)
        QTest.qWait(600)
        self.assertEqual(window.latest_snapshot["recorded"], 0)
        self.assertEqual(window.latest_snapshot["samples"], 0)
        self.assertEqual(window.parameter_model.rowCount(), 0)
        self.assertEqual(window.recording_status.text(), "等待开始采集")
        self.assertEqual(window.archive_path.text(), str(window.archive))
        self.assertEqual(window.archive_path.toolTip(), str(window.archive))
        self.assertTrue(window.archive.is_file())
        self.assertTrue(all(source["status"]["state"] == "configured" for source in window.latest_snapshot["sources"]))
        self.assertIn("待开始", demo.status.text())
        window.navigate(2)
        QTest.qWait(30)
        self.assertTrue(window.grab().save(str(args.output / "plugins-ready.png")))
        self.capture_layouts("ready", (2,))
        QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.parameter_model.rowCount() == 4)
        self.assertEqual(window.recording_status.text(), "采集中")
        self.assertEqual(window.pages.currentIndex(), 0)
        wait_until(lambda: not window.capture_button.isEnabled())
        def inspect_evidence():
            dialog = QApplication.activeModalWidget()
            self.assertEqual(dialog.windowTitle(), "数据与证据溯源")
            self.assertTrue(dialog.grab().save(str(args.output / "evidence.png")))
            dialog.reject()
        QTimer.singleShot(80, inspect_evidence)
        window.show_evidence(window.parameter_model.rows[0])
        # Thin handles keep their native hit regions and still resize/scroll the data panes.
        dragged = []
        for splitter in window.pages.widget(0).findChildren(QSplitter):
            original = splitter.sizes()
            handle = splitter.handle(1)
            center = handle.rect().center()
            delta = QPoint(32, 0) if splitter.orientation() == Qt.Orientation.Horizontal else QPoint(0, 32)
            QTest.mouseMove(handle, center)
            QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=center)
            QTest.mouseMove(handle, center + delta, 30)
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=center + delta)
            self.assertNotEqual(splitter.sizes(), original)
            dragged.append(splitter.orientation().name)
            splitter.setSizes(original)
        window.resize(1160, 780)
        QTest.qWait(40)
        bar = window.parameter_view.horizontalScrollBar()
        self.assertTrue(bar.isVisible())
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.minimum())
        option = QStyleOptionSlider()
        bar.initStyleOption(option)
        thumb = bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, option,
                                         QStyle.SubControl.SC_ScrollBarSlider, bar)
        center = thumb.center()
        QTest.mouseMove(bar, center)
        self.assertTrue(window.grab().save(str(args.output / "thin-controls-hover.png")))
        QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=center)
        QTest.mouseMove(bar, center + QPoint(36, 0), 30)
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=center + QPoint(36, 0))
        self.assertGreater(bar.value(), bar.minimum())
        bar.setValue(bar.minimum())
        window.trend_selector.setCurrentIndex(0)
        window.trend_selector.showPopup()
        QTest.qWait(40)
        popup = QApplication.activePopupWidget()
        self.assertIsNotNone(popup)
        self.assertTrue(popup.grab().save(str(args.output / "dropdown.png")))
        QTest.keyClick(window.trend_selector.view(), Qt.Key.Key_Down)
        QTest.keyClick(window.trend_selector.view(), Qt.Key.Key_Return)
        self.assertEqual(window.trend_selector.currentIndex(), 1)
        self.assertFalse(popup.isVisible())
        window.resize(1440, 900)
        QTest.qWait(40)
        page = window.relations_page
        QTest.mouseClick(window.relations_nav, Qt.MouseButton.LeftButton)
        QTest.qWait(30)
        for index, sample in enumerate(page.input_model.rows):
            if sample["source"] in {"sensor_a", "sensor_b"}:
                page.input_model.setData(page.input_model.index(index, 0), Qt.CheckState.Checked.value,
                                         Qt.ItemDataRole.CheckStateRole)
        page.name.setText("双路温差")
        page.formula.setText("A-B")
        page.unit.setText("degC")
        page.window_ms.setValue(50)
        QTest.mouseClick(page.create_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(page.engine.relations), 1, page.feedback.text())
        wait_until(lambda: page.engine.relations and page.engine.relations[0].origin_ns is not None)
        first = page.engine.relations[0]
        wait_until(lambda: len(first.history) > 5)
        self.assertEqual(first.expression.names, {"A", "B"})
        self.assertEqual(len(first.evidence), 2)
        page.name.setText("温差条件")
        page.formula.setText("abs(A-B)<=0.25")
        page.unit.clear()
        QTest.mouseClick(page.create_button, Qt.MouseButton.LeftButton)
        condition = page.engine.relations[1]
        wait_until(lambda: condition.origin_ns is not None)
        self.assertIsInstance(condition.value, (bool, type(None)))
        for index, sample in enumerate(page.input_model.rows):
            if sample["source"] == "control":
                page.input_model.setData(page.input_model.index(index, 0), Qt.CheckState.Checked.value,
                                         Qt.ItemDataRole.CheckStateRole)
        page.name.setText("三路组合")
        page.formula.setText("A+B>C")
        QTest.mouseClick(page.create_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(page.engine.relations[2].bindings), 3)
        QTest.mouseClick(window.relations_nav, Qt.MouseButton.LeftButton)
        self.assertEqual(window.pages.currentIndex(), 3)
        self.assertEqual(page.relationships.rowCount(), 3)
        page.relationships.selectRow(0)
        self.assertGreater(len(page.plot.points), 0)
        self.assertTrue(window.grab().save(str(args.output / "relations.png")))
        self.capture_layouts("active", (0, 3))
        self.assertTrue((self.data_dir / "relations.json").exists())
        # A separate plugin copy keeps collecting when the demo module is disabled.
        separate = self.data_dir / ("独立采集插件" + plugin_suffix)
        shutil.copyfile(args.bin_dir / ("bus_simulated" + plugin_suffix), separate)
        window.client.send("add", "independent", str(separate), "period_ms=5")
        window.client.send("start", "independent")
        wait_until(lambda: self.source_state("independent") == "运行中")
        wait_until(lambda: demo.stop_button.isEnabled())
        QTest.mouseClick(demo.stop_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: all(self.source_state(source) == "已停止" for source in ("sensor_a", "sensor_b", "control", "feedback")))
        self.assertTrue(window.client.active)
        self.assertEqual(self.source_state("independent"), "运行中")
        self.assertEqual(window.recording_status.text(), "采集中")
        before = next(row for row in window.latest_snapshot["sources"] if row["id"] == "independent")["recorded"]
        wait_until(lambda: next(row for row in window.latest_snapshot["sources"] if row["id"] == "independent")["recorded"] > before)
        self.assertIn("已停用", demo.status.text())
        self.assertTrue(window.capture_button.isVisible())
        self.assertFalse(window.capture_button.isEnabled())
        # Starting another ready source must not restart the disabled demo plugin.
        window.client.send("add", "independent_ready", str(separate), "period_ms=5")
        wait_until(lambda: self.source_state("independent_ready") == "已配置" and window.capture_button.isEnabled())
        QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("independent_ready") == "运行中")
        self.assertTrue(all(self.source_state(source) == "已停止" for source in ("sensor_a", "sensor_b", "control", "feedback")))
        window.navigate(2)
        QTest.mouseClick(demo.start_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.capture_button.isEnabled())
        QTest.qWait(400)
        self.assertTrue(all(self.source_state(source) == "已停止" for source in ("sensor_a", "sensor_b", "control", "feedback")))
        QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("sensor_a") == "运行中")
        QTest.mouseClick(window.end_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: not window.client.active)
        self.assertEqual(window.last_metadata["capture_status"], "complete")
        archive = window.archive
        window.navigate(2)
        def configure_replay():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, ReplaySetupDialog)
            dialog.archive.setText(str(archive))
            dialog.speed.setValue(1)
            self.assertTrue(dialog.grab().save(str(args.output / "replay-setup.png")))
            dialog.validate()
        QTimer.singleShot(80, configure_replay)
        QTest.mouseClick(window.plugin_center.cards["replay"].start_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("replay") == "已配置" and window.capture_button.isEnabled())
        QTest.qWait(400)
        self.assertEqual(window.latest_snapshot["recorded"], 0)
        self.assertEqual(window.parameter_model.rowCount(), 0)
        QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("replay") == "运行中")
        wait_until(lambda: window.latest_snapshot["recorded"] > 0)
        self.assertEqual(window.pages.currentIndex(), 0)
        replay = window.plugin_center.cards["replay"]
        wait_until(lambda: replay.stop_button.isEnabled())
        QTest.mouseClick(replay.stop_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("replay") == "已停止")
        self.assertEqual(window.recording_status.text(), "已暂停采集")
        window.end_session()
        wait_until(lambda: not window.client.active)
        self.assertEqual(window.recording_status.text(), "已结束 · 归档已封存")
        self.assertEqual(window.archive_path.text(), str(window.archive))
        reopened = MainWindow(args.bin_dir, self.data_dir, self.configuration)
        self.assertEqual([r.name for r in reopened.relations_page.engine.relations], ["双路温差", "温差条件", "三路组合"])
        reopened.close()
        receipt["checks"].append({"name": self._testMethodName, "status": "passed",
                                   "plugin_details_verified": True, "named_relations": 3,
                                   "enable_without_acquisition_verified": True,
                                   "capture_button_visible_on_all_pages": True,
                                   "persistent_archive_status_verified": True,
                                   "splitter_drag_verified": dragged, "scrollbar_drag_verified": True,
                                   "dropdown_keyboard_selection_verified": True,
                                   "disabled_plugin_excluded_from_start": True,
                                   "stopping_demo_preserved_other_plugin": True,
                                   "replay_started_from_card": True, "definitions_reopened": True})


    def test_06_mil1553b_enable_start_stop_and_seal(self):
        window = self.window
        window.navigate(2)
        card = window.plugin_center.cards["mil1553b"]
        self.assertTrue(window.grab().save(str(args.output / "1553b-plugins.png")))
        QTest.mouseClick(card.start_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("bus1553") == "已配置" and window.capture_button.isEnabled())
        QTest.qWait(350)
        self.assertEqual(window.latest_snapshot["recorded"], 0)
        self.assertEqual(window.parameter_model.rowCount(), 0)
        self.assertEqual(Path(window.config_path.text()).name, "mil1553b-demo.ini")
        QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.parameter_model.rowCount() == 7)
        wait_until(lambda: window.latest_snapshot["recorded"] > 40)
        rows = {row["parameter"]: row for row in window.parameter_model.rows}
        self.assertTrue(rows["temperature_a"]["payload_valid"])
        self.assertTrue(rows["position"]["payload_valid"])
        self.assertIn("complete:", rows["mil1553b.protocol_status.ch0"]["value"]["label"])
        self.assertTrue(window.grab().save(str(args.output / "1553b-monitor.png")))
        page = window.relations_page
        window.navigate(3)
        QTest.qWait(30)
        for index, row in enumerate(page.input_model.rows):
            if row["parameter"] in {"temperature_a", "temperature_b"}:
                page.input_model.setData(page.input_model.index(index, 0), Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole)
        page.name.setText("1553B温差")
        page.formula.setText("A-B")
        page.unit.setText("degC")
        page.window_ms.setValue(50)
        QTest.mouseClick(page.create_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(page.engine.relations), 1, page.feedback.text())
        wait_until(lambda: bool(page.engine.relations) and len(page.engine.relations[0].history) > 2)
        self.assertIsNotNone(page.engine.relations[0].origin_ns)
        window.navigate(3)
        self.assertTrue(window.grab().save(str(args.output / "1553b-relations.png")))
        window.navigate(2)
        QTest.mouseClick(card.stop_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("bus1553") == "已停止")
        QTest.mouseClick(card.start_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.capture_button.isEnabled())
        QTest.qWait(350)
        self.assertEqual(self.source_state("bus1553"), "已停止")
        QTest.mouseClick(window.capture_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.source_state("bus1553") == "运行中")
        window.end_session()
        wait_until(lambda: not window.client.active)
        self.assertEqual(window.last_metadata["archive"]["integrity"], "sealed")
        self.assertEqual(window.last_metadata["capture_status"], "complete")
        self.assertEqual(window.last_metadata["analysis_status"], "complete")
        receipt["checks"].append({"name": self._testMethodName, "status": "passed", "enable_without_capture": True,
            "parameter_count": 7, "relation": "1553B温差", "restart_requires_start_button": True, "metadata": window.last_metadata})

    def test_07_artwork_pack_and_empty_states(self):
        manifest = json.loads((ASSET_DIR / "prompts.json").read_text(encoding="utf-8"))
        entries = manifest["assets"]
        self.assertGreaterEqual(len(entries), 20)
        self.assertEqual(len({entry["id"] for entry in entries}), len(entries))
        self.assertEqual({entry["kind"] for entry in entries}, {"icon", "background", "illustration"})
        hashes = []
        catalog = QWidget()
        catalog.setWindowTitle("AEROBUS · 美术素材验收")
        catalog.setStyleSheet(self.window.styleSheet())
        catalog.setObjectName("sheet")
        root = QVBoxLayout(catalog)
        title = QLabel(f"AEROBUS · {len(entries)} 项美术素材")
        title.setObjectName("heading")
        root.addWidget(title)
        grid = QGridLayout()
        grid.setSpacing(8)
        for i, entry in enumerate(entries):
            path = ASSET_DIR / entry["file"]
            self.assertEqual(digest(path), entry["sha256"])
            hashes.append(entry["sha256"])
            pixmap = QPixmap(str(path))
            self.assertFalse(pixmap.isNull(), entry["id"])
            self.assertEqual([pixmap.width(), pixmap.height()], entry["size"])
            if entry["transparent_background"]:
                image = pixmap.toImage()
                self.assertTrue(image.hasAlphaChannel(), entry["id"])
                # Allow up to 2/255 alpha residue along otherwise transparent edges.
                self.assertLessEqual(image.pixelColor(0, 0).alpha(), 2, entry["id"])
                self.assertTrue(any(image.pixelColor(x, y).alpha() > 0
                    for x in range(0, image.width(), image.width() // 16)
                    for y in range(0, image.height(), image.height() // 16)), entry["id"])
            tile = QWidget()
            content = QVBoxLayout(tile)
            content.setContentsMargins(4, 4, 4, 4)
            content.setSpacing(4)
            picture = QLabel()
            picture.setPixmap(pixmap.scaled(144, 80, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
            picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
            content.addWidget(picture)
            caption = QLabel(entry["usage"])
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            content.addWidget(caption)
            filename = QLabel(entry["id"])
            filename.setObjectName("muted")
            filename.setAlignment(Qt.AlignmentFlag.AlignCenter)
            content.addWidget(filename)
            grid.addWidget(tile, i // 6, i % 6)
        self.assertEqual(len(set(hashes)), len(entries))
        root.addLayout(grid, 1)
        catalog.resize(1380, 840)
        catalog.show()
        QTest.qWait(50)
        self.assertTrue(catalog.grab().save(str(args.output / "asset-catalog.png")))
        catalog.close()
        for page in (0, 1, 3):
            self.window.navigate(page)
            QTest.qWait(30)
            self.assertTrue(self.window.grab().save(str(args.output / f"empty-page-{page}.png")))
        self.assertEqual(self.window.offline_results.currentIndex(), 0)
        receipt["checks"].append({"name": self._testMethodName, "status": "passed",
            "generated_asset_count": len(entries), "unique_sha256": len(set(hashes)),
            "kinds": sorted({entry["kind"] for entry in entries}), "native_png_decoding": True})


if __name__ == "__main__":
    from PySide6.QtCore import qVersion
    receipt["qt_version"] = qVersion()
    suite = (unittest.defaultTestLoader.loadTestsFromNames(remaining, sys.modules[__name__]) if remaining
             else unittest.defaultTestLoader.loadTestsFromTestCase(FrontendIntegration))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    receipt["success"] = result.wasSuccessful() and not qt_errors
    receipt["qt_callback_errors"] = qt_errors
    (args.output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Verification receipt: " + str(args.output / "receipt.json"))
    raise SystemExit(0 if receipt["success"] else 1)
