"""Complete-file relation processing and a disk-backed history index, outside Qt."""
import argparse
import csv
from contextlib import closing
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

from .project import read_shared, write_json
from .relations import RelationEngine
from .outcomes import OUTCOME_LABELS


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def preview_points(rows, start, end, anchor=0, buckets=512):
    """Bound display memory while preserving each bucket's extrema and a gap."""
    bins = {}
    previous_time = start
    for sequence,t,value,usable,text in rows:
        relative = (t-anchor)/1e9 if t is not None else previous_time
        previous_time = relative
        point = (relative,value,bool(usable),text,sequence)
        bucket = max(0,min(buckets-1,int((relative-start)/max(end-start,1e-9)*buckets)))
        if bucket not in bins:
            bins[bucket] = {"first":point,"last":point}
        item = bins[bucket]
        item["last"] = point
        if usable and value is not None:
            if "min" not in item or value < item["min"][1]: item["min"] = point
            if "max" not in item or value > item["max"][1]: item["max"] = point
        else:
            item.setdefault("gap",point)
    return sorted({point for item in bins.values() for point in item.values()},key=lambda p:p[4])


def create_index(path):
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE samples(seq INTEGER PRIMARY KEY,t INTEGER,source TEXT,parameter TEXT,grp TEXT,usable INTEGER,value REAL,payload TEXT);
        CREATE INDEX sample_time ON samples(source,parameter,grp,t);
        CREATE TABLE events(seq INTEGER PRIMARY KEY,t INTEGER,rule TEXT,outcome TEXT,payload TEXT);
        CREATE INDEX event_time ON events(t);
        CREATE TABLE relations(seq INTEGER PRIMARY KEY,id TEXT,t INTEGER,usable INTEGER,value REAL,payload TEXT);
        CREATE INDEX relation_time ON relations(id,t);
    """)
    return db


def relation_row(relation, sample, sequence, revision):
    return {"schema": 1, "relation_id": relation.id, "name": relation.name, "formula": relation.formula,
            "relations_sha256": revision, "sample_sequence": sequence, "time_ns": relation.time_ns,
            "time_group": relation.time_group, "value": relation.value, "usable": relation.status == "有效",
            "status": relation.status, "unit": relation.unit, "trigger": {k: sample[k] for k in
                ("source", "parameter", "record_index", "generation", "recording_session_id", "configuration_sha256")},
            "evidence": relation.evidence if relation.status == "有效" else []}


def export_report(directory, metadata, export_csv=False):
    directory = Path(directory)
    with (directory / "history.sqlite").open("rb") as stream:
        if hashlib.file_digest(stream,"sha256").hexdigest() != metadata["index_sha256"]:
            raise ValueError("历史索引摘要不匹配，请从原始归档重新分析。")
    with closing(sqlite3.connect(directory / "history.sqlite")) as db:
        outcomes = list(db.execute("SELECT outcome,count(*) FROM events GROUP BY outcome"))
        relations = list(db.execute("SELECT id,count(*),sum(usable),sum(usable=1 AND value=0) FROM relations GROUP BY id"))
        definitions = json.loads((directory / metadata["definition"]).read_text(encoding="utf-8"))
        names = {r["id"]: r for r in definitions["relations"]}
        run_document = directory.parent / "run.json"
        run = json.loads(run_document.read_text(encoding="utf-8")) if run_document.exists() else {"label": "离线分析", "notes": "", "steps": []}
        configuration_sha = metadata["configuration_sha256"]
        rows = "".join(f"<tr><td>{html.escape(OUTCOME_LABELS[name])} / {html.escape(name)}</td><td>{count}</td></tr>" for name, count in outcomes)
        relation_rows = "".join(f"<tr><td>{html.escape(names[identifier]['name'])}</td><td><code>{html.escape(names[identifier]['formula'])}</code></td>"
                                f"<td>{count}</td><td>{usable}</td></tr>" for identifier, count, usable, _ in relations)
        charts = []
        for identifier, _, _, _ in relations[:4]:
            x0,x1,y0,y1 = db.execute("SELECT min(t),max(t),min(value),max(value) FROM relations WHERE id=? AND usable=1",(identifier,)).fetchone()
            if x0 is None:
                continue
            displayed = preview_points(db.execute("SELECT seq,t,value,usable,payload FROM relations WHERE id=? ORDER BY seq",(identifier,)),0,max((x1-x0)/1e9,1e-9),x0,350)
            path = []; connected = False; epoch = None
            for t,value,usable,text,sequence in displayed:
                row = json.loads(text)
                current_epoch = tuple(row["trigger"][key] for key in ("generation","recording_session_id"))
                if not usable or value is None:
                    connected = False
                    continue
                x,y = 40+t/max((x1-x0)/1e9,1e-9)*720,170-(value-y0)/max(1e-12,y1-y0)*140
                path.append(f"{'L' if connected and epoch == current_epoch else 'M'}{x:.2f},{y:.2f}")
                connected = True; epoch = current_epoch
            charts.append(f"<h3>{html.escape(names[identifier]['name'])} / {html.escape(names[identifier]['unit'])}</h3>"
                          f'<svg viewBox="0 0 800 210" role="img"><path d="M40 20V175H770" fill="none" stroke="#dfe4e6"/>'
                          f'<path d="{" ".join(path)}" fill="none" stroke="#087e8b" stroke-width="1.7"/>'
                          f'<text x="40" y="200">0 s</text><text x="650" y="200">{(x1-x0)/1e9:.6g} s</text>'
                          f'<text x="40" y="16">{y0:.6g} .. {y1:.6g}</text></svg>')
        steps = "".join(f"<li>{html.escape(step)}</li>" for step in run.get("steps", []))
        anomalies = []
        for text, in db.execute("SELECT payload FROM events WHERE outcome NOT IN ('consistent','response_observed') ORDER BY seq LIMIT 100"):
            event = json.loads(text)
            evidence = ", ".join(f"{e['source']} / #{e['record_index']}" for e in event["evidence"])
            anomalies.append(f"<tr><td>{html.escape(event['rule'])}</td><td>{html.escape(OUTCOME_LABELS[event['outcome']])}</td>"
                             f"<td>{event['time_ns']/1e9:.6f}</td><td>{html.escape(evidence)}</td></tr>")
        archive = html.escape(run.get("archive",metadata["archive"]))
        build_text = html.escape(encode(metadata["build"]))
        state = "完整参数计算完成" if metadata["status"] == "complete" else "输入未完整结束，结果供诊断"
        content = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>AEROBUS 试验报告</title>
<style>body{{font:15px 'Microsoft YaHei UI',sans-serif;color:#20292e;max-width:1000px;margin:44px auto;padding:0 24px}}h1{{font-size:28px}}h2{{font-size:19px;margin-top:32px}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #e4e8ea}}code{{overflow-wrap:anywhere}}p{{line-height:1.7}}svg{{width:100%;max-height:230px}}.meta{{color:#566770;overflow-wrap:anywhere}}@media print{{body{{margin:0}}h2{{break-after:avoid}}tr,svg{{break-inside:avoid}}}}</style>
<h1>AEROBUS · {html.escape(run['label'])}</h1><p>{state}。软件合成数据与真实设备试验需分别标识；记录完整不等于所有试验判据通过。</p>
<p>参数样本：{metadata['samples']}；关系计算记录：{metadata['results']}；后端事件：{metadata['events']}。</p>
<h2>试验步骤与人工备注</h2><ol>{steps}</ol><p>{html.escape(run.get('notes','')).replace(chr(10),'<br>')}</p>
<h2>后端判据统计</h2><table><tr><th>结果</th><th>次数</th></tr>{rows}</table>
<h2>自定义关系</h2><p>条件公式的 true/false 是表达式结果；是否合格由试验判据定义。以下曲线为范围预览，完整结果保存在 JSONL／CSV 与历史索引中。</p>
<table><tr><th>关系</th><th>公式</th><th>计算次数</th><th>有效次数</th></tr>{relation_rows}</table>{''.join(charts)}
<h2>异常定位（前100条）</h2><table><tr><th>规则</th><th>结果</th><th>时间 / s</th><th>原始记录</th></tr>{''.join(anomalies)}</table>
<h2>复核依据</h2><p class="meta">软件构建：{build_text}<br>原始归档：{archive}<br>归档 SHA-256：{metadata["archive_sha256"]}<br>参数配置 SHA-256：{configuration_sha}<br>关系定义 SHA-256：{metadata['relations_sha256']}<br>关系结果 SHA-256：{metadata.get('results_sha256','计算中')}<br>原始参数、后端事件、关系结果均保留记录位置与会话身份。项目内路径以项目文档为准。</p></html>"""
        (directory / "report.html").write_text(content, encoding="utf-8")
        if export_csv:
            for table, filename, keys in (
                ("samples", "parameters.csv", ["source", "parameter", "time_ns", "value", "unit", "usable", "quality", "record_index", "generation", "recording_session_id", "configuration_sha256"]),
                ("relations", "relations.csv", ["relation_id", "name", "formula", "time_ns", "value", "unit", "usable", "status", "relations_sha256", "trigger", "evidence"]),
                ("events", "events.csv", ["rule", "outcome", "time_ns", "metric", "evidence"])):
                with (directory / filename).open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=keys)
                    writer.writeheader()
                    for text, in db.execute(f"SELECT payload FROM {table} ORDER BY seq"):
                        row = json.loads(text)
                        writer.writerow({key: encode(row.get(key)) if isinstance(row.get(key), (dict, list)) else row.get(key) for key in keys})


