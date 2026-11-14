# -*- coding: utf-8 -*-
"""三个功能场景的验收测试：分类 / 上网模式 / 新老划断。

每个场景 3 条，检查：
- 规则结论（result_status、project_class、allowed_grid_modes、legacy_policy_applies）
- 缺字段清单
- 回答必须带 ≥3 条可核对证据（locator + quote）

运行：python tools/scenario_tests.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ask as ask_mod  # noqa: E402


def conclusion(assessment: dict, field: str):
    for c in (assessment or {}).get("conclusions", []):
        if c.get("field") == field:
            return c.get("value")
    return None


def check(case: dict) -> tuple[bool, list[str]]:
    out = ask_mod.ask(case["question"], project=case["project"], policy_as_of=case.get("policy_as_of"))
    assessment = out.get("assessment") or {}
    expected = case["expected"]
    problems: list[str] = []

    if assessment.get("result_status") != expected.get("result_status"):
        problems.append(f"result_status={assessment.get('result_status')} 期望 {expected.get('result_status')}")
    if "project_class" in expected:
        got = conclusion(assessment, "project_class")
        if got != expected["project_class"]:
            problems.append(f"project_class={got} 期望 {expected['project_class']}")
    if "allowed_grid_modes" in expected:
        got = conclusion(assessment, "allowed_grid_modes")
        if got != expected["allowed_grid_modes"]:
            problems.append(f"allowed_grid_modes={got} 期望 {expected['allowed_grid_modes']}")
    if "legacy_policy_applies" in expected:
        got = conclusion(assessment, "legacy_policy_applies")
        if got != expected["legacy_policy_applies"]:
            problems.append(f"legacy_policy_applies={got} 期望 {expected['legacy_policy_applies']}")
    if "missing_fields" in expected and assessment.get("missing_fields") != expected["missing_fields"]:
        problems.append(f"missing={assessment.get('missing_fields')} 期望 {expected['missing_fields']}")

    evidence = out["retrieval"]["evidence"]
    if len(evidence) < 3:
        problems.append(f"证据只有 {len(evidence)} 条（要求 ≥3）")
    for e in evidence:
        if not e.get("locator_id") or not e.get("quote"):
            problems.append(f"证据缺 locator/quote：{e}")
            break

    return (not problems), problems


def main() -> int:
    cases = [json.loads(l) for l in (ROOT / "eval" / "scenarios.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    bad = 0
    for case in cases:
        ok, problems = check(case)
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {case['scenario_id']} {case['scenario']}: {case['question']}")
        for p in problems:
            print("       -", p)
        bad += 0 if ok else 1
    print(f"\n{len(cases) - bad}/{len(cases)} 场景通过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
