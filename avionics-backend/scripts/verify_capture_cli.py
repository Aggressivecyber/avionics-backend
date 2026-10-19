"""Public CLI acceptance for persistent SDK errors and child-process lifetime."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", default="build-gcc-release")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    run = Path("runs") / f"capture_cli_{time.time_ns()}"
    run.mkdir(parents=True)
    binaries = Path(args.build).resolve() / "bin"
    suffix = ".exe" if os.name == "nt" else ""
    plugin_suffix = ".dll" if os.name == "nt" else ".so"
    env = os.environ.copy()
    if os.name == "nt" and "AVIONICS_MINGW_BIN" in env:
        env["PATH"] = env["AVIONICS_MINGW_BIN"] + ";" + env.get("PATH", "")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    simulated = (binaries / f"bus_simulated{plugin_suffix}").as_posix()
    fault = (binaries / f"bus_fault{plugin_suffix}").as_posix()
    archive = run / "degraded.avbus"
    backend = str(binaries / f"bus_backend{suffix}")
    process = subprocess.Popen([backend, "--interactive", str(archive), "config/ground-monitor.example.ini"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, text=True, encoding="utf-8", creationflags=flags)
    try:
        process.stdin.write(f"add sensor_a {simulated} period_ms=1\nstart sensor_a\n"
                            f"add bad {fault} mode=exception\nstart bad\n")
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while not (archive.exists() and archive.stat().st_size > 512):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("fault test capture did not start")
            time.sleep(0.01)
        time.sleep(0.05)
        output, _ = process.communicate("status bad\nremove bad\nstats\nquit\n", timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    (run / "degraded.log").write_text(output, encoding="utf-8")
    assert process.returncode == 4 and "injected asynchronous SDK failure" in output
    summary = json.loads(Path(str(archive) + ".session.json").read_text(encoding="utf-8"))
    journal = [json.loads(line) for line in Path(str(archive) + ".lifecycle.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary["archive"]["integrity"] == "sealed" and summary["recorded"] == summary["accepted"] > 2
    assert summary["capture_status"] == "degraded" and summary["dropped"] == 0
    assert any(e["action"] == "removed" and e["source"] == "bad" and e["failure"] for e in journal)
    orphan_archive = run / "interrupted.avbus"
    parent = subprocess.Popen([backend, "--interactive", str(orphan_archive)], env=env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", creationflags=flags)
    child_handles = []
    try:
        parent.stdin.write(f"add probe {simulated} period_ms=1\nstart probe\n")
        parent.stdin.flush()
        deadline = time.monotonic() + 5
        while not (orphan_archive.exists() and orphan_archive.stat().st_size > 512):
            if parent.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("orphan test capture did not start")
            time.sleep(0.01)
        if os.name == "nt":
            command = r'''$taskChildren=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=__PID__" | Where-Object {$_.Name -eq 'bus_capture_worker.exe'} | Select-Object -ExpandProperty ProcessId)
$taskModules=@((Get-Process -Id __PID__).Modules | ForEach-Object {$_.ModuleName})
@{children=$taskChildren;modules=$taskModules} | ConvertTo-Json -Compress'''.replace("__PID__", str(parent.pid))
            powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
            observed = json.loads(subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                                                 capture_output=True, text=True, timeout=10, check=True, env=env,
                                                 creationflags=flags).stdout)
            assert len(observed["children"]) == 1
            assert not any(name.lower() == "bus_simulated.dll" for name in observed["modules"])
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            for pid in observed["children"]:
                handle = kernel.OpenProcess(0x100000, False, pid)
                assert handle
                child_handles.append(handle)
        else:
            children = Path(f"/proc/{parent.pid}/task/{parent.pid}/children").read_text().split()
            observed = {"children": [int(pid) for pid in children], "modules": Path(f"/proc/{parent.pid}/maps").read_text().splitlines()}
            assert len(children) == 1 and not any("bus_simulated.so" in row for row in observed["modules"])
        parent.kill()
        interrupted, _ = parent.communicate(timeout=5)
        if os.name == "nt":
            assert all(kernel.WaitForSingleObject(handle, 2000) == 0 for handle in child_handles)
        else:
            deadline = time.monotonic() + 2
            for pid in observed["children"]:
                while Path(f"/proc/{pid}/stat").exists() and Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z":
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
        (run / "interrupted.log").write_text(interrupted, encoding="utf-8")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.communicate()
        for handle in child_handles:
            kernel.CloseHandle(handle)
    inspect = subprocess.run([str(binaries / f"bus_analyze{suffix}"), "--inspect", str(orphan_archive)], env=env,
                             capture_output=True, text=True, timeout=5, creationflags=flags)
    assert inspect.returncode != 0
    (run / "interrupted-inspect.log").write_text(inspect.stdout + inspect.stderr, encoding="utf-8")
    receipt = {"sdk_error_exit": process.returncode, "sdk_error_reason_persisted_after_remove": True,
               "raw_archive_sealed": True, "raw_dropped": 0, "parent_loaded_sdk": False,
               "children_terminated_with_parent": len(observed["children"]), "interrupted_archive_rejected": True}
    (run / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    print(f"artifacts={run.as_posix()}")


if __name__ == "__main__":
    main()
