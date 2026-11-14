#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 里程碑的最小 assess 实现：按 schema 谓词白名单解释 rules.json。

这不是通用规则引擎，只执行本项目规则用到的受限谓词（比较 / 布尔 / 缺值 /
日期比较），不执行任意代码；规则求值只读 model/schema.json 与 knowledge/rules.json。

用法（项目根执行）:
    python src/rule_eval.py --cases eval/cases.jsonl
    python src/rule_eval.py --project eval/project.example.json [--policy-as-of 2025-06-01]

输出契约（方案 §4）:
    assess_project(project, policy_as_of) -> {
        result_status, conclusions, missing_fields, rule_ids,
        source_refs, temporal_scope, reasons
    }

约定:
- case 按 rules.json 里的顺序 first-match；未命中且无法判定时走 default。
- 缺数据不能当否定结论：条件因字段为 null 而不成立时，规则返回 insufficient，
  并把该字段计入 missing_fields；字段齐全但确实不符合 case 时才继续匹配。
- 一个规则失败不阻止其他规则给出结论；只要有一条规则 insufficient，整体
  result_status 就是 insufficient（结论仍保留，不丢部分结果）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "model" / "schema.json"
RULES_PATH = ROOT / "knowledge" / "rules.json"

RESULT_STATUSES = ("supported", "insufficient", "conflict", "out_of_scope")
TRUE, FALSE, UNKNOWN = "true", "false", "unknown"


# ------------------------------------------------------------------ loading

def load_model() -> tuple[dict, list[dict]]:
    schema = json.loads(SCHEMA_PATH.read_text("utf-8"))
    rules = json.loads(RULES_PATH.read_text("utf-8"))["rules"]
    return schema, rules


# ------------------------------------------------------------------ predicates

def _as_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _null_fields_for(cond: dict, values: dict) -> list[str]:
    """条件因空值受阻时，实际缺了哪些字段（字段对字段比较会缺两个）。"""
    out: list[str] = []
    if values.get(cond["field"]) is None:
        out.append(cond["field"])
    if cond.get("predicate") == "lte_field":
        other = cond.get("value_field")
        if other and values.get(other) is None:
            out.append(other)
    return out


def eval_condition(cond: dict, values: dict) -> tuple[str, bool]:
    """返回 (true/false/unknown, 是否“因为字段为空而不成立”)。"""
    field = cond["field"]
    pred = cond["predicate"]
    expected = cond.get("value")
    actual = values.get(field)

    if pred == "is_null":
        return (TRUE if actual is None else FALSE), False
    if pred == "is_not_null":
        return (FALSE if actual is None else TRUE), actual is None
    if pred in ("is_true", "is_false"):
        if actual is None:
            return UNKNOWN, True
        if not isinstance(actual, bool):
            return UNKNOWN, False
        ok = actual is True if pred == "is_true" else actual is False
        return (TRUE if ok else FALSE), False

    if pred == "lte_field":
        other = values.get(cond.get("value_field"))
        if actual is None or other is None:
            return UNKNOWN, True
        if not (_as_number(actual) and _as_number(other)):
            return UNKNOWN, False
        return (TRUE if actual <= other else FALSE), False

    if actual is None:
        return UNKNOWN, True

    if pred in ("equals", "not_equals"):
        ok = actual == expected
        if pred == "not_equals":
            ok = not ok
        return (TRUE if ok else FALSE), False
    if pred in ("in", "not_in"):
        ok = actual in expected
        if pred == "not_in":
            ok = not ok
        return (TRUE if ok else FALSE), False
    if pred in ("lte", "lt", "gte", "gt"):
        if not (_as_number(actual) and _as_number(expected)):
            return UNKNOWN, False
        ops = {"lte": actual <= expected, "lt": actual < expected,
               "gte": actual >= expected, "gt": actual > expected}
        return (TRUE if ops[pred] else FALSE), False
    if pred in ("date_lte", "date_lt", "date_gte", "date_gt"):
        # ISO 日期（YYYY-MM-DD）可按字符串比较；schema 已限定日期字段格式。
        if not isinstance(actual, str) or not isinstance(expected, str):
            return UNKNOWN, False
        ops = {"date_lte": actual <= expected, "date_lt": actual < expected,
               "date_gte": actual >= expected, "date_gt": actual > expected}
        return (TRUE if ops[pred] else FALSE), False
    raise ValueError(f"未知谓词（schema 白名单外）: {pred}")


