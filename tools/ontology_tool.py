# -*- coding: utf-8 -*-
"""新能源 Ontology 项目工具（硬门禁阶段）。

用法（项目根执行）:
    python tools/ontology_tool.py validate
    python tools/ontology_tool.py validate --report reviews/validate-report.json
    python tools/ontology_tool.py locators
    python tools/ontology_tool.py assess --cases eval/cases.jsonl
    python tools/ontology_tool.py assess --project eval/project.example.json
    python tools/ontology_tool.py query --case-id O-Q01
    python tools/ontology_tool.py query --all
    python tools/ontology_tool.py export
    python tools/ontology_tool.py constraints list
    python tools/ontology_tool.py constraints check --group eval/constraint_group.example.json --json
    python tools/ontology_tool.py consistency list
    python tools/ontology_tool.py consistency show O-K-001

validate 检查:
    1) sources.jsonl 字段齐全、source_id 唯一、raw sha256 与文件一致；
    2) normalized/*.jsonl 的 locator_id 可解析且唯一，text 非空；
    3) schema.json 自检（字段/谓词/关系/输出字段）；
    4) entities / facts / rules 的 ID 前缀、枚举、谓词白名单、引用解析；
    5) 引用 quote 必须是对应 locator 记录 text 的子串（去空白后），长度 1-40；
    6) 规则不得引用 eval_only / corroborate 来源，至少 1 条 normative；
    7) 规则条件字段必须在该规则 input_fields 中，且是 schema 字段或其他规则结论；
    8) eval/cases.jsonl 结构（case_id 唯一、项目字段在 schema 中、期望输出枚举合法）；
    9) knowledge/consistency.json 的 claim 结构、relation/effect 派生一致、引用可定位、
       rule/fact/relation 引用可解析，且覆盖全部被规则/事实引用的 locator。

assess 用 src/rule_eval.py 按 rules.json 求值，输出 result_status /
conclusions / missing_fields / rule_ids / source_refs / temporal_scope。

纪律: 只读项目文件，不联网，不修改任何数据；发现影响主流程的错误时返回非零。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "model" / "schema.json"
SOURCES_PATH = ROOT / "sources.jsonl"
EXTRA_SOURCES_PATH = ROOT / "sources_ext.jsonl"
RULES_PATH = ROOT / "knowledge" / "rules.json"
RELATIONS_PATH = ROOT / "knowledge" / "relations.jsonl"
CONSTRAINT_SCHEMA_PATH = ROOT / "model" / "constraint_schema.json"
CONSTRAINTS_PATH = ROOT / "knowledge" / "constraints.json"
CONSISTENCY_PATH = ROOT / "knowledge" / "consistency.json"
ENTITIES_PATH = ROOT / "knowledge" / "entities.jsonl"
FACTS_PATH = ROOT / "knowledge" / "facts.jsonl"
NORMALIZED_DIR = ROOT / "normalized"

SOURCE_REQUIRED = [
    "source_id", "title", "canonical_url", "fetched_url", "issuer", "role",
    "raw_path", "raw_sha256", "text_path", "parse_status", "batch",
    "published_at", "issued_at", "retrieved_at",
]
LOCATOR_KIND_RE = re.compile(r"^(?P<sid>[^#]+)#(?P<kind>[a-z]+)=(?P<value>.+)$")
ID_PREFIX = {"entity": "O-E-", "fact": "O-F-", "rule": "O-R-", "constraint": "O-C-"}
ALLOWED_KINDS = {"metadata", "section", "clause", "qa", "page", "link", "item"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.counts: dict[str, int] = {}

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"[{where}] {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"[{where}] {msg}")

    @property
    def ok(self) -> bool:
        return not self.errors


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno} JSON 解析失败: {exc}") from exc
    return rows


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def parse_date(value: str) -> date | None:
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------- schema


def validate_schema(schema: dict, rep: Report) -> None:
    where = "schema"
    for key in ("schema_version", "batch", "fields", "output_fields", "predicates", "classes"):
        if key not in schema:
            rep.error(where, f"缺少顶层键 {key}")
    fields = schema.get("fields", {})
    output_fields = schema.get("output_fields", {})
    if not fields:
        rep.error(where, "fields 为空")
    for name, spec in fields.items():
        _check_field_spec(f"{where}.fields.{name}", name, spec, rep)
    for name, spec in output_fields.items():
        _check_field_spec(f"{where}.output_fields.{name}", name, spec, rep)

    pred_names = set()
    for idx, pred in enumerate(schema.get("predicates", [])):
        w = f"{where}.predicates[{idx}]"
        name = pred.get("name")
        if not name or not re.match(r"^[a-z_]+$", name):
            rep.error(w, f"谓词名非法: {name!r}")
            continue
        if name in pred_names:
            rep.error(w, f"谓词重复: {name}")
        pred_names.add(name)
        args = pred.get("args")
        if not isinstance(args, list) or not args:
            rep.error(w, f"{name} args 必须是非空数组")
        vt = pred.get("value_type")
        if vt not in {"same_as_field", "array_of_field", "number", "date", "none", "field"}:
            rep.error(w, f"{name} value_type 非法: {vt!r}")

    class_ids = set()
    for idx, cls in enumerate(schema.get("classes", [])):
        cid = cls.get("class_id")
        if not cid:
            rep.error(f"{where}.classes[{idx}]", "缺少 class_id")
        elif cid in class_ids:
            rep.error(f"{where}.classes[{idx}]", f"class_id 重复: {cid}")
        class_ids.add(cid)

    rel_names = set()
    for idx, rel in enumerate(schema.get("relations", [])):
        name = rel.get("name")
        if not name:
            rep.error(f"{where}.relations[{idx}]", "缺少 name")
        elif name in rel_names:
            rep.error(f"{where}.relations[{idx}]", f"关系重复: {name}")
        rel_names.add(name)
        for cls in rel.get("subject_classes", []):
            if cls not in class_ids:
                rep.error(f"{where}.relations[{idx}]", f"subject_classes 含未定义类: {cls}")

    edge_names = set()
    for idx, rel in enumerate(schema.get("entity_relations", [])):
        w = f"{where}.entity_relations[{idx}]"
        name = rel.get("name")
        if not name or not re.match(r"^[a-z_]+$", str(name)):
            rep.error(w, f"边关系名非法: {name!r}")
            continue
        if name in edge_names or name in rel_names:
            rep.error(w, f"边关系与已有关系重名: {name}")
        edge_names.add(name)
        for key in ("domain", "range"):
            kinds = rel.get(key)
            if not isinstance(kinds, list) or not kinds:
                rep.error(w, f"{key} 必须是非空数组")
                continue
            for kind in kinds:
                if kind not in {"entity", "fact", "rule", "locator"}:
                    rep.error(w, f"{key} 含非法节点类型: {kind!r}")


def _check_field_spec(where: str, name: str, spec: dict, rep: Report) -> None:
    ftype = spec.get("type")
    if ftype not in {"string", "enum", "number", "date", "boolean", "enum_set"}:
        rep.error(where, f"{name} type 非法: {ftype!r}")
        return
    if ftype in {"enum", "enum_set"}:
        values = spec.get("values")
        if not isinstance(values, list) or not values:
            rep.error(where, f"{name} 缺少非空 values")
        elif len(set(map(str, values))) != len(values):
            rep.error(where, f"{name} values 有重复")
    if ftype == "number" and spec.get("unit") is None:
        rep.warn(where, f"{name} 是数值字段但没有 unit")


# ---------------------------------------------------------------- sources


def load_sources(schema: dict, rep: Report) -> dict[str, dict]:
    paths = [p for p in (SOURCES_PATH, EXTRA_SOURCES_PATH) if p.exists()]
    if not paths:
        rep.error("sources", f"找不到 {SOURCES_PATH.relative_to(ROOT)}")
        return {}
    rows: list[dict] = []
    for path in paths:
        rows.extend(load_jsonl(path))
    sources: dict[str, dict] = {}
    roles = set(schema.get("roles", []))
    levels = set(schema.get("evidence_levels", []))
    allowed_batches = set(schema.get("batches") or [schema.get("batch")])
    for idx, row in enumerate(rows, 1):
        w = f"sources[{idx}]"
        missing = [k for k in SOURCE_REQUIRED if k not in row]
        if missing:
            rep.error(w, f"缺少字段: {', '.join(missing)}")
            continue
        sid = row["source_id"]
        w = f"sources:{sid}"
        if sid in sources:
            rep.error(w, "source_id 重复")
            continue
        sources[sid] = row
        if row["batch"] not in allowed_batches:
            rep.error(w, f"batch 不一致: {row['batch']!r}")
        if row["role"] not in roles:
            rep.error(w, f"role 非法: {row['role']!r}")
        level = row.get("evidence_level")
        if row["role"] == "build":
            if level not in levels:
                rep.error(w, f"build 来源 evidence_level 非法: {level!r}")
        elif level is not None:
            rep.error(w, f"{row['role']} 来源不得有 evidence_level")
        if row["parse_status"] != "parsed":
            rep.error(w, f"parse_status 不是 parsed: {row['parse_status']!r}")
        for key in ("published_at", "issued_at"):
            val = row.get(key)
            if val is not None and parse_date(val) is None:
                rep.error(w, f"{key} 不是合法 ISO 日期: {val!r}")
        try:
            datetime.fromisoformat(str(row["retrieved_at"]))
        except ValueError:
            rep.error(w, f"retrieved_at 不是合法 ISO 时间: {row['retrieved_at']!r}")
        raw = ROOT / row["raw_path"]
        if not raw.is_file():
            rep.error(w, f"raw 文件不存在: {row['raw_path']}")
        else:
            actual = sha256_file(raw)
            if actual != row["raw_sha256"]:
                rep.error(w, f"raw_sha256 不匹配: 记录 {row['raw_sha256']} 实际 {actual}")
        if not (ROOT / row["text_path"]).is_file():
            rep.error(w, f"text_path 不存在: {row['text_path']}")
        kinds = row.get("locator_kinds") or []
        bad = [k for k in kinds if k not in ALLOWED_KINDS]
        if bad:
            rep.error(w, f"locator_kinds 含未知类型: {bad}")
        corr = row.get("corroborates")
        if corr and corr not in sources and corr not in {r["source_id"] for r in rows}:
            rep.error(w, f"corroborates 指向不存在的来源: {corr}")
    rep.counts["sources"] = len(sources)
    return sources


# ---------------------------------------------------------------- locators


def load_locators(sources: dict[str, dict], rep: Report) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for sid, src in sources.items():
        path = ROOT / src["text_path"]
        if not path.is_file():
            continue
        for lineno, rec in enumerate(load_jsonl(path), 1):
            w = f"{sid}:line{lineno}"
            for key in ("locator_id", "kind", "value", "text"):
                if key not in rec:
                    rep.error(w, f"缺少字段 {key}")
                    break
            else:
                loc = rec["locator_id"]
                m = LOCATOR_KIND_RE.match(loc)
                if not m:
                    rep.error(w, f"locator_id 格式非法: {loc!r}")
                    continue
                if m.group("sid") != sid:
                    rep.error(w, f"locator_id 的 source_id 与文件不符: {loc!r}")
                if m.group("kind") != rec["kind"] or m.group("value") != rec["value"]:
                    rep.error(w, f"locator_id 与 kind/value 不一致: {loc!r}")
                if rec["kind"] not in (src.get("locator_kinds") or []):
                    rep.error(w, f"kind={rec['kind']} 未在 sources.locator_kinds 登记")
                if loc in index:
                    rep.error(w, f"locator_id 重复: {loc}")
                    continue
                if not compact(rec["text"]):
                    rep.error(w, f"text 为空: {loc}")
                index[loc] = {**rec, "source_id": sid}
    rep.counts["locators"] = len(index)
    return index


# ---------------------------------------------------------------- citations


def validate_refs(
    refs: list[dict],
    owner: str,
    sources: dict[str, dict],
    locators: dict[str, dict],
    schema: dict,
    rep: Report,
    require_normative: bool,
) -> int:
    """校验引用；返回 normative 引用数。"""
    normative = 0
    if not isinstance(refs, list) or not refs:
        rep.error(owner, "source_refs 不能为空")
        return 0
    seen = set()
    for i, ref in enumerate(refs, 1):
        w = f"{owner}.refs[{i}]"
        sid = ref.get("source_id")
        loc = ref.get("locator_id")
        quote = ref.get("quote")
        level = ref.get("evidence_level")
        if not sid or not loc:
            rep.error(w, "缺少 source_id 或 locator_id")
            continue
        src = sources.get(sid)
        if src is None:
            rep.error(w, f"source_id 不存在: {sid}")
            continue
        if src["role"] != "build":
            rep.error(w, f"不得引用 {src['role']} 来源: {sid}")
        if level != src.get("evidence_level"):
            rep.error(w, f"evidence_level 与来源不符: {level!r} != {src.get('evidence_level')!r}")
        if level not in schema.get("evidence_levels", []):
            rep.error(w, f"evidence_level 非法: {level!r}")
        if level == "normative":
            normative += 1
        m = LOCATOR_KIND_RE.match(loc)
        if not m:
            rep.error(w, f"locator_id 格式非法: {loc!r}")
            continue
        if m.group("sid") != sid:
            rep.error(w, f"locator_id 的 source_id 与 source_id 字段不符: {loc!r}")
            continue
        record = locators.get(loc)
        if record is None:
            rep.error(w, f"locator 在 normalized 中不存在: {loc}")
            continue
        if ref.get("kind") != record["kind"] or ref.get("value") != record["value"]:
            rep.error(w, f"kind/value 与 locator 记录不符: {loc}")
        if not isinstance(quote, str) or not quote.strip():
            rep.error(w, "quote 为空")
            continue
        qlen = len(quote)
        if qlen > 40:
            rep.error(w, f"quote 超过 40 字（{qlen}）: {quote}")
        if qlen < 8:
            rep.warn(w, f"quote 偏短（{qlen} 字），可复核是否足以定位: {quote}")
        if compact(quote) not in compact(record["text"]):
            rep.error(w, f"quote 在 {loc} 的文本中找不到: {quote}")
        pair = (loc, quote)
        if pair in seen:
            rep.error(w, "同一 owner 内引用重复（locator + quote）")
        seen.add(pair)
    if require_normative and normative == 0:
        rep.error(owner, "至少需要 1 条 normative 引用")
    return normative


# ---------------------------------------------------------------- entities / facts


def validate_entities(schema: dict, sources: dict, locators: dict, rep: Report) -> None:
    if not ENTITIES_PATH.exists():
        rep.warn("entities", "knowledge/entities.jsonl 不存在，跳过")
        return
    rows = load_jsonl(ENTITIES_PATH)
    classes = {c["class_id"] for c in schema.get("classes", [])}
    seen = set()
    for idx, row in enumerate(rows, 1):
        w = f"entities[{idx}]"
        eid = row.get("entity_id", "")
        if not eid.startswith(ID_PREFIX["entity"]):
            rep.error(w, f"entity_id 前缀必须是 {ID_PREFIX['entity']}: {eid!r}")
        if eid in seen:
            rep.error(w, f"entity_id 重复: {eid}")
        seen.add(eid)
        if row.get("class") not in classes:
            rep.error(w, f"class 未在 schema.classes 定义: {row.get('class')!r}")
        if not row.get("label"):
            rep.error(w, "缺少 label")
        validate_refs(row.get("source_refs", []), f"entities:{eid}", sources, locators, schema, rep, False)
        _check_temporal_scope(row.get("temporal_scope"), f"entities:{eid}", rep)
    rep.counts["entities"] = len(rows)


def validate_facts(schema: dict, sources: dict, locators: dict, rep: Report) -> None:
    if not FACTS_PATH.exists():
        rep.warn("facts", "knowledge/facts.jsonl 不存在，跳过")
        return
    rows = load_jsonl(FACTS_PATH)
    relations = {r["name"] for r in schema.get("relations", [])}
    seen = set()
    for idx, row in enumerate(rows, 1):
        w = f"facts[{idx}]"
        fid = row.get("fact_id", "")
        if not fid.startswith(ID_PREFIX["fact"]):
            rep.error(w, f"fact_id 前缀必须是 {ID_PREFIX['fact']}: {fid!r}")
        if fid in seen:
            rep.error(w, f"fact_id 重复: {fid}")
        seen.add(fid)
        if row.get("relation") not in relations:
            rep.error(w, f"relation 未在 schema.relations 定义: {row.get('relation')!r}")
        if row.get("claim_status") not in {"supported", "conflict", "unknown"}:
            rep.error(w, f"claim_status 非法: {row.get('claim_status')!r}")
        if not row.get("statement"):
            rep.error(w, "缺少 statement")
        validate_refs(row.get("source_refs", []), f"facts:{fid}", sources, locators, schema, rep, False)
        _check_temporal_scope(row.get("temporal_scope"), f"facts:{fid}", rep)
    rep.counts["facts"] = len(rows)


# ---------------------------------------------------------------- rules


def _condition_value_ok(field_spec: dict, value) -> bool:
    ftype = field_spec["type"]
    if ftype == "enum":
        return isinstance(value, str) and value in field_spec["values"]
    if ftype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ftype == "date":
        return parse_date(value) is not None
    if ftype == "boolean":
        return isinstance(value, bool)
    if ftype == "string":
        return isinstance(value, str)
    return False


def _check_condition(
    cond: dict,
    owner: str,
    rule: dict,
    schema: dict,
    produced: set[str],
    rep: Report,
) -> None:
    preds = {p["name"]: p for p in schema.get("predicates", [])}
    name = cond.get("predicate")
    field = cond.get("field")
    w = f"{owner}.condition"
    if name not in preds:
        rep.error(w, f"谓词不在白名单: {name!r}")
        return
    if field not in rule.get("input_fields", []):
        rep.error(w, f"条件字段未在 input_fields 声明: {field!r}")
    spec = schema.get("fields", {}).get(field) or schema.get("output_fields", {}).get(field)
    if spec is None:
        rep.error(w, f"字段未在 schema 定义: {field!r}")
        return
    if field not in schema.get("fields", {}) and field not in produced:
        rep.error(w, f"字段 {field!r} 既不是输入字段，也没有规则能产出")
    pred = preds[name]
    if pred["value_type"] == "same_as_field":
        if "value" not in cond:
            rep.error(w, f"{name} 缺少 value")
        elif not _condition_value_ok(spec, cond["value"]):
            rep.error(w, f"value 类型/枚举与字段 {field} 不符: {cond['value']!r}")
    elif pred["value_type"] == "array_of_field":
        val = cond.get("value")
        if not isinstance(val, list) or not val:
            rep.error(w, f"{name} value 必须是非空数组")
        else:
            bad = [v for v in val if not _condition_value_ok(spec, v)]
            if bad:
                rep.error(w, f"{name} 数组元素与字段 {field} 不符: {bad!r}")
    elif pred["value_type"] == "number":
        if spec["type"] != "number":
            rep.error(w, f"{name} 只能用于 number 字段，当前 {field} 是 {spec['type']}")
        if not _condition_value_ok(spec, cond.get("value")):
            rep.error(w, f"{name} value 必须是数字: {cond.get('value')!r}")
    elif pred["value_type"] == "date":
        if spec["type"] != "date":
            rep.error(w, f"{name} 只能用于 date 字段，当前 {field} 是 {spec['type']}")
        if parse_date(cond.get("value")) is None:
            rep.error(w, f"{name} value 必须是 ISO 日期: {cond.get('value')!r}")
    elif pred["value_type"] == "field":
        other = cond.get("value_field")
        if not isinstance(other, str) or not other.strip():
            rep.error(w, f"{name} value_field 必须是非空字符串")
            return
        if other == field:
            rep.error(w, f"{name} 不能把字段与自身比较: {field!r}")
            return
        if other not in rule.get("input_fields", []):
            rep.error(w, f"{name} 比较字段未在 input_fields 声明: {other!r}")
        other_spec = schema.get("fields", {}).get(other) or schema.get("output_fields", {}).get(other)
        if other_spec is None:
            rep.error(w, f"{name} 比较字段未在 schema 定义: {other!r}")
        else:
            if spec["type"] != "number":
                rep.error(w, f"{name} 只能用于 number 字段，当前 {field} 是 {spec['type']}")
            if other_spec["type"] != "number":
                rep.error(w, f"{name} 比较字段必须是 number，当前 {other} 是 {other_spec['type']}")
        if "value" in cond:
            rep.error(w, f"{name} 不接受 value，请用 value_field")
    elif pred["value_type"] == "none":
        if "value" in cond:
            rep.error(w, f"{name} 不接受 value")
        if pred.get("field_type") and spec["type"] != pred["field_type"]:
            rep.error(w, f"{name} 只能用于 {pred['field_type']} 字段，当前 {field} 是 {spec['type']}")


def _check_conclusion(concl: dict, owner: str, schema: dict, rep: Report, allow_empty_set: bool) -> None:
    field = concl.get("field")
    spec = schema.get("output_fields", {}).get(field)
    if spec is None:
        rep.error(f"{owner}.conclusion", f"结论字段未在 schema.output_fields 定义: {field!r}")
        return
    value = concl.get("value")
    ftype = spec["type"]
    if ftype == "enum":
        if not isinstance(value, str) or value not in spec["values"]:
            rep.error(f"{owner}.conclusion", f"{field} 取值非法: {value!r}")
    elif ftype == "enum_set":
        if not isinstance(value, list) or any(v not in spec["values"] for v in value):
            rep.error(f"{owner}.conclusion", f"{field} 必须是合法值数组: {value!r}")
        elif len(set(value)) != len(value):
            rep.error(f"{owner}.conclusion", f"{field} 数组有重复值")
        elif not value and not allow_empty_set:
            rep.warn(f"{owner}.conclusion", f"{field} 为空数组，确认是否符合预期")
    elif ftype == "boolean":
        if not isinstance(value, bool):
            rep.error(f"{owner}.conclusion", f"{field} 必须是布尔值: {value!r}")
    elif ftype == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            rep.error(f"{owner}.conclusion", f"{field} 必须是数字: {value!r}")
    elif ftype == "date":
        if parse_date(value) is None:
            rep.error(f"{owner}.conclusion", f"{field} 必须是 ISO 日期: {value!r}")


def _check_temporal_scope(scope, owner: str, rep: Report) -> None:
    if scope is None:
        rep.error(owner, "缺少 temporal_scope")
        return
    if not isinstance(scope, dict):
        rep.error(owner, "temporal_scope 必须是对象")
        return
    for key, val in scope.items():
        if key.endswith(("_at", "_from", "_to", "_date", "_cutoff")) and val is not None:
            if parse_date(str(val)) is None:
                rep.error(owner, f"temporal_scope.{key} 不是 ISO 日期: {val!r}")
    start_raw = scope.get("effective_from")
    if not start_raw:
        rep.error(owner, "temporal_scope.effective_from 必填")
        return
    start = parse_date(str(start_raw))
    end = parse_date(str(scope.get("effective_to"))) if scope.get("effective_to") else None
    if start is not None and end is not None and start > end:
        rep.error(owner, f"temporal_scope 起止倒置: {start_raw} > {scope.get('effective_to')}")


def validate_rules(schema: dict, sources: dict, locators: dict, rep: Report) -> None:
    if not RULES_PATH.exists():
        rep.error("rules", f"找不到 {RULES_PATH.relative_to(ROOT)}")
        return
    doc = load_json(RULES_PATH)
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        rep.error("rules", "规则集为空")
        return
    produced: set[str] = set()
    for rule in rules:
        for case in rule.get("cases", []):
            for concl in case.get("then", {}).get("conclusions", []):
                produced.add(concl.get("field", ""))
    seen = set()
    total_refs = 0
    normative_refs = 0
    for idx, rule in enumerate(rules, 1):
        rid = rule.get("rule_id", "")
        w = f"rules[{idx}]"
        if not rid.startswith(ID_PREFIX["rule"]):
            rep.error(w, f"rule_id 前缀必须是 {ID_PREFIX['rule']}: {rid!r}")
        if rid in seen:
            rep.error(w, f"rule_id 重复: {rid}")
        seen.add(rid)
        w = f"rule:{rid}"
        for key in ("title", "input_fields", "cases", "source_refs", "temporal_scope"):
            if key not in rule:
                rep.error(w, f"缺少字段 {key}")
        if rule.get("knowledge_kind") != "derived":
            rep.error(w, f"knowledge_kind 必须是 derived: {rule.get('knowledge_kind')!r}")
        if rule.get("rule_status") not in {"draft", "reviewed", "active", "deprecated"}:
            rep.error(w, f"rule_status 非法: {rule.get('rule_status')!r}")
        inputs = rule.get("input_fields", [])
        if not isinstance(inputs, list) or not inputs:
            rep.error(w, "input_fields 必须是非空数组")
            continue
        for field in inputs:
            if field not in schema.get("fields", {}) and field not in produced:
                rep.error(w, f"input_fields 含未定义字段: {field!r}")
            if field not in schema.get("fields", {}) and field not in produced:
                continue
            if field in produced and field in schema.get("fields", {}):
                rep.warn(w, f"input_fields 中的 {field!r} 既是输入字段又被规则产出，注意覆盖关系")

        cases = rule.get("cases", [])
        if not isinstance(cases, list) or not cases:
            rep.error(w, "cases 必须是非空数组")
        case_ids = set()
        for ci, case in enumerate(cases, 1):
            cw = f"{w}.case[{ci}]"
            cid = case.get("case_id")
            if not cid:
                rep.error(cw, "缺少 case_id")
            elif cid in case_ids:
                rep.error(cw, f"case_id 重复: {cid}")
            case_ids.add(cid)
            if "blocks_on_unknown" in case and case["blocks_on_unknown"] is not True:
                rep.error(cw, "blocks_on_unknown 只支持 true（缺省即关闭）")
            when = case.get("when", {})
            if when.get("logic") not in {"all", "any"}:
                rep.error(cw, f"when.logic 非法: {when.get('logic')!r}")
            conds = when.get("conditions", [])
            if not isinstance(conds, list) or not conds:
                rep.error(cw, "conditions 必须是非空数组")
            for cond in conds:
                _check_condition(cond, cw, rule, schema, produced, rep)
            then = case.get("then", {})
            concls = then.get("conclusions", [])
            if not isinstance(concls, list) or not concls:
                rep.error(cw, "conclusions 必须是非空数组")
            fields = [c.get("field") for c in concls]
            if len(set(fields)) != len(fields):
                rep.error(cw, "同一结论内字段重复")
            for concl in concls:
                _check_conclusion(concl, cw, schema, rep, allow_empty_set=False)

        guards = rule.get("guards", [])
        if not isinstance(guards, list):
            rep.error(w, "guards 必须是数组")
            guards = []
        guard_ids = set()
        for gi, guard in enumerate(guards, 1):
            gw = f"{w}.guard[{gi}]"
            if not isinstance(guard, dict):
                rep.error(gw, "guard 必须是对象")
                continue
            gid = guard.get("guard_id")
            if not isinstance(gid, str) or not gid.strip():
                rep.error(gw, "guard_id 必须是非空字符串")
            elif gid in guard_ids:
                rep.error(gw, f"guard_id 重复: {gid}")
            else:
                guard_ids.add(gid)
            when = guard.get("when", {})
            if not isinstance(when, dict) or when.get("logic") not in {"all", "any"}:
                rep.error(gw, f"when.logic 非法: {when.get('logic') if isinstance(when, dict) else when!r}")
            else:
                conds = when.get("conditions", [])
                if not isinstance(conds, list) or not conds:
                    rep.error(gw, "conditions 必须是非空数组")
                for cond in conds:
                    _check_condition(cond, gw, rule, schema, produced, rep)
            if guard.get("status") != "insufficient":
                rep.error(gw, f"guard.status 只支持 insufficient: {guard.get('status')!r}")
            missing_fields = guard.get("missing_fields", [])
            if not isinstance(missing_fields, list) or not missing_fields:
                rep.error(gw, "missing_fields 必须是非空数组")
            else:
                for field in missing_fields:
                    if not isinstance(field, str) or not field.strip():
                        rep.error(gw, "missing_fields 只能包含非空字符串")
                    elif field not in inputs:
                        rep.error(gw, f"guard.missing_fields 未在 input_fields 声明: {field!r}")
                    elif field not in schema.get("fields", {}):
                        rep.error(gw, f"guard.missing_fields 必须是基础输入字段: {field!r}")
            reason = guard.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                rep.error(gw, "reason 必须是非空字符串")
            if "label" in guard and (not isinstance(guard["label"], str) or not guard["label"].strip()):
                rep.error(gw, "label 必须是非空字符串")

        default = rule.get("default")
        if not isinstance(default, dict):
            rep.error(w, "缺少 default（缺数据/不匹配时的出口）")
        else:
            if default.get("status") not in schema.get("rule_default_status", []):
                rep.error(w, f"default.status 非法: {default.get('status')!r}")
            for concl in default.get("conclusions", []):
                _check_conclusion(concl, f"{w}.default", schema, rep, allow_empty_set=True)

        total_refs += len(rule.get("source_refs", []) or [])
        normative_refs += validate_refs(
            rule.get("source_refs", []), w, sources, locators, schema, rep, require_normative=True
        )
        _check_temporal_scope(rule.get("temporal_scope"), w, rep)
        for key in ("exceptions", "notes", "open_items"):
            val = rule.get(key, [])
            if not isinstance(val, list) or any(not isinstance(x, str) or not x.strip() for x in val):
                rep.error(w, f"{key} 必须是非空字符串数组（可为空数组）")
    rep.counts["rules"] = len(rules)
    rep.counts["refs"] = total_refs
    rep.counts["normative_refs"] = normative_refs


def _node_kind(node_id: str) -> str | None:
    if not isinstance(node_id, str) or not node_id:
        return None
    if node_id.startswith(ID_PREFIX["entity"]):
        return "entity"
    if node_id.startswith(ID_PREFIX["fact"]):
        return "fact"
    if node_id.startswith(ID_PREFIX["rule"]):
        return "rule"
    if "#" in node_id:
        return "locator"
    return None


def _load_entities_by_id() -> dict[str, dict]:
    if not ENTITIES_PATH.exists():
        return {}
    return {row["entity_id"]: row for row in load_jsonl(ENTITIES_PATH)}


def _load_facts_by_id() -> dict[str, dict]:
    if not FACTS_PATH.exists():
        return {}
    return {row["fact_id"]: row for row in load_jsonl(FACTS_PATH)}


def _load_rules_by_id() -> dict[str, dict]:
    if not RULES_PATH.exists():
        return {}
    return {row["rule_id"]: row for row in load_json(RULES_PATH).get("rules", [])}


def validate_relations(
    schema: dict,
    sources: dict,
    locators: dict,
    rep: Report,
) -> None:
    edge_defs = {e["name"]: e for e in schema.get("entity_relations", [])}
    if not RELATIONS_PATH.exists():
        rep.warn("relations", "knowledge/relations.jsonl 不存在，跳过")
        return
    rows = load_jsonl(RELATIONS_PATH)
    entities = _load_entities_by_id()
    facts = _load_facts_by_id()
    rules = _load_rules_by_id()
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    total_refs = 0
    for idx, row in enumerate(rows, 1):
        w = f"relations[{idx}]"
        rid = row.get("relation_id", "")
        if not rid.startswith("O-REL-"):
            rep.error(w, f"relation_id 前缀必须是 O-REL-: {rid!r}")
        if rid in seen_ids:
            rep.error(w, f"relation_id 重复: {rid}")
        seen_ids.add(rid)
        name = row.get("relation")
        spec = edge_defs.get(name)
        if spec is None:
            rep.error(w, f"relation 未在 schema.entity_relations 定义: {name!r}")
            continue
        src = row.get("from")
        dst = row.get("to")
        if not src or not dst:
            rep.error(w, "缺少 from/to")
            continue
        ksrc = _node_kind(src)
        kdst = _node_kind(dst)
        if ksrc not in spec.get("domain", []):
            rep.error(w, f"from 节点类型不符: {src!r} -> {ksrc!r}，要求 {spec.get('domain')}")
        if kdst not in spec.get("range", []):
            rep.error(w, f"to 节点类型不符: {dst!r} -> {kdst!r}，要求 {spec.get('range')}")
        if src == dst:
            rep.error(w, f"自指边: {src}")
        for node, kind in ((src, ksrc), (dst, kdst)):
            if kind == "entity" and node not in entities:
                rep.error(w, f"entity 不存在: {node}")
            elif kind == "fact" and node not in facts:
                rep.error(w, f"fact 不存在: {node}")
            elif kind == "rule" and node not in rules:
                rep.error(w, f"rule 不存在: {node}")
            elif kind == "locator" and node not in locators:
                rep.error(w, f"locator 不存在: {node}")
        key = (str(name), src, dst)
        if key in seen_edges:
            rep.error(w, f"重复边: {src} -{name}-> {dst}")
        seen_edges.add(key)
        note = row.get("note")
        if not isinstance(note, str) or not note.strip():
            rep.error(w, "note 必须是非空字符串（需说明该边凭什么成立）")
        refs = row.get("source_refs", [])
        norm = validate_refs(
            refs, f"relations:{rid}",
            sources, locators, schema, rep, True,
        )
        total_refs += norm
        if isinstance(refs, list):
            if len(refs) < 2:
                rep.error(w, f"关系边至少需要 2 条引用，实际 {len(refs)} 条")
            if name != "constrains" and norm != len(refs):
                rep.error(w, f"{name} 边引用必须全部为 normative，实际 {norm}/{len(refs)} 条")
            if name == "constrains" and norm == 0:
                rep.error(w, "constrains 边至少需要 1 条 normative 引用")
        _check_temporal_scope(row.get("temporal_scope"), f"relations:{rid}", rep)
    cycle = _find_subclass_cycle(rows)
    if cycle:
        rep.error("relations", "subclass_of 存在环: " + " -> ".join(cycle))
    constrained = {
        row.get("from") for row in rows
        if row.get("relation") == "constrains" and row.get("from") in facts and row.get("to") in rules
    }
    for fid in sorted(set(facts) - constrained):
        rep.error("relations", f"fact 没有 constrains 出边，事实影响面会静默为空: {fid}")
    rep.counts["relations"] = len(rows)
    rep.counts["relation_refs"] = total_refs


def _find_subclass_cycle(rows: list[dict]) -> list[str] | None:
    """返回一条 subclass_of 环（含首尾重复节点）；无环返回 None。"""
    graph: dict[str, list[str]] = {}
    for row in rows:
        if row.get("relation") == "subclass_of" and row.get("from") and row.get("to"):
            graph.setdefault(row["from"], []).append(row["to"])
    done: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in done:
            return None
        if node in on_stack:
            return stack[stack.index(node):] + [node]
        on_stack.add(node)
        stack.append(node)
        for nxt in graph.get(node, []):
            found = visit(nxt)
            if found:
                return found
        stack.pop()
        on_stack.remove(node)
        done.add(node)
        return None

    for node in graph:
        found = visit(node)
        if found:
            return found
    return None


def _rules_citing(locator_id: str, rules: dict[str, dict]) -> set[str]:
    hit: set[str] = set()
    for rid, rule in rules.items():
        for ref in rule.get("source_refs", []):
            if ref.get("locator_id") == locator_id:
                hit.add(rid)
                break
    return hit


def _direct_rule_ids(
    node: str,
    kind: str,
    edges: list[dict],
    entities: dict[str, dict],
    facts: dict[str, dict],
    rules: dict[str, dict],
) -> set[str]:
    direct: set[str] = set()
    if kind == "locator":
        direct |= _rules_citing(node, rules)
    elif kind == "entity":
        # 实体定义改动：引用其定义条款的规则需复核（条款级影响）。
        for ref in entities.get(node, {}).get("source_refs", []):
            direct |= _rules_citing(ref.get("locator_id", ""), rules)
    elif kind == "fact":
        # 事实内容改动只沿 constrains 边传播。共享引用不等于依赖该事实，
        # 条款级影响走 locator 查询，避免把"引用同一原文"误报为受影响。
        pass
    elif kind == "rule":
        direct.add(node)
    for row in edges:
        if row.get("relation") == "constrains" and row.get("from") == node:
            direct.add(row.get("to"))
    return direct


RELATIONS_API_VERSION = "relations/v1"


def _subclass_ancestors(node: str, edges: list[dict]) -> list[str]:
    """沿 subclass_of 向上取全部祖先；不假设只有一层。"""
    ancestors: list[str] = []
    seen = {node}
    frontier = [node]
    while frontier:
        current = frontier.pop(0)
        for row in edges:
            if row.get("relation") != "subclass_of" or row.get("from") != current:
                continue
            parent = row.get("to")
            if parent and parent not in seen:
                seen.add(parent)
                ancestors.append(parent)
                frontier.append(parent)
    return ancestors


def cmd_relations(args: argparse.Namespace) -> int:
    if not RELATIONS_PATH.exists():
        print(f"FAIL: 找不到 {RELATIONS_PATH.relative_to(ROOT)}")
        return 1
    schema = load_json(SCHEMA_PATH)
    rep = Report()
    sources = load_sources(schema, rep)
    locators = load_locators(sources, rep)
    edges = load_jsonl(RELATIONS_PATH)
    entities = _load_entities_by_id()
    facts = _load_facts_by_id()
    rules = _load_rules_by_id()
    action = args.action
    if action == "list":
        for row in edges:
            print(f"{row['relation_id']:10s} {row['relation']:12s} {row['from']} -> {row['to']}")
        print(f"共 {len(edges)} 条关系边")
        return 0
    node = args.node
    if not node:
        print("FAIL: show/impact/ancestors 需要节点 ID")
        return 1
    if action == "ancestors":
        kind = _node_kind(node)
        if kind != "entity":
            print(f"FAIL: ancestors 只支持 entity 节点，收到 {kind or '未知类型'}: {node}")
            return 1
        if node not in entities:
            print(f"节点 {node} 不存在")
            return 1
        ancestors = _subclass_ancestors(node, edges)
        if args.json:
            print(json.dumps({
                "schema_version": RELATIONS_API_VERSION,
                "node": node,
                "kind": "entity",
                "ancestors": ancestors,
            }, ensure_ascii=False, indent=2))
            return 0
        print(f"node: {node}  (entity)")
        if ancestors:
            print("== 分类上级（沿 subclass_of 向上） ==")
            for aid in ancestors:
                print(f"  {aid}  {entities[aid]['label']}")
        else:
            print("  （没有分类上级）")
        return 0
    if action == "show":
        by_id = next((r for r in edges if r["relation_id"] == node), None)
        touched = [by_id] if by_id else [r for r in edges if r["from"] == node or r["to"] == node]
        if not touched:
            kind = _node_kind(node)
            known = {
                "entity": node in entities,
                "fact": node in facts,
                "rule": node in rules,
                "locator": node in locators,
            }.get(kind, False)
            if known:
                print(f"节点 {node} 存在，但没有关系边")
                return 0
            print(f"节点或边 {node} 不存在")
            return 1
        for row in touched:
            arrow = "边" if by_id else ("出" if row["from"] == node else "入")
            print(f"{row['relation_id']} [{arrow}] {row['relation']}: {row['from']} -> {row['to']}")
            print(f"    note: {row.get('note', '')}")
            for ref in row.get("source_refs", []):
                print(f"    {ref['locator_id']} | {ref['quote']}")
        return 0
    if action == "impact":
        kind = _node_kind(node)
        if kind is None:
            print(f"FAIL: 无法识别节点类型: {node}")
            return 1
        direct = _direct_rule_ids(node, kind, edges, entities, facts, rules)
        children = sorted({row["from"] for row in edges if row["relation"] == "refines" and row["to"] == node})
        parents = sorted({row["to"] for row in edges if row["relation"] == "refines" and row["from"] == node})
        # 关联规则只是“同一上级/下级条款被引用”的候选清单，不能据此判定规则受影响。
        # 典型反例：浙江第四条是比例条款，O-R-002 是上网模式规则，两者共引国家第五条，
        # 但改比例不会改变上网模式集合。候选必须人工判断，不能混进“直接受影响”。
        related: set[str] = set()
        if kind == "locator":
            for parent in parents:
                related |= _rules_citing(parent, rules)
            for child in children:
                related |= _rules_citing(child, rules)
        related -= direct
        if args.json:
            print(json.dumps({
                "schema_version": RELATIONS_API_VERSION,
                "node": node, "kind": kind,
                "direct": sorted(direct),
                "related_rules": sorted(related),
                "parent_clauses": parents,
                "child_clauses": children,
            }, ensure_ascii=False, indent=2))
            return 0
        print(f"node: {node}  ({kind})")
        print("== 直接受影响规则 ==")
        if direct:
            for rid in sorted(direct):
                print(f"  {rid}  {rules[rid]['title']}")
        else:
            print("  （无）")
        print("== 关联规则（候选，需人工判断；不代表受影响） ==")
        if related:
            for rid in sorted(related):
                print(f"  {rid}  {rules[rid]['title']}")
        else:
            print("  （无）")
        if parents:
            print("== 上级条款（本条款细化它，上级改动时需复核） ==")
            for c in parents:
                print(f"  {c}")
        if children:
            print("== 下级细化条款（上级改动时需复核，不自动算规则受影响） ==")
            for c in children:
                print(f"  {c}")
        return 0
    print(f"FAIL: 未知 action {action}")
    return 1


def validate_constraints(schema: dict, sources: dict, locators: dict, rep: Report) -> None:
    """多项目约束的结构与引用校验（constraints/v1）。"""
    if not CONSTRAINT_SCHEMA_PATH.exists():
        rep.error("constraints", "找不到 model/constraint_schema.json")
        return
    cschema = load_json(CONSTRAINT_SCHEMA_PATH)
    for key in ("schema_version", "group_required_fields", "group_optional_fields",
                "group_fields", "member_extension_fields", "max_members", "statuses",
                "decisions", "constraint_types"):
        if key not in cschema:
            rep.error("constraint_schema", f"缺少顶层键 {key}")
    if cschema.get("schema_version") != "constraint-schema/v1":
        rep.error("constraint_schema", f"schema_version 非法: {cschema.get('schema_version')!r}")
    ext = cschema.get("member_extension_fields", {})
    group_fields = cschema.get("group_fields", {})
    max_members = cschema.get("max_members", 20)
    if not isinstance(max_members, int) or isinstance(max_members, bool) or max_members < 1:
        rep.error("constraint_schema", "max_members 必须是正整数")
    for name, spec in group_fields.items():
        if not isinstance(spec, dict) or "type" not in spec:
            rep.error(f"constraint_schema.group_fields.{name}", "必须是含 type 的对象")
        if spec.get("type") not in {"string", "date", "array", "object"}:
            rep.error(f"constraint_schema.group_fields.{name}", f"type 非法: {spec.get('type')!r}")
    for name, spec in ext.items():
        if not isinstance(spec, dict) or "type" not in spec:
            rep.error(f"constraint_schema.member_extension_fields.{name}", "必须是含 type 的对象")
            continue
        if spec.get("type") not in {"string", "boolean", "date", "number"}:
            rep.error(f"constraint_schema.member_extension_fields.{name}", f"type 非法: {spec.get('type')!r}")

    if not CONSTRAINTS_PATH.exists():
        rep.error("constraints", f"找不到 {CONSTRAINTS_PATH.relative_to(ROOT)}")
        return
    doc = load_json(CONSTRAINTS_PATH)
    if doc.get("constraint_set_version") != "constraints/v1":
        rep.error("constraints", f"constraint_set_version 非法: {doc.get('constraint_set_version')!r}")
    rows = doc.get("constraints")
    if not isinstance(rows, list) or not rows:
        rep.error("constraints", "constraints 必须是非空数组")
        return
    project_classes = set(
        schema.get("output_fields", {}).get("project_class", {}).get("values", [])
    )
    type_specs = cschema.get("constraint_types", {})
    allowed_group = set(cschema.get("group_required_fields", [])) | set(
        cschema.get("group_optional_fields", [])
    )
    seen: set[str] = set()
    total_refs = 0
    normative_refs = 0
    for idx, row in enumerate(rows, 1):
        w = f"constraints[{idx}]"
        cid = row.get("constraint_id", "")
        if not isinstance(cid, str) or not cid.startswith(ID_PREFIX["constraint"]):
            rep.error(w, f"constraint_id 前缀必须是 {ID_PREFIX['constraint']}: {cid!r}")
        elif cid in seen:
            rep.error(w, f"constraint_id 重复: {cid}")
        seen.add(cid)
        w = f"constraint:{cid}"
        for key in ("title", "question", "constraint_type", "parameters",
                    "source_refs", "temporal_scope", "notes", "open_items"):
            if key not in row:
                rep.error(w, f"缺少字段 {key}")
        for key in ("title", "question"):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                rep.error(w, f"{key} 必须是非空字符串")
        ctype = row.get("constraint_type")
        type_spec = type_specs.get(ctype) if isinstance(ctype, str) else None
        if type_spec is None:
            rep.error(w, f"constraint_type 非法: {ctype!r}")
            continue
        params = row.get("parameters")
        if not isinstance(params, dict):
            rep.error(w, "parameters 必须是对象")
            params = {}
        required = type_spec.get("required_parameters", [])
        optional = type_spec.get("optional_parameters", [])
        allowed = set(required) | set(optional)
        unknown = sorted(set(params) - allowed)
        if unknown:
            rep.error(w, f"parameters 含未声明键: {', '.join(unknown)}")
        for name in required:
            if name not in params:
                rep.error(w, f"parameters 缺少必填项: {name}")

        def field_ok(name, kind=None):
            if name not in ext:
                rep.error(w, f"parameters.{name} 不是 member_extension_fields 字段: {name!r}")
                return False
            if kind is not None and ext[name].get("type") != kind:
                rep.error(w, f"parameters.{name} 类型应为 {kind}: {ext[name].get('type')!r}")
                return False
            return True

        if ctype == "site_red_line":
            use_field = params.get("use_field")
            if isinstance(use_field, str):
                field_ok(use_field, "boolean")
            else:
                rep.error(w, "parameters.use_field 必须是字符串")
            pair = params.get("pair_fields")
            if not isinstance(pair, list) or len(pair) != 2:
                rep.error(w, "parameters.pair_fields 必须是两个字段的数组")
            else:
                if pair[0] == pair[1]:
                    rep.error(w, "parameters.pair_fields 不能是同一字段")
                for name in pair:
                    if not isinstance(name, str):
                        rep.error(w, "parameters.pair_fields 只能包含字符串")
                    else:
                        field_ok(name, "string")
            if "consistency_field" in params:
                flag = params.get("consistency_field")
                if not isinstance(flag, str):
                    rep.error(w, "parameters.consistency_field 必须是字符串")
                elif flag in ext:
                    field_ok(flag, "boolean")
                elif flag not in schema.get("fields", {}):
                    rep.error(w, f"parameters.consistency_field 不是成员字段: {flag!r}")
                elif schema["fields"][flag].get("type") != "boolean":
                    rep.error(w, f"parameters.consistency_field 类型应为 boolean: {flag!r}")
            if "exception" in params:
                exc = params.get("exception")
                if not isinstance(exc, dict) or set(exc) != {"project_class", "connection_type"}:
                    rep.error(w, "parameters.exception 必须恰含 project_class 与 connection_type")
                else:
                    if exc.get("project_class") not in project_classes:
                        rep.error(w, f"exception.project_class 非法: {exc.get('project_class')!r}")
                    conn = schema.get("fields", {}).get("connection_type", {}).get("values", [])
                    if exc.get("connection_type") not in conn:
                        rep.error(w, f"exception.connection_type 非法: {exc.get('connection_type')!r}")
        elif ctype == "account_merge":
            for name in ("group_by", "account_field"):
                if not isinstance(params.get(name), str):
                    rep.error(w, f"parameters.{name} 必须是字符串")
                else:
                    field_ok(params[name], "string")
        elif ctype == "connection_point":
            for name in ("group_by", "connection_field"):
                if not isinstance(params.get(name), str):
                    rep.error(w, f"parameters.{name} 必须是字符串")
                else:
                    field_ok(params[name], "string")
            anchor_name = params.get("anchor_field")
            if not isinstance(anchor_name, str):
                rep.error(w, "parameters.anchor_field 必须是字符串")
            elif anchor_name not in allowed_group:
                rep.error(w, f"anchor_field 必须是 group 字段: {anchor_name!r}")
            elif group_fields.get(anchor_name, {}).get("type") != "string":
                rep.error(w, f"anchor_field 类型必须是 string: {anchor_name!r}")
            diff = params.get("differentiation_fields")
            if not isinstance(diff, list) or not diff:
                rep.error(w, "parameters.differentiation_fields 必须是非空数组")
            else:
                if len(set(diff)) != len(diff):
                    rep.error(w, "parameters.differentiation_fields 有重复")
                for name in diff:
                    if not isinstance(name, str):
                        rep.error(w, "differentiation_fields 只能包含字符串")
                    else:
                        field_ok(name, "string")
            targets = params.get("target_classes")
            if not isinstance(targets, list) or not targets:
                rep.error(w, "parameters.target_classes 必须是非空数组")
            else:
                bad = [x for x in targets if x not in project_classes]
                if bad:
                    rep.error(w, f"target_classes 含非法分类: {bad!r}")
                if len(set(targets)) != len(targets):
                    rep.error(w, "target_classes 有重复")

        total_refs += len(row.get("source_refs", []) or [])
        normative_refs += validate_refs(
            row.get("source_refs", []), w, sources, locators, schema, rep, require_normative=True
        )
        _check_temporal_scope(row.get("temporal_scope"), w, rep)
        for key in ("notes", "open_items"):
            val = row.get(key, [])
            if not isinstance(val, list) or any(not isinstance(x, str) or not x.strip() for x in val):
                rep.error(w, f"{key} 必须是非空字符串数组（可为空数组）")
    rep.counts["constraints"] = len(rows)
    rep.counts["constraint_refs"] = total_refs
    rep.counts["constraint_normative_refs"] = normative_refs


# ---------------------------------------------------------------- consistency

CONSISTENCY_RELATIONS = {"same_text", "refined", "conflict", "single_source"}
CONSISTENCY_EFFECTS = {"same", "provincial_narrower", "provincial_broader", "interpretive", "different"}
CONSISTENCY_RISKS = {"none", "rule_uncovered", "rule_scope_missing", "expiry_unmodeled"}
CONSISTENCY_CLAIM_RE = re.compile(r"^O-K-\d{3}$")


def _iter_refs(obj) -> list[str]:
    """递归收集任意结构里的 source_refs.locator_id。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "source_refs" and isinstance(value, list):
                for ref in value:
                    if isinstance(ref, dict) and ref.get("locator_id"):
                        found.append(ref["locator_id"])
            else:
                found.extend(_iter_refs(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_iter_refs(item))
    return found


def _expected_relation(effects: list[str]) -> str | None:
    """从 statement 级 effect 派生 claim 级 relation（review 34 §2.2）。"""
    if not effects:
        return None
    if "different" in effects:
        return "conflict"
    if len(effects) == 1:
        return "single_source"
    if any(e.startswith("provincial_") or e == "interpretive" for e in effects):
        return "refined"
    return "same_text"


def validate_consistency(
    schema: dict, sources: dict, locators: dict, rep: Report
) -> None:
    """校验 knowledge/consistency.json（consistency/v1）。"""
    if not CONSISTENCY_PATH.exists():
        rep.error("consistency", f"找不到 {CONSISTENCY_PATH.relative_to(ROOT).as_posix()}")
        return
    try:
        doc = load_json(CONSISTENCY_PATH)
    except json.JSONDecodeError as exc:
        rep.error("consistency", f"JSON 解析失败: {exc}")
        return
    if doc.get("schema_version") != "consistency/v1":
        rep.error(
            "consistency",
            f"schema_version 必须是 consistency/v1: {doc.get('schema_version')!r}",
        )
    claims = doc.get("claims")
    if not isinstance(claims, list) or not claims:
        rep.error("consistency", "claims 必须是非空数组")
        return
    rules = _load_rules_by_id()
    facts = _load_facts_by_id()
    relations: dict[str, dict] = {}
    if RELATIONS_PATH.exists():
        for row in load_jsonl(RELATIONS_PATH):
            relations[row.get("relation_id")] = row

    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    covered: set[str] = set()
    total_refs = 0
    normative_refs = 0

    for idx, claim in enumerate(claims, 1):
        w = f"consistency[{idx}]"
        cid = claim.get("claim_id", "")
        if not CONSISTENCY_CLAIM_RE.match(cid):
            rep.error(w, f"claim_id 必须形如 O-K-001: {cid!r}")
        if cid in seen_ids:
            rep.error(w, f"claim_id 重复: {cid}")
        seen_ids.add(cid)
        key = claim.get("key")
        if not isinstance(key, str) or not key.strip():
            rep.error(w, "key 不能为空")
        elif key in seen_keys:
            rep.error(w, f"key 重复: {key}")
        else:
            seen_keys.add(key)
        if not claim.get("label"):
            rep.error(w, "label 不能为空")
        relation = claim.get("relation")
        if relation not in CONSISTENCY_RELATIONS:
            rep.error(w, f"relation 非法: {relation!r}")
        risk = claim.get("risk")
        if risk not in CONSISTENCY_RISKS:
            rep.error(w, f"risk 非法: {risk!r}")
        note = claim.get("note")
        if not isinstance(note, str) or not note.strip():
            rep.error(w, "note 不能为空")
        elif risk not in (None, "none") and "影响" not in note:
            rep.error(w, f"risk={risk} 时 note 必须写清影响")
        temp = claim.get("temporal")
        if not isinstance(temp, dict) or temp.get("kind") not in ("none", "expiring", "expired"):
            rep.error(w, "temporal 必须是 {kind: none|expiring|expired, at, note}")
        elif temp["kind"] == "none":
            if temp.get("at") is not None:
                rep.error(w, "temporal.kind=none 时 at 必须为 null")
        else:
            if parse_date(temp.get("at")) is None:
                rep.error(w, f"temporal.at 必须是 YYYY-MM-DD: {temp.get('at')!r}")
            if not isinstance(temp.get("note"), str) or not temp["note"].strip():
                rep.error(w, "temporal.kind 非 none 时 temporal.note 不能为空")

        statements = claim.get("statements")
        if not isinstance(statements, list) or not statements:
            rep.error(w, "statements 必须是非空数组")
            continue
        effects: list[str] = []
        normative = 0
        for j, stm in enumerate(statements, 1):
            sw = f"{w}.statements[{j}]"
            if not isinstance(stm, dict):
                rep.error(sw, "必须是对象")
                continue
            effect = stm.get("effect")
            if effect not in CONSISTENCY_EFFECTS:
                rep.error(sw, f"effect 非法: {effect!r}")
            else:
                effects.append(effect)
                if effect == "different" and not (stm.get("note") or "").strip():
                    rep.error(sw, "effect=different 时必须写 note 说明为何互斥")
            if stm.get("evidence_level") == "normative":
                normative += 1
            if stm.get("locator_id"):
                covered.add(stm["locator_id"])
        normative_refs += validate_refs(statements, w, sources, locators, schema, rep, False)
        total_refs += len(statements)

        canonical = claim.get("canonical")
        locs = {s.get("locator_id") for s in statements if isinstance(s, dict)}
        if canonical not in locs:
            rep.error(w, f"canonical 必须是 statements 中的 locator: {canonical!r}")
        else:
            canon_effects = [
                s.get("effect") for s in statements
                if isinstance(s, dict) and s.get("locator_id") == canonical
            ]
            if "same" not in canon_effects:
                rep.error(w, "canonical 对应语句的 effect 必须是 same")
        expected = _expected_relation(effects)
        if expected and relation != expected:
            rep.error(w, f"relation={relation} 与 effect 派生结果 {expected} 不一致")
        if relation == "conflict" and normative == 0:
            rep.error(w, "relation=conflict 需要至少 1 条 normative 对照")
        for rid in claim.get("rule_refs", []) or []:
            if rid not in rules:
                rep.error(w, f"rule_refs 指向不存在的规则: {rid}")
        for fid in claim.get("fact_refs", []) or []:
            if fid not in facts:
                rep.error(w, f"fact_refs 指向不存在的事实: {fid}")
        for eid in claim.get("refines_edges", []) or []:
            edge = relations.get(eid)
            if edge is None:
                rep.error(w, f"refines_edges 指向不存在的边: {eid}")
            elif edge.get("relation") != "refines":
                rep.error(w, f"refines_edges 只接受 refines 边: {eid}（{edge.get('relation')}）")

    # 覆盖门禁：规则与事实引用到的每个 locator 都必须进表
    required: set[str] = set()
    if RULES_PATH.exists():
        required |= set(_iter_refs(load_json(RULES_PATH)))
    if FACTS_PATH.exists():
        for row in load_jsonl(FACTS_PATH):
            required |= set(_iter_refs({"source_refs": row.get("source_refs") or []}))
    for loc in sorted(required - covered):
        rep.error("consistency", f"未覆盖被规则/事实引用的 locator: {loc}")

    rep.counts["claims"] = len(claims)
    rep.counts["claim_refs"] = total_refs
    rep.counts["claim_normative_refs"] = normative_refs


def validate_cases(schema: dict, rep: Report) -> None:
    """eval/cases.jsonl 的结构检查；不在这里执行规则（执行见 src/rule_eval.py）。"""
    path = ROOT / "eval" / "cases.jsonl"
    if not path.exists():
        rep.error("cases", "找不到 eval/cases.jsonl")
        rep.counts["cases"] = 0
        return
    allowed_status = {"supported", "insufficient", "conflict", "out_of_scope"}
    seen: set[str] = set()
    n = 0
    for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        w = f"cases:L{lineno}"
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            rep.error(w, f"JSON 解析失败: {exc}")
            continue
        if not isinstance(case, dict):
            rep.error(w, "每行必须是一个对象")
            continue
        n += 1
        cid = case.get("case_id")
        if not isinstance(cid, str) or not cid.strip():
            rep.error(w, "case_id 必须是非空字符串")
        elif cid in seen:
            rep.error(w, f"case_id 重复: {cid}")
        else:
            seen.add(cid)
        project = case.get("project")
        if not isinstance(project, dict):
            rep.error(w, "project 必须是对象")
        else:
            for key in project:
                if key not in schema.get("fields", {}):
                    rep.error(w, f"project 含 schema 未声明字段: {key}")
        if "policy_as_of" in case and case["policy_as_of"] is not None:
            if not isinstance(case["policy_as_of"], str):
                rep.error(w, "policy_as_of 必须是字符串或 null")
        expected = case.get("expected")
        if not isinstance(expected, dict):
            rep.error(w, "expected 必须是对象")
            continue
        status = expected.get("result_status")
        if status not in allowed_status:
            rep.error(w, f"expected.result_status 非法: {status!r}")
        missing = expected.get("missing_fields")
        if not isinstance(missing, list) or any(not isinstance(x, str) or not x for x in missing):
            rep.error(w, "expected.missing_fields 必须是字符串数组")
        conclusions = expected.get("conclusions")
        if not isinstance(conclusions, list):
            rep.error(w, "expected.conclusions 必须是数组")
        else:
            for item in conclusions:
                if not isinstance(item, dict) or "field" not in item or "value" not in item:
                    rep.error(w, "expected.conclusions 每项必须含 field 与 value")
                elif item["field"] not in schema.get("output_fields", {}):
                    rep.error(w, f"expected.conclusions 字段未在 output_fields 声明: {item['field']}")
    rep.counts["cases"] = n


# ---------------------------------------------------------------- commands


def cmd_validate(args: argparse.Namespace) -> int:
    rep = Report()
    if not SCHEMA_PATH.exists():
        print(f"FAIL: 找不到 {SCHEMA_PATH.relative_to(ROOT)}")
        return 1
    schema = load_json(SCHEMA_PATH)
    validate_schema(schema, rep)
    sources = load_sources(schema, rep)
    locators = load_locators(sources, rep)
    if args.samples:
        validate_locator_samples(sources, locators, rep)
    validate_entities(schema, sources, locators, rep)
    validate_facts(schema, sources, locators, rep)
    validate_rules(schema, sources, locators, rep)
    validate_relations(schema, sources, locators, rep)
    validate_constraints(schema, sources, locators, rep)
    validate_consistency(schema, sources, locators, rep)
    validate_cases(schema, rep)

    print("== 计数 ==")
    for key in ("sources", "locators", "entities", "facts", "rules", "relations",
                "constraints", "claims", "cases", "refs", "normative_refs", "relation_refs",
                "constraint_refs", "constraint_normative_refs", "claim_refs",
                "claim_normative_refs"):
        print(f"  {key:10s} {rep.counts.get(key, 0)}")
    if rep.warnings:
        print(f"\n== warnings ({len(rep.warnings)}) ==")
        for w in rep.warnings:
            print("  WARN", w)
    if rep.errors:
        print(f"\n== errors ({len(rep.errors)}) ==")
        for e in rep.errors:
            print("  ERROR", e)
        print("\nRESULT: FAIL")
    else:
        print("\nRESULT: PASS（无硬错误）")

    if args.report:
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "project_root": str(ROOT),
            "result": "pass" if rep.ok else "fail",
            "counts": rep.counts,
            "errors": rep.errors,
            "warnings": rep.warnings,
        }
        out = Path(args.report)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"report -> {out.relative_to(ROOT)}")
    return 0 if rep.ok else 1


