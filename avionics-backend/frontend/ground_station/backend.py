"""Asynchronous process boundary. No acquisition or blocking waits run in the GUI."""
from collections import deque
import json
import os
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


def writable_data_path(text: str) -> Path:
    path = Path(text).expanduser().resolve()
    if os.name == "nt" and path.drive.upper() not in {"D:", "E:"}:
        raise ValueError("试验数据和输出目录请选择 D 盘或 E 盘。")
    return path


def quote_argument(value: str) -> str:
    if any(c in value for c in "\r\n\0"):
        raise ValueError("控制参数不能包含换行或空字符。")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class BackendClient(QObject):
    ready = Signal(object)
    snapshot = Signal(object)
    command_finished = Signal(str, object, bool, object)
    sealed = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool)
    busy_changed = Signal(bool)
    exited = Signal(int)

    def __init__(self, bin_dir: Path, parent=None):
        super().__init__(parent)
        self.bin_dir = bin_dir
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PATH", str(bin_dir) + os.pathsep + environment.value("PATH"))
        self.process.setProcessEnvironment(environment)
        self.poll = QTimer(self)
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._poll)
        self.active = False
        self.is_ready = False
        self.closing = False
        self.final_metadata = None
        self.buffer = bytearray()
        self.queue = deque()
        self.pending = None
        self.next_request = 0
        self.sample_cursor = self.event_cursor = 0
        self.stderr = ""

    def start(self, archive: Path, configuration: Path):
        if self.active:
            raise RuntimeError("当前会话尚未结束。")
        self.buffer.clear()
        self.queue.clear()
        self.pending = None
        self.sample_cursor = self.event_cursor = 0
        self.final_metadata = None
        self.is_ready = self.closing = False
        self.stderr = ""
        self.active = True
        self.active_changed.emit(True)
        suffix = ".exe" if os.name == "nt" else ""
        self.process.start(str(self.bin_dir / ("bus_backend" + suffix)),
                           ["--control", str(archive), str(configuration)])

    def send(self, command: str, *arguments):
        if not self.active or self.closing:
            return
        try:
            encoded = " ".join(str(x) if isinstance(x, int) else quote_argument(str(x)) for x in arguments)
            self.next_request += 1
            line = f"{self.next_request} {command} {encoded}\n".encode("utf-8")
            if len(line) > 65536:
                raise ValueError("控制参数超过 64 KiB。")
        except ValueError as error:
            self.failed.emit(str(error))
            return
        self.queue.append((self.next_request, command, arguments, line))
        if command == "quit":
            self.closing = True
            self.poll.stop()
        self._pump()

    def _pump(self):
        if self.is_ready and self.pending is None and self.queue:
            self.pending = self.queue.popleft()
            if self.process.write(self.pending[3]) < 0:
                self.failed.emit("无法写入后端控制管道。")
                self.process.closeWriteChannel()
        self.busy_changed.emit(self.pending is not None and self.pending[1] != "snapshot" or bool(self.queue))

    def _poll(self):
        if self.pending is None and not self.queue:
            self.send("snapshot", self.sample_cursor, self.event_cursor)

    def _read_stdout(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self.buffer:
            line, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            try:
                message = json.loads(line.decode("utf-8"))
                if message["schema"] != 1:
                    raise ValueError("不支持的控制协议版本。")
                if message.get("type") == "ready":
                    self.is_ready = True
                    self.ready.emit(message)
                    if not self.closing:
                        self.poll.start()
                    self._pump()
                elif message.get("type") == "closed":
                    self.final_metadata = message["data"]
                else:
                    if self.pending is None or message["request"] != self.pending[0]:
                        raise ValueError("控制响应的请求编号不匹配。")
                    _, command, arguments, _ = self.pending
                    self.pending = None
                    ok = message["ok"]
                    payload = message["data"] if ok else message["error"]
                    if ok and command == "snapshot":
                        self.sample_cursor = payload["sample_cursor"]
                        self.event_cursor = payload["event_cursor"]
                        self.snapshot.emit(payload)
                    self.command_finished.emit(command, arguments, ok, payload)
                    self._pump()
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                self.poll.stop()
                self.is_ready = False
                self.failed.emit(f"后端控制协议错误：{error}")
                self.process.closeWriteChannel()
                return

    def _read_stderr(self):
        text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        if text:
            self.stderr = (self.stderr + "\n" + text)[-8192:]
            self.failed.emit(text)

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.active = self.is_ready = False
            self.queue.clear()
            self.pending = None
            self.active_changed.emit(False)
            self.busy_changed.emit(False)
            self.failed.emit("后端无法启动：" + self.process.errorString())
            self.exited.emit(-1)
        elif error == QProcess.ProcessError.Crashed:
            self.failed.emit("后端异常退出，当前记录尚未确认封存。")

    def _finished(self, exit_code, exit_status):
        self._read_stdout()
        self._read_stderr()
        self.poll.stop()
        self.active = self.is_ready = False
        self.pending = None
        self.queue.clear()
        self.active_changed.emit(False)
        self.busy_changed.emit(False)
        if self.final_metadata is None:
            self.failed.emit(f"后端退出（代码 {exit_code}），未收到封存回执；不能确认记录完整。")
        else:
            metadata = self.final_metadata
            expected = (1 if not metadata["recording_healthy"] else
                        3 if metadata["analysis_status"] != "complete" else
                        4 if metadata["capture_status"] != "complete" else 0)
            if exit_status != QProcess.ExitStatus.NormalExit or exit_code != expected:
                self.failed.emit(f"已收到会话结果，但后端退出状态异常（{exit_code}）；请检查会话回执。")
            else:
                self.sealed.emit(metadata)
        self.exited.emit(exit_code)


def read_jsonl_tail(path: Path, limit=512):
    # Read a bounded tail rather than loading an entire long-running test into the GUI.
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        start = max(0, size - 2 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        lines = stream.read().splitlines()[-limit:]
    return [json.loads(line.decode("utf-8")) for line in lines]