def run_analysis(input_analysis, definitions, output, archive, follow=False):
    input_analysis, definitions, output = Path(input_analysis), Path(definitions), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    db = create_index(output / "history.sqlite")
    done = threading.Event()
    if follow:
        def read_control():
            for line in sys.stdin:
                if line.strip() == "finish":
                    break
            done.set()
        threading.Thread(target=read_control, daemon=True).start()
    else:
        done.set()
    sample_stream = event_stream = result_stream = None
    revision = None
    samples = events = results = 0
    metadata = {}
    try:
        while True:
            document_bytes = read_shared(definitions)
            new_revision = hashlib.sha256(document_bytes).hexdigest()
            if new_revision != revision:
                if result_stream is not None:
                    result_stream.close()
                revision = new_revision
                document = json.loads(document_bytes)
                engine = RelationEngine.from_document(document)
                definition_name, result_name = f"relations-{revision}.json", f"relations-{revision}.jsonl"
                (output / definition_name).write_bytes(document_bytes)
                result_stream = (output / result_name).open("w", encoding="utf-8", newline="\n")
                db.execute("DELETE FROM relations")
                results = 0
                def evaluate(row, sequence):
                    nonlocal results
                    engine.feed([row])
                    for relation in engine.by_input.get((row["source"], row["parameter"]), ()):
                        result = relation_row(relation, row, sequence, revision)
                        text = encode(result)
                        result_stream.write(text + "\n")
                        results += 1
                        value = result["value"]
                        db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?)", (results, relation.id, result["time_ns"],
                                   result["usable"], value if isinstance(value, (int, float)) else None, text))
                for sequence, text in db.execute("SELECT seq,payload FROM samples ORDER BY seq"):
                    evaluate(json.loads(text), sequence)
            sample_path, event_path = input_analysis / "parameters.jsonl", input_analysis / "events.jsonl"
            if sample_stream is None and sample_path.exists():
                sample_stream = sample_path.open("rb")
            if event_stream is None and event_path.exists():
                event_stream = event_path.open("rb")
            processed = 0
            if sample_stream is not None:
                for _ in range(1000):
                    position = sample_stream.tell()
                    line = sample_stream.readline()
                    if not line.endswith(b"\n"):
                        sample_stream.seek(position)
                        break
                    row = json.loads(line)
                    samples += 1
                    value = row["value"]
                    db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?)", (samples, row["time_ns"], row["source"], row["parameter"],
                               row["time_group"], row["usable"], value if isinstance(value, (int, float)) else None, encode(row)))
                    evaluate(row, samples)
                    processed += 1
            if event_stream is not None:
                for _ in range(1000):
                    position = event_stream.tell()
                    line = event_stream.readline()
                    if not line.endswith(b"\n"):
                        event_stream.seek(position)
                        break
                    row = json.loads(line)
                    events += 1
                    db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (events, row["time_ns"], row["rule"], row["outcome"], encode(row)))
                    processed += 1
            result_stream.flush()
            db.commit()
            metadata = {"schema": 1, "status": "running", "samples": samples, "events": events, "results": results,
                        "relations_sha256": revision, "definition": definition_name, "result_file": result_name,
                        "input_analysis": str(input_analysis.resolve()), "archive": str(Path(archive).resolve()),
                        "current": [{"id": r.id, **{k: getattr(r, k) for k in ("status", "value", "time_ns", "last_value", "last_time_ns",
                                    "origin_ns", "time_group", "evidence")}, "history": list(r.history)} for r in engine.relations]}
            if not (output / "summary.json").exists():
                write_json(output / "summary.json", metadata)
            if follow:
                print(encode(metadata),flush=True)
            if done.is_set() and processed == 0:
                if sample_stream is None or event_stream is None:
                    raise ValueError("完整参数／事件文件不存在。")
                if sample_stream.read() or event_stream.read():
                    raise ValueError("参数／事件文件以不完整JSON行结束。")
                break
            if not processed:
                time.sleep(.1)
        result_stream.close()
        result_stream = None
        with (output / result_name).open("rb") as stream:
            metadata["results_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        with Path(archive).open("rb") as stream:
            metadata["archive_sha256"] = hashlib.file_digest(stream,"sha256").hexdigest()
        metadata["configuration_sha256"] = hashlib.sha256((input_analysis / "configuration.ini").read_bytes()).hexdigest()
        provenance = output.parent / "run.json"
        metadata["build"] = (json.loads(provenance.read_text(encoding="utf-8"))["build"] if provenance.exists() else
                             json.loads((input_analysis / "summary.json").read_text(encoding="utf-8"))["build"])
        capture_result = output.parent / "capture-result.json"
        metadata["status"] = "complete"
        if follow and (not capture_result.exists() or json.loads(capture_result.read_text(encoding="utf-8"))["analysis_status"] != "complete"):
            metadata["status"] = "partial"
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        db = None
        with (output / "history.sqlite").open("rb") as stream:
            metadata["index_sha256"] = hashlib.file_digest(stream,"sha256").hexdigest()
        write_json(output / "summary.json", metadata)
        export_report(output, metadata)
        return metadata
    finally:
        for stream in (sample_stream, event_stream, result_stream):
            if stream is not None:
                stream.close()
        if db is not None:
            db.close()


def main(arguments=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--relations", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args(arguments)
    if args.export:
        export_report(args.output, json.loads((args.output / "summary.json").read_text(encoding="utf-8")), True)
    else:
        metadata = run_analysis(args.input, args.relations, args.output, args.archive, args.follow)
        print(encode(metadata))


if __name__ == "__main__":
    main()