def _candidate_fields(rule: dict, values: dict, base_fields: set[str]) -> set[str]:
    """default 落到 insufficient 时，哪些“补上就可能命中某条 case”的字段才算缺失。

    只统计“其余条件已为真、仅因空值无法判定”的 case。若本规则所有 case 都已被
    明确否定（例如 project_class=unclassified，补 province 也命中不了任何分支），
    再报输入字段只会误导调用方（对应 reviews/37 F-2）。
    """
    out: set[str] = set()
    for case in rule["cases"]:
        nulls: list[str] = []
        others_true = True
        for cond in case["when"]["conditions"]:
            result, nullish = eval_condition(cond, values)
            if result == UNKNOWN and nullish:
                nulls.extend(_null_fields_for(cond, values))
            elif result != TRUE:
                others_true = False
        if nulls and others_true:
            out.update(f for f in nulls if f in base_fields)
    return out


def eval_when(when: dict, values: dict) -> tuple[str, list[str], bool]:
    """返回 (true/false/unknown, 因空值受阻的字段, 是否有“真否定”)。"""
    results = [eval_condition(c, values) for c in when.get("conditions", [])]
    logic = when.get("logic", "all")
    null_fields: list[str] = []
    for c, (_, nullish) in zip(when["conditions"], results):
        if nullish:
            null_fields.extend(_null_fields_for(c, values))
    null_fields = list(dict.fromkeys(null_fields))

    if logic == "all":
        if any(r == FALSE and not nullish for r, nullish in results):
            return FALSE, [], True
        if any(r == FALSE for r, _ in results):
            return UNKNOWN, null_fields, False
        if any(r == UNKNOWN for r, _ in results):
            return UNKNOWN, null_fields, False
        return TRUE, [], False

    if logic == "any":
        if any(r == TRUE for r, _ in results):
            return TRUE, [], False
        if any(r == UNKNOWN for r, _ in results):
            return UNKNOWN, null_fields, False
        return FALSE, null_fields, False

    raise ValueError(f"未知 when.logic: {logic}")


# ------------------------------------------------------------------ assess

def _conclusion_map(case: dict) -> dict:
    """case 的结论表（字段 → 值），用于判断两个分支是否给出同一结论。"""
    return {c["field"]: c["value"] for c in case.get("then", {}).get("conclusions", [])}


def _apply_conclusions(conclusions, rule_id, case_id, output, conflicts, reasons):
    for c in conclusions:
        field, value = c["field"], c["value"]
        if field in output and output[field] != value:
            conflicts.add(field)
            reasons.append(f"conflict:{field} 同时得到 {output[field]!r} 与 {value!r}（{rule_id}/{case_id}）")
            continue
        output[field] = value


