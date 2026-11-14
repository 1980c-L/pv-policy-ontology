# -*- coding: utf-8 -*-
"""负例测试：证明 validate 在坏数据上会失败（本轮证据脚本，可复跑）。

做法：把项目复制到 `.tmp/negtest/<case>`，注入一种坏数据，要求 validate 非零退出
并报出指定错误。中间副本位于 `.tmp/negtest/`，可随时删除。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK：统一让本脚本输出 UTF-8，避免坏字符导致打印崩溃。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

PROJ = Path(__file__).resolve().parents[1]
BASE = PROJ / ".tmp" / "negtest"


def prepare(name: str) -> Path:
    dst = BASE / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        PROJ, dst,
        ignore=shutil.ignore_patterns(".tmp", ".git", "reviews", "exports", "__pycache__"),
    )
    return dst


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def save(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(root / "tools" / "ontology_tool.py"), "--root", str(root), "validate"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=CHILD_ENV,
    )
    return proc.returncode, proc.stdout + proc.stderr


def case(name: str, mutate, expect: str) -> bool:
    root = prepare(name)
    mutate(root)
    rc, out = run(root)
    ok = rc != 0 and expect in out
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: rc={rc}, 期望包含 {expect!r}")
    if not ok:
        print(out[-1500:])
    return ok


def m_bad_predicate(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][0]["cases"][0]["when"]["conditions"][0]["predicate"] = "nonsense"
    save(p, doc)


def m_bad_compare_field(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    rule = doc["rules"][0]
    case = next(c for c in rule["cases"]
                if c["case_id"] == "guangdong_factory_full_self_consumption_reference_large")
    cond = next(c for c in case["when"]["conditions"] if c["predicate"] == "lte_field")
    cond["value_field"] = "site_category"
    save(p, doc)


def m_bad_quote(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][2]["source_refs"][0]["quote"] = "这句话在原文里并不存在"
    save(p, doc)


def m_eval_only_ref(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][1]["source_refs"][0]["source_id"] = "PV-CASE-20250402"
    save(p, doc)


def m_field_not_declared(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][0]["cases"][0]["when"]["conditions"][0]["field"] = "spot_market_region"
    save(p, doc)


def m_bad_sha(root: Path):
    p = root / "sources.jsonl"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows[0]["raw_sha256"] = "0" * 64
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n", encoding="utf-8")


def m_bad_locator(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    ref = doc["rules"][0]["source_refs"][0]
    ref["locator_id"] = "PV-NAT-2025-007-ATTACH#clause=第四十四条"
    ref["value"] = "第四十四条"
    save(p, doc)


def _relations(root: Path) -> list[dict]:
    p = root / "knowledge" / "relations.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _save_relations(root: Path, rows: list[dict]) -> None:
    p = root / "knowledge" / "relations.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def m_guard_no_reason(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][0]["guards"][0].pop("reason", None)
    save(p, doc)


def m_guard_bad_status(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][0]["guards"][0]["status"] = "supported"
    save(p, doc)


def m_guard_empty_missing(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][0]["guards"][0]["missing_fields"] = []
    save(p, doc)


def m_guard_duplicate_id(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][0]["guards"].append(dict(doc["rules"][0]["guards"][0]))
    save(p, doc)


def m_guard_field_not_input(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    doc["rules"][0]["guards"][0]["missing_fields"] = ["spot_market_region"]
    save(p, doc)


def m_rel_dangling(root: Path):
    rows = _relations(root)
    rows[0]["to"] = "O-E-999"
    _save_relations(root, rows)


def m_rel_bad_type(root: Path):
    rows = _relations(root)
    rows[0]["relation"] = "refines"
    _save_relations(root, rows)


def m_rel_duplicate(root: Path):
    rows = _relations(root)
    dup = dict(rows[0])
    dup["relation_id"] = "O-REL-099"
    rows.append(dup)
    _save_relations(root, rows)


def m_rel_self(root: Path):
    rows = _relations(root)
    rows[0]["to"] = rows[0]["from"]
    _save_relations(root, rows)


def m_rel_no_note(root: Path):
    rows = _relations(root)
    rows[0].pop("note", None)
    _save_relations(root, rows)


def m_rel_bad_temporal(root: Path):
    rows = _relations(root)
    rows[0]["temporal_scope"] = {"effective_from": "2028-01-01", "effective_to": "2025-01-01"}
    _save_relations(root, rows)


def m_rel_single_ref(root: Path):
    rows = _relations(root)
    rows[0]["source_refs"] = rows[0]["source_refs"][:1]
    _save_relations(root, rows)


def m_rel_note_null(root: Path):
    rows = _relations(root)
    rows[0]["note"] = None
    _save_relations(root, rows)


def m_rel_note_number(root: Path):
    rows = _relations(root)
    rows[0]["note"] = 123
    _save_relations(root, rows)


def m_rel_note_list(root: Path):
    rows = _relations(root)
    rows[0]["note"] = []
    _save_relations(root, rows)


def m_rel_fact_without_constrains(root: Path):
    rows = _relations(root)
    rows = [r for r in rows if r["relation_id"] != "O-REL-010"]
    _save_relations(root, rows)


def _constraints(root: Path) -> tuple[Path, dict]:
    p = root / "knowledge" / "constraints.json"
    return p, load(p)


def m_constraint_bad_type(root: Path):
    p, doc = _constraints(root)
    doc["constraints"][0]["constraint_type"] = "nonsense"
    save(p, doc)


def m_constraint_unknown_param(root: Path):
    p, doc = _constraints(root)
    doc["constraints"][0]["parameters"]["unexpected"] = "x"
    save(p, doc)


def m_constraint_no_normative(root: Path):
    p, doc = _constraints(root)
    doc["constraints"][0]["source_refs"] = doc["constraints"][0]["source_refs"][:-1]
    save(p, doc)


def m_constraint_duplicate_id(root: Path):
    p, doc = _constraints(root)
    doc["constraints"][1]["constraint_id"] = doc["constraints"][0]["constraint_id"]
    save(p, doc)


def m_constraint_bad_prefix(root: Path):
    p, doc = _constraints(root)
    doc["constraints"][0]["constraint_id"] = "C-001"
    save(p, doc)


def m_constraint_schema_missing_key(root: Path):
    p = root / "model" / "constraint_schema.json"
    doc = load(p)
    doc.pop("decisions", None)
    save(p, doc)


def m_constraint_pair_same(root: Path):
    p, doc = _constraints(root)
    doc["constraints"][0]["parameters"]["pair_fields"] = ["building_red_line_id", "building_red_line_id"]
    save(p, doc)


def m_constraint_bad_target_class(root: Path):
    p, doc = _constraints(root)
    doc["constraints"][3]["parameters"]["target_classes"] = ["nonsense"]
    save(p, doc)


def m_constraint_bad_temporal(root: Path):
    p, doc = _constraints(root)
    doc["constraints"][0]["temporal_scope"] = {"effective_from": "2028-01-01", "effective_to": "2025-01-01"}
    save(p, doc)


def m_rel_cycle(root: Path):
    rows = _relations(root)
    # 001 O-E-002 -> O-E-001；把 002 反过来，形成 O-E-001 -> O-E-002 -> O-E-001。
    rows[1]["from"] = "O-E-001"
    rows[1]["to"] = "O-E-002"
    _save_relations(root, rows)


def _claims(root: Path) -> tuple[Path, dict]:
    p = root / "knowledge" / "consistency.json"
    return p, load(p)


def m_claim_bad_id(root: Path):
    p, doc = _claims(root)
    doc["claims"][0]["claim_id"] = "K-001"
    save(p, doc)


def m_claim_duplicate_id(root: Path):
    p, doc = _claims(root)
    doc["claims"][1]["claim_id"] = doc["claims"][0]["claim_id"]
    save(p, doc)


def m_claim_bad_schema(root: Path):
    p, doc = _claims(root)
    doc["schema_version"] = "consistency/v0"
    save(p, doc)


def m_claim_bad_quote(root: Path):
    p, doc = _claims(root)
    doc["claims"][0]["statements"][0]["quote"] = "原文里没有这句话"
    save(p, doc)


def m_claim_canonical_missing(root: Path):
    p, doc = _claims(root)
    doc["claims"][0]["canonical"] = "PV-GD-2025-002#section=第六节"
    save(p, doc)


def m_claim_canonical_not_same(root: Path):
    p, doc = _claims(root)
    claim = next(c for c in doc["claims"] if c["claim_id"] == "O-K-003")
    for stm in claim["statements"]:
        if stm["locator_id"] == claim["canonical"]:
            stm["effect"] = "provincial_narrower"
    save(p, doc)


def m_claim_relation_mismatch(root: Path):
    p, doc = _claims(root)
    doc["claims"][0]["relation"] = "conflict"
    save(p, doc)


def m_claim_different_no_note(root: Path):
    p, doc = _claims(root)
    stm = doc["claims"][0]["statements"][0]
    stm["effect"] = "different"
    stm.pop("note", None)
    save(p, doc)


def m_claim_bad_temporal(root: Path):
    p, doc = _claims(root)
    doc["claims"][0]["temporal"] = {"kind": "expiring", "at": "2028-02", "note": "写错格式"}
    save(p, doc)


def m_claim_risk_no_impact(root: Path):
    p, doc = _claims(root)
    claim = next(c for c in doc["claims"] if c["claim_id"] == "O-K-015")
    claim["note"] = "浙江超容量电压按集中式备案。"
    save(p, doc)


def m_claim_dangling_fact(root: Path):
    p, doc = _claims(root)
    doc["claims"][0]["fact_refs"] = ["O-F-099"]
    save(p, doc)


def m_claim_refines_not_refines(root: Path):
    rows = _relations(root)
    other = next(r for r in rows if r.get("relation") != "refines")
    p, doc = _claims(root)
    doc["claims"][0]["refines_edges"] = [other["relation_id"]]
    save(p, doc)


def m_claim_file_missing(root: Path):
    (root / "knowledge" / "consistency.json").unlink()


def m_claim_coverage_gap(root: Path):
    p = root / "knowledge" / "rules.json"
    doc = load(p)
    ref = dict(doc["rules"][0]["source_refs"][0])
    ref["locator_id"] = "PV-NAT-2025-007-ATTACH#clause=第一条"
    ref["value"] = "第一条"
    ref["quote"] = "第一条"
    doc["rules"][0]["source_refs"].append(ref)
    save(p, doc)


if __name__ == "__main__":
    if BASE.exists():
        shutil.rmtree(BASE)
    results = [
        case("bad-predicate", m_bad_predicate, "谓词不在白名单"),
        case("bad-compare-field", m_bad_compare_field, "比较字段必须是 number"),
        case("bad-quote", m_bad_quote, "找不到"),
        case("eval-only-ref", m_eval_only_ref, "不得引用"),
        case("field-not-declared", m_field_not_declared, "未在 input_fields 声明"),
        case("bad-sha", m_bad_sha, "raw_sha256 不匹配"),
        case("bad-locator", m_bad_locator, "locator 在 normalized 中不存在"),
        case("rel-dangling", m_rel_dangling, "entity 不存在"),
        case("rel-bad-type", m_rel_bad_type, "to 节点类型不符"),
        case("rel-duplicate", m_rel_duplicate, "重复边"),
        case("rel-self", m_rel_self, "自指边"),
        case("rel-no-note", m_rel_no_note, "note 必须是非空字符串"),
        case("rel-bad-temporal", m_rel_bad_temporal, "起止倒置"),
        case("rel-single-ref", m_rel_single_ref, "至少需要 2 条引用"),
        case("rel-note-null", m_rel_note_null, "note 必须是非空字符串"),
        case("rel-note-number", m_rel_note_number, "note 必须是非空字符串"),
        case("rel-note-list", m_rel_note_list, "note 必须是非空字符串"),
        case("rel-fact-without-constrains", m_rel_fact_without_constrains, "fact 没有 constrains 出边"),
        case("rel-cycle", m_rel_cycle, "subclass_of 存在环"),
        case("guard-no-reason", m_guard_no_reason, "reason 必须是非空字符串"),
        case("guard-bad-status", m_guard_bad_status, "guard.status 只支持 insufficient"),
        case("guard-empty-missing", m_guard_empty_missing, "missing_fields 必须是非空数组"),
        case("guard-duplicate-id", m_guard_duplicate_id, "guard_id 重复"),
        case("guard-field-not-input", m_guard_field_not_input, "未在 input_fields 声明"),
        case("constraint-bad-type", m_constraint_bad_type, "constraint_type 非法"),
        case("constraint-unknown-param", m_constraint_unknown_param, "parameters 含未声明键"),
        case("constraint-no-normative", m_constraint_no_normative, "至少需要 1 条 normative 引用"),
        case("constraint-duplicate-id", m_constraint_duplicate_id, "constraint_id 重复"),
        case("constraint-bad-prefix", m_constraint_bad_prefix, "constraint_id 前缀必须是 O-C-"),
        case("constraint-schema-missing-key", m_constraint_schema_missing_key, "缺少顶层键 decisions"),
        case("constraint-pair-same", m_constraint_pair_same, "不能是同一字段"),
        case("constraint-bad-target-class", m_constraint_bad_target_class, "target_classes 含非法分类"),
        case("constraint-bad-temporal", m_constraint_bad_temporal, "起止倒置"),
        case("claim-bad-id", m_claim_bad_id, "claim_id 必须形如 O-K-001"),
        case("claim-duplicate-id", m_claim_duplicate_id, "claim_id 重复"),
        case("claim-bad-schema", m_claim_bad_schema, "schema_version 必须是 consistency/v1"),
        case("claim-bad-quote", m_claim_bad_quote, "找不到"),
        case("claim-canonical-missing", m_claim_canonical_missing, "canonical 必须是 statements 中的 locator"),
        case("claim-canonical-not-same", m_claim_canonical_not_same, "canonical 对应语句的 effect 必须是 same"),
        case("claim-relation-mismatch", m_claim_relation_mismatch, "与 effect 派生结果"),
        case("claim-different-no-note", m_claim_different_no_note, "必须写 note 说明为何互斥"),
        case("claim-bad-temporal", m_claim_bad_temporal, "temporal.at 必须是 YYYY-MM-DD"),
        case("claim-risk-no-impact", m_claim_risk_no_impact, "note 必须写清影响"),
        case("claim-dangling-fact", m_claim_dangling_fact, "fact_refs 指向不存在的事实"),
        case("claim-refines-not-refines", m_claim_refines_not_refines, "refines_edges 只接受 refines 边"),
        case("claim-coverage-gap", m_claim_coverage_gap, "未覆盖被规则/事实引用的 locator"),
        case("claim-file-missing", m_claim_file_missing, "找不到 knowledge/consistency.json"),
    ]
    print(f"\n{sum(results)}/{len(results)} 负例按预期失败")
    sys.exit(0 if all(results) else 1)