def validate_locator_samples(sources: dict, locators: dict, rep: Report) -> None:
    """硬门禁要求的 3 个真实 locator 样例必须在索引中可解析。"""
    samples = [
        "PV-NAT-2025-007-ATTACH#clause=第四十三条",
        "PV-EXPLAIN-20250123#section=四(一)",
        "PV-FAQ-20250411#qa=Q71",
    ]
    for loc in samples:
        if loc not in locators:
            rep.error("locator-samples", f"样例 locator 不存在: {loc}")
        else:
            rec = locators[loc]
            if not compact(rec["text"]):
                rep.error("locator-samples", f"样例 locator 文本为空: {loc}")


def cmd_locators(args: argparse.Namespace) -> int:
    schema = load_json(SCHEMA_PATH) if SCHEMA_PATH.exists() else {}
    rep = Report()
    sources = load_sources(schema, rep)
    locators = load_locators(sources, rep)
    wanted = args.locator or [
        "PV-NAT-2025-007-ATTACH#clause=第四十三条",
        "PV-EXPLAIN-20250123#section=四(一)",
        "PV-FAQ-20250411#qa=Q71",
    ]
    for loc in wanted:
        rec = locators.get(loc)
        if rec is None:
            print(f"NOT FOUND {loc}")
            continue
        text = compact(rec["text"])
        print(f"{loc}")
        print(f"  source={rec['source_id']} kind={rec['kind']} value={rec['value']} chars={len(text)}")
        print(f"  text[:120]={text[:120]}")
    return 0