def assess_project(project: dict, policy_as_of: str | None = None) -> dict:
    schema, rules = load_model()
    output_keys = set(schema["output_fields"])
    base_fields = set(schema["fields"]) - output_keys
    # 输入只接受 schema 的基础字段；派生字段（如 project_class）一律由规则重算，
    # 不允许调用方注入，否则下游规则会按错误分类判定（对应复审 M4-1）。
    values = {k: v for k, v in project.items() if k in base_fields}
    # 空白字符串按“未提供”处理：否则 province="" / policy_as_of="" 会绕过
    # is_null guard，重新落国家 66kV 分支（reviews/25 N6）。
    for k, v in list(values.items()):
        if isinstance(v, str) and not v.strip():
            values[k] = None
    injected = sorted(set(project) - base_fields)
    if policy_as_of and values.get("policy_as_of") is None:
        # 显式 None 不能被 setdefault 挡住：CLI/请求给的日期应补进项目
        # （对应 reviews/25 B4）。项目自带非空日期时仍以项目为准。
        values["policy_as_of"] = policy_as_of

    output: dict = {}
    conclusions: list[dict] = []
    missing: set[str] = set()
    conflicts: set[str] = set()
    reasons: list[str] = []
    if injected:
        reasons.append(f"忽略非输入字段: {', '.join(injected)}（派生字段由规则重算）")
    used_rule_ids: list[str] = []
    refs: list[dict] = []
    scopes: list[dict] = []
    rule_statuses: dict[str, str] = {}

    for rule in sorted(rules, key=lambda r: r["rule_id"]):
        scope = rule.get("temporal_scope") or {}
        scopes.append(scope)
        if policy_as_of:
            start, end = scope.get("effective_from"), scope.get("effective_to")
            if (start and policy_as_of < start) or (end and policy_as_of > end):
                rule_statuses[rule["rule_id"]] = "out_of_scope"
                reasons.append(f"{rule['rule_id']}: policy_as_of={policy_as_of} 不在 {start}–{end or '∞'}")
                continue

        matched = None
        blocked_fields: set[str] = set()
        had_unknown = False
        # 优先分支（省级细则等）只因必需字段未知而无法判定时，后续分支的
        # “命中”不能拿来定论：未知的省份/日期可能正好把它推翻（172 准入
        # 复验 O1：浙江大型工商业缺日期/省份被国家 spot=false 分支兜成
        # supported）。被拦下的字段单独记，后续命中时退回 default。
        priority_blocked: set[str] = set()
        priority_case_id: str | None = None
        priority_case: dict | None = None

        # rule-level guards：在 case 之前评估。命中表示“该规则在当前数据下
        # 不能给结论”，强制 insufficient 并报出应补字段，而不是落到 default
        # 或继续被后面的无日期分支误判（对应 reviews/23 B1）。
        guard_hit = None
        for guard in rule.get("guards", []):
            result, null_fields, genuine_false = eval_when(guard["when"], values)
            if result == TRUE:
                guard_hit = guard
                break
            if result == UNKNOWN:
                blocked_fields.update(null_fields)

        if guard_hit is None:
            for case in rule["cases"]:
                result, null_fields, genuine_false = eval_when(case["when"], values)
                if result == TRUE:
                    if priority_blocked:
                        # 未知字段只有在“可能改变结论”时才拦：若后续分支与
                        # 优先分支的结论完全相同，补上未知字段也不会翻转
                        # 答案（例如现货地区两种模式与国家口径一致），可以
                        # 直接定论，避免为了保守把已确定的答案降级。
                        if _conclusion_map(priority_case) == _conclusion_map(case):
                            matched = case
                            break
                        reasons.append(
                            f"{rule['rule_id']}: {priority_case_id} 需 "
                            f"{', '.join(sorted(priority_blocked))} 才能判定，"
                            f"{case['case_id']} 不得代替定论"
                        )
                        break
                    matched = case
                    break
                if result == UNKNOWN:
                    had_unknown = True
                    blocked_fields.update(null_fields)
                    if case.get("blocks_on_unknown"):
                        # 只收基础字段：派生字段（如 project_class）缺了，
                        # 上游规则已经会拦，这里不重复报。
                        priority_blocked.update(f for f in null_fields if f in base_fields)
                        priority_case_id = case["case_id"]
                        priority_case = case
                # 真否定：该 case 与当前数据不符，继续看下一个

        rule_id = rule["rule_id"]
        if guard_hit is not None:
            used_rule_ids.append(rule_id)
            rule_statuses[rule_id] = guard_hit.get("status", "insufficient")
            reasons.append(f"{rule_id}: guard {guard_hit['guard_id']} 命中，{guard_hit.get('reason', '')}".strip())
            missing.update(f for f in guard_hit.get("missing_fields", []) if f in base_fields)
        elif matched is not None:
            used_rule_ids.append(rule_id)
            rule_statuses[rule_id] = "supported"
            case_id = matched["case_id"]
            _apply_conclusions(matched["then"]["conclusions"], rule_id, case_id, output, conflicts, reasons)
            for c in matched["then"]["conclusions"]:
                conclusions.append({"rule_id": rule_id, "case_id": case_id,
                                    "field": c["field"], "value": c["value"]})
        else:
            default = rule["default"]
            status = default.get("status", "insufficient")
            if priority_blocked:
                # 优先分支只是“缺字段未能判定”，不是“被否定”：default 若
                # 带结论也不能用，统一按 insufficient 处理。
                status = "insufficient"
            # 上游派生字段（如 project_class）为空时，本规则整体无法运行；
            # 缺什么由上游规则报，不要把本规则里的其余输入（如 spot_market_region）
            # 也列成“用户该补的数据”。
            derived_blocked = any(f not in base_fields for f in blocked_fields)
            blocked_fields = {f for f in blocked_fields if f in base_fields}
            if status == "insufficient" and not blocked_fields and not derived_blocked:
                # default 时只报“补上就可能命中”的字段；已提供但不可用
                # （例如容量口径为直流侧）不冒充字段缺失，原因写进 reasons。
                blocked_fields = _candidate_fields(rule, values, base_fields)
            if status == "insufficient":
                had_unknown = True
            rule_statuses[rule_id] = status
            if status == "insufficient":
                reasons.append(f"{rule_id}: insufficient，{default.get('reason', '')}".strip())
                if not default.get("conclusions"):
                    # default 不出结论时也要让调用方知道规则被评估过
                    used_rule_ids.append(rule_id)
            elif status == "out_of_scope":
                reasons.append(f"{rule_id}: out_of_scope")
            else:
                used_rule_ids.append(rule_id)
            if default.get("conclusions") and not priority_blocked:
                _apply_conclusions(default["conclusions"], rule_id, "default", output, conflicts, reasons)
                for c in default["conclusions"]:
                    conclusions.append({"rule_id": rule_id, "case_id": "default",
                                        "field": c["field"], "value": c["value"]})
            if not derived_blocked:
                missing.update(blocked_fields)

        # 下游规则（如 O-R-002 读 project_class）需要看到本规则的结论；
        # 但真正的项目输入不被结论覆盖（setdefault 语义）。
        for k, v in output.items():
            values.setdefault(k, v)

        for ref in rule.get("source_refs", []):
            if ref not in refs:
                refs.append(ref)

    if conflicts:
        result_status = "conflict"
    elif any(s == "insufficient" for s in rule_statuses.values()):
        result_status = "insufficient"
    elif rule_statuses and all(s == "out_of_scope" for s in rule_statuses.values()):
        result_status = "out_of_scope"
    else:
        result_status = "supported"

    # 只输出 schema 定义的 output_fields
    for f in list(output):
        if f not in schema["output_fields"]:
            reasons.append(f"内部字段未在 output_fields 登记，已剔除: {f}")
            output.pop(f)

    scope = {
        "policy_document": scopes[0].get("policy_document") if scopes else None,
        "effective_from": scopes[0].get("effective_from") if scopes else None,
        "effective_to": scopes[0].get("effective_to") if scopes else None,
        "legacy_cutoff": next((s.get("legacy_cutoff") for s in scopes if s.get("legacy_cutoff")), None),
        # 回显实际参与判定的日期（项目自带日期或请求级补入），
        # 不是只看函数参数；否则 ask 会把项目日期误报成“未提供”（reviews/25 N7）。
        "policy_as_of": values.get("policy_as_of"),
    }

    return {
        "result_status": result_status,
        "conclusions": conclusions,
        # 只报基础输入字段；派生字段（如 project_class）不是“用户该补的数据”
        "missing_fields": sorted(f for f in missing if f in base_fields),
        "rule_ids": list(dict.fromkeys(used_rule_ids)),
        "source_refs": refs,
        "temporal_scope": scope,
        "reasons": reasons,
    }


