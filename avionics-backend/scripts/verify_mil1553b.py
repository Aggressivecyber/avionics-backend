"""Verify the public 1553B capture/replacement/replay path and independent output identities."""
import argparse
import hashlib
import json
import os
import queue
from pathlib import Path
import subprocess
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    binaries = args.build.resolve() / "bin"
    run = args.output.resolve() / ("run-" + str(time.time_ns()))
    if os.name == "nt" and run.drive.upper() == "C:":
        raise ValueError("Verification outputs must be on D/E")
    run.mkdir(parents=True, exist_ok=False)
    config = root / "config/mil1553b-demo.ini"
    suffix = ".exe" if os.name == "nt" else ""
    module = ".dll" if os.name == "nt" else ".so"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    env = os.environ.copy()
    if os.name == "nt" and "AVIONICS_MINGW_BIN" in env:
        env["PATH"] = env["AVIONICS_MINGW_BIN"] + ";" + env.get("PATH", "")
    checks = []
    original_id = None
    for replay in (False, True):
        name = "replay" if replay else "live"
        archive = run / (name + ".avbus")
        plugin = binaries / (("bus_replay" if replay else "bus_mil1553b_simulated") + module)
        process = subprocess.Popen([str(binaries / ("bus_backend" + suffix)), "--control", str(archive), str(config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=env, creationflags=flags)
        lines = queue.Queue()
        observed = []
        request_id = 0

        def collect():
            for line in process.stdout:
                observed.append(line)
                lines.put(line)
            lines.put(None)

        def exchange(command, *arguments):
            nonlocal request_id
            request_id += 1
            encoded = [json.dumps(argument, ensure_ascii=False) if isinstance(argument, str) else str(argument) for argument in arguments]
            process.stdin.write(f"{request_id} {command} " + " ".join(encoded) + "\n")
            process.stdin.flush()
            response = json.loads(lines.get(timeout=10))
            assert response["request"] == request_id and response["ok"], response
            return response["data"]

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            ready = json.loads(lines.get(timeout=10))
            assert ready["type"] == "ready", ready
            options = f"path={(run / 'live.avbus').as_posix()};speed=0" if replay else "period_ms=2;channel=0;fault_every=0"
            exchange("add", "bus1553", plugin.as_posix(), options)
            exchange("start", "bus1553")
            deadline = time.monotonic() + 8
            while True:
                snapshot = exchange("snapshot", 0, 0)
                if (replay and snapshot["recorded"] == checks[0]["records"]) or (not replay and snapshot["recorded"] >= 40 and
                    any(event["outcome"] == "response_observed" for event in snapshot["events"])):
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("capture did not reach the required recorded count and response observation")
                time.sleep(0.03)
            if not replay:
                before = snapshot["recorded"]
                exchange("replace", "bus1553", plugin.as_posix(), "period_ms=2;channel=0;fault_every=7")
                deadline = time.monotonic() + 8
                while exchange("snapshot", 0, 0)["recorded"] < before + 40:
                    if time.monotonic() >= deadline:
                        raise AssertionError("replacement did not produce enough fault-injection records")
                    time.sleep(0.03)
            exchange("quit")
            closed = json.loads(lines.get(timeout=10))
            assert closed["type"] == "closed", closed
            process.wait(timeout=10)
            reader.join(timeout=2)
            output = "".join(observed)
        except BaseException:
            process.kill()
            process.wait(timeout=10)
            reader.join(timeout=2)
            (run / (name + ".log")).write_text("".join(observed), encoding="utf-8")
            raise
        (run / (name + ".log")).write_text(output, encoding="utf-8")
        if process.returncode or "command failed:" in output:
            raise RuntimeError(output)
        offline = run / (name + "-offline")
        analyzed = subprocess.run([str(binaries / ("bus_analyze" + suffix)), "--analyze", str(config), str(archive), str(offline)],
            env=env, capture_output=True, text=True, timeout=15, check=True, creationflags=flags)
        (run / (name + "-offline.log")).write_text(analyzed.stdout + analyzed.stderr, encoding="utf-8")
        inspection = subprocess.run([str(binaries / ("bus_analyze" + suffix)), "--inspect", str(archive)],
            env=env, capture_output=True, text=True, timeout=15, check=True, creationflags=flags)
        integrity = json.loads(inspection.stdout)
        metadata = json.loads(Path(str(archive) + ".session.json").read_text(encoding="utf-8"))
        assert metadata["capture_status"] == metadata["analysis_status"] == "complete"
        assert metadata["accepted"] == metadata["recorded"] > 32 and metadata["dropped"] == 0
        assert integrity["integrity"] == "sealed"
        assert integrity["file_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
        online = Path(str(archive) + ".analysis")
        hashes = {}
        for filename in ("parameters.jsonl", "events.jsonl"):
            assert (online / filename).read_bytes() == (offline / filename).read_bytes(), filename
            hashes[filename] = hashlib.sha256((online / filename).read_bytes()).hexdigest()
        samples = [json.loads(line) for line in (online / "parameters.jsonl").read_text(encoding="utf-8").splitlines()]
        events = [json.loads(line) for line in (online / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        diagnostics = [sample for sample in samples if sample["parameter"] == "mil1553b.protocol_status.ch0"]
        assert len(diagnostics) == metadata["recorded"]
        bad_records = {sample["record_index"] for sample in diagnostics if sample["value"]["code"] == 1}
        assert bad_records and all("parity" in sample["value"]["label"] for sample in diagnostics if sample["record_index"] in bad_records)
        engineering = [sample for sample in samples if sample["parameter"] != "mil1553b.protocol_status.ch0"]
        assert all(not sample["payload_valid"] for sample in engineering if sample["record_index"] in bad_records)
        assert all(not sample["usable"] for sample in diagnostics if sample["record_index"] in bad_records)
        assert {sample["parameter"] for sample in engineering} == {"command", "position", "temperature_a", "temperature_b", "received_temperature", "broadcast_counter"}
        digest = hashlib.sha256(config.read_bytes()).hexdigest()
        assert all(sample["configuration_sha256"] == digest and sample["recording_session_id"] == integrity["recording_session_id"] for sample in samples)
        generations = {sample["generation"] for sample in diagnostics}
        if replay:
            assert len(generations) == 1 and integrity["recording_session_id"] != original_id
            assert all(sample["origin_session_id"] == original_id and sample["origin_source"] == "bus1553" and sample["origin_record_index"] > 0 for sample in samples)
            assert all(not sample["usable"] for sample in samples), "recorded clock must not masquerade as host time"
        else:
            original_id = integrity["recording_session_id"]
            assert len(generations) == 2
            assert any(event["outcome"] == "consistent" for event in events)
            assert any(event["outcome"] == "response_observed" for event in events)
            for kind in ("bc_to_rt", "rt_to_bc", "rt_to_rt", "bc_broadcast"):
                assert any(kind in sample["value"]["label"] for sample in diagnostics)
            assert any("bus_a" in sample["value"]["label"] for sample in diagnostics)
            assert any("bus_b" in sample["value"]["label"] for sample in diagnostics)
        checks.append({"mode": name, "records": metadata["recorded"], "samples": len(samples), "events": len(events),
            "malformed_records": len(bad_records), "generations": len(generations), "archive": integrity,
            "online_offline_sha256": hashes, "configuration_sha256": digest})
    assert checks[0]["records"] == checks[1]["records"], "replay lost records"
    receipt = {"success": True, "scope": "synthetic-1553b-monitor-software", "hardware_validated": False, "checks": checks}
    (run / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"success": True, "receipt": str(run / "receipt.json"), "checks": checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
