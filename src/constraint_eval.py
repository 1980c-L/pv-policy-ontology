# -*- coding: utf-8 -*-
"""多项目约束求值（constraints/v1）。

输入是一组项目（group），输出是按约束逐条判定的 satisfied / violated /
insufficient / conflict / not_applicable，以及 must_merge / may_separate /
no_new_connection_point 等决策标签。项目分类由 rule_eval.assess_project 重算，
不接受调用方注入 project_class。

设计边界：
- 只做“同一用地红线”内的四条约束，不替代备案审查、产权核验或电网接入批复。
- 缺字段返回 insufficient，不猜；显式声明与红线 ID 矛盾返回 conflict。
- 同一约束内已能判定的成员照常判定，缺字段的成员单列，不用整体 insufficient 掩盖违规。
- 组内成员 1–20 个；成员字段=model/schema.json 基础字段 + 本组扩展字段。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCHEMA_VERSION = "constraints/v1"
STATUSES = ["satisfied", "violated", "insufficient", "conflict", "not_applicable"]
STATUS_ORDER = {
    "not_applicable": 1,
    "satisfied": 2,
    "insufficient": 3,
    "violated": 4,
    "conflict": 5,
}
ROOT = Path(__file__).resolve().parents[1]


class GroupValidationError(ValueError):
    """输入组结构不合法；CLI 映射为退出码 2。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(value):
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _is_date(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        from datetime import date
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _load_context(root: Path | None = None):
    root = Path(root) if root else ROOT
    schema = _load_json(root / "model" / "schema.json")
    cschema = _load_json(root / "model" / "constraint_schema.json")
    doc = _load_json(root / "knowledge" / "constraints.json")
    return root, schema, cschema, doc


def _base_fields(schema: dict) -> set[str]:
    return set(schema.get("fields", {}))


def _allowed_member_fields(schema: dict, cschema: dict) -> set[str]:
    return _base_fields(schema) | set(cschema.get("member_extension_fields", {}))


def validate_group_shape(group: dict, schema: dict, cschema: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(group, dict):
        return ["group 必须是 JSON 对象"]
    required = set(cschema.get("group_required_fields", []))
    allowed_group = required | set(cschema.get("group_optional_fields", []))
    unknown_group = sorted(set(group) - allowed_group)
    if unknown_group:
        errors.append(f"group 含未知字段: {', '.join(unknown_group)}")
    for key in required:
        if key not in group:
            errors.append(f"group 缺少字段: {key}")
    if not isinstance(group.get("group_id"), str) or not group.get("group_id", "").strip():
        errors.append("group.group_id 必须是非空字符串")
    policy_as_of = _norm(group.get("policy_as_of"))
    if policy_as_of is not None and not _is_date(policy_as_of):
        errors.append("group.policy_as_of 必须是 ISO 日期 YYYY-MM-DD")
    label = group.get("label")
    if label is not None and not isinstance(label, str):
        errors.append("group.label 必须是字符串")
    anchor = _norm(group.get("existing_connection_point_id"))
    if anchor is not None and not isinstance(anchor, str):
        errors.append("group.existing_connection_point_id 必须是字符串")
    anchor_map = group.get("existing_connection_points")
    if anchor_map is not None:
        if not isinstance(anchor_map, dict):
            errors.append("group.existing_connection_points 必须是对象")
        else:
            for red_line, points in anchor_map.items():
                if not isinstance(red_line, str) or not red_line.strip():
                    errors.append("group.existing_connection_points 的键必须是非空红线标识")
                if isinstance(points, str):
                    points = [points]
                if not isinstance(points, list) or not points:
                    errors.append(f"group.existing_connection_points[{red_line!r}] 必须是非空字符串或字符串数组")
                    continue
                if any(not isinstance(p, str) or not p.strip() for p in points):
                    errors.append(f"group.existing_connection_points[{red_line!r}] 含空或非字符串连接点")

    members = group.get("members")
    max_members = int(cschema.get("max_members", 20))
    if not isinstance(members, list) or not members:
        return errors + ["group.members 必须是非空数组"]
    if len(members) > max_members:
        errors.append(f"group.members 超过上限 {max_members}")
    allowed_member = _allowed_member_fields(schema, cschema)
    base_specs = schema.get("fields", {})
    extension_specs = cschema.get("member_extension_fields", {})
    seen_pid = set()
    for idx, member in enumerate(members, 1):
        w = f"members[{idx}]"
        if not isinstance(member, dict):
            errors.append(f"{w} 必须是 JSON 对象")
            continue
        unknown = sorted(set(member) - allowed_member)
        if unknown:
            errors.append(f"{w} 含未知字段: {', '.join(unknown)}")
        pid = _norm(member.get("project_id"))
        if not isinstance(pid, str) or not pid:
            errors.append(f"{w}.project_id 必须是非空字符串")
        elif pid in seen_pid:
            errors.append(f"{w}.project_id 重复: {pid}")
        else:
            seen_pid.add(pid)
        for name, value in member.items():
            value = _norm(value)
            if value is None:
                continue
            spec = base_specs.get(name) or extension_specs.get(name) or {}
            ftype = spec.get("type")
            if ftype == "date" and not _is_date(value):
                errors.append(f"{w}.{name} 必须是 ISO 日期 YYYY-MM-DD")
            elif ftype == "boolean" and not isinstance(value, bool):
                errors.append(f"{w}.{name} 必须是布尔值")
            elif ftype == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                errors.append(f"{w}.{name} 必须是数字")
            elif ftype == "enum" and value not in spec.get("values", []):
                errors.append(f"{w}.{name} 不在枚举内: {value!r}")
            elif ftype == "string" and not isinstance(value, str):
                errors.append(f"{w}.{name} 必须是字符串")
    return errors


def _assess_member(root: Path, schema: dict, member: dict, policy_as_of: str | None) -> dict:
    sys.path.insert(0, str(root / "src"))
    from rule_eval import assess_project  # 延迟导入，避免 validate 依赖

    base = {k: v for k, v in member.items() if k in _base_fields(schema)}
    for k, v in list(base.items()):
        base[k] = _norm(v)
    # 成员自带日期既用于条件取值，也用于规则时效过滤；只有未给时才用组级日期
    # （reviews/29 N-01：此前只影响取值，不影响规则是否生效）。
    effective_policy = _norm(member.get("policy_as_of")) or policy_as_of
    return assess_project(base, effective_policy)


def _project_class(assessment: dict) -> str | None:
    for c in assessment.get("conclusions", []):
        if c.get("field") == "project_class":
            return c.get("value")
    return None


def _prefixed(pid: str, fields: list[str]) -> list[str]:
    return [f"{pid}.{field}" for field in fields]


def _agg(statuses: list[str]) -> str:
    if not statuses:
        return "not_applicable"
    return max(statuses, key=lambda s: STATUS_ORDER.get(s, 0))


def _dedupe_refs(refs: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for ref in refs:
        key = (ref.get("locator_id"), ref.get("quote"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _result(constraint: dict, status: str, decision: str, affected: list[str],
            missing: list[str], reasons: list[str]) -> dict:
    return {
        "constraint_id": constraint["constraint_id"],
        "title": constraint.get("title", ""),
        "status": status,
        "decision": decision,
        "affected_project_ids": list(dict.fromkeys(affected)),
        "missing_fields": sorted(set(missing)),
        "reasons": reasons,
        "source_refs": constraint.get("source_refs", []),
    }


def _site_red_line(constraint: dict, members: list[dict], assessments: dict[str, dict]) -> dict:
    params = constraint.get("parameters", {})
    use_field = params.get("use_field")
    pair = params.get("pair_fields") or []
    if len(pair) != 2:
        raise ValueError(f"{constraint['constraint_id']} pair_fields 必须是两个字段")
    left_field, right_field = pair
    exception = params.get("exception") or {}
    consistency_field = params.get("consistency_field")
    statuses: list[str] = []
    affected: list[str] = []
    missing: list[str] = []
    reasons: list[str] = []
    applicable = 0
    exception_hits = 0
    for member in members:
        pid = member["project_id"]
        use = _norm(member.get(use_field))
        if use is None:
            statuses.append("insufficient")
            affected.append(pid)
            missing.append(f"{pid}.{use_field}")
            reasons.append(f"{pid}: 未提供 {use_field}，无法判断是否使用该用地")
            continue
        if use is False:
            statuses.append("not_applicable")
            continue
        applicable += 1
        left = _norm(member.get(left_field))
        right = _norm(member.get(right_field))
        if left is None or right is None:
            statuses.append("insufficient")
            affected.append(pid)
            if left is None:
                missing.append(f"{pid}.{left_field}")
            if right is None:
                missing.append(f"{pid}.{right_field}")
            reasons.append(f"{pid}: 缺少 {left_field} 或 {right_field}，无法比对红线")
            continue
        same_flag = _norm(member.get(consistency_field)) if consistency_field else None
        if same_flag is False and left == right:
            statuses.append("conflict")
            affected.append(pid)
            reasons.append(f"{pid}: {consistency_field}=false 但两个红线 ID 相同，输入自相矛盾")
            continue
        if same_flag is True and left != right:
            statuses.append("conflict")
            affected.append(pid)
            reasons.append(f"{pid}: {consistency_field}=true 但两个红线 ID 不同，输入自相矛盾")
            continue
        if left == right:
            statuses.append("satisfied")
            reasons.append(f"{pid}: 两个红线 ID 相同")
            continue
        if exception:
            need_class = exception.get("project_class")
            need_conn = exception.get("connection_type")
            conn = _norm(member.get("connection_type"))
            cls = _project_class(assessments.get(pid, {}))
            if conn is None:
                statuses.append("insufficient")
                affected.append(pid)
                missing.append(f"{pid}.connection_type")
                reasons.append(f"{pid}: 红线不同，且未提供 connection_type，无法判断专线例外")
                continue
            if cls is None:
                statuses.append("insufficient")
                affected.append(pid)
                fields = assessments.get(pid, {}).get("missing_fields", [])
                if fields:
                    missing.extend(f"{pid}.{f}" for f in fields)
                else:
                    # 分类未定但规则没有点名缺字段时，至少把 project_class 标成未定，
                    # 避免 insufficient + missing_fields=[]（reviews/29 B-02）。
                    missing.append(f"{pid}.project_class")
                reasons.append(f"{pid}: 红线不同，且项目分类未定，无法判断是否适用专线例外")
                continue
            if conn == need_conn and cls == need_class:
                statuses.append("not_applicable")
                exception_hits += 1
                reasons.append(f"{pid}: 大型工商业专线供电，按 Q16 可跨越用地红线")
                continue
            statuses.append("violated")
            affected.append(pid)
            reasons.append(f"{pid}: 红线不同，且不满足大型工商业专线例外")
            continue
        statuses.append("violated")
        affected.append(pid)
        reasons.append(f"{pid}: 两个红线 ID 不同，不满足同一用地红线要求")
    status = _agg(statuses)
    if status == "conflict":
        decision = "conflict"
    elif status == "violated":
        decision = "same_red_line_required"
    elif status == "insufficient":
        decision = "insufficient_information"
    elif status == "satisfied":
        decision = "same_red_line_confirmed"
    elif exception_hits and exception_hits == applicable:
        decision = "dedicated_line_exception"
    else:
        decision = "not_applicable"
    return _result(constraint, status, decision, affected, missing, reasons)


def _account_merge(constraint: dict, group: dict, members: list[dict]) -> dict:
    params = constraint.get("parameters", {})
    group_by = params.get("group_by", "red_line_id")
    account_field = params.get("account_field", "electricity_account_id")
    if len(members) < 2:
        return _result(constraint, "not_applicable", "not_applicable", [], [], ["组内只有一个项目，不涉及多项目合并"])
    statuses: list[str] = []
    repeated_affected: list[str] = []
    missing_affected: list[str] = []
    missing: list[str] = []
    reasons: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for member in members:
        red_line = _norm(member.get(group_by))
        if red_line is None:
            # 不整条早退：缺红线的成员单独记 insufficient，其他红线仍继续判。
            pid = member["project_id"]
            statuses.append("insufficient")
            missing_affected.append(pid)
            missing.append(f"{pid}.{group_by}")
            reasons.append(f"{pid}: 缺少 {group_by}，无法判断是否与其它项目同一红线")
            continue
        buckets.setdefault(red_line, []).append(member)
    for red_line, bucket in buckets.items():
        if len(bucket) < 2:
            statuses.append("not_applicable")
            continue
        no_account = [m["project_id"] for m in bucket if _norm(m.get(account_field)) is None]
        by_account: dict[str, list[str]] = {}
        for member in bucket:
            account = _norm(member.get(account_field))
            if account is not None:
                by_account.setdefault(account, []).append(member["project_id"])
        repeated = {account: pids for account, pids in by_account.items() if len(pids) > 1}
        if repeated:
            # Q15 的“均对应用一户号”按户号子集判：A/A/B 中 A/A 必须合并，
            # B 仍可独立；不能用“桶内出现第二个户号”把整桶降成 may_separate。
            statuses.append("violated")
            for pids in repeated.values():
                repeated_affected.extend(pids)
            detail = "；".join(f"户号 {account}: {', '.join(pids)}" for account, pids in sorted(repeated.items()))
            reasons.append(f"红线 {red_line}: 同一户号对应多个项目（{detail}），按 Q15 应合并为 1 个项目；其余户号可独立")
        if no_account:
            statuses.append("insufficient")
            missing_affected.extend(no_account)
            missing.extend(f"{pid}.{account_field}" for pid in no_account)
            reasons.append(f"红线 {red_line}: {', '.join(no_account)} 缺少 {account_field}，无法判断其是否与同红线项目合并")
        if not repeated and not no_account:
            statuses.append("satisfied")
            reasons.append(f"红线 {red_line}: {len(bucket)} 个项目对应 {len(by_account)} 个用电户号，可分别建设")
    status = _agg(statuses)
    # must_merge 只指向“同户号重复”的成员；缺户号/缺红线的成员只在 missing/reason。
    # 没有重复户号、整条约束为 insufficient 时，affected 才指向缺字段成员。
    if repeated_affected:
        affected = list(dict.fromkeys(repeated_affected))
    elif status == "insufficient":
        affected = list(dict.fromkeys(missing_affected))
    else:
        affected = []
    if status == "violated":
        decision = "must_merge"
    elif status == "satisfied":
        decision = "may_separate"
    elif status == "insufficient":
        decision = "insufficient_information"
    else:
        decision = "not_applicable"
    return _result(constraint, status, decision, affected, missing, reasons)


def _differentiated(members: list[dict], fields: list[str]) -> tuple[bool, list[str]]:
    diff = False
    unknown: list[str] = []
    for field in fields:
        vals = []
        for member in members:
            value = _norm(member.get(field))
            if value is None:
                unknown.append(f"{member['project_id']}.{field}")
            else:
                vals.append(value)
        if len(set(vals)) > 1:
            diff = True
    return diff, sorted(set(unknown))


def _anchor_for_red_line(group: dict, red_line: str, bucket_count: int,
                            anchor_field: str, anchor_map_field: str) -> tuple[set[str] | None, str]:
    """按红线取既有点集合。单锚点只允许用于单红线组，避免跨红线复用。"""
    anchor_map = group.get(anchor_map_field)
    if isinstance(anchor_map, dict):
        if red_line in anchor_map:
            points = anchor_map[red_line]
            if isinstance(points, str):
                points = [points]
            return {_norm(p) for p in points if _norm(p) is not None}, f"group.{anchor_map_field}[{red_line}]"
        return None, f"group.{anchor_map_field}[{red_line}]"
    single = _norm(group.get(anchor_field))
    if single is not None and bucket_count == 1:
        return {single}, f"group.{anchor_field}"
    return None, f"group.{anchor_field}"


def _connection_point(constraint: dict, group: dict, members: list[dict], assessments: dict[str, dict]) -> dict:
    params = constraint.get("parameters", {})
    group_by = params.get("group_by", "red_line_id")
    diff_fields = params.get("differentiation_fields") or []
    target_classes = set(params.get("target_classes") or [])
    anchor_field = params.get("anchor_field", "existing_connection_point_id")
    anchor_map_field = params.get("anchor_map_field", "existing_connection_points")
    connection_field = params.get("connection_field", "grid_connection_point_id")
    if len(members) < 2:
        return _result(constraint, "not_applicable", "not_applicable", [], [], ["组内只有一个项目，不涉及新增连接点"])
    statuses: list[str] = []
    affected: list[str] = []
    missing: list[str] = []
    reasons: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for member in members:
        red_line = _norm(member.get(group_by))
        if red_line is None:
            # 缺红线的成员单列，不掩盖其他红线已能判定的新增点（reviews/29 B-02）。
            pid = member["project_id"]
            statuses.append("insufficient")
            affected.append(pid)
            missing.append(f"{pid}.{group_by}")
            reasons.append(f"{pid}: 缺少 {group_by}，无法判断适用哪条红线的连接点锚点")
            continue
        buckets.setdefault(red_line, []).append(member)
    for red_line, bucket in buckets.items():
        if len(bucket) < 2:
            statuses.append("not_applicable")
            continue
        classes: dict[str, str | None] = {}
        for member in bucket:
            classes[member["project_id"]] = _project_class(assessments.get(member["project_id"], {}))
        unresolved = [pid for pid, cls in classes.items() if cls is None]
        if unresolved:
            statuses.append("insufficient")
            affected.extend(unresolved)
            for pid in unresolved:
                fields = assessments.get(pid, {}).get("missing_fields", [])
                if not fields:
                    # 分类无法确定但规则没有点名缺字段时，至少把 project_class 标成未定，
                    # 避免出现 insufficient + missing_fields=[]（reviews/29 B-02/B5）。
                    missing.append(f"{pid}.project_class")
                else:
                    missing.extend(f"{pid}.{f}" for f in fields)
            reasons.append(f"红线 {red_line}: {', '.join(unresolved)} 项目分类未定，无法判断是否工商业")
        outside = [pid for pid, cls in classes.items()
                   if cls is not None and (cls == "unclassified" or cls not in target_classes)]
        if outside:
            # 非工商业成员不参与本约束；工商业子集仍可判定。unclassified 是“不属于四类”，
            # 不是“缺字段”，因此记 not_applicable 而不是 insufficient。
            statuses.append("not_applicable")
            reasons.append(f"红线 {red_line}: 非工商业项目（{', '.join(outside)}）不参与 O-C-004；工商业子集继续判定")
        eligible = [m for m in bucket if classes[m["project_id"]] in target_classes]
        if len(eligible) < 2:
            if not unresolved:
                statuses.append("not_applicable")
                reasons.append(f"红线 {red_line}: 工商业成员不足 2 个，不属于 Q31 的多项目情形")
            continue
        differentiated, unknown = _differentiated(eligible, diff_fields)
        if not differentiated:
            if unknown:
                statuses.append("insufficient")
                affected.extend(sorted({pid for item in unknown for pid in [item.split('.', 1)[0]]}))
                missing.extend(unknown)
                reasons.append(f"红线 {red_line}: 投资主体/分期信息不足，无法确认是否属于不同主体或分期")
                continue
            statuses.append("not_applicable")
            reasons.append(f"红线 {red_line}: 工商业成员的投资主体与分期均相同，不属于 Q31 的分期或不同主体情形")
            continue
        anchor, anchor_name = _anchor_for_red_line(group, red_line, len(buckets), anchor_field, anchor_map_field)
        no_cp = [m["project_id"] for m in eligible if _norm(m.get(connection_field)) is None]
        if no_cp:
            statuses.append("insufficient")
            affected.extend(no_cp)
            missing.extend(f"{pid}.{connection_field}" for pid in no_cp)
            reasons.append(f"红线 {red_line}: {', '.join(no_cp)} 缺少 {connection_field}")
        if anchor is None:
            statuses.append("insufficient")
            affected.extend(m["project_id"] for m in eligible)
            missing.append(anchor_name)
            reasons.append(f"红线 {red_line}: 未提供该红线的已有连接点锚点/集合，不能用别的红线或组级单锚点代替")
            continue
        present = [(m["project_id"], _norm(m.get(connection_field))) for m in eligible
                   if _norm(m.get(connection_field)) is not None]
        outside_points = [(pid, point) for pid, point in present if point not in anchor]
        if outside_points:
            statuses.append("violated")
            affected.extend(pid for pid, _ in outside_points)
            reasons.append(
                f"红线 {red_line}: 连接点 {sorted({p for _, p in outside_points})} 不在该红线既有点集合 "
                f"{sorted(anchor)} 内，视为新增公共电网连接点"
            )
        elif not no_cp:
            statuses.append("satisfied")
            reasons.append(f"红线 {red_line}: 所有项目连接点都在该红线既有点集合 {sorted(anchor)} 内，未新增")
    status = _agg(statuses)
    if status == "violated":
        decision = "no_new_connection_point"
    elif status == "satisfied":
        decision = "existing_point_shared"
    elif status == "insufficient":
        decision = "insufficient_information"
    else:
        decision = "not_applicable"
    return _result(constraint, status, decision, affected, missing, reasons)


def check_group(group: dict, root: Path | None = None) -> dict:
    """求值一个约束组。结构不合法抛 GroupValidationError。"""
    root, schema, cschema, doc = _load_context(root)
    errors = validate_group_shape(group, schema, cschema)
    if errors:
        raise GroupValidationError(errors)
    members = [dict(m) for m in group["members"]]
    for member in members:
        for key, value in list(member.items()):
            member[key] = _norm(value)
    policy_as_of = _norm(group.get("policy_as_of"))
    assessments = {
        member["project_id"]: _assess_member(root, schema, member, policy_as_of)
        for member in members
    }
    results = []
    for constraint in doc.get("constraints", []):
        ctype = constraint.get("constraint_type")
        if ctype == "site_red_line":
            results.append(_site_red_line(constraint, members, assessments))
        elif ctype == "account_merge":
            results.append(_account_merge(constraint, group, members))
        elif ctype == "connection_point":
            results.append(_connection_point(constraint, group, members, assessments))
        else:
            raise ValueError(f"未知 constraint_type: {ctype!r}")
    statuses = [r["status"] for r in results]
    group_status = _agg(statuses)
    missing = sorted({f for r in results for f in r["missing_fields"]})
    refs = _dedupe_refs([ref for r in results for ref in r["source_refs"]])
    summary = {name: sum(1 for r in results if r["status"] == name) for name in STATUSES}
    return {
        "schema_version": SCHEMA_VERSION,
        "group_id": group["group_id"],
        "policy_as_of": policy_as_of,
        "result_status": group_status,
        "constraints": results,
        "missing_fields": missing,
        "source_refs": refs,
        "member_assessments": [
            {
                "project_id": member["project_id"],
                "policy_as_of": _norm(member.get("policy_as_of")) or policy_as_of,
                "project_class": _project_class(assessments[member["project_id"]]),
                "result_status": assessments[member["project_id"]].get("result_status"),
                "missing_fields": assessments[member["project_id"]].get("missing_fields", []),
            }
            for member in members
        ],
        "summary": {**summary, "total": len(results)},
    }


def list_constraints(root: Path | None = None) -> dict:
    _, _, _, doc = _load_context(root)
    return {
        "schema_version": SCHEMA_VERSION,
        "constraints": [
            {
                "constraint_id": c["constraint_id"],
                "title": c.get("title", ""),
                "constraint_type": c.get("constraint_type"),
            }
            for c in doc.get("constraints", [])
        ],
    }


def show_constraint(constraint_id: str, root: Path | None = None) -> dict | None:
    _, _, _, doc = _load_context(root)
    for c in doc.get("constraints", []):
        if c["constraint_id"] == constraint_id:
            return c
    return None
