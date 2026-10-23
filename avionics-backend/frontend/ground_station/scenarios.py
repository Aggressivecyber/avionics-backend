"""Scenario contract and independent acceptance oracle (the producer is a C++ plugin)."""
from collections import Counter
import hashlib
import html
import json
import math
from pathlib import Path

from .project import write_json
from .outcomes import OUTCOME_LABELS

NAMES = {"normal": "正常运行", "limit": "参数越限", "timeout": "响应超时"}
DEFAULT = {"schema": 1, "kind": "normal", "period_ms": 20, "duration_ms": 4000,
           "trigger_ms": 1000, "response_ms": 100, "deadline_ms": 200,
           "baseline_milli": 20000, "limit_milli": 25000, "high_milli": 35000}


def validate(document):
    if not isinstance(document, dict) or set(document) != set(DEFAULT):
        raise ValueError("场景字段不完整或含有未知字段。")
    if document["schema"] != 1 or document["kind"] not in NAMES:
        raise ValueError("不支持的场景类型或版本。")
    for key in set(DEFAULT) - {"kind"}:
        if type(document[key]) is not int:
            raise ValueError("场景数值必须为整数：" + key)
    period, duration = document["period_ms"], document["duration_ms"]
    if not 1 <= period <= 1000 or not 100 <= duration <= 600000 or duration // period > 20000:
        raise ValueError("采样间隔为1–1000 ms，时长为100–600000 ms，最多20001帧。")
    if any(document[key] <= 0 or document[key] % period for key in ("duration_ms", "trigger_ms", "response_ms")):
        raise ValueError("时长、触发时刻和反馈延迟必须是采样间隔的正整数倍。")
    if document["deadline_ms"] <= 0 or document["trigger_ms"] + max(document["response_ms"], document["deadline_ms"]) >= duration:
        raise ValueError("结束时间必须晚于反馈到达和响应截止时刻。")
    late = document["response_ms"] > document["deadline_ms"]
    if late != (document["kind"] == "timeout"):
        raise ValueError("响应超时场景的反馈延迟须大于最长响应时间；其他场景须在期限内响应。")
    if any(not -1000000 <= document[key] <= 1000000 for key in ("baseline_milli", "limit_milli", "high_milli")):
        raise ValueError("温度范围为−1000至1000 °C。")
    if document["baseline_milli"] > document["limit_milli"] or (document["kind"] == "limit" and document["high_milli"] <= document["limit_milli"]):
        raise ValueError("需满足：正常温度 ≤ 温度上限 < 越限温度。")
    return dict(document)


def plugin_configuration(spec):
    return ";".join(["scenario=" + spec["kind"]] +
                    [f"{key}={spec[key]}" for key in ("period_ms", "duration_ms", "trigger_ms", "response_ms", "baseline_milli", "high_milli")])


def configuration_text(spec):
    text = "# Software-generated scenario; not an aircraft ICD.\n[dictionary]\nschema=2\nversion=scenario-v1\n"
    for name, offset, unit in (("temperature", 0, "degC"), ("command", 64, "ratio"), ("position", 128, "ratio")):
        text += (f"\n[parameter {name}]\nsource=scenario\nprotocol=65535\nchannel=0\nbit_offset={offset}\n"
                 f"bit_width=64\nbyte_order=little\ntype=signed\nscale=0.001\nunit={unit}\n"
                 f"max_age_ms={spec['period_ms'] * 2}\nsequence_step=1\n")
    text += ("\n[clock scenario]\ndomain=2\ngroup=scenario\noffset_ns=0\nuncertainty_ns=0\n"
             "\n[limit temperature_limit]\ninput_source=scenario\ninput_parameter=temperature\n"
             f"upper={spec['limit_milli'] * 0.001:.17g}\n"
             "\n[response actuator]\ncommand_source=scenario\ncommand_parameter=command\n"
             "response_source=scenario\nresponse_parameter=position\ncommand_threshold=0.5\nresponse_threshold=0.5\n"
             f"max_delay_ms={spec['deadline_ms']}\n\n[processing]\nallowed_lateness_ms=0\n")
    return text