def cmd_assess(args: argparse.Namespace) -> int:
    """调用 src/rule_eval.py：按 rules.json 评估单个项目或固定案例。"""
    sys.path.insert(0, str(ROOT / "src"))
    from rule_eval import assess_project, run_cases  # 延迟导入，validate 不需要它

    if args.project:
        project = json.loads(Path(args.project).read_text("utf-8"))
        print(json.dumps(assess_project(project, args.policy_as_of), ensure_ascii=False, indent=2))
        return 0
    cases_path = Path(args.cases) if args.cases else ROOT / "eval" / "cases.jsonl"
    return run_cases(cases_path)


# ---------------------------------------------------------------- query / export


def _load_knowledge() -> tuple[dict, list[dict], list[dict], dict]:
    schema = load_json(SCHEMA_PATH)
    entities = load_jsonl(ENTITIES_PATH)
    facts = load_jsonl(FACTS_PATH)
    rules = load_json(RULES_PATH)
    return schema, entities, facts, rules


def _field_view(schema: dict, name: str) -> dict:
    spec = schema.get("fields", {}).get(name, {})
    return {
        "field": name,
        "type": spec.get("type"),
        "unit": spec.get("unit"),
        "label": spec.get("label"),
        "enum": spec.get("values") or spec.get("enum"),
    }


