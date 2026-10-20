"""Bounded, event-time evaluation of user-defined parameter relationships."""
import ast
from collections import deque
import math
import operator
from uuid import uuid4


ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
COMPARISON = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
              ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


class Expression:
    def __init__(self, text, names):
        if not text.strip() or len(text) > 512:
            raise ValueError("公式不能为空，长度最多 512 字符。")
        try:
            self.root = ast.parse(text, mode="eval").body
        except SyntaxError as error:
            raise ValueError("公式语法错误：" + error.msg) from error
        if len(list(ast.walk(self.root))) > 128:
            raise ValueError("公式过于复杂，请拆分为更简单的关系。")
        self.names = set()
        self._validate(self.root, set(names))
        if not self.names:
            raise ValueError("公式至少需要使用一个所选参数。")

    def _validate(self, node, names):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float, bool):
            try:
                finite = math.isfinite(node.value)
            except OverflowError as error:
                raise ValueError("公式常数超出数值范围。") from error
            if not finite:
                raise ValueError("常数必须是有限数值。")
        elif isinstance(node, ast.Name):
            if node.id not in names:
                raise ValueError("变量未选择：" + node.id + "。请勾选对应参数，或使用左侧已分配的别名。")
            self.names.add(node.id)
        elif isinstance(node, ast.BinOp) and type(node.op) in ARITHMETIC:
            self._validate(node.left, names)
            self._validate(node.right, names)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
            self._validate(node.operand, names)
        elif isinstance(node, ast.Compare) and all(type(op) in COMPARISON for op in node.ops):
            for child in [node.left] + node.comparators:
                self._validate(child, names)
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            for child in node.values:
                self._validate(child, names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "abs" and len(node.args) == 1 and not node.keywords:
            self._validate(node.args[0], names)
        else:
            raise ValueError("公式仅支持所选变量、数值、+ - * /、比较、and/or/not 和 abs()。")

    def evaluate(self, values):
        result = self._value(self.root, values)
        if not math.isfinite(result):
            raise ArithmeticError("计算结果不是有限数值")
        return result

    def _value(self, node, values):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp):
            return ARITHMETIC[type(node.op)](self._value(node.left, values), self._value(node.right, values))
        if isinstance(node, ast.UnaryOp):
            value = self._value(node.operand, values)
            if isinstance(node.op, ast.Not):
                return not value
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(self._value(child, values) for child in node.values)
            return any(self._value(child, values) for child in node.values)
        if isinstance(node, ast.Call):
            return abs(self._value(node.args[0], values))
        left = self._value(node.left, values)
        for op, child in zip(node.ops, node.comparators):
            right = self._value(child, values)
            if not COMPARISON[type(op)](left, right):
                return False
            left = right
        return True


