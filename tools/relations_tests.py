# -*- coding: utf-8 -*-
"""关系层回归：分类祖先、影响面口径、删除边后的能力变化。

不依赖 pytest；直接调用 ontology_tool.py，对当前项目跑断言。
删除测试在 .tmp/reltests 的副本里做，不改项目文件。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = Path(__file__).resolve().parents[1]
TOOL = PROJ / "tools" / "ontology_tool.py"
BASE = PROJ / ".tmp" / "reltests"
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


def run(root: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), "relations", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=CHILD_ENV,
    )
    return proc.returncode, proc.stdout, proc.stderr


def json_cmd(root: Path, *args: str) -> dict:
    rc, out, err = run(root, *args, "--json")
    if rc != 0:
        raise RuntimeError(f"命令失败 rc={rc}: {' '.join(args)}\n{out}\n{err}")
    return json.loads(out)


def copy_project(name: str) -> Path:
    dst = BASE / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        PROJ, dst,
        ignore=shutil.ignore_patterns(".tmp", ".git", "reviews", "exports", "__pycache__"),
    )
    return dst


def remove_edge(root: Path, relation_id: str) -> None:
    p = root / "knowledge" / "relations.jsonl"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if r.get("relation_id") != relation_id]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def baseline() -> None:
    got = json_cmd(PROJ, "ancestors", "O-E-005")["ancestors"]
    check("CQ5 大型工商业的分类上级是分布式光伏", got == ["O-E-001"], str(got))
    for eid in ("O-E-002", "O-E-003", "O-E-004"):
        got = json_cmd(PROJ, "ancestors", eid)["ancestors"]
        check(f"CQ5 {eid} 的分类上级是 O-E-001", got == ["O-E-001"], str(got))

    zj4 = json_cmd(PROJ, "impact", "PV-ZJ-2026-015#clause=第四条")
    check("impact JSON 版本为 relations/v1", zj4.get("schema_version") == "relations/v1", str(zj4.get("schema_version")))
    check("浙江第四条：直接受影响含 O-R-001（66kV 分支）、O-R-002（浙江大型余电上网分支）与 O-R-004",
          set(zj4["direct"]) == {"O-R-001", "O-R-002", "O-R-004"}, str(zj4["direct"]))
    check("浙江第四条：O-R-002 已是直接引用，不再计入候选", zj4["related_rules"] == [], str(zj4["related_rules"]))
    check("浙江第四条：直接引用来自 source_refs，不是 refines 候选", "O-R-002" in zj4["direct"])

    gd2 = json_cmd(PROJ, "impact", "PV-GD-2025-002#section=二")
    check("广东第二节：直接 O-R-004 / 候选 O-R-002", gd2["direct"] == ["O-R-004"] and gd2["related_rules"] == ["O-R-002"], str(gd2))

    nat5 = json_cmd(PROJ, "impact", "PV-NAT-2025-007-ATTACH#clause=第五条")
    check("国家第五条：直接 O-R-002 / 候选含 O-R-001（浙江第四条粒度）与 O-R-004", nat5["direct"] == ["O-R-002"] and set(nat5["related_rules"]) == {"O-R-001", "O-R-004"}, str(nat5))
    check("国家第五条：下级含浙江第四条与广东第二节", set(nat5["child_clauses"]) == {
        "PV-ZJ-2026-015#clause=第四条", "PV-GD-2025-002#section=二",
    }, str(nat5["child_clauses"]))

    nat4 = json_cmd(PROJ, "impact", "PV-NAT-2025-007-ATTACH#clause=第四条")
    check("国家第四条：直接 O-R-001", nat4["direct"] == ["O-R-001"], str(nat4["direct"]))
    check("国家第四条：下级含浙江第五条、广东第一节（浙江第三条复述已删边）", set(nat4["child_clauses"]) == {
        "PV-ZJ-2026-015#clause=第五条",
        "PV-GD-2025-002#section=一",
    }, str(nat4["child_clauses"]))

    fact_expect = {
        "O-F-001": "O-R-001",
        "O-F-002": "O-R-003",
        "O-F-003": "O-R-003",
        "O-F-004": "O-R-002",
        "O-F-005": "O-R-001",
    }
    for fid, rid in fact_expect.items():
        got = json_cmd(PROJ, "impact", fid)["direct"]
        check(f"事实 {fid} 只约束到 {rid}", got == [rid], str(got))


def deletion_tests() -> None:
    root = copy_project("del-006")
    remove_edge(root, "O-REL-006")
    got = json_cmd(root, "impact", "PV-ZJ-2026-015#clause=第四条")
    check("删 006：浙江第四条 direct 仍含三条规则（含 O-R-002 的直接引用）",
          set(got["direct"]) == {"O-R-001", "O-R-002", "O-R-004"}, str(got["direct"]))
    check("删 006：浙江第四条 related 变空（证明该边曾是候选来源）", got["related_rules"] == [], str(got["related_rules"]))

    root = copy_project("del-009")
    remove_edge(root, "O-REL-009")
    got = json_cmd(root, "impact", "PV-GD-2025-002#section=二")
    check("删 009：广东第二节 related 变空", got["related_rules"] == [], str(got["related_rules"]))

    root = copy_project("del-008")
    remove_edge(root, "O-REL-008")
    got = json_cmd(root, "impact", "O-F-005")["direct"]
    check("删 008：O-F-005 直接影响面为空", got == [], str(got))

    root = copy_project("del-010")
    remove_edge(root, "O-REL-010")
    got = json_cmd(root, "impact", "O-F-001")["direct"]
    check("删 010：O-F-001 直接影响面为空", got == [], str(got))

    root = copy_project("del-013")
    remove_edge(root, "O-REL-013")
    got = json_cmd(root, "impact", "O-F-004")["direct"]
    check("删 013：O-F-004 直接影响面为空", got == [], str(got))

    root = copy_project("del-004")
    remove_edge(root, "O-REL-004")
    got = json_cmd(root, "ancestors", "O-E-005")["ancestors"]
    check("删 004：大型工商业没有分类上级（证明该边是 CQ5 的必要条件）", got == [], str(got))


def main() -> int:
    BASE.mkdir(parents=True, exist_ok=True)
    try:
        baseline()
        deletion_tests()
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
    print(f"\n{PASS}/{PASS + FAIL} 关系层回归通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