def _condition_view(schema: dict, cond: dict) -> dict:
    view = dict(cond)
    view["field_meta"] = _field_view(schema, cond.get("field", ""))
    return view


def _refs(refs: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in refs:
        key = (r.get("locator_id"), r.get("quote"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source_id": r.get("source_id"),
            "locator_id": r.get("locator_id"),
            "evidence_level": r.get("evidence_level"),
            "quote": r.get("quote"),
        })
    return out


def _rule_cases_for(rules: dict, rule_id: str) -> list[dict]:
    for rule in rules.get("rules", []):
        if rule["rule_id"] == rule_id:
            return rule.get("cases", [])
    return []


def _find_rule(rules: dict, rule_id: str) -> dict | None:
    for rule in rules.get("rules", []):
        if rule["rule_id"] == rule_id:
            return rule
    return None


def _query_class_requirements(schema: dict, rules: dict, params: dict) -> dict:
    project_class = params.get("project_class")
    rule = _find_rule(rules, "O-R-001")
    if rule is None:
        return {"result": [], "notes": ["缺少 O-R-001"]}
    cases = []
    for case in _rule_cases_for(rules, "O-R-001"):
        hits = [c for c in case.get("then", {}).get("conclusions", [])
                if c.get("field") == "project_class" and c.get("value") == project_class]
        if not hits:
            continue
        cases.append({
            "case_id": case["case_id"],
            "label": case.get("label"),
            "conditions": [_condition_view(schema, c) for c in case["when"]["conditions"]],
        })
    return {
        "params": {"project_class": project_class},
        "result": cases,
        "notes": [
            "分类不是单看容量：同一容量在不同电压、场地、投资主体下可能落到不同类型。",
            "条件中的数字阈值来自第四条；容量口径必须是交流侧，否则 assess 返回 insufficient。",
        ],
        "exceptions": rule.get("exceptions", []),
        "evidence": _refs(rule.get("source_refs", [])),
    }


def _query_allowed_modes(schema: dict, rules: dict, params: dict) -> dict:
    project_class = params.get("project_class")
    rule = _find_rule(rules, "O-R-002")
    if rule is None:
        return {"result": [], "notes": ["缺少 O-R-002"]}
    matches = []
    for case in _rule_cases_for(rules, "O-R-002"):
        conds = case.get("when", {}).get("conditions", [])
        if not any(c.get("field") == "project_class" for c in conds):
            continue
        cls_cond = next(c for c in conds if c.get("field") == "project_class")
        if cls_cond.get("predicate") == "equals" and cls_cond.get("value") == project_class:
            matches.append(case)
        elif cls_cond.get("predicate") == "in" and project_class in (cls_cond.get("value") or []):
            matches.append(case)
    results = []
    for case in matches:
        modes = []
        same_red_line = None
        for con in case.get("then", {}).get("conclusions", []):
            if con.get("field") == "allowed_grid_modes":
                modes = con.get("value")
            if con.get("field") == "requires_same_red_line_for_self_consumption":
                same_red_line = con.get("value")
        results.append({
            "case_id": case["case_id"],
            "label": case.get("label"),
            "allowed_grid_modes": modes,
            "requires_same_red_line_for_self_consumption": same_red_line,
            "extra_conditions": [_condition_view(schema, c) for c in case["when"]["conditions"]
                                 if c.get("field") != "project_class"],
        })
    return {
        "params": {"project_class": project_class},
        "result": results,
        "notes": [
            "这里给的是“允许集合”，不是对某个具体项目已选模式的合法性判定；具体项目用 assess 比较。",
            "大型工商业的现货市场条件在 assess 中由 spot_market_region 决定；未知时返回 insufficient。",
        ],
        "exceptions": rule.get("exceptions", []),
        "evidence": _refs(rule.get("source_refs", [])),
    }


def _query_date_semantics(schema: dict, entities: list[dict], facts: list[dict], rules: dict) -> dict:
    date_fields = []
    for name, spec in schema.get("fields", {}).items():
        if spec.get("type") == "date":
            date_fields.append(_field_view(schema, name))
    date_facts = [f for f in facts if "日期" in f.get("statement", "") or "日" in f.get("statement", "")]
    rule = _find_rule(rules, "O-R-003")
    conditions = []
    if rule:
        for case in rule.get("cases", []):
            for c in case.get("when", {}).get("conditions", []):
                if c.get("predicate", "").startswith("date_"):
                    conditions.append({
                        "case_id": case["case_id"],
                        "case_label": case.get("label"),
                        "condition": _condition_view(schema, c),
                        "conclusions": case.get("then", {}).get("conclusions", []),
                    })
    evidence = []
    if rule:
        evidence.extend(_refs(rule.get("source_refs", [])))
    for f in date_facts:
        evidence.extend(_refs(f.get("source_refs", [])))
    return {
        "params": {},
        "result": {
            "date_fields": date_fields,
            "facts": [{"fact_id": f["fact_id"], "statement": f["statement"]} for f in date_facts],
            "rule_conditions": conditions,
        },
        "notes": [
            "备案日、备案变更日、并网日、发布日、施行日不能合并成一个“项目日期”。",
            "Q71：发布之日指 2025-01-23（含当日）；Q72：2025-05-01 前含当日且须全容量并网。",
        ],
        "exceptions": rule.get("exceptions", []) if rule else [],
        "evidence": evidence,
    }


def _query_missing_field_impact(schema: dict, rules: dict, params: dict) -> dict:
    field = params.get("field")
    hits = []
    all_refs: list[dict] = []
    for rule in rules.get("rules", []):
        if field not in rule.get("input_fields", []):
            continue
        cases = []
        for case in rule.get("cases", []):
            touched = [c for c in case.get("when", {}).get("conditions", []) if c.get("field") == field]
            if touched:
                cases.append({
                    "case_id": case["case_id"],
                    "label": case.get("label"),
                    "conditions": [_condition_view(schema, c) for c in touched],
                })
        hits.append({
            "rule_id": rule["rule_id"],
            "title": rule.get("title"),
            "cases": cases,
            "default_status": rule.get("default", {}).get("status"),
        })
        all_refs.extend(rule.get("source_refs", []))
    return {
        "params": {"field": field},
        "result": hits,
        "notes": [
            "字段缺失时，规则不应把“未知”当作否定；assess 会返回 insufficient 并列出缺项。",
            "证据列的是使用该字段的规则引用；缺字段本身不改变条款含义，只改变可判定性。",
        ],
        "evidence": _refs(all_refs),
    }


def _query_source_coverage(entities: list[dict], facts: list[dict], rules: dict, params: dict) -> dict:
    owner = params.get("owner_id")
    refs: list[dict] = []
    if owner.startswith("O-E-"):
        refs = next((e.get("source_refs", []) for e in entities if e["entity_id"] == owner), [])
    elif owner.startswith("O-F-"):
        refs = next((f.get("source_refs", []) for f in facts if f["fact_id"] == owner), [])
    elif owner.startswith("O-R-"):
        rule = _find_rule(rules, owner)
        refs = rule.get("source_refs", []) if rule else []
    levels: dict[str, int] = {}
    for r in refs:
        lv = r.get("evidence_level") or "unset"
        levels[lv] = levels.get(lv, 0) + 1
    return {
        "params": {"owner_id": owner},
        "result": {"evidence_level_counts": levels, "refs": _refs(refs)},
        "notes": ["正文/解读/问答的证据等级分开统计；问答与解读不能冒充规范正文。"],
        "evidence": _refs(refs),
    }


def _query_temporal_scope(rules: dict, params: dict) -> dict:
    as_of = params.get("policy_as_of")
    rows = []
    date_refs: list[dict] = []
    for rule in rules.get("rules", []):
        scope = rule.get("temporal_scope", {})
        start = scope.get("effective_from")
        end = scope.get("effective_to")
        if start and as_of < start:
            status = "out_of_scope"
        elif end and as_of > end:
            status = "out_of_scope"
        else:
            status = "active"
        rows.append({
            "rule_id": rule["rule_id"],
            "title": rule.get("title"),
            "effective_from": start,
            "effective_to": end,
            "status_at_as_of": status,
        })
        for r in rule.get("source_refs", []):
            q = r.get("quote") or ""
            if "施行" in q or "发布之日起" in q:
                date_refs.append(r)
    return {
        "params": {"policy_as_of": as_of},
        "result": rows,
        "notes": ["policy_as_of 是问题所问时点，不是抓取时点；越界返回 out_of_scope，不默认套当前政策。"],
        "evidence": _refs(date_refs),
    }


def run_query(query: dict, schema: dict, entities: list[dict], facts: list[dict], rules: dict) -> dict:
    qtype = query.get("query_type")
    params = query.get("params") or {}
    if qtype == "class_requirements":
        body = _query_class_requirements(schema, rules, params)
    elif qtype == "allowed_modes":
        body = _query_allowed_modes(schema, rules, params)
    elif qtype == "date_semantics":
        body = _query_date_semantics(schema, entities, facts, rules)
    elif qtype == "missing_field_impact":
        body = _query_missing_field_impact(schema, rules, params)
    elif qtype == "source_coverage":
        body = _query_source_coverage(entities, facts, rules, params)
    elif qtype == "temporal_scope":
        body = _query_temporal_scope(rules, params)
    else:
        raise SystemExit(f"未知 query_type: {qtype}")
    out = {
        "query_id": query.get("query_id"),
        "question": query.get("question"),
        "query_type": qtype,
        "result": body["result"],
        "notes": body.get("notes", []),
        "exceptions": body.get("exceptions", []),
        "evidence": body.get("evidence", []),
    }
    if body.get("params"):
        out["params"] = body["params"]
    return out


def load_queries() -> list[dict]:
    path = ROOT / "eval" / "queries.jsonl"
    if not path.exists():
        raise SystemExit(f"找不到 {path.relative_to(ROOT)}")
    return load_jsonl(path)


def cmd_query(args: argparse.Namespace) -> int:
    queries = load_queries()
    schema, entities, facts, rules = _load_knowledge()
    if args.case_id:
        wanted = [q for q in queries if q["query_id"] == args.case_id]
        if not wanted:
            print(f"FAIL: 未定义 query_id {args.case_id}")
            return 1
        print(json.dumps(run_query(wanted[0], schema, entities, facts, rules), ensure_ascii=False, indent=2))
        return 0
    if args.all:
        out = []
        for q in queries:
            out.append(run_query(q, schema, entities, facts, rules))
        print(json.dumps({"batch": "pv-national-2025-v1", "queries": out}, ensure_ascii=False, indent=2))
        return 0
    print("已定义查询：")
    for q in queries:
        print(f"  {q['query_id']}  {q.get('question')}")
    return 0


EXPORT_FIELDS = [
    "record_id", "record_type", "text", "entity_ids", "fact_ids", "rule_ids",
    "source_refs", "conditions", "temporal_scope", "claim_status", "knowledge_kind", "batch",
]


def build_export_records(schema: dict, entities: list[dict], facts: list[dict], rules: dict,
                         constraints: dict | None = None) -> list[dict]:
    batch = rules.get("batch") or "pv-national-2025-v1"
    records: list[dict] = []
    for e in entities:
        records.append({
            "record_id": e["entity_id"],
            "record_type": "entity",
            "text": e.get("definition", ""),
            "entity_ids": [e["entity_id"]],
            "fact_ids": [],
            "rule_ids": [],
            "source_refs": e.get("source_refs", []),
            "conditions": [],
            "temporal_scope": e.get("temporal_scope", {}),
            "claim_status": "supported",
            "knowledge_kind": "extracted",
            "batch": batch,
        })
    for f in facts:
        records.append({
            "record_id": f["fact_id"],
            "record_type": "fact",
            "text": f.get("statement", ""),
            "entity_ids": [],
            "fact_ids": [f["fact_id"]],
            "rule_ids": [],
            "source_refs": f.get("source_refs", []),
            "conditions": f.get("conditions", []),
            "temporal_scope": f.get("temporal_scope", {}),
            "claim_status": f.get("claim_status", "unknown"),
            "knowledge_kind": "extracted",
            "batch": batch,
        })
    for r in rules.get("rules", []):
        records.append({
            "record_id": r["rule_id"],
            "record_type": "rule",
            "text": r.get("title", ""),
            "entity_ids": [],
            "fact_ids": [],
            "rule_ids": [r["rule_id"]],
            "source_refs": r.get("source_refs", []),
            "conditions": [
                {
                    "case_id": c.get("case_id"),
                    "when": c.get("when", {}),
                    "then": c.get("then", {}),
                }
                for c in r.get("cases", [])
            ] + [{"default": r.get("default", {}), "exceptions": r.get("exceptions", [])}],
            "temporal_scope": r.get("temporal_scope", {}),
            "claim_status": "supported",
            "knowledge_kind": "derived",
            "batch": batch,
        })
    for c in (constraints or {}).get("constraints", []):
        records.append({
            "record_id": c["constraint_id"],
            "record_type": "constraint",
            "text": c.get("title", ""),
            "entity_ids": [],
            "fact_ids": [],
            "rule_ids": [],
            "source_refs": c.get("source_refs", []),
            "conditions": [{
                "constraint_type": c.get("constraint_type"),
                "parameters": c.get("parameters", {}),
                "question": c.get("question", ""),
            }],
            "temporal_scope": c.get("temporal_scope", {}),
            "claim_status": "supported",
            "knowledge_kind": "derived",
            "batch": batch,
        })
    return records


def cmd_export(args: argparse.Namespace) -> int:
    schema, entities, facts, rules = _load_knowledge()
    constraints = load_json(CONSTRAINTS_PATH) if CONSTRAINTS_PATH.exists() else {}
    records = build_export_records(schema, entities, facts, rules, constraints)
    errors = []
    for rec in records:
        missing = [k for k in EXPORT_FIELDS if k not in rec]
        if missing:
            errors.append(f"{rec.get('record_id')}: 缺字段 {missing}")
        if not str(rec.get("text", "")).strip():
            errors.append(f"{rec.get('record_id')}: text 为空")
        if not rec.get("source_refs"):
            errors.append(f"{rec.get('record_id')}: source_refs 为空")
        if rec.get("knowledge_kind") not in {"extracted", "derived"}:
            errors.append(f"{rec.get('record_id')}: knowledge_kind 非法")
    if errors:
        for e in errors:
            print("ERROR", e)
        print("RESULT: FAIL")
        return 1
    out = Path(args.output) if args.output else ROOT / "exports" / "ontology_evidence.jsonl"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=False) + "\n")
    by_type: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for rec in records:
        by_type[rec["record_type"]] = by_type.get(rec["record_type"], 0) + 1
        by_kind[rec["knowledge_kind"]] = by_kind.get(rec["knowledge_kind"], 0) + 1
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"exported {len(records)} records -> {shown}")
    print(f"  by_type: {by_type}")
    print(f"  by_kind: {by_kind}")
    return 0