class Relation:
    def __init__(self, name, formula, bindings, unit, window_ns, identifier=None):
        self.id = identifier or uuid4().hex
        self.name, self.formula, self.unit = name, formula, unit
        self.expression = Expression(formula, [b["alias"] for b in bindings])
        self.bindings = [b for b in bindings if b["alias"] in self.expression.names]
        self.window_ns = window_ns
        self.reset()

    def reset(self):
        self.latest = {}
        self.epochs = {}
        self.history = deque(maxlen=512)
        self.status = "等待输入"
        self.value = self.time_ns = self.origin_ns = None
        self.last_value = self.last_time_ns = None
        self.time_group = ""
        self.evidence = []

    def gap(self, reason):
        if self.status != reason or self.value is not None:
            self.history.append((self.time_ns, None))
        self.value = None
        self.status = reason

    def consume(self, sample):
        key = sample["source"], sample["parameter"]
        epoch = (sample["recording_session_id"], sample["origin_session_id"],
                 sample["generation"], sample["origin_generation"], sample["time_group"])
        if key in self.epochs and self.epochs[key] != epoch:
            self.latest.clear()
            self.history.clear()
            self.origin_ns = self.time_ns = None
            self.last_value = self.last_time_ns = None
            self.gap("来源代次变化，等待新数据")
        self.epochs[key] = epoch
        if not sample["usable"] or not isinstance(sample["value"], (int, float)):
            self.latest.pop(key, None)
            self.gap("输入质量无效：" + (" / ".join(sample["quality"]) or "非数值"))
            return
        self.latest[key] = sample
        keys = [(b["source"], b["parameter"]) for b in self.bindings]
        if any(key not in self.latest for key in keys):
            self.gap("等待所有输入")
            return
        inputs = [self.latest[key] for key in keys]
        if len({s["time_group"] for s in inputs}) != 1:
            self.gap("输入时基不一致")
            return
        times = [s["time_ns"] for s in inputs]
        self.time_ns = max(times)
        self.time_group = inputs[0]["time_group"]
        earliest = min(s["time_ns"] - s["uncertainty_ns"] for s in inputs)
        latest = max(s["time_ns"] + s["uncertainty_ns"] for s in inputs)
        if len(inputs) > 1 and latest - earliest > self.window_ns:
            self.gap("超出时间对齐窗口")
            return
        if any(s["max_age_ns"] and self.time_ns - s["time_ns"] > s["max_age_ns"] for s in inputs):
            self.gap("输入超过有效时间")
            return
        try:
            value = self.expression.evaluate({b["alias"]: s["value"] for b, s in zip(self.bindings, inputs)})
        except ZeroDivisionError:
            self.gap("计算失败：除数为 0")
            return
        except ArithmeticError as error:
            self.gap("计算失败：" + str(error))
            return
        self.value = value
        self.last_value, self.last_time_ns = value, self.time_ns
        self.status = "有效"
        if self.origin_ns is None:
            self.origin_ns = self.time_ns
        self.history.append((self.time_ns, value))
        self.evidence = [{key: sample[key] for key in ("source", "parameter", "value", "unit", "record_index", "generation",
                         "recording_session_id", "configuration_sha256", "time_ns")} for sample in inputs]


class RelationEngine:
    def __init__(self):
        self.relations = []
        self.by_input = {}

    def add(self, name, formula, bindings, unit, window_ns, identifier=None):
        name = name.strip()
        if not name.strip() or len(name) > 64:
            raise ValueError("关系名称须为 1–64 个字符。")
        if any(relation.name == name for relation in self.relations):
            raise ValueError("关系名称已存在，请换一个名称。")
        if not bindings:
            raise ValueError("请先选择输入参数。")
        if not 0 <= window_ns <= 60000000000:
            raise ValueError("时间对齐窗口须为 0–60000 ms。")
        if len(self.relations) >= 32:
            raise ValueError("当前工作区最多保留 32 条关系。")
        relation = Relation(name.strip(), formula, bindings, unit, window_ns, identifier)
        self.relations.append(relation)
        for binding in relation.bindings:
            key = binding["source"], binding["parameter"]
            self.by_input.setdefault(key, []).append(relation)
        return relation

    def remove(self, relation):
        self.relations.remove(relation)
        for binding in relation.bindings:
            key = binding["source"], binding["parameter"]
            self.by_input[key].remove(relation)

    def feed(self, samples, omitted=0):
        if omitted:
            for relation in self.relations:
                relation.latest.clear()
                relation.gap("显示采样存在缺口，等待新输入")
        for sample in samples:
            for relation in self.by_input.get((sample["source"], sample["parameter"]), ()):
                relation.consume(sample)

    def reset(self):
        for relation in self.relations:
            relation.reset()

    def document(self):
        return {"schema": 1, "relations": [{"id": r.id, "name": r.name, "formula": r.formula, "unit": r.unit,
                "bindings": r.bindings, "window_ns": r.window_ns} for r in self.relations]}

    @classmethod
    def from_document(cls, document):
        if document["schema"] != 1:
            raise ValueError("不支持的关系配置版本。")
        engine = cls()
        for item in document["relations"]:
            engine.add(item["name"], item["formula"], item["bindings"], item["unit"], item["window_ns"], item.get("id"))
        return engine
