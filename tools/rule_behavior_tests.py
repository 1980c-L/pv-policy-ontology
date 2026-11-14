# -*- coding: utf-8 -*-
"""规则行为测试：按 rules.json 的声明条件做确定性求值，验证“缺字段不反推”等边界。

说明：
- 这是规则 JSON 的测试夹具，不是 `assess_project` 实现；只支持 schema 白名单谓词。
- 运行时只读 model/schema.json 与 knowledge/rules.json，不写任何文件。
- 用法：python tools/rule_behavior_tests.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 三态求值直接复用引擎的谓词实现，避免夹具与 src/rule_eval.py 两套语义
# 漂移（2026-11-14 起 blocks_on_unknown 需要 UNKNOWN 语义，布尔匹配已不够）。
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from rule_eval import TRUE, UNKNOWN, eval_when, _conclusion_map  # noqa: E402
SCHEMA = json.loads((ROOT / "model" / "schema.json").read_text(encoding="utf-8"))
RULES = json.loads((ROOT / "knowledge" / "rules.json").read_text(encoding="utf-8"))
RULE_BY_ID = {r["rule_id"]: r for r in RULES["rules"]}


def _cmp(name, actual, expected):
    if actual is None or expected is None:
        return False
    if name == "lte":
        return actual <= expected
    if name == "lt":
        return actual < expected
    if name == "gte":
        return actual >= expected
    if name == "gt":
        return actual > expected
    return False


def check(cond, project):
    pred, field = cond["predicate"], cond["field"]
    actual = project.get(field)
    # 与 assess_project 的空白归一化保持一致（reviews/25 N6）。
    if isinstance(actual, str) and not actual.strip():
        actual = None
    value = cond.get("value")
    if pred == "equals":
        return actual == value
    if pred == "not_equals":
        return actual != value
    if pred == "in":
        return actual in value
    if pred == "not_in":
        return actual not in value
    if pred in ("lte", "lt", "gte", "gt"):
        return _cmp(pred, actual, value)
    if pred in ("date_lte", "date_lt", "date_gte", "date_gt"):
        return _cmp(pred[5:], str(actual) if actual is not None else None, value)
    if pred == "is_null":
        return actual is None
    if pred == "is_not_null":
        return actual is not None
    if pred == "is_true":
        return actual is True
    if pred == "is_false":
        return actual is False
    raise ValueError(f"未知谓词 {pred}")


def when_matches(case, project):
    conds = case["when"]["conditions"]
    results = [check(c, project) for c in conds]
    return all(results) if case["when"].get("logic", "all") == "all" else any(results)


def _normalize(project):
    """空白字符串按“未提供”处理，与 assess_project 的入口归一化保持一致。"""
    return {k: (None if isinstance(v, str) and not v.strip() else v) for k, v in project.items()}


def eval_rule(rule_id, project):
    rule = RULE_BY_ID[rule_id]
    project = _normalize(project)
    for guard in rule.get("guards", []):
        result, _nulls, _ = eval_when(guard["when"], project)
        if result == TRUE:
            return {"case_id": None, "guard_id": guard.get("guard_id"),
                    "status": guard.get("status", "insufficient"), "conclusions": []}
    # 与 src/rule_eval.py 同语义：优先分支（blocks_on_unknown）只因缺字段
    # 未判定时，后续 first-match 不得代替它的结论。
    priority = None
    for case in rule["cases"]:
        result, nulls, _ = eval_when(case["when"], project)
        if result == TRUE:
            if priority is not None and _conclusion_map(priority[0]) != _conclusion_map(case):
                return {"case_id": None, "status": "insufficient", "conclusions": [],
                        "blocked_by": priority[0]["case_id"]}
            return {"case_id": case["case_id"], **case["then"]}
        if result == UNKNOWN and case.get("blocks_on_unknown") and nulls:
            priority = (case, nulls)
    return {"case_id": None, **rule["default"]}


def run_case(label, rule_id, project, expect_status=None, expect_case=None, expect_field=None,
             expect_guard=None, expect_blocked_by=None):
    got = eval_rule(rule_id, project)
    ok = True
    if expect_case is not None:
        ok = ok and got.get("case_id") == expect_case
    if expect_guard is not None:
        ok = ok and got.get("guard_id") == expect_guard
    if expect_status is not None:
        ok = ok and got.get("status") == expect_status
    if expect_field is not None:
        field, value = expect_field
        concl = {c["field"]: c["value"] for c in got.get("conclusions", [])}
        ok = ok and concl.get(field) == value
    if expect_blocked_by is not None:
        ok = ok and got.get("blocked_by") == expect_blocked_by
    detail = got.get("case_id") or got.get("status")
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: -> {detail}")
    if not ok:
        print(json.dumps(got, ensure_ascii=False, indent=1))
    return ok


def p(**kw):
    base = {
        "investor_type": None, "site_category": None, "connection_voltage_kv": None,
        "connection_type": None, "investor_same_legal_person_as_user": None,
        "capacity_mw": None, "capacity_basis": None, "grid_mode": None,
        "spot_market_region": None, "same_red_line": None,
        "filing_date": None, "filing_change_date": None,
        "grid_connection_date": None, "grid_connection_full_capacity": None,
        "site_owned_by_investor": None, "policy_as_of": None,
        "province": None, "rural_public_site": None, "self_consumption_ratio_pct": None,
        "factory_10kv_line_count": None, "max_pv_capacity_per_10kv_line_mw": None,
        "existing_supply_voltage_kv": None,
        "grid_opinion_date": None, "construction_completion_date": None,
    }
    base.update(kw)
    return base


def main():
    results = []

    # ---- O-R-001 正向：四类各一
    results.append(run_case(
        "001 66kV 缺 province -> guard 不给国家 66kV 大型结论", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=40, capacity_basis="ac_inverter_sum", same_red_line=True,
          policy_as_of="2026-09-01"),
        expect_status="insufficient",
        expect_guard="voltage_66kv_requires_province"))
    results.append(run_case(
        "001 66kV province 空串 -> 按缺失处理", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=40, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="", policy_as_of="2026-09-01"),
        expect_status="insufficient",
        expect_guard="voltage_66kv_requires_province"))
    results.append(run_case(
        "001 66kV 缺 investor_type -> 110 分支不得判大型", "O-R-001",
        p(site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=40, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-09-01"),
        expect_status="insufficient"))
    results.append(run_case(
        "001 natural household", "O-R-001",
        p(investor_type="natural_person", site_category="residential", connection_voltage_kv=0.38,
          capacity_mw=5, capacity_basis="ac_inverter_sum", site_owned_by_investor=True, same_red_line=True),
        expect_case="natural_person_household", expect_field=("project_class", "natural_person_household")))
    results.append(run_case(
        "001 non-natural household", "O-R-001",
        p(investor_type="non_natural_person", site_category="residential", connection_voltage_kv=10,
          capacity_mw=6, capacity_basis="ac_inverter_sum", same_red_line=True),
        expect_case="non_natural_person_household"))
    results.append(run_case(
        "001 large 35kV", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=35,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=20, capacity_basis="ac_inverter_sum", same_red_line=True),
        expect_case="large_industrial_business_35kv", expect_field=("project_class", "large_industrial_business")))
    results.append(run_case(
        "001 large 110kV", "O-R-001",
        p(investor_type="non_natural_person", site_category="other_building", connection_voltage_kv=110,
          connection_type="dedicated_line", investor_same_legal_person_as_user=True,
          capacity_mw=50, capacity_basis="ac_inverter_sum", same_red_line=True),
        expect_case="large_industrial_business_110kv"))
    # ---- O-R-001 浙江 66kV：浙江第三条未列 66kV，超第三条按集中式管理
    results.append(run_case(
        "001 浙江 66kV/40MW + 细则生效日 -> 不判大型，unclassified（按集中式）", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=40, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-08-03"),
        expect_case="zhejiang_66kv_centralized",
        expect_field=("project_class", "unclassified")))
    results.append(run_case(
        "001 浙江 66kV/40MW + 细则生效前一日 -> 仍按国家 66kV 判大型", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=40, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-08-02"),
        expect_case="large_industrial_business_110kv"))
    results.append(run_case(
        "001 浙江 110kV/50MW + 细则生效后 -> 第三条含 110kV，仍判大型", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=110,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=50, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-09-01"),
        expect_case="large_industrial_business_110kv"))
    results.append(run_case(
        "001 浙江 66kV/60MW -> 电压超第三条，同样按集中式 unclassified", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=60, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-09-01"),
        expect_case="zhejiang_66kv_centralized",
        expect_field=("project_class", "unclassified")))
    results.append(run_case(
        "001 浙江 66kV/5MW 非自然人户用 -> 按集中式 unclassified", "O-R-001",
        p(investor_type="non_natural_person", site_category="residential", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-09-01"),
        expect_case="zhejiang_66kv_centralized",
        expect_field=("project_class", "unclassified")))
    results.append(run_case(
        "001 浙江 66kV/40MW 投资人与用户不同法人 -> 仍按集中式 unclassified", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="dedicated_line", investor_same_legal_person_as_user=False,
          capacity_mw=40, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-09-01"),
        expect_case="zhejiang_66kv_centralized",
        expect_field=("project_class", "unclassified")))
    results.append(run_case(
        "001 浙江 66kV 缺 policy_as_of -> guard 强制 insufficient", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=40, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang"),
        expect_status="insufficient",
        expect_guard="zhejiang_66kv_requires_policy_as_of"))
    results.append(run_case(
        "001 浙江 66kV/40MW 细则失效后（2031）-> 不套浙江分支，按国家 66kV 判大型", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=66,
          connection_type="user_side_grid", investor_same_legal_person_as_user=True,
          capacity_mw=40, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2031-01-01"),
        expect_case="large_industrial_business_110kv"))
    results.append(run_case(
        "001 general industrial", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=6, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="other", rural_public_site=False),
        expect_case="general_industrial_business"))

    # ---- B-1R：缺关键字段必须 insufficient，不得给 unclassified
    results.append(run_case(
        "001 缺 connection_type（10kV/8MW 自然人住宅）", "O-R-001",
        p(investor_type="natural_person", site_category="residential", connection_voltage_kv=10,
          capacity_mw=8, capacity_basis="ac_inverter_sum", site_owned_by_investor=True, same_red_line=True),
        expect_status="insufficient"))
    results.append(run_case(
        "001 缺 同一法人（35kV/20MW 非住宅）", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=35,
          connection_type="user_side_grid", capacity_mw=20, capacity_basis="ac_inverter_sum", same_red_line=True),
        expect_status="insufficient"))
    results.append(run_case(
        "001 缺 connection_type（35kV/20MW 非住宅）", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=35,
          investor_same_legal_person_as_user=True, capacity_mw=20, capacity_basis="ac_inverter_sum",
          same_red_line=True),
        expect_status="insufficient"))
    results.append(run_case(
        "001 容量口径为直流侧", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=6, capacity_basis="dc_module_sum", same_red_line=True),
        expect_status="insufficient"))

    # ---- 数据齐备但不符合四类：才允许 unclassified
    results.append(run_case(
        "001 齐备但 10kV/8MW 不属四类", "O-R-001",
        p(investor_type="non_natural_person", site_category="residential", connection_voltage_kv=10,
          connection_type="public_grid", investor_same_legal_person_as_user=False,
          capacity_mw=8, capacity_basis="ac_inverter_sum", same_red_line=True),
        expect_case="unclassified_complete_non_natural",
        expect_field=("project_class", "unclassified")))

    # ---- O-R-002
    results.append(run_case(
        "002 户用三模式", "O-R-002",
        p(project_class="natural_person_household"), expect_case="household_three_modes"))
    results.append(run_case(
        "002 一般工商业两模式", "O-R-002",
        p(project_class="general_industrial_business"), expect_case="general_industrial_two_modes"))
    results.append(run_case(
        "002 大型 + 现货 false + 他省 -> 仅全自发自用", "O-R-002",
        p(project_class="large_industrial_business", province="guangdong",
          spot_market_region=False),
        expect_case="large_default_self_consumption"))
    results.append(run_case(
        "002 大型 + 现货 false + 缺省份 -> insufficient（不得默认国家口径）", "O-R-002",
        p(project_class="large_industrial_business", spot_market_region=False),
        expect_status="insufficient"))
    results.append(run_case(
        "002 大型 + 现货 null -> insufficient", "O-R-002",
        p(project_class="large_industrial_business", spot_market_region=None),
        expect_status="insufficient"))
    results.append(run_case(
        "002 大型 + 现货 true -> 两模式", "O-R-002",
        p(project_class="large_industrial_business", spot_market_region=True),
        expect_case="large_spot_market_two_modes"))
    results.append(run_case(
        "002 浙江大型 + 现货 null + 2026-08-03 -> 两模式（不要求现货地区）", "O-R-002",
        p(project_class="large_industrial_business", province="zhejiang", policy_as_of="2026-08-03"),
        expect_case="zhejiang_large_industrial_two_modes",
        expect_field=("allowed_grid_modes",
                      ["full_self_consumption", "self_consumption_surplus_feed_in"])))
    results.append(run_case(
        "002 浙江大型 + 2026-08-02（细则前）-> 仍按国家口径仅全自发自用", "O-R-002",
        p(project_class="large_industrial_business", province="zhejiang",
          spot_market_region=False, policy_as_of="2026-08-02"),
        expect_case="large_default_self_consumption"))
    results.append(run_case(
        "002 浙江大型 + 无问题日期 -> insufficient（不默认按新细则）", "O-R-002",
        p(project_class="large_industrial_business", province="zhejiang"),
        expect_status="insufficient"))
    # 172 准入复验 O1：优先分支（浙江细则）只因缺字段未判定时，国家
    # spot=false 分支不得代替它给“仅全部自发自用”。
    results.append(run_case(
        "002 浙江大型 + 现货 false + 无日期 -> insufficient（不得落国家全自发自用）", "O-R-002",
        p(project_class="large_industrial_business", province="zhejiang", spot_market_region=False),
        expect_status="insufficient"))
    results.append(run_case(
        "002 大型 + 现货 false + 缺省份 + 2026-11-14 -> insufficient（未知省份不当 other）", "O-R-002",
        p(project_class="large_industrial_business", spot_market_region=False,
          policy_as_of="2026-11-14"),
        expect_status="insufficient"))
    results.append(run_case(
        "002 大型 + 现货 false + 其他省 + 2026-11-14 -> 仍按国家口径仅全自发自用", "O-R-002",
        p(project_class="large_industrial_business", province="guangdong", spot_market_region=False,
          policy_as_of="2026-11-14"),
        expect_case="large_default_self_consumption"))
    results.append(run_case(
        "002 大型 + 现货 true + 缺省份 -> 两模式（补省份不改变结论，不降级）", "O-R-002",
        p(project_class="large_industrial_business", spot_market_region=True,
          policy_as_of="2026-11-14"),
        expect_case="large_spot_market_two_modes"))

    # ---- O-R-003
    results.append(run_case(
        "003 1-23 备案 + 5-1 全容量 -> true", "O-R-003",
        p(filing_date="2025-01-23", grid_connection_date="2025-05-01", grid_connection_full_capacity=True),
        expect_case="legacy_policy_applies", expect_field=("legacy_policy_applies", True)))
    results.append(run_case(
        "003 部分容量 -> false", "O-R-003",
        p(filing_date="2025-01-10", grid_connection_date="2025-04-20", grid_connection_full_capacity=False),
        expect_case="partial_capacity_not_legacy", expect_field=("legacy_policy_applies", False)))
    results.append(run_case(
        "003 1-24 备案 -> false", "O-R-003",
        p(filing_date="2025-01-24", grid_connection_date="2025-04-20", grid_connection_full_capacity=True),
        expect_case="filed_after_publication_not_legacy"))
    results.append(run_case(
        "003 5-2 并网 -> false", "O-R-003",
        p(filing_date="2025-01-10", grid_connection_date="2025-05-02", grid_connection_full_capacity=True),
        expect_case="late_connection_not_legacy"))
    results.append(run_case(
        "003 并网状态缺失 -> insufficient", "O-R-003",
        p(filing_date="2025-01-10", grid_connection_date=None, grid_connection_full_capacity=True),
        expect_status="insufficient"))

    # ---- O-R-001 广东设施农业分支
    results.append(run_case(
        "001 广东设施农业 10kV/5MW -> 一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="agricultural_structure", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", policy_as_of="2026-11-12"),
        expect_case="guangdong_agricultural_structure_general_industrial",
        expect_field=("project_class", "general_industrial_business")))
    results.append(run_case(
        "001 广东设施农业但问的是 2025-06 时点 -> 不套用现行广东口径，判 insufficient（无适用分类）", "O-R-001",
        p(investor_type="non_natural_person", site_category="agricultural_structure", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", policy_as_of="2025-06-01"),
        expect_status="insufficient"))
    results.append(run_case(
        "001 广东设施农业 8MW 超 6MW -> 广东 6MW 口径不覆盖，判 insufficient", "O-R-001",
        p(investor_type="non_natural_person", site_category="agricultural_structure", connection_voltage_kv=10,
          capacity_mw=8, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", policy_as_of="2026-11-12"),
        expect_status="insufficient"))

    # ---- O-R-001 广东农村公共建筑分支
    results.append(run_case(
        "001 广东农村公共建筑 10kV/5MW/rural=true -> 非自然人户用", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", rural_public_site=True, policy_as_of="2026-06-01"),
        expect_case="guangdong_rural_public_non_natural_household",
        expect_field=("project_class", "non_natural_person_household")))
    results.append(run_case(
        "001 广东村内道路/小公园（other_building）3MW/rural=true -> 非自然人户用", "O-R-001",
        p(investor_type="non_natural_person", site_category="other_building", connection_voltage_kv=10,
          capacity_mw=3, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", rural_public_site=True, policy_as_of="2027-01-01"),
        expect_case="guangdong_rural_public_non_natural_household"))
    results.append(run_case(
        "001 广东农村公共建筑缺 rural_public_site -> 不得静默落一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", policy_as_of="2026-06-01"),
        expect_status="insufficient",
        expect_blocked_by="guangdong_rural_public_non_natural_household"))
    results.append(run_case(
        "001 广东农村公共建筑缺 province -> 不得把未知当非广东", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          rural_public_site=True, policy_as_of="2026-06-01"),
        expect_status="insufficient",
        expect_blocked_by="guangdong_rural_public_non_natural_household"))
    results.append(run_case(
        "001 广东农村公共建筑缺 policy_as_of -> 不得猜是否在试行期内", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", rural_public_site=True),
        expect_status="insufficient",
        expect_blocked_by="guangdong_rural_public_non_natural_household"))
    results.append(run_case(
        "001 广东公共机构 rural_public_site=false -> 明确不适用，落一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", rural_public_site=False, policy_as_of="2026-06-01"),
        expect_case="general_industrial_business",
        expect_field=("project_class", "general_industrial_business")))
    results.append(run_case(
        "001 广东农村公共建筑 2026-02-10（试行首日）-> 非自然人户用", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", rural_public_site=True, policy_as_of="2026-02-10"),
        expect_case="guangdong_rural_public_non_natural_household"))
    results.append(run_case(
        "001 广东农村公共建筑 2028-02-09（期满当日）-> 非自然人户用", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", rural_public_site=True, policy_as_of="2028-02-09"),
        expect_case="guangdong_rural_public_non_natural_household"))
    results.append(run_case(
        "001 广东农村公共建筑 2028-02-10（期满次日）-> 新分支不覆盖，落一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", rural_public_site=True, policy_as_of="2028-02-10"),
        expect_case="general_industrial_business"))
    results.append(run_case(
        "001 广东农村公共建筑 6.5MW 超 6MW -> 不适用扩围", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          connection_type="public_grid", investor_same_legal_person_as_user=False,
          capacity_mw=6.5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="guangdong", rural_public_site=True, policy_as_of="2026-06-01"),
        expect_case="unclassified_complete_non_natural",
        expect_field=("project_class", "unclassified")))
    results.append(run_case(
        "001 其他省农村公共建筑 rural=true -> 国家口径一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", rural_public_site=True, policy_as_of="2026-06-01"),
        expect_case="general_industrial_business"))
    results.append(run_case(
        "001 广东农村公共建筑 same_red_line=false -> 原文未设红线条件，仍归非自然人户用", "O-R-001",
        p(investor_type="non_natural_person", site_category="public_institution", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=False,
          province="guangdong", rural_public_site=True, policy_as_of="2026-06-01"),
        expect_case="guangdong_rural_public_non_natural_household"))

    results.append(run_case(
        "001 广东厂房 A：2 回 10kV、单回 5MW、总 8MW -> 参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="guangdong_factory_multi_line_reference_large"))
    results.append(run_case(
        "001 广东厂房 A：光伏 380V 接入 2 回 10kV 线路、总 8MW -> 仍参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=0.38, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="guangdong_factory_multi_line_reference_large"))
    results.append(run_case(
        "001 广东厂房仅 1 回 10kV、5MW -> 不参照，按一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=5, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="general_industrial_business"))
    results.append(run_case(
        "001 广东厂房单回接入 6.5MW -> 超每回 6MW，落 unclassified", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=6.5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="unclassified_complete_non_natural"))
    results.append(run_case(
        "001 广东厂房 10 回、单回 6MW、总 55MW -> 超 50MW，落 unclassified", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=55, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=10, max_pv_capacity_per_10kv_line_mw=6, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="unclassified_complete_non_natural"))
    results.append(run_case(
        "001 广东厂房全部自发自用、并网 10kV 不高于既有 10kV -> 参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="full_self_consumption", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="guangdong_factory_full_self_consumption_reference_large"))
    results.append(run_case(
        "001 广东厂房全部自发自用、容量与容量口径均未填 -> B 支无容量门槛，仍参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=None, capacity_basis=None, grid_mode="full_self_consumption", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="guangdong_factory_full_self_consumption_reference_large"))
    results.append(run_case(
        "001 广东厂房全部自发自用、8MW、直流侧容量口径 -> B 支不套容量阈值，仍参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="dc_module_sum", grid_mode="full_self_consumption", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="guangdong_factory_full_self_consumption_reference_large"))
    results.append(run_case(
        "001 广东厂房全部自发自用、光伏 380V 接入、既有 10kV -> 参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=0.38, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="full_self_consumption", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="guangdong_factory_full_self_consumption_reference_large"))
    results.append(run_case(
        "001 广东厂房全部自发自用，但并网 20kV 高于既有 10kV -> 不参照，落 unclassified", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=20, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="full_self_consumption", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="unclassified_complete_non_natural"))
    results.append(run_case(
        "001 广东厂房余电上网，不满足全部自发自用 -> 不参照，落 unclassified", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="unclassified_complete_non_natural"))
    results.append(run_case(
        "001 广东厂房缺线路回数 -> 不得静默落一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=None, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_status="insufficient",
        expect_blocked_by="guangdong_factory_multi_line_reference_large"))
    results.append(run_case(
        "001 广东厂房缺单回最大容量 -> 不得静默落一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=None, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_status="insufficient",
        expect_blocked_by="guangdong_factory_multi_line_reference_large"))
    results.append(run_case(
        "001 广东厂房全部自发自用，缺既有供电电压 -> 不得静默落一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="full_self_consumption", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=None, policy_as_of="2026-06-01"),
        expect_status="insufficient",
        expect_blocked_by="guangdong_factory_full_self_consumption_reference_large"))
    results.append(run_case(
        "001 广东厂房 2 回 10kV/8MW，缺省份 -> 不得把未知当非广东", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="full_self_consumption", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province=None, factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_status="insufficient",
        expect_blocked_by="guangdong_factory_full_self_consumption_reference_large"))
    results.append(run_case(
        "001 广东厂房 2 回 10kV/8MW，缺 policy_as_of -> 不得猜是否在试行期", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=8, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of=None),
        expect_status="insufficient",
        expect_blocked_by="guangdong_factory_multi_line_reference_large"))
    results.append(run_case(
        "001 广东厂房 2 回 10kV/5MW，2026-02-10（试行首日）-> 参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=5, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-02-10"),
        expect_case="guangdong_factory_multi_line_reference_large"))
    results.append(run_case(
        "001 广东厂房 2 回 10kV/5MW，2028-02-09（期满当日）-> 参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=5, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2028-02-09"),
        expect_case="guangdong_factory_multi_line_reference_large"))
    results.append(run_case(
        "001 广东厂房 2 回 10kV/5MW，2028-02-10（期满次日）-> 新分支不覆盖，按一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=5, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2028-02-10"),
        expect_case="general_industrial_business"))
    results.append(run_case(
        "001 其他省厂房 2 回 10kV/5MW -> 广东参照分支不适用，按一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=5, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="zhejiang", factory_10kv_line_count=2, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="general_industrial_business"))
    results.append(run_case(
        "001 广东厂房全部自发自用、并网不高于既有，60MW -> 原文未设容量上限，仍参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=60, capacity_basis="ac_inverter_sum", grid_mode="full_self_consumption", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=1, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="guangdong_factory_full_self_consumption_reference_large"))
    results.append(run_case(
        "001 广东厂房 10 回、单回 5MW、总 50MW -> 50MW 上限边界，参照大型工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="industrial_commercial", connection_voltage_kv=10, connection_type="user_side_grid", investor_same_legal_person_as_user=True, capacity_mw=50, capacity_basis="ac_inverter_sum", grid_mode="self_consumption_surplus_feed_in", spot_market_region=False, same_red_line=True, site_owned_by_investor=True, province="guangdong", factory_10kv_line_count=10, max_pv_capacity_per_10kv_line_mw=5, existing_supply_voltage_kv=10, policy_as_of="2026-06-01"),
        expect_case="guangdong_factory_multi_line_reference_large"))

    # ---- O-R-001 浙江设施农业分支
    results.append(run_case(
        "001 浙江设施农业 10kV/5MW -> 一般工商业", "O-R-001",
        p(investor_type="non_natural_person", site_category="agricultural_structure", connection_voltage_kv=10,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-09-01"),
        expect_case="zhejiang_facility_agriculture_general_industrial",
        expect_field=("project_class", "general_industrial_business")))
    results.append(run_case(
        "001 浙江设施农业 6MW（不低于6MW -> 集中式，超四类）-> unclassified", "O-R-001",
        p(investor_type="non_natural_person", site_category="agricultural_structure", connection_voltage_kv=10,
          connection_type="public_grid", investor_same_legal_person_as_user=False,
          capacity_mw=6, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-09-01"),
        expect_case="unclassified_complete_non_natural",
        expect_field=("project_class", "unclassified")))
    results.append(run_case(
        "001 浙江设施农业但 policy_as_of 在细则生效前 -> 不套浙江分支", "O-R-001",
        p(investor_type="non_natural_person", site_category="agricultural_structure", connection_voltage_kv=10,
          connection_type="public_grid", investor_same_legal_person_as_user=False,
          capacity_mw=5, capacity_basis="ac_inverter_sum", same_red_line=True,
          province="zhejiang", policy_as_of="2026-06-01"),
        expect_case="unclassified_complete_non_natural"))

    # ---- O-R-004 广东 30% 比例
    results.append(run_case(
        "004 广东设施农业 35% -> met", "O-R-004",
        p(province="guangdong", project_class="general_industrial_business",
          site_category="agricultural_structure", grid_mode="self_consumption_surplus_feed_in",
          self_consumption_ratio_pct=35),
        expect_case="gd_ratio_met", expect_field=("provincial_ratio_requirement", "met")))
    results.append(run_case(
        "004 广东设施农业 20% -> not_met", "O-R-004",
        p(province="guangdong", project_class="general_industrial_business",
          site_category="agricultural_structure", grid_mode="self_consumption_surplus_feed_in",
          self_consumption_ratio_pct=20),
        expect_case="gd_ratio_not_met", expect_field=("provincial_ratio_requirement", "not_met")))
    results.append(run_case(
        "004 三日期满足豁免 -> exempted", "O-R-004",
        p(province="guangdong", project_class="general_industrial_business",
          site_category="agricultural_structure", grid_mode="self_consumption_surplus_feed_in",
          filing_date="2024-12-01", grid_opinion_date="2025-06-01",
          construction_completion_date="2026-03-01"),
        expect_case="gd_ratio_exempted", expect_field=("provincial_ratio_requirement", "exempted")))
    results.append(run_case(
        "004 豁免三日期边界（1-23 / 12-30 / 4-10）-> exempted", "O-R-004",
        p(province="guangdong", project_class="general_industrial_business",
          site_category="agricultural_structure", grid_mode="self_consumption_surplus_feed_in",
          filing_date="2025-01-23", grid_opinion_date="2025-12-30",
          construction_completion_date="2026-04-10"),
        expect_case="gd_ratio_exempted"))
    results.append(run_case(
        "004 备案 1-24 且无比例 -> insufficient", "O-R-004",
        p(province="guangdong", project_class="general_industrial_business",
          site_category="agricultural_structure", grid_mode="self_consumption_surplus_feed_in",
          filing_date="2025-01-24"),
        expect_status="insufficient"))
    results.append(run_case(
        "004 浙江一般工商业 20% + 细则生效后 -> no_requirement", "O-R-004",
        p(province="zhejiang", project_class="general_industrial_business",
          site_category="industrial_commercial", grid_mode="self_consumption_surplus_feed_in",
          self_consumption_ratio_pct=20, policy_as_of="2026-09-01"),
        expect_case="zj_no_requirement",
        expect_field=("provincial_ratio_requirement", "no_requirement")))
    results.append(run_case(
        "004 浙江大型工商业 35% + 细则生效后 -> no_requirement", "O-R-004",
        p(province="zhejiang", project_class="large_industrial_business",
          site_category="industrial_commercial", grid_mode="self_consumption_surplus_feed_in",
          self_consumption_ratio_pct=35, policy_as_of="2026-09-01"),
        expect_case="zj_no_requirement",
        expect_field=("provincial_ratio_requirement", "no_requirement")))
    results.append(run_case(
        "004 浙江未给 policy_as_of -> not_applicable（保守，不冒充免比例）", "O-R-004",
        p(province="zhejiang", project_class="general_industrial_business",
          site_category="agricultural_structure", grid_mode="self_consumption_surplus_feed_in",
          self_consumption_ratio_pct=20),
        expect_case="provincial_ratio_not_applicable",
        expect_field=("provincial_ratio_requirement", "not_applicable")))
    results.append(run_case(
        "004 浙江细则生效前（2026-06-01）-> not_applicable", "O-R-004",
        p(province="zhejiang", project_class="general_industrial_business",
          site_category="industrial_commercial", grid_mode="self_consumption_surplus_feed_in",
          self_consumption_ratio_pct=20, policy_as_of="2026-06-01"),
        expect_case="provincial_ratio_not_applicable"))
    results.append(run_case(
        "004 广东 policy_as_of=2029-01-01（试行期满）-> not_applicable", "O-R-004",
        p(province="guangdong", project_class="general_industrial_business",
          site_category="agricultural_structure", grid_mode="self_consumption_surplus_feed_in",
          self_consumption_ratio_pct=40, policy_as_of="2029-01-01"),
        expect_case="gd_policy_expired",
        expect_field=("provincial_ratio_requirement", "not_applicable")))
    results.append(run_case(
        "004 广东一般工商业但非设施农业 -> not_applicable", "O-R-004",
        p(province="guangdong", project_class="general_industrial_business",
          site_category="industrial_commercial", grid_mode="self_consumption_surplus_feed_in",
          self_consumption_ratio_pct=20),
        expect_case="provincial_ratio_not_applicable"))

    print(f"\n{sum(results)}/{len(results)} 行为用例通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