def cmd_constraints(args: argparse.Namespace) -> int:
    """多项目约束：list / show / check（constraints/v1）。"""
    sys.path.insert(0, str(ROOT / "src"))
    from constraint_eval import (
        GroupValidationError,
        check_group,
        list_constraints,
        show_constraint,
    )

    action = args.action
    if action == "list":
        payload = list_constraints(ROOT)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        for c in payload["constraints"]:
            print(f"{c['constraint_id']}  [{c['constraint_type']}]  {c['title']}")
        return 0
    if action == "show":
        if not args.constraint_id:
            print("FAIL: show 需要 constraint_id")
            return 2
        payload = show_constraint(args.constraint_id, ROOT)
        if payload is None:
            print(f"FAIL: 未知约束 {args.constraint_id}")
            return 1
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if action == "check":
        if not args.group:
            print("FAIL: check 需要 --group <json文件|->")
            return 2
        try:
            if args.group == "-":
                group = json.load(sys.stdin)
            else:
                group = json.loads(Path(args.group).read_text(encoding="utf-8"))
            payload = check_group(group, ROOT)
        except GroupValidationError as exc:
            print("FAIL: 输入组结构不合法")
            for err in exc.errors:
                print("  ERROR", err)
            return 2
        except (OSError, json.JSONDecodeError) as exc:
            print(f"FAIL: 读取组输入失败: {exc}")
            return 2
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"group: {payload['group_id']}  结果: {payload['result_status']}")
        for c in payload["constraints"]:
            print(f"  {c['constraint_id']}  {c['status']:<14} {c['decision']:<28} {c['title']}")
            for reason in c["reasons"]:
                print(f"      - {reason}")
        if payload["missing_fields"]:
            print("  缺字段: " + ", ".join(payload["missing_fields"]))
        return 0
    print(f"FAIL: 未知 action {action}")
    return 2