# ------------------------------------------------------------------ cases runner

def _subset_match(actual, expected) -> list[str]:
    """expected 里写的键必须与 actual 对应值一致；列表按集合比较。"""
    problems: list[str] = []
    for k, ev in expected.items():
        if k not in actual:
            problems.append(f"缺少输出键 {k}")
            continue
        av = actual[k]
        if isinstance(ev, list) and isinstance(av, list):
            if k == "conclusions":
                got = {(c.get("field"), json.dumps(c.get("value"), ensure_ascii=False)) for c in av}
                want = {(c.get("field"), json.dumps(c.get("value"), ensure_ascii=False)) for c in ev}
                if want - got:
                    problems.append(f"conclusions 缺少 {sorted(want - got)}")
            elif set(ev) != set(av):
                problems.append(f"{k}: 期望 {ev}，实际 {av}")
        elif av != ev:
            problems.append(f"{k}: 期望 {ev!r}，实际 {av!r}")
    return problems


def run_cases(cases_path: Path) -> int:
    cases_path = Path(cases_path).resolve()
    cases = [json.loads(line) for line in cases_path.read_text("utf-8").splitlines() if line.strip()]
    results = []
    failures = 0
    for case in cases:
        out = assess_project(case["project"], case.get("policy_as_of"))
        problems = _subset_match(out, case.get("expected", {}))
        ok = not problems
        if not ok:
            failures += 1
        results.append({"case_id": case["case_id"], "ok": ok, "problems": problems, "result": out})
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {case['case_id']}: {out['result_status']}"
              + ("" if ok else " | " + "; ".join(problems)))

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{cases_path.stem}.json"
    out_path.write_text(json.dumps({"cases_file": str(cases_path.relative_to(ROOT)),
                                    "passed": len(cases) - failures,
                                    "total": len(cases),
                                    "results": results}, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"{len(cases) - failures}/{len(cases)} cases 通过；结果写入 {out_path.relative_to(ROOT)}")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按 rules.json 评估单个项目或跑固定案例")
    ap.add_argument("--cases", help="cases.jsonl 路径（默认跑 eval/cases.jsonl）")
    ap.add_argument("--project", help="单个项目 JSON 文件；不能与 --cases 同时用")
    ap.add_argument("--policy-as-of", help="问题所问时点 YYYY-MM-DD")
    args = ap.parse_args(argv)

    if args.project:
        project = json.loads(Path(args.project).read_text("utf-8"))
        print(json.dumps(assess_project(project, args.policy_as_of), ensure_ascii=False, indent=2))
        return 0
    cases_path = Path(args.cases) if args.cases else ROOT / "eval" / "cases.jsonl"
    return run_cases(cases_path)


if __name__ == "__main__":
    sys.exit(main())