def relations_document(spec):
    upper = f"{spec['limit_milli'] * 0.001:.17g}"
    return {"schema": 1, "relations": [
        {"id": identifier, "name": name, "formula": formula, "unit": unit, "window_ns": 0,
         "bindings": [{"alias": "A", "source": "scenario", "parameter": "temperature"}]}
        for identifier, name, formula, unit in (
            ("scenario-temperature-excess", "温度相对上限", "A - " + upper, "degC"),
            ("scenario-temperature-high", "温度越限条件", "A > " + upper, ""))]}


def expected_events(spec):
    events = []
    if spec["kind"] == "limit":
        events.append(("temperature_limit", "parameter_above_limit", spec["trigger_ms"], spec["high_milli"] / 1000))
    if spec["kind"] == "timeout":
        events.append(("actuator", "response_timeout", spec["trigger_ms"] + spec["deadline_ms"], None))
    else:
        events.append(("actuator", "response_observed", spec["trigger_ms"] + spec["response_ms"], spec["response_ms"]))
    return sorted(events)


def describe_events(events):
    descriptions = []
    for rule, outcome, ms, metric in sorted(events, key=lambda row:row[2]):
        seconds = f"{ms/1000:.9f}".rstrip("0").rstrip(".")
        detail = "" if metric is None else f"（{metric:g} {'°C' if outcome.startswith('parameter_') else 'ms'}）"
        descriptions.append(f"{seconds} s {OUTCOME_LABELS[outcome]}{detail}")
    return "；".join(descriptions) if descriptions else "无事件"