def cmd_consistency(args: argparse.Namespace) -> int:
    """跨文档一致性对照表：list / show（consistency/v1）。"""
    if not CONSISTENCY_PATH.exists():
        print("FAIL: 找不到 knowledge/consistency.json")
        return 1
    doc = load_json(CONSISTENCY_PATH)
    claims = doc.get("claims") or []
    if args.action == "list":
        if args.json:
            payload = {
                "schema_version": doc.get("schema_version"),
                "claims": [
                    {
                        "claim_id": c.get("claim_id"),
                        "key": c.get("key"),
                        "label": c.get("label"),
                        "relation": c.get("relation"),
                        "risk": c.get("risk"),
                        "temporal": c.get("temporal"),
                        "statements": len(c.get("statements") or []),
                    }
                    for c in claims
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        for c in claims:
            print(
                f"{c['claim_id']}  {c['relation']:<12} {c.get('risk', 'none'):<18} "
                f"{len(c.get('statements') or []):>2} 条  {c.get('label')}"
            )
        return 0
    if args.action == "show":
        if not args.claim_id:
            print("FAIL: show 需要 claim_id")
            return 2
        for c in claims:
            if c.get("claim_id") == args.claim_id or c.get("key") == args.claim_id:
                print(json.dumps(c, ensure_ascii=False, indent=2))
                return 0
        print(f"FAIL: 未知 claim {args.claim_id}")
        return 1
    print(f"FAIL: 未知 action {args.action}")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="新能源 Ontology 工具")
    parser.add_argument("--root", help="项目根（默认本文件上一级）；仅用于负例测试")
    sub = parser.add_subparsers(dest="command", required=True)
    p_val = sub.add_parser("validate", help="校验来源、locator、schema、entities、facts、rules")
    p_val.add_argument("--report", help="把结果写成 JSON（相对项目根）")
    p_val.add_argument("--samples", action="store_true", help="额外检查硬门禁 3 个 locator 样例")
    p_loc = sub.add_parser("locators", help="打印 locator 解析结果")
    p_loc.add_argument("locator", nargs="*", help="locator_id；缺省打印 3 个样例")
    p_ass = sub.add_parser("assess", help="按规则评估单个项目或跑 eval/cases.jsonl")
    p_ass.add_argument("--cases", help="cases.jsonl 路径（默认 eval/cases.jsonl）")
    p_ass.add_argument("--project", help="单个项目 JSON 文件")
    p_ass.add_argument("--policy-as-of", help="问题所问时点 YYYY-MM-DD")
    p_q = sub.add_parser("query", help="执行 eval/queries.jsonl 中定义的条件/关系查询")
    p_q.add_argument("--case-id", help="查询 ID，如 O-Q01")
    p_q.add_argument("--all", action="store_true", help="执行全部查询并输出 JSON")
    p_e = sub.add_parser("export", help="导出 exports/ontology_evidence.jsonl")
    p_e.add_argument("--output", help="输出路径（默认 exports/ontology_evidence.jsonl）")
    p_rel = sub.add_parser("relations", help="查看实体关系边与影响面")
    p_rel.add_argument("action", choices=["list", "show", "impact", "ancestors"])
    p_rel.add_argument("node", nargs="?", help="show/impact/ancestors 的节点 ID")
    p_rel.add_argument("--json", action="store_true", help="impact/ancestors 输出 JSON")
    p_c = sub.add_parser("constraints", help="多项目约束：list / show / check")
    p_c.add_argument("action", choices=["list", "show", "check"])
    p_c.add_argument("constraint_id", nargs="?", help="show 的约束 ID，如 O-C-001")
    p_c.add_argument("--group", help="check 的组 JSON 文件；- 表示 stdin")
    p_c.add_argument("--json", action="store_true", help="输出 JSON")
    p_k = sub.add_parser("consistency", help="跨文档一致性对照表：list / show")
    p_k.add_argument("action", choices=["list", "show"])
    p_k.add_argument("claim_id", nargs="?", help="show 的 claim_id 或 key，如 O-K-001")
    p_k.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    if args.root:
        _set_root(args.root)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "assess":
        return cmd_assess(args)
    if args.command == "query":
        return cmd_query(args)
    if args.command == "export":
        return cmd_export(args)
    if args.command == "relations":
        return cmd_relations(args)
    if args.command == "constraints":
        return cmd_constraints(args)
    if args.command == "consistency":
        return cmd_consistency(args)
    return cmd_locators(args)


def _set_root(path: str) -> None:
    global ROOT, SCHEMA_PATH, SOURCES_PATH, EXTRA_SOURCES_PATH, RULES_PATH, RELATIONS_PATH, CONSTRAINT_SCHEMA_PATH, CONSTRAINTS_PATH, CONSISTENCY_PATH, ENTITIES_PATH, FACTS_PATH, NORMALIZED_DIR
    ROOT = Path(path).resolve()
    SCHEMA_PATH = ROOT / "model" / "schema.json"
    SOURCES_PATH = ROOT / "sources.jsonl"
    EXTRA_SOURCES_PATH = ROOT / "sources_ext.jsonl"
    RULES_PATH = ROOT / "knowledge" / "rules.json"
    RELATIONS_PATH = ROOT / "knowledge" / "relations.jsonl"
    CONSTRAINT_SCHEMA_PATH = ROOT / "model" / "constraint_schema.json"
    CONSTRAINTS_PATH = ROOT / "knowledge" / "constraints.json"
    CONSISTENCY_PATH = ROOT / "knowledge" / "consistency.json"
    ENTITIES_PATH = ROOT / "knowledge" / "entities.jsonl"
    FACTS_PATH = ROOT / "knowledge" / "facts.jsonl"
    NORMALIZED_DIR = ROOT / "normalized"


if __name__ == "__main__":
    sys.exit(main())
