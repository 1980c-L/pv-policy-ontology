# -*- coding: utf-8 -*-
"""多项目约束回归（constraints/v1）：固定组 + 结构负例 + CLI 接口。"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from constraint_eval import GroupValidationError, check_group  # noqa: E402

GROUPS = ROOT / "eval" / "constraint_groups.jsonl"
TOOL = ROOT / "tools" / "ontology_tool.py"

EXPECTED = {
    "G-01-ancillary-same": ("satisfied", {"O-C-001": ("satisfied", "same_red_line_confirmed")}),
    "G-02-ancillary-different": ("violated", {"O-C-001": ("violated", "same_red_line_required")}),
    "G-03-account-must-merge": ("violated", {"O-C-003": ("violated", "must_merge")}),
    "G-04-account-may-separate": ("satisfied", {"O-C-003": ("satisfied", "may_separate")}),
    "G-05-account-missing": ("insufficient", {"O-C-003": ("insufficient", "insufficient_information")}),
    "G-06-account-no-redline": ("insufficient", {"O-C-003": ("insufficient", "insufficient_information")}),
    "G-07-substation-same": ("satisfied", {"O-C-002": ("satisfied", "same_red_line_confirmed")}),
    "G-08-substation-exception": ("not_applicable", {"O-C-002": ("not_applicable", "dedicated_line_exception")}),
    "G-09-substation-violate": ("violated", {"O-C-002": ("violated", "same_red_line_required")}),
    "G-10-substation-missing-conn": ("insufficient", {"O-C-002": ("insufficient", "insufficient_information")}),
    "G-11-connection-new": ("violated", {"O-C-004": ("violated", "no_new_connection_point")}),
    "G-12-connection-same": ("satisfied", {"O-C-004": ("satisfied", "existing_point_shared")}),
    "G-13-connection-no-anchor": ("insufficient", {"O-C-004": ("insufficient", "insufficient_information")}),
    "G-14-connection-same-investor": ("satisfied", {"O-C-004": ("not_applicable", "not_applicable")}),
    "G-15-site-conflict": ("conflict", {"O-C-001": ("conflict", "conflict")}),
    "G-16-connection-non-industrial": ("satisfied", {"O-C-004": ("not_applicable", "not_applicable")}),
    "G-17-connection-no-diff-info": ("insufficient", {"O-C-004": ("insufficient", "insufficient_information")}),
    "G-18-substation-large-violate": ("violated", {"O-C-002": ("violated", "same_red_line_required")}),
    "G-19-site-pair-missing": ("insufficient", {"O-C-001": ("insufficient", "insufficient_information")}),
    "G-20-single-project": ("not_applicable", {}),
    "G-21-account-no-redline-one-missing": ("insufficient", {"O-C-003": ("insufficient", "insufficient_information")}),
    "G-22-account-mixed": ("violated", {"O-C-003": ("violated", "must_merge")}),
    "G-23-account-subset-missing": ("violated", {"O-C-003": ("violated", "must_merge")}),
    "G-24-account-subset-missing-redline": ("violated", {"O-C-003": ("violated", "must_merge")}),
    "G-25-connection-missing-plus-new": ("violated", {"O-C-004": ("violated", "no_new_connection_point")}),
    "G-26-connection-unclassified-plus-new": ("violated", {"O-C-004": ("violated", "no_new_connection_point")}),
    "G-27-connection-cross-redline-map": ("satisfied", {"O-C-004": ("satisfied", "existing_point_shared")}),
    "G-28-connection-multi-anchor": ("satisfied", {"O-C-004": ("satisfied", "existing_point_shared")}),
    "G-29-ancillary-flag-false": ("satisfied", {"O-C-001": ("satisfied", "same_red_line_confirmed")}),
    "G-30-connection-two-industrial-one-residential": ("violated", {"O-C-004": ("violated", "no_new_connection_point")}),
    "G-31-connection-cross-redline-partial-anchor": ("insufficient", {"O-C-004": ("insufficient", "insufficient_information")}),
}


def load_groups() -> list[dict]:
    return [json.loads(line) for line in GROUPS.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    failures: list[str] = []
    passed = 0
    groups = {g["group_id"]: g for g in load_groups()}

    def ok(cond: bool, msg: str) -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(msg)

    ok(set(groups) == set(EXPECTED), f"组集合不一致: {sorted(set(groups) ^ set(EXPECTED))}")
    for gid, (want_group, want_constraints) in EXPECTED.items():
        group = groups.get(gid)
        if group is None:
            failures.append(f"{gid}: 组不存在")
            continue
        result = check_group(group)
        ok(result["result_status"] == want_group,
           f"{gid}: result_status={result['result_status']} != {want_group}")
        got = {c["constraint_id"]: (c["status"], c["decision"]) for c in result["constraints"]}
        for cid, want in want_constraints.items():
            ok(got.get(cid) == want, f"{gid}.{cid}: {got.get(cid)} != {want}")
        ok(result["schema_version"] == "constraints/v1", f"{gid}: schema_version 错")
        ok(len(result["constraints"]) == 4, f"{gid}: 约束条数不是 4")
        ok(result["summary"]["total"] == 4, f"{gid}: summary.total 错")
        ok(result["summary"][result["result_status"]] >= 1,
           f"{gid}: summary 未计当前结果 {result['result_status']}")

    # 缺字段必须点名到成员，而不是只给空结论
    r5 = check_group(groups["G-05-account-missing"])
    c5 = next(c for c in r5["constraints"] if c["constraint_id"] == "O-C-003")
    ok("P-05B.electricity_account_id" in c5["missing_fields"], "G-05: 缺失户号未点名")
    ok(c5["status"] == "insufficient" and c5["decision"] == "insufficient_information",
       "G-05: 无重复户号时应判 insufficient")
    ok(set(c5["affected_project_ids"]) == {"P-05B"}, "G-05: insufficient 时 affected 应恰为缺字段成员")
    r19 = check_group(groups["G-19-site-pair-missing"])
    ok("P-19.ancillary_site_red_line_id" in r19["missing_fields"], "G-19: 缺失附属场所红线未点名")
    r17 = check_group(groups["G-17-connection-no-diff-info"])
    ok("P-17A.investor_id" in r17["missing_fields"], "G-17: 缺失投资主体未点名")

    # B-01 复核：混合户号必须只把重复子集判为必须合并
    r22 = check_group(groups["G-22-account-mixed"])
    c22 = next(c for c in r22["constraints"] if c["constraint_id"] == "O-C-003")
    ok(set(c22["affected_project_ids"]) == {"P-22A", "P-22B"}, "G-22: 受影响成员应恰为 A/A，B 可独立")
    ok(any("ACC-A" in x and "P-22C" not in x for x in c22["reasons"]), "G-22: 理由应点名 ACC-A 子集且不含 B")

    # B-02 复核：缺字段单列，不能掩盖已能判定的 must_merge / 新增连接点
    r23 = check_group(groups["G-23-account-subset-missing"])
    c23 = next(c for c in r23["constraints"] if c["constraint_id"] == "O-C-003")
    ok(c23["status"] == "violated" and c23["decision"] == "must_merge", "G-23: 缺户号不应掩盖 A/A 的 must_merge")
    ok("P-23C.electricity_account_id" in c23["missing_fields"], "G-23: 缺户号应单列")
    ok(set(c23["affected_project_ids"]) == {"P-23A", "P-23B"},
       "G-23: must_merge 时 affected 应恰为 A/A，缺户号的 C 不得混入")
    r24 = check_group(groups["G-24-account-subset-missing-redline"])
    c24 = next(c for c in r24["constraints"] if c["constraint_id"] == "O-C-003")
    ok(c24["status"] == "violated" and c24["decision"] == "must_merge", "G-24: 缺红线不应掩盖 A/A 的 must_merge")
    ok("P-24C.red_line_id" in c24["missing_fields"], "G-24: 缺红线应单列")
    ok(set(c24["affected_project_ids"]) == {"P-24A", "P-24B"},
       "G-24: must_merge 时 affected 应恰为 A/A，缺红线的 C 不得混入")
    r25 = check_group(groups["G-25-connection-missing-plus-new"])
    c25 = next(c for c in r25["constraints"] if c["constraint_id"] == "O-C-004")
    ok(c25["status"] == "violated" and "P-25A" in c25["affected_project_ids"], "G-25: 缺连接点不应掩盖 CP-NEW 新增")
    ok("P-25C.grid_connection_point_id" in c25["missing_fields"], "G-25: 缺连接点应单列")
    r26 = check_group(groups["G-26-connection-unclassified-plus-new"])
    c26 = next(c for c in r26["constraints"] if c["constraint_id"] == "O-C-004")
    ok(set(c26["affected_project_ids"]) == {"P-26A"}, "G-26: unclassified 不参与，工商业子集只报 P-26A")
    ok(c26["missing_fields"] == [], "G-26: 已判定的违规不应产生空泛缺字段")

    # B-03 复核：锚点按红线取，不得跨红线复用
    r27 = check_group(groups["G-27-connection-cross-redline-map"])
    c27 = next(c for c in r27["constraints"] if c["constraint_id"] == "O-C-004")
    ok(c27["status"] == "satisfied", "G-27: 两条红线各自锚点应分别满足")
    ok(len(c27["reasons"]) == 2 and all("未新增" in x for x in c27["reasons"]), "G-27: 两条红线都应给出满足理由")
    r28 = check_group(groups["G-28-connection-multi-anchor"])
    c28 = next(c for c in r28["constraints"] if c["constraint_id"] == "O-C-004")
    ok(c28["status"] == "satisfied", "G-28: 同红线多个既有点应可用数组表达")
    r31 = check_group(groups["G-31-connection-cross-redline-partial-anchor"])
    c31 = next(c for c in r31["constraints"] if c["constraint_id"] == "O-C-004")
    ok(c31["status"] == "insufficient", "G-31: 缺另一红线锚点应判 insufficient 而非 violated")
    ok(c31["missing_fields"] == ["group.existing_connection_points[RL-Y2]"], "G-31: 缺锚点应点名具体红线")
    ok(set(c31["affected_project_ids"]) == {"P-31C", "P-31D"}, "G-31: 只应影响缺锚点红线的成员")

    # B-04 复核：复合 same_red_line=false 不构成 pair 矛盾
    r29 = check_group(groups["G-29-ancillary-flag-false"])
    c29 = next(c for c in r29["constraints"] if c["constraint_id"] == "O-C-001")
    ok(c29["status"] == "satisfied" and c29["decision"] == "same_red_line_confirmed",
       "G-29: same_red_line=false 但两个 ID 相同应 satisfied")
    ok(r29["result_status"] == "satisfied", "G-29: 合法自用电不应因复合字段判 conflict")

    # 结构负例
    base = copy.deepcopy(groups["G-20-single-project"])
    cases = []
    bad = copy.deepcopy(base); bad["members"][0]["project_class"] = "general_industrial_business"
    cases.append(("注入派生字段", bad))
    bad = copy.deepcopy(base); bad["members"].append(copy.deepcopy(base["members"][0]))
    cases.append(("project_id 重复", bad))
    bad = copy.deepcopy(base); bad["members"] = [copy.deepcopy(base["members"][0]) for _ in range(21)]
    for i, m in enumerate(bad["members"]):
        m["project_id"] = f"P-{i}"
    cases.append(("超过 20 人", bad))
    bad = copy.deepcopy(base); bad["policy_as_of"] = "2026/01/01"
    cases.append(("组日期格式错", bad))
    bad = copy.deepcopy(base); del bad["group_id"]
    cases.append(("缺 group_id", bad))
    bad = copy.deepcopy(base); bad["label"] = 123
    cases.append(("label 非字符串", bad))
    bad = copy.deepcopy(base); bad["existing_connection_points"] = ["CP-A"]
    cases.append(("existing_connection_points 非对象", bad))
    bad = copy.deepcopy(base); bad["existing_connection_points"] = {"RL-A": []}
    cases.append(("existing_connection_points 空数组", bad))
    for name, bad_group in cases:
        try:
            check_group(bad_group)
        except GroupValidationError:
            ok(True, "")
        else:
            failures.append(f"结构负例未拦截: {name}")

    # CLI 接口：list/show/check 的 JSON 与退出码
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(TOOL), *args], cwd=str(ROOT),
                              capture_output=True, text=True, encoding="utf-8")

    p = run("constraints", "list", "--json")
    ok(p.returncode == 0, f"CLI list exit={p.returncode}")
    try:
        ok(len(json.loads(p.stdout)["constraints"]) == 4, "CLI list 条数不是 4")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"CLI list JSON 解析失败: {exc}")
    p = run("constraints", "show", "O-C-001")
    ok(p.returncode == 0 and '"O-C-001"' in p.stdout, f"CLI show O-C-001 exit={p.returncode}")
    p = run("constraints", "show", "O-C-999")
    ok(p.returncode == 1, f"CLI show 未知 ID 应 exit 1，实际 {p.returncode}")

    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "group.json"
        good.write_text(json.dumps(groups["G-01-ancillary-same"], ensure_ascii=False), encoding="utf-8")
        p = run("constraints", "check", "--group", str(good), "--json")
        ok(p.returncode == 0 and '"constraints/v1"' in p.stdout, f"CLI check exit={p.returncode or p.stderr}")
        bad = Path(td) / "bad.json"
        bad.write_text(json.dumps(cases[0][1], ensure_ascii=False), encoding="utf-8")
        p = run("constraints", "check", "--group", str(bad), "--json")
        ok(p.returncode == 2, f"CLI check 非法组应 exit 2，实际 {p.returncode}")

    total = passed + len(failures)
    print(f"constraint tests: {passed}/{total} passed")
    for f in failures:
        print("  FAIL", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
