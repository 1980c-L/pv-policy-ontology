# -*- coding: utf-8 -*-
"""真实问法回归：19 条大白话问题，检查路由、结论状态、关键表述与首条引用。

边界：
- 不带 project 跑，锁的是“回答层”行为（范围声明/省级要点/口径模板）与路由；
- 结构化判定由 eval/cases.jsonl（22 条）与 scenario_tests.py 覆盖，这里不重复判规则；
- must_include 是子串断言，只防“答非所问”回退，不评价文风。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import ask  # noqa: E402

EXPECT = ROOT / "eval" / "real_question_expect.jsonl"
QUESTIONS = ROOT / "eval" / "real_questions.jsonl"
PROJECTS = ROOT / "eval" / "real_question_projects.jsonl"


def load(path: Path) -> dict[str, dict]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["id"]] = rec
    return out


def check(exp: dict, got: dict) -> list[str]:
    problems: list[str] = []
    ans = got["answer"]
    if got["scenario"] != exp["scenario"]:
        problems.append(f"scenario {got['scenario']} != {exp['scenario']}")
    if ans["verdict"] != exp["verdict"]:
        problems.append(f"verdict {ans['verdict']} != {exp['verdict']}")
    text = " ".join([ans["summary"]] + ans["points"])
    for word in exp.get("must_include", []):
        if word not in text:
            problems.append(f"缺少表述：{word}")
    prefix = exp.get("top1_prefix")
    if prefix:
        top = ans["source_refs"][0]["locator_id"] if ans["source_refs"] else ""
        if not top.startswith(prefix):
            problems.append(f"首条引用 {top or '-'} 不以 {prefix} 开头")
    return problems


def check_project(rec: dict, questions: dict[str, dict]) -> list[str]:
    qid = rec["id"]
    project = questions[qid]["project"] if rec.get("inherit_project") else rec["project"]
    got = ask.ask(questions[qid]["question"], project=project)
    ans = got["answer"]
    problems: list[str] = []
    if ans["verdict"] != rec["verdict"]:
        problems.append(f"verdict {ans['verdict']} != {rec['verdict']}")
    text = " ".join([ans["summary"]] + ans["points"])
    for word in rec.get("must_include", []):
        if word not in text:
            problems.append(f"缺少表述：{word}")
    return problems


def run_projects(questions: dict[str, dict]) -> tuple[int, int]:
    if not PROJECTS.exists():
        return 0, 0
    passed = failed = 0
    for line in PROJECTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        problems = check_project(rec, questions)
        if problems:
            failed += 1
            print(f"[FAIL] {rec['id']}（带 project）" + "；".join(problems))
        else:
            passed += 1
    print(f"真实问法 + 项目字段抽查：{passed}/{passed + failed} 通过")
    return passed, failed


def main() -> int:
    expect = load(EXPECT)
    questions = load(QUESTIONS)
    passed = failed = 0
    for qid in sorted(questions):
        exp = expect.get(qid)
        if not exp:
            print(f"[FAIL] {qid} 没有预期记录")
            failed += 1
            continue
        got = ask.ask(questions[qid]["question"])
        problems = check(exp, got)
        if problems:
            failed += 1
            print(f"[FAIL] {qid} " + "；".join(problems))
        else:
            passed += 1
    print(f"真实问法回归：{passed}/{passed + failed} 通过")
    p_pass, p_fail = run_projects(questions)
    total_pass, total_fail = passed + p_pass, failed + p_fail
    print(f"合计：{total_pass}/{total_pass + total_fail} 通过")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
