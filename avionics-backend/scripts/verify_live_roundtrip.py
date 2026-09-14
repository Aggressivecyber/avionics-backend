"""Optional stdlib-only acceptance check; the backend and its build remain C++.

Runs the public configured CLI, replaces a live source, then compares its JSONL
outputs byte-for-byte with offline analysis of the captured raw archive.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", default="build-gcc-debug")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    run = Path("runs") / f"live_roundtrip_{time.time_ns()}"
    run.mkdir(parents=True)
    env = os.environ.copy()
    if os.name == "nt":
        env["PATH"] = r"C:\msys64\mingw64\bin;" + env.get("PATH", "")
    suffix = ".exe" if os.name == "nt" else ""
    plugin_suffix = ".dll" if os.name == "nt" else ".so"
    binaries = Path(args.build) / "bin"
    plugin = (binaries / f"bus_simulated{plugin_suffix}").as_posix()
    archive = run / "capture.avbus"
    command = [str(binaries / f"bus_backend{suffix}"), "--interactive", str(archive), "config/correlation-demo.ini"]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, text=True, encoding="utf-8", errors="replace", creationflags=flags)
    try:
        process.stdin.write(f"add sensor_a {plugin} period_ms=5;initial_milli=10000;step_milli=0\n")
        process.stdin.write(f"add sensor_b {plugin} period_ms=5;initial_milli=10100;step_milli=0\n")
        process.stdin.write("start sensor_a\nstart sensor_b\n")
        process.stdin.flush()
        time.sleep(0.2)
        process.stdin.write(f"replace sensor_a {plugin} period_ms=5;initial_milli=12000;step_milli=0\n")
        process.stdin.flush()
        time.sleep(0.2)
        process.stdin.write("stats\nquit\n")
        process.stdin.flush()
        output, _ = process.communicate(timeout=10)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    (run / "live.log").write_text(output, encoding="utf-8")
    if process.returncode or "command failed:" in output:
        raise RuntimeError(f"Live command failed: {output}")
    offline = run / "offline"
    result = subprocess.run([str(binaries / f"bus_analyze{suffix}"), "--analyze", "config/correlation-demo.ini",
                             str(archive), str(offline)], env=env, capture_output=True, text=True, timeout=10,
                            creationflags=flags)
    (run / "offline.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    live = Path(str(archive) + ".analysis")
    for name in ("parameters.jsonl", "events.jsonl"):
        if (live / name).read_bytes() != (offline / name).read_bytes():
            raise AssertionError(f"Live/offline mismatch: {name}")
    samples = [json.loads(line) for line in (live / "parameters.jsonl").read_text(encoding="utf-8").splitlines()]
    if len(samples) < 4:
        raise AssertionError("Too few live samples")
    assert {s["source"] for s in samples} == {"sensor_a", "sensor_b"}
    assert {s["parameter"] for s in samples} == {"temperature_a", "temperature_b"}
    assert len({s["generation"] for s in samples if s["source"] == "sensor_a"}) == 2
    assert len({s["generation"] for s in samples if s["source"] == "sensor_b"}) == 1
    for line in (live / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        assert all(e["record_index"] > 0 for e in event["evidence"])
    summary = json.loads((offline / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "complete" and summary["samples"] == len(samples)
    check = {"live_exit": process.returncode, "offline_exit": result.returncode,
             "samples": len(samples), "results": summary["results"], "rejected": summary["rejected"],
             "byte_identical_parameters": True, "byte_identical_events": True,
             "changed_source_generations": 2, "unaffected_source_generations": 1}
    (run / "verification.json").write_text(json.dumps(check, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(check))
    print(f"artifacts={run.as_posix()}")


if __name__ == "__main__":
    main()
