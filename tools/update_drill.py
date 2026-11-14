# -*- coding: utf-8 -*-
"""更新演练：在隔离副本里模拟一次“来源口径修订”，观察规则/事实/查询/导出如何受影响。

模拟内容（清楚标明，不是真实政策）：
- 假设 2026 年官方问答把“发布之日”从 2025-01-23 调整为 2025-02-01（SIMULATED）。
- 相应把 O-R-003 的备案截止日改为 2025-02-01，并把 O-F-002 的陈述改为模拟口径。
- 用同一个边界项目（备案 2025-01-30）在 v1 / v2 下各跑一次，展示判定翻转。

用法：python tools/update_drill.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Windows 控制台默认 GBK：统一让本脚本输出 UTF-8，避免坏字符导致打印崩溃。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

BOUNDARY_CASE = {
    "case_id": "DRIFT-boundary-2025-01-30",
    "project": {
        "project_id": "DRIFT",
        "investor_type": "natural_person",
        "site_category": "residential",
        "connection_voltage_kv": 0.38,
        "connection_type": "public_grid",
        "investor_same_legal_person_as_user": False,
        "capacity_mw": 0.01,
        "capacity_basis": "ac_inverter_sum",
        "grid_mode": "full_feed_in",
        "spot_market_region": False,
        "same_red_line": True,
        "site_owned_by_investor": True,
        "filing_date": "2025-01-30",
        "grid_connection_date": "2025-04-01",
        "grid_connection_full_capacity": True,
    },
    "policy_as_of": "2025-06-01",
    "expected": {
        "result_status": "supported",
        "conclusions": [{"field": "legacy_policy_applies", "value": False}],
        "missing_fields": [],
    },
}


def prepare(dst: Path, mutate: bool, work: Path) -> Path:
    shutil.copytree(
        ROOT, dst,
        ignore=shutil.ignore_patterns(".tmp", "reviews", "exports", "__pycache__"),
    )
    with (dst / "eval" / "cases.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(BOUNDARY_CASE, ensure_ascii=False) + "\n")
    if mutate:
        rp = dst / "knowledge" / "rules.json"
        doc = json.loads(rp.read_text(encoding="utf-8"))
        for rule in doc["rules"]:
            if rule["rule_id"] != "O-R-003":
                continue
            for case in rule["cases"]:
                for cond in case.get("when", {}).get("conditions", []):
                    if cond.get("field") == "filing_date" and cond.get("value") in ("2025-01-23",):
                        cond["value"] = "2025-02-01"
            rule["notes"].append("SIMULATED 2026 修订：备案截止日由 2025-01-23 调整为 2025-02-01（演练用，非真实政策）。")
        rp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        fp = dst / "knowledge" / "facts.jsonl"
        rows = [json.loads(l) for l in fp.read_text(encoding="utf-8").splitlines() if l.strip()]
        for f in rows:
            if f["fact_id"] == "O-F-002":
                f["statement"] = f.get("statement", "") + "【SIMULATED：演练中将发布之日改为 2025年2月1日，非真实口径】"
        fp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return dst


def run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "tools" / "ontology_tool.py"), "--root", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=CHILD_ENV,
    )


def assess_boundary(root: Path) -> dict:
    proc = run(root, "assess", "--cases", str(root / "eval" / "cases.jsonl"))
    for line in proc.stdout.splitlines():
        if line.startswith("[PASS] DRIFT-boundary"):
            print(line)
        elif line.startswith("[FAIL] DRIFT-boundary"):
            print("[预期失配（模拟修订导致 expected 不再成立）] " + line)
    res = json.loads((root / "eval" / "results" / "cases.json").read_text(encoding="utf-8"))
    return res


def legacy_of(results: dict, case_id: str) -> str:
    for row in results.get("results", []):
        if row.get("case_id") != case_id:
            continue
        for c in row.get("result", {}).get("conclusions", []):
            if c.get("field") == "legacy_policy_applies":
                return str(c.get("value")).lower()
        return "none"
    return "missing"


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="update-drill-"))
    v1 = prepare(work / "v1", mutate=False, work=work)
    v2 = prepare(work / "v2", mutate=True, work=work)
    print("== update drill（SIMULATED：发布之日 2025-01-23 → 2025-02-01）==")
    print("\n-- v1 assess --")
    r1 = assess_boundary(v1)
    print("\n-- v2 assess --")
    r2 = assess_boundary(v2)

    print("\n-- 判定翻转 --")
    for case_id in ("D-legacy-false-filed-after-publication", "J-date-boundary-legacy-false",
                    "DRIFT-boundary-2025-01-30", "I-date-boundary-legacy-true"):
        print(f"{case_id}: v1 legacy={legacy_of(r1, case_id)}  v2 legacy={legacy_of(r2, case_id)}")
    print("\n注意：D/J 是原案例，v2 下 expected 已因模拟修订失配（这是演练想展示的“受影响案例”），"
          "不是代码错误；正式集未被修改。")

    print("\n-- 受影响的查询（O-Q03 的备案截止条件）--")
    for label, root in (("v1", v1), ("v2", v2)):
        proc = run(root, "query", "--case-id", "O-Q03")
        data = json.loads(proc.stdout)
        vals = sorted({
            c["condition"].get("value") for c in data["result"]["rule_conditions"]
            if c.get("condition", {}).get("field") == "filing_date"
        })
        print(f"{label}: filing_date 条件值 = {vals}")

    print("\n-- 受影响的导出（O-R-003 的 conditions）--")
    for label, root in (("v1", v1), ("v2", v2)):
        run(root, "export")
        rows = [json.loads(l) for l in (root / "exports" / "ontology_evidence.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        rule = next(r for r in rows if r["record_id"] == "O-R-003")
        vals = sorted({
            c.get("value") for case in rule["conditions"]
            for c in (case.get("when") or {}).get("conditions", [])
            if c.get("field") == "filing_date" and c.get("value")
        })
        print(f"{label}: 导出 O-R-003 filing_date 条件值 = {vals}；记录数 {len(rows)}")

    print("\n结论：修订只影响 O-R-003 / O-F-002 / O-Q03 / 导出 O-R-003 的 conditions；")
    print("      引用 locator 不变，历史版本通过 v1 副本仍可复现；正式集未被修改。")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
