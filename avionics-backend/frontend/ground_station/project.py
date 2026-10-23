"""Portable project documents. All stored project resources use relative paths."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4


def read_shared(path):
    # Windows readers must allow an atomic replacement while keeping their snapshot handle.
    if os.name != "nt":
        return Path(path).read_bytes()
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path),0x80000000,7,None,3,0x80,None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    with os.fdopen(msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY),"rb") as stream:
        return stream.read()


def write_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Project:
    def __init__(self, path, document):
        self.path, self.document = Path(path).resolve(), document
        self.root = self.path.parent

    @classmethod
    def create(cls, root, name, configuration, relations):
        identifier = uuid4().hex
        document = {"schema": 1, "id": identifier, "name": name, "created": datetime.now(timezone.utc).isoformat(),
                    "configuration": "", "relations": f"projects/{identifier}/relations.json",
                    "sources": [], "disabled_plugins": [], "runs": [], "layout": {}, "steps": []}
        project = cls(Path(root) / f"{name}_{identifier[:8]}.aerobus.json", document)
        project.set_configuration(configuration)
        write_json(project.resource(document["relations"]), relations)
        project.save()
        return project

    @classmethod
    def open(cls, path):
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(document,dict) or document.get("schema") != 1:
            raise ValueError("不支持的项目格式。")
        project = cls(path, document)
        types = {"id":str,"name":str,"configuration":str,"relations":str,"runs":list,"sources":list,"layout":dict,"steps":list,"disabled_plugins":list}
        if any(not isinstance(document.get(key),kind) for key,kind in types.items()):
            raise ValueError("项目字段格式不正确。")
        if len(document["id"]) != 32 or any(c not in "0123456789abcdef" for c in document["id"]):
            raise ValueError("项目ID格式不正确。")
        if type(document["layout"].get("page",0)) is not int or document["layout"].get("page",0) not in range(6):
            raise ValueError("项目页面编号无效。")
        splitters = document["layout"].get("splitters",[])
        if not isinstance(splitters,list) or any(not isinstance(sizes,list) or any(type(size) is not int or size < 0 for size in sizes) for sizes in splitters):
            raise ValueError("项目面板布局无效。")
        if any(not isinstance(step,str) for step in document["steps"]):
            raise ValueError("试验步骤须为文字。")
        for source in document["sources"]:
            if not isinstance(source,dict) or any(not isinstance(source.get(key),str) for key in ("source","plugin","location","config")):
                raise ValueError("项目采集来源字段格式不正确。")
            location = source["location"]
            plugin = Path(source["plugin"])
            if location not in {"builtin","project","external"} or (location == "builtin" and plugin.name != source["plugin"]) or (location == "external" and not plugin.is_absolute()):
                raise ValueError("项目插件路径类型无效。")
            if location == "project":
                project.resource(source["plugin"])
        if "scenario" in document:
            from .scenarios import validate
            validate(document["scenario"])
        for key in ("configuration", "relations"):
            if not project.resource(document[key]).is_file():
                raise ValueError("项目资源不存在：" + document[key])
        for run in document["runs"]:
            if not isinstance(run,dict) or any(not isinstance(run.get(key),str) for key in ("id","label","archive","analysis","relations","status","notes")):
                raise ValueError("项目采集记录字段格式不正确。")
            for key in ("archive","analysis","relations"):
                project.resource(run[key])
            if run["status"] not in {"已封存","已降级","异常","未封存","待校验"}:
                run["status"] = "待校验"
        return project

    def resource(self, relative):
        if Path(relative).is_absolute():
            raise ValueError("项目内部资源必须使用相对路径。")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("项目资源路径不能越过根目录。")
        return path

    def set_configuration(self, configuration):
        content = Path(configuration).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        relative = f"projects/{self.document['id']}/config/{digest}.ini"
        path = self.resource(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.document["configuration"] = relative

    def new_run(self):
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8]
        base = f"projects/{self.document['id']}/records/{identifier}"
        directory = self.resource(base)
        directory.mkdir(parents=True, exist_ok=False)
        run = {"id": identifier, "label": "采集 " + str(len(self.document["runs"]) + 1),
               "created": datetime.now(timezone.utc).isoformat(), "archive": base + f"/{self.document['name']}_{identifier}.avbus",
               "analysis": base + "/analysis", "relations": base + "/relations.json",
               "status": "准备中", "notes": ""}
        write_json(self.resource(run["relations"]), json.loads(self.resource(self.document["relations"]).read_text(encoding="utf-8")))
        self.document["runs"].append(run)
        self.save()
        return run

    def save_sources(self, source_configs, bin_dir, disabled):
        sources = []
        for source, plugin, config in source_configs.values():
            path = Path(plugin).resolve()
            kind = "builtin" if path.parent == Path(bin_dir).resolve() else "project" if path.is_relative_to(self.root) else "external"
            value = path.name if kind == "builtin" else path.relative_to(self.root).as_posix() if kind == "project" else str(path)
            sources.append({"source": source, "plugin": value, "location": kind, "config": config})
        self.document["sources"] = sources
        self.document["disabled_plugins"] = sorted(disabled)
        self.save()

    def sources(self, bin_dir):
        return [(item["source"], str(Path(bin_dir) / item["plugin"] if item["location"] == "builtin" else
                self.resource(item["plugin"]) if item["location"] == "project" else Path(item["plugin"])), item["config"])
                for item in self.document["sources"]]

    def save(self):
        write_json(self.path, self.document)


def remember_project(registry, path):
    entries = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else []
    text = str(Path(path).resolve())
    write_json(registry, [text] + [item for item in entries if item != text][:11])
