# -*- coding: utf-8 -*-
"""冲突演练：在隔离副本里注入一条“模拟冲突规则”，验证 assess 会保留 conflict。

说明：
- 只改副本，不动本项目 knowledge/rules.json。
- O-R-099 是清楚标明的模拟事实（simulated conflict），不是真实政策矛盾。
- 用法：python tools/conflict_drill.py
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

# 与 O-R-001 自然人户用同样的输入，但给出“一般工商业”的相反结论
SIM_RULE = {
    "rule_id": "O-R-099",
    "title": "【模拟冲突】自然人户用 vs 一般工商业（演练用，非真实政策）",
    "knowledge_kind": "derived",
    "rule_status": "draft",
    "input_fields": ["investor_type", "site_category", "connection_voltage_kv"],
    "cases": [
        {
            "case_id": "sim_conflict_natural",
            "label": "模拟：把自然人户用判成一般工商业",
            "when": {
                "logic": "all",
                "conditions": [
                    {"predicate": "equals", "field": "investor_type", "value": "natural_person"},
                    {"predicate": "equals", "field": "site_category", "value": "residential"},
                    {"predicate": "lte", "field": "connection_voltage_kv", "value": 0.38},
                ],
            },
            "then": {"conclusions": [{"field": "project_class", "value": "general_industrial_business"}]},
        }
    ],
    "default": {"status": "insufficient", "conclusions": [], "reason": "演练规则，不参与真实判定。"},
    "exceptions": ["SIMULATED：本条仅用于验证 conflict 分支，不得写入正式规则集。"],
    "notes": ["模拟冲突演练。"],
    "source_refs": [
        {
            "source_id": "PV-NAT-2025-007-ATTACH",
            "locator_id": "PV-NAT-2025-007-ATTACH#clause=第四条",
            "kind": "clause",
            "value": "第四条",
            "quote": "分布式光伏发电分为自然人户用、非自然人户用、一般工商业和大型工商业四种类型",
            "evidence_level": "normative",
        }
    ],
    "temporal_scope": {"effective_from": "2025-01-23", "effective_to": None},
}

PROJECT = {
    "project_id": "DRILL-CONFLICT",
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
    "filing_date": "2024-12-01",
    "grid_connection_date": "2025-04-01",
    "grid_connection_full_capacity": True,
}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="conflict-drill-"))
    copy = work / "proj"
    shutil.copytree(
        ROOT, copy,
        ignore=shutil.ignore_patterns(".tmp", "reviews", "exports", "__pycache__", "eval-results"),
    )
    rules_path = copy / "knowledge" / "rules.json"
    doc = json.loads(rules_path.read_text(encoding="utf-8"))
    doc["rules"].append(SIM_RULE)
    rules_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    project_path = copy / "eval" / "drill_project.json"
    project_path.write_text(json.dumps(PROJECT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(copy / "tools" / "ontology_tool.py"), "--root", str(copy),
         "assess", "--project", str(project_path), "--policy-as-of", "2025-06-01"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=CHILD_ENV,
    )
    print("== conflict drill ==")
    print(proc.stdout.strip()[:2000])
    if proc.returncode != 0:
        print("STDERR:", proc.stderr.strip()[:1000])
        return 1
    if '"result_status": "conflict"' not in proc.stdout:
        print("FAIL: 未出现 conflict")
        return 1
    print("PASS: assess 保留 conflict（模拟规则 O-R-099，非真实政策矛盾）")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
