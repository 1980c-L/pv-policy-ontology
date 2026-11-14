# -*- coding: utf-8 -*-
"""ask/v1 契约测试：输入形状、错误码、批量顺序、schema 校验。

只测接口契约，不重复测规则语义（那由 assess / behavior / scenario 覆盖）。
依赖 jsonschema（系统 Python 已有 4.26）；缺失时退化为手检关键字段。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASK = ROOT / "tools" / "ask.py"
RESPONSE_SCHEMA = ROOT / "interface" / "ask.schema.json"
INPUT_SCHEMA = ROOT / "interface" / "ask-input.schema.json"
EXAMPLE_PROJECT = ROOT / "eval" / "project.example.json"

ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

try:
    from jsonschema import Draft202012Validator
except Exception:  # pragma: no cover - 环境缺包时仍跑关键断言
    Draft202012Validator = None


def run(args, stdin=None):
    return subprocess.run(
        [sys.executable, str(ASK), *args],
        cwd=str(ROOT),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ENV,
    )


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def assert_schema(payload: dict, label: str):
    if Draft202012Validator is None:
        assert payload.get("schema_version") == "ask/v1", label
        return
    validator = Draft202012Validator(load(RESPONSE_SCHEMA))
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    assert not errors, f"{label} schema 失败：" + "；".join(
        f"/{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5])


def parse(proc):
    assert proc.returncode == 0, f"exit={proc.returncode} stderr={proc.stderr[-800:]}"
    return json.loads(proc.stdout)


def expect_error(proc, code: str = "invalid_request"):
    assert proc.returncode == 2, f"期望 exit 2，实际 {proc.returncode}；stderr={proc.stderr[-500:]}"
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == code, payload
    assert_schema(payload, "error")
    return payload


def test_version():
    p = run(["--version"])
    assert p.returncode == 0 and p.stdout.strip() == "ask/v1", p.stdout


def test_dump_schema():
    p = run(["--dump-schema"])
    payload = json.loads(p.stdout)
    assert payload.get("$id") == "urn:newenergy-ontology:ask:v1:response", payload.get("$id")


def test_single():
    p = run(["--question", "分布式光伏发电分为哪四种类型？", "--json"])
    payload = parse(p)
    assert_schema(payload, "single")
    assert len(payload["results"]) == 1
    r = payload["results"][0]
    assert r["answer"]["verdict"] == "evidence_only", r["answer"]["verdict"]
    assert len(r["retrieval"]["evidence"]) == 5
    assert r["retrieval"]["params"]["b"] == 0.3, r["retrieval"]["params"]
    assert isinstance(r["warnings"], list)


def test_batch_stdin():
    batch = [
        {"question": "分布式光伏发电分为哪四种类型？", "request_id": "a", "top_k": 2},
        {"question": "浙江细则有哪些要点？", "request_id": "b"},
    ]
    p = run(["--input", "-", "--json"], stdin=json.dumps(batch, ensure_ascii=False))
    payload = parse(p)
    assert_schema(payload, "batch")
    assert [r.get("request_id") for r in payload["results"]] == ["a", "b"]
    assert len(payload["results"][0]["retrieval"]["evidence"]) == 2
    assert payload["results"][1]["scenario"] == "province", payload["results"][1]["scenario"]
    assert payload["results"][1]["answer"]["verdict"] == "province_summary"


def test_project():
    project = load(EXAMPLE_PROJECT)
    req = {"question": "这个项目属于哪类？", "project": project, "policy_as_of": "2026-11-12"}
    p = run(["--input", "-", "--json"], stdin=json.dumps(req, ensure_ascii=False))
    payload = parse(p)
    assert_schema(payload, "project")
    r = payload["results"][0]
    assert r["assessment"] is not None
    assert r["scenario"] == "classification"
    assert r["answer"]["verdict"] == "supported", r["answer"]["verdict"]
    assert r["answer"]["source_refs"]


def test_project_date_not_misread():
    """N2：项目对象缺 policy_as_of、但请求给了日期时，不得提示“未提供日期”。"""
    project = {
        "project_id": "W2", "investor_type": "non_natural_person",
        "site_category": "industrial_commercial", "connection_voltage_kv": 66,
        "connection_type": "user_side_grid", "investor_same_legal_person_as_user": True,
        "capacity_mw": 40, "capacity_basis": "ac_inverter_sum",
        "grid_mode": "full_self_consumption", "spot_market_region": False,
        "same_red_line": True, "filing_date": "2025-03-01",
        "grid_connection_date": "2025-03-01", "grid_connection_full_capacity": True,
        "province": "zhejiang",
    }
    for as_of, expected in (("2026-08-02", "large_industrial_business"),
                            ("2026-09-01", "unclassified")):
        req = {"question": "这个项目属于哪类？", "project": project, "policy_as_of": as_of}
        payload = parse(run(["--input", "-", "--json"], stdin=json.dumps(req, ensure_ascii=False)))
        r = payload["results"][0]
        classes = [c["value"] for c in r["assessment"]["conclusions"] if c["field"] == "project_class"]
        assert classes == [expected], (as_of, classes)
        text = "；".join(r["answer"]["points"])
        assert "未提供 policy_as_of" not in text, (as_of, text)


def test_project_date_only_in_project():
    """N7：日期只在项目对象里时，不得反向提示“未提供 policy_as_of”。"""
    project = {
        "project_id": "W2", "investor_type": "non_natural_person",
        "site_category": "industrial_commercial", "connection_voltage_kv": 66,
        "connection_type": "user_side_grid", "investor_same_legal_person_as_user": True,
        "capacity_mw": 40, "capacity_basis": "ac_inverter_sum",
        "grid_mode": "full_self_consumption", "spot_market_region": False,
        "same_red_line": True, "filing_date": "2025-03-01",
        "grid_connection_date": "2025-03-01", "grid_connection_full_capacity": True,
        "province": "zhejiang", "policy_as_of": "2026-08-02",
    }
    req = {"question": "这个项目属于哪类？", "project": project}
    payload = parse(run(["--input", "-", "--json"], stdin=json.dumps(req, ensure_ascii=False)))
    r = payload["results"][0]
    classes = [c["value"] for c in r["assessment"]["conclusions"] if c["field"] == "project_class"]
    assert classes == ["large_industrial_business"], classes
    assert "未提供 policy_as_of" not in "；".join(r["answer"]["points"])

def test_scope():
    p = run(["--question", "储能消防间距有规定吗？", "--json"])
    payload = parse(p)
    assert_schema(payload, "scope")
    r = payload["results"][0]
    assert r["scenario"] == "scope"
    assert r["answer"]["verdict"] == "out_of_scope"
    assert r["answer"]["source_refs"]


def test_unknown_field():
    p = run(["--input", "-", "--json"], stdin='{"question":"x","foo":1}')
    payload = expect_error(p)
    assert payload["error"]["details"][0]["field"] == "foo"


def test_invalid_topk():
    for bad in (0, 21, "5", True):
        p = run(["--input", "-", "--json"], stdin=json.dumps({"question": "x", "top_k": bad}))
        expect_error(p)


def test_invalid_date():
    for bad in ("2026-13-01", "2026-1-1", "20261112"):
        p = run(["--input", "-", "--json"], stdin=json.dumps({"question": "x", "policy_as_of": bad}))
        expect_error(p)


def test_missing_question():
    p = run(["--input", "-", "--json"], stdin='{"top_k":3}')
    expect_error(p)


def test_empty_batch():
    p = run(["--input", "-", "--json"], stdin="[]")
    payload = parse(p)
    assert payload["results"] == []
    assert_schema(payload, "empty")


def test_scalar_input():
    p = run(["--input", "-", "--json"], stdin='"hello"')
    expect_error(p)


def test_conflict_args():
    p = run(["--question", "x", "--input", "-", "--json"], stdin="{}")
    expect_error(p)


def test_human_mode():
    p = run(["--question", "分布式光伏发电分为哪四种类型？"])
    assert p.returncode == 0, p.stderr[-500:]
    assert "问题：" in p.stdout and "路由：" in p.stdout


def test_bad_json():
    p = run(["--input", "-", "--json"], stdin="{not json")
    expect_error(p)


def test_input_schema_file():
    schema = load(INPUT_SCHEMA)
    assert schema.get("$id") == "urn:newenergy-ontology:ask:v1:input"
    if Draft202012Validator is None:
        return
    req = {"question": "浙江细则有哪些要点？", "top_k": 3}
    Draft202012Validator(schema).validate(req)
    Draft202012Validator(schema).validate([req, req])


TESTS = [
    test_version, test_dump_schema, test_single, test_batch_stdin, test_project,
    test_project_date_not_misread, test_project_date_only_in_project, test_scope, test_unknown_field,
    test_invalid_topk, test_invalid_date,
    test_missing_question, test_empty_batch, test_scalar_input, test_conflict_args,
    test_human_mode, test_bad_json, test_input_schema_file,
]


def main() -> int:
    passed = failed = 0
    for fn in TESTS:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - 契约测试要把失败原因打全
            failed += 1
            print(f"[FAIL] {fn.__name__}：{e}")
        else:
            passed += 1
            print(f"[PASS] {fn.__name__}")
    print(f"ask/v1 契约测试：{passed}/{passed + failed} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