def verify_scenario(spec, input_analysis, output, archive, metadata):
    """Compare full stored outputs with the declared contract, never with GUI preview data."""
    spec = validate(spec)
    input_analysis, output, archive = Path(input_analysis), Path(output), Path(archive)
    samples = [json.loads(line) for line in (input_analysis / "parameters.jsonl").read_text(encoding="utf-8").splitlines()]
    events = [json.loads(line) for line in (input_analysis / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    relations = [json.loads(line) for line in (output / metadata["result_file"]).read_text(encoding="utf-8").splitlines()]
    count = spec["duration_ms"] // spec["period_ms"] + 1
    checks = []

    def check(name, passed, expected, actual, evidence=None):
        checks.append({"name": name, "passed": bool(passed), "expected": str(expected), "actual": str(actual), "evidence": evidence})

    first = samples[0] if samples else None
    expected_sha = hashlib.sha256(configuration_text(spec).encode()).hexdigest()
    check("字典与规则快照", metadata["configuration_sha256"] == expected_sha,
          "使用场景配套字典与规则", "与场景一致" if metadata["configuration_sha256"] == expected_sha else "配置已更改", first)
    definition = json.loads((output / metadata["definition"]).read_text(encoding="utf-8"))
    check("公式定义", definition == relations_document(spec), "温度−上限；温度>上限", "与场景一致" if definition == relations_document(spec) else "公式定义已更改", first)
    capture = json.loads((archive.parent / "capture-result.json").read_text(encoding="utf-8"))
    complete = (capture["recording_healthy"] and capture["capture_status"] == "complete" and capture["analysis_status"] == "complete"
                and metadata["status"] == "complete" and capture["accepted"] == capture["recorded"] == count and capture["dropped"] == 0
                and capture["archive"]["integrity"] == "sealed" and capture["archive"]["records"] == count
                and capture["archive"]["file_sha256"] == metadata["archive_sha256"])
    check("原始记录与封存", complete, f"{count}帧，拒绝0帧，完整封存",
          f"{capture['recorded']}帧，拒绝{capture['dropped']}帧，" + ("封存校验一致" if complete else "完整性检查未通过"), first)
    counts = Counter(row["parameter"] for row in samples)
    check("完整参数数量", counts == {name: count for name in ("temperature", "command", "position")},
          f"3个参数，每个{count}点", " · ".join(f"{label}{counts[name]}点" for name,label in
              (("temperature","温度"),("command","指令"),("position","反馈"))), first)
    normalized = []
    for parameter in ("temperature", "command", "position"):
        rows = [row for row in samples if row["parameter"] == parameter]
        failures = []
        for index, row in enumerate(rows):
            ms = index * spec["period_ms"]
            if parameter == "temperature":
                expected = (spec["high_milli"] if spec["kind"] == "limit" and ms >= spec["trigger_ms"] else spec["baseline_milli"]) / 1000
            else:
                edge = spec["trigger_ms"] + (spec["response_ms"] if parameter == "position" else 0)
                expected = float(ms >= edge)
            good = (row["source"] == "scenario" and row["usable"] and row["quality"] == [] and
                    row["time_group"] == "scenario" and row["clock_domain"] == 2 and
                    row["unit"] == ("degC" if parameter == "temperature" else "ratio") and
                    row["recording_session_id"] == capture["archive"]["recording_session_id"] and
                    row["sequence"] == index and row["record_index"] == index + 1 and
                    row["time_ns"] == 1000000000 + ms * 1000000 and
                    row["configuration_sha256"] == expected_sha and
                    isinstance(row["value"], (int, float)) and math.isclose(row["value"], expected, rel_tol=0, abs_tol=1e-9))
            if not good:
                failures.append((row, f"第{index+1}点应为{expected:g}，实际{row['value']}；时间/质量/证据同时核对"))
            normalized.append([parameter, row["time_ns"], row["value"], row["usable"], row["record_index"]])
        check({"temperature":"温度解析", "command":"指令解析", "position":"反馈解析"}[parameter],
              not failures and len(rows) == count, f"{count}点数值、时间与报文序号全部一致",
              failures[0][1] if failures else f"已核对{len(rows)}点", failures[0][0] if failures else rows[0] if rows else None)
    expected = expected_events(spec)
    actual = sorted((row["rule"], row["outcome"], (row["time_ns"]-1000000000)/1000000, row["metric"]) for row in events)
    event_evidence = [e for row in events for e in row["evidence"]]
    events_match = len(expected) == len(actual) and all(a[:3] == b[:3] and
        ((a[3] is None and b[3] is None) or (isinstance(a[3], (int, float)) and isinstance(b[3], (int, float)) and
          math.isclose(a[3], b[3], rel_tol=0, abs_tol=1e-9))) for a,b in zip(expected, actual))
    check("告警与响应事件", events_match and all(row["time_group"] == "scenario" for row in events),
          describe_events(expected), describe_events(actual), event_evidence[0] if event_evidence else first)
    lookup = {(row["parameter"], row["record_index"]): row for row in samples}
    evidence_ok = bool(event_evidence)
    for item in event_evidence + [e for row in relations if row["usable"] for e in row["evidence"]]:
        row = lookup.get((item["parameter"], item["record_index"]))
        evidence_ok = evidence_ok and row is not None and item["recording_session_id"] == capture["archive"]["recording_session_id"] and all(item[key] == row[key] for key in
            ("source", "recording_session_id", "configuration_sha256", "generation", "time_ns"))
    check("结果到原始报文的关联", evidence_ok, "每条证据对应本次原始记录和参数", "一致" if evidence_ok else "证据缺失或不匹配", event_evidence[0] if event_evidence else first)
    for identifier, condition in (("scenario-temperature-excess", False), ("scenario-temperature-high", True)):
        declared = next(row for row in relations_document(spec)["relations"] if row["id"] == identifier)
        rows = [row for row in relations if row["relation_id"] == identifier]
        failures = []
        for index, row in enumerate(rows):
            high = spec["kind"] == "limit" and index * spec["period_ms"] >= spec["trigger_ms"]
            milli = spec["high_milli"] if high else spec["baseline_milli"]
            value = milli > spec["limit_milli"] if condition else (milli-spec["limit_milli"]) / 1000
            if (not row["usable"] or not isinstance(row["value"], (int, float)) or
                (condition and type(row["value"]) is not bool) or
                row["formula"] != declared["formula"] or row["relations_sha256"] != metadata["relations_sha256"] or
                row["time_group"] != "scenario" or row["unit"] != declared["unit"] or len(row["evidence"]) != 1 or
                row["trigger"]["source"] != "scenario" or row["trigger"]["parameter"] != "temperature" or
                row["trigger"]["recording_session_id"] != capture["archive"]["recording_session_id"] or
                not math.isclose(row["value"], value, rel_tol=0, abs_tol=1e-9) or
                row["time_ns"] != 1000000000 + index * spec["period_ms"] * 1000000 or
                row["trigger"]["record_index"] != index + 1):
                failures.append(row)
        check("条件公式" if condition else "差值公式", not failures and len(rows) == count and len(relations) == count*2,
              f"{count}点全部符合独立预期", f"{len(rows)}点，{len(failures)}处不匹配",
              failures[0]["trigger"] if failures else rows[0]["trigger"] if rows else None)
    run = json.loads((archive.parent / "run.json").read_text(encoding="utf-8"))
    not_run = not run["capture_started"] and not samples
    if not_run:
        checks = [{"name":"采集启动", "passed":None, "expected":"手动点击开始采集后执行场景验收",
                   "actual":"只准备了配置，尚未开始采集", "evidence":None}]
    passed = None if not_run else all(row["passed"] for row in checks)
    verdict = "未运行" if passed is None else "通过" if passed else "未通过"
    result = {"schema":1, "oracle":"scenario-contract-v1", "scenario":spec, "passed":passed,
              "checks":checks, "build":metadata["build"], "archive_sha256":metadata["archive_sha256"],
              "configuration_sha256":metadata["configuration_sha256"], "relations_sha256":metadata["relations_sha256"],
              "data_signature":hashlib.sha256(json.dumps([normalized, actual], separators=(",", ":")).encode()).hexdigest()}
    write_json(output / "acceptance.json", result)
    rows = "".join(f"<tr><td>{html.escape(row['name'])}</td><td>{'未运行' if row['passed'] is None else '通过' if row['passed'] else '未通过'}</td>"
                   f"<td>{html.escape(row['expected'])}</td><td>{html.escape(row['actual'])}</td>"
                   f"<td>{'#'+str(row['evidence']['record_index']) if row['evidence'] else '—'}</td></tr>" for row in checks)
    (output / "acceptance.html").write_text(f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>AEROBUS 场景验收</title>
<style>body{{font:14px 'Microsoft YaHei UI',sans-serif;color:#20292e;max-width:1088px;margin:32px auto;padding:24px}}h1{{font-size:24px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #e8e8e8;overflow-wrap:anywhere}}code{{overflow-wrap:anywhere}}p{{line-height:1.7}}</style>
<h1>{NAMES[spec['kind']]} · {verdict if passed is None else '验收'+verdict}</h1>
<p>软件合成场景，{spec['duration_ms']/1000:g} s，采样间隔{spec['period_ms']} ms。预期异常被正确识别也属于验收通过。</p>
<p>以下结果来自完整记录；不代表真实设备或实际总线验收。原始报文可在应用的验收结果中逐项打开。</p>
<table><tr><th>检查</th><th>结论</th><th>预期</th><th>实际</th><th>原始报文</th></tr>{rows}</table>
<p>场景快照：<code>{html.escape(json.dumps(spec, ensure_ascii=False))}</code></p>
<p>数据特征摘要：<code>{result['data_signature']}</code><br>软件：<code>{html.escape(json.dumps(result['build']))}</code><br>原始归档：<code>{result['archive_sha256']}</code></p></html>""", encoding="utf-8")
    return result
