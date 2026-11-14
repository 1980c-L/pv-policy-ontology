# -*- coding: utf-8 -*-
"""跨文档一致性对照表回归（consistency/v1）。

不依赖 pytest：直接读 knowledge/consistency.json 与规则/事实里的 locator，
在测试内**独立重算**派生关系、覆盖集合与事实覆盖，再对若干关键 claim 做定点断言。
CLI 契约（list / show）也在这里钉住，避免接口被无意改动。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = Path(__file__).resolve().parents[1]
TOOL = PROJ / "tools" / "ontology_tool.py"
DOC_PATH = PROJ / "knowledge" / "consistency.json"
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f" :: {detail}" if detail else ""))


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def collect_locators(obj) -> list[str]:
    """独立实现：递归收集 source_refs.locator_id（故意不 import ontology_tool）。"""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "source_refs" and isinstance(v, list):
                for ref in v:
                    if isinstance(ref, dict) and ref.get("locator_id"):
                        out.append(ref["locator_id"])
            else:
                out.extend(collect_locators(v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(collect_locators(item))
    return out


def derive(effects: list[str]) -> str:
    """独立实现：review 34 §2.2 的派生规则。"""
    if "different" in effects:
        return "conflict"
    if len(effects) == 1:
        return "single_source"
    if any(e.startswith("provincial_") or e == "interpretive" for e in effects):
        return "refined"
    return "same_text"


def cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=CHILD_ENV,
    )
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    if not DOC_PATH.exists():
        print(f"[FAIL] 缺少 {DOC_PATH}")
        return 1
    doc = load_json(DOC_PATH)
    claims = doc.get("claims") or []
    by_id = {c.get("claim_id"): c for c in claims}

    # 1 结构与编号
    check("schema_version=consistency/v1", doc.get("schema_version") == "consistency/v1", doc.get("schema_version"))
    check("共 19 条 claim", len(claims) == 19, str(len(claims)))
    expected_ids = [f"O-K-{i:03d}" for i in range(1, 20)]
    check("claim_id 为 O-K-001..019 且不重不缺", [c.get("claim_id") for c in claims] == expected_ids,
          str([c.get("claim_id") for c in claims]))
    check("key 唯一", len({c.get("key") for c in claims}) == len(claims))

    # 2 派生关系自洽（独立重算）
    bad = []
    for c in claims:
        effects = [s.get("effect") for s in c.get("statements") or []]
        want = derive([e for e in effects if isinstance(e, str)])
        if c.get("relation") != want:
            bad.append((c.get("claim_id"), c.get("relation"), want))
    check("relation 与 effect 派生一致", not bad, str(bad))

    # 3 canonical 必须是 same 语句
    bad = []
    for c in claims:
        canon = c.get("canonical")
        stms = [s for s in c.get("statements") or [] if s.get("locator_id") == canon]
        if not stms or "same" not in [s.get("effect") for s in stms]:
            bad.append(c.get("claim_id"))
    check("canonical ∈ statements 且 effect=same", not bad, str(bad))

    # 4 覆盖门禁（独立重算）：规则与事实引用的 locator 必须全进表
    required: set[str] = set(collect_locators(load_json(PROJ / "knowledge" / "rules.json")))
    for row in load_jsonl(PROJ / "knowledge" / "facts.jsonl"):
        required |= set(collect_locators({"source_refs": row.get("source_refs") or []}))
    covered: set[str] = set()
    for c in claims:
        for s in c.get("statements") or []:
            if s.get("locator_id"):
                covered.add(s["locator_id"])
    check("被引用 locator 共 17 个", len(required) == 17, str(sorted(required)))
    check("覆盖被规则/事实引用的全部 locator", not (required - covered), str(sorted(required - covered)))

    # 5 五条事实全部有 claim 承接
    missed = [f"O-F-{i:03d}" for i in range(1, 6)
              if not any(f"O-F-{i:03d}" in (c.get("fact_refs") or []) for c in claims)]
    check("O-F-001..005 均被 claim 引用", not missed, str(missed))

    # 6 refines_edges 都是现存 refines 边
    rel = {r["relation_id"]: r for r in load_jsonl(PROJ / "knowledge" / "relations.jsonl")}
    bad = []
    for c in claims:
        for eid in c.get("refines_edges") or []:
            if eid not in rel or rel[eid].get("relation") != "refines":
                bad.append((c.get("claim_id"), eid))
    check("refines_edges 只指向现存 refines 边", not bad, str(bad))

    # 7 定点断言：四条核心同文/细化事项
    k1 = by_id["O-K-001"]
    check("K-01 四类划分为 same_text 且含国家第四条与浙江第三条",
          k1["relation"] == "same_text"
          and "PV-NAT-2025-007-ATTACH#clause=第四条" in {s["locator_id"] for s in k1["statements"]}
          and "PV-ZJ-2026-015#clause=第三条" in {s["locator_id"] for s in k1["statements"]})
    k3 = by_id["O-K-003"]
    check("K-03 66kV 差异判 refined 且浙江为 provincial_narrower",
          k3["relation"] == "refined"
          and any(s["effect"] == "provincial_narrower" for s in k3["statements"]))
    k6 = by_id["O-K-006"]
    check("K-06 浙江放宽余电上网判 refined，风险已由 O-R-002 浙江分支承接（risk=none）",
          k6["relation"] == "refined" and k6["risk"] == "none"
          and any(s["effect"] == "provincial_broader" for s in k6["statements"])
          and "zhejiang_large_industrial_two_modes" in k6["note"])
    k11 = by_id["O-K-011"]
    check("K-11 交流侧容量口径为 single_source（仅国家第十三条）",
          k11["relation"] == "single_source" and len(k11["statements"]) == 1
          and k11["statements"][0]["evidence_level"] == "normative")
    k12 = by_id["O-K-012"]
    check("K-12 广东 30% 与浙江无比例判 refined（省级分叉）",
          k12["relation"] == "refined"
          and any(s["effect"] == "provincial_broader" for s in k12["statements"]))
    check("无 conflict（本轮无同级同地同时点的互斥结论）",
          all(c["relation"] != "conflict" for c in claims)
          and not any(s["effect"] == "different" for c in claims for s in c["statements"]))

    # 8 时点三条
    def temporal(k: str) -> dict:
        return by_id[k].get("temporal") or {}
    check("K-17 广东试行期 2028-02-09 到期",
          temporal("O-K-017").get("kind") == "expiring" and temporal("O-K-017").get("at") == "2028-02-09",
          str(temporal("O-K-017")))
    check("K-18 浙江细则 2030-01-16 到期",
          temporal("O-K-018").get("kind") == "expiring" and temporal("O-K-018").get("at") == "2030-01-16",
          str(temporal("O-K-018")))
    check("K-19 国家五年期 2030-01-23 届满且标 expiry_unmodeled",
          temporal("O-K-019").get("kind") == "expiring"
          and temporal("O-K-019").get("at") == "2030-01-23"
          and by_id["O-K-019"]["risk"] == "expiry_unmodeled",
          str(temporal("O-K-019")))

    # 9 风险标注入表（登记不改判定的那几条）
    risks = {c["claim_id"]: c["risk"] for c in claims if c["risk"] != "none"}
    want_risks = {
        "O-K-015": "rule_uncovered",
        "O-K-016": "rule_uncovered", "O-K-019": "expiry_unmodeled",
    }
    check("risk 标注集合与登记清单一致", risks == want_risks, f"{risks} vs {want_risks}")
    check("risk≠none 的 note 都写了「影响」",
          all("影响" in c["note"] for c in claims if c["risk"] != "none"))

    # 10 CLI 契约
    rc, out, err = cli("consistency", "list")
    check("consistency list 退出码 0", rc == 0, err[-300:])
    check("consistency list 打印 19 行", len([l for l in out.splitlines() if l.startswith("O-K-")]) == 19)
    rc, out, err = cli("consistency", "list", "--json")
    ok = False
    try:
        payload = json.loads(out)
        ok = (rc == 0 and len(payload.get("claims") or []) == 19
              and {"claim_id", "key", "label", "relation", "risk", "temporal", "statements"}
              <= set(payload["claims"][0]))
    except json.JSONDecodeError:
        ok = False
    check("consistency list --json 结构稳定", ok, out[:300])
    rc, out, err = cli("consistency", "show", "O-K-001")
    ok = False
    try:
        ok = rc == 0 and json.loads(out).get("claim_id") == "O-K-001"
    except json.JSONDecodeError:
        ok = False
    check("consistency show 支持 claim_id", ok, out[:300])
    rc, out, err = cli("consistency", "show", k1["key"])
    ok = False
    try:
        ok = rc == 0 and json.loads(out).get("claim_id") == "O-K-001"
    except json.JSONDecodeError:
        ok = False
    check("consistency show 支持 key", ok, out[:300])
    rc, _out, _err = cli("consistency", "show", "O-K-999")
    check("consistency show 未知 claim 失败", rc != 0, str(rc))

    print(f"\n{PASS}/{PASS + FAIL} 断言通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
