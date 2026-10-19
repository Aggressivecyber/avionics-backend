"""Verify that analysis failure preserves raw data for complete offline recomputation."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", default="build-gcc-release")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    run = Path("runs") / f"analysis_recovery_{time.time_ns()}"
    run.mkdir(parents=True)
    config = Path("config/ground-monitor.example.ini")
    text = config.read_text(encoding="utf-8")
    assert "max_reorder_samples=8192" in text and "allowed_lateness_ms=50" in text
    failing = run / "failing.ini"
    failing.write_text(text.replace("max_reorder_samples=8192", "max_reorder_samples=1")
                      .replace("allowed_lateness_ms=50", "allowed_lateness_ms=60000"), encoding="utf-8")
    binaries = Path(args.build).resolve() / "bin"
    suffix = ".exe" if os.name == "nt" else ""
    plugin_suffix = ".dll" if os.name == "nt" else ".so"
    env = os.environ.copy()
    if os.name == "nt":
        env["PATH"] = r"C:\msys64\mingw64\bin;" + env.get("PATH", "")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    archive = run / "capture.avbus"
    process = subprocess.Popen([str(binaries / f"bus_backend{suffix}"), "--interactive", str(archive), str(failing)],
                               env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", creationflags=flags)
    try:
        plugin = (binaries / f"bus_simulated{plugin_suffix}").as_posix()
        process.stdin.write(f"add sensor_a {plugin} period_ms=1;initial_milli=10000;step_milli=0\n"
                            f"add sensor_b {plugin} period_ms=1;initial_milli=10100;step_milli=0\n"
                            "start sensor_a\nstart sensor_b\n")
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while not (archive.exists() and archive.stat().st_size > 8):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("capture did not produce raw data within the verification deadline")
            time.sleep(0.01)
        output, _ = process.communicate("stats\nquit\n", timeout=10)
        (run / "live.log").write_text(output, encoding="utf-8")
    finally:
        if process.poll() is None:
            process.kill()
            output, _ = process.communicate()
            (run / "live.log").write_text(output, encoding="utf-8")
    assert process.returncode == 3 and "reorder sample capacity exceeded" in output
    accepted, completed, dropped = map(int, re.findall(r"^(?:>\s*)*accepted=(\d+) completed=(\d+) dropped=(\d+)", output, re.MULTILINE)[-1])
    assert accepted > 2 and dropped == 0 and completed < accepted
    recovery = run / "recomputed"
    result = subprocess.run([str(binaries / f"bus_analyze{suffix}"), "--analyze", str(config), str(archive), str(recovery)],
                            env=env, capture_output=True, text=True, timeout=10, creationflags=flags, check=True)
    (run / "recomputed.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    summary = json.loads((recovery / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "complete" and summary["records"] == accepted and summary["samples"] == accepted
    receipt = {"live_exit": process.returncode, "accepted": accepted, "raw_records_recovered": summary["records"],
               "raw_dropped": dropped, "analysis_input_completed_before_failure": completed,
               "offline_status": summary["status"], "offline_samples": summary["samples"]}
    (run / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    print(f"artifacts={run.as_posix()}")


if __name__ == "__main__":
    main()
