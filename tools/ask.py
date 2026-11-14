# -*- coding: utf-8 -*-
"""最小的端到端问答链路：自然语言问题 -> 检索证据 -> 规则判定 -> 带引用回答。

设计边界：
- 只做“路由 + 组织答案”，不实现新的检索算法（复用 BM25 + 证据等级重排）。
- 规则判定只调用 src/rule_eval.assess_project，不复制规则逻辑。
- 检索只提供候选证据，不参与判定；判定结果永远来自 rules.json。
- 不联网、不调用模型；纯 stdlib + 本仓库代码。

CLI（ask/v1 稳定接口，契约见 interface/README.md）:
    python tools/ask.py --question "10千伏8兆瓦的项目属于一般工商业吗？"
    python tools/ask.py --question "这个项目还能按原政策吗？" --project eval/project.example.json
    python tools/ask.py --question "..." --json
    echo '{"question":"分布式光伏分几类？","top_k":3}' | python tools/ask.py --input -
    python tools/ask.py --input requests.json --json
    python tools/ask.py --dump-schema
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import rule_eval  # noqa: E402
import retrieval_eval2 as ret  # noqa: E402

SCHEMA_VERSION = "ask/v1"
TOP_K = 5          # 缺省值；运行时以 knowledge/retrieval_params.json 为准
LEVEL_BONUS = 0.05  # 缺省值；同上
DEFAULT_PARAMS = {"k1": 1.5, "b": 0.75, "level_bonus": LEVEL_BONUS, "top_k": TOP_K}

CLASS_LABELS = {
    "natural_person_household": "自然人户用",
    "non_natural_person_household": "非自然人户用",
    "general_industrial_business": "一般工商业",
    "large_industrial_business": "大型工商业",
    "unclassified": "不属于四类（字段齐备但不符合）",
}
MODE_LABELS = {
    "full_feed_in": "全额上网",
    "full_self_consumption": "全部自发自用",
    "self_consumption_surplus_feed_in": "自发自用余电上网",
}

# 语料范围声明与省级细则要点：数据放 knowledge/，不在代码里硬写政策结论
DATA_DIR = ROOT / "knowledge"
_ROUTE_CACHE: dict | None = None
_DOCS: list[dict] | None = None


def corpus_docs() -> list[dict]:
    global _DOCS
    if _DOCS is None:
        _DOCS = ret.load_corpus()
    return _DOCS


def _load_json(name: str) -> dict:
    path = DATA_DIR / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _retrieval_routes() -> list[dict]:
    global _ROUTE_CACHE
    if _ROUTE_CACHE is None:
        _ROUTE_CACHE = _load_json("retrieval_routes.json").get("provinces", [])
    return _ROUTE_CACHE


_PARAMS_CACHE: dict | None = None


def retrieval_params() -> dict:
    """运行时检索参数；ask 与证据回检共用，避免两套口径。"""
    global _PARAMS_CACHE
    if _PARAMS_CACHE is None:
        raw = _load_json("retrieval_params.json")
        bm25 = raw.get("bm25") or {}
        p = {
            "k1": float(bm25.get("k1", DEFAULT_PARAMS["k1"])),
            "b": float(bm25.get("b", DEFAULT_PARAMS["b"])),
            "level_bonus": float(raw.get("level_bonus", DEFAULT_PARAMS["level_bonus"])),
            "top_k": int(raw.get("top_k", DEFAULT_PARAMS["top_k"])),
        }
        if p["k1"] <= 0 or not (0 <= p["b"] <= 1) or p["level_bonus"] < 0:
            raise ValueError(f"retrieval_params.json 取值不合法：{p}")
        if not 1 <= p["top_k"] <= 50:
            raise ValueError(f"retrieval_params.json top_k 越界：{p['top_k']}")
        _PARAMS_CACHE = p
    return _PARAMS_CACHE


def route_docs(docs: list[dict], question: str) -> tuple[list[dict], str]:
    """按提问里的省名收窄候选：命中省名 → 国家 + 该省；未命中 → 只留国家来源。

    只有一个省时才收窄；同时提到两个省则不过滤（避免误删）。
    """
    routes = _retrieval_routes()
    prefixes = [r["source_prefix"] for r in routes]
    is_prov = lambda d: any(d["source_id"].startswith(p) for p in prefixes)  # noqa: E731
    hits = [(r["name"], r["source_prefix"]) for r in routes if r["name"] in question]
    if not hits:
        return [d for d in docs if not is_prov(d)], "national"
    if len(hits) > 1:
        return docs, "multi_province"
    _, prefix = hits[0]
    return [d for d in docs if (not is_prov(d)) or d["source_id"].startswith(prefix)], hits[0][0]


def match_scope(question: str) -> dict | None:
    """命中范围声明才返回；组内 AND、组间 OR。越靠前的条目越具体。"""
    for entry in _load_json("scope.json").get("entries", []):
        for group in entry.get("match", []):
            if all(w in question for w in group):
                return entry
    return None

# 路由关键词：先匹配更具体的，再落到通用证据检索
ROUTES = [
    ("legacy", ("原有政策", "原政策", "老政策", "新老", "划断", "5月1日", "五月一日",
                "发布之日前", "发布之日", "并网投产")),
    ("grid_mode", ("上网模式", "上网方式", "怎么上网", "能否上网", "余电", "全额上网",
                   "自发自用", "上网")),
    ("classification", ("属于哪", "哪一类", "什么类型", "分类", "户用", "工商业", "容量上限",
                        "算哪", "算不算", "属于", "归哪", "几类", "几种")),
    ("effective", ("施行", "有效期", "废止", "生效", "什么时候开始")),
    ("capacity", ("容量", "兆瓦", "千瓦", "交流侧", "逆变器")),
    ("province", ("浙江", "广东", "江苏", "省细则", "细则", "省级")),
]


def route(question: str) -> str:
    for name, words in ROUTES:
        if any(w in question for w in words):
            return name
    return "evidence"


def best_quote(text: str, question: str, limit: int = 100) -> str:
    """取与问题词面重合度最高的一句话；找不到就取开头。"""
    q = ret.norm_text(question)
    q_grams = {q[i:i + 2] for i in range(max(len(q) - 1, 0))}
    parts = [p.strip() for p in re.split(r"[。；\n]", text) if p.strip()]
    if not parts:
        return text[:limit]
    # FAQ 的 text 常以“问题？”开头：优先取真正的回答句，避免引用一个问句
    answers = [p for p in parts if not p.endswith("？")]
    parts = answers or parts
    best = max(parts, key=lambda p: sum(1 for g in q_grams if g in ret.norm_text(p)))
    best = best if len(best) <= limit else best[:limit] + "…"
    return best


def rank_docs(question: str, docs: list[dict], bm25=None, params: dict | None = None) -> list[tuple[float, int]]:
    """对给定候选池打分排序；BM25 必须在池内建索引（idf/avg_len 随池变）。"""
    p = params or retrieval_params()
    if not docs:
        return []
    bm = bm25 or ret.BM25(docs, k1=p["k1"], b=p["b"])
    raw = bm.score(question)
    lo, hi = min(raw), max(raw)
    span = (hi - lo) or 1.0
    scored = []
    for i, d in enumerate(docs):
        base = (raw[i] - lo) / span
        lvl = p["level_bonus"] * ret.LEVEL_WEIGHT.get(d["level"], 0.0)
        scored.append((base + lvl, i))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def retrieve(question: str, top_k: int | None = None, docs: list[dict] | None = None) -> dict:
    p = retrieval_params()
    if top_k is None:
        top_k = p["top_k"]
    if docs is None:
        docs, route_name = route_docs(corpus_docs(), question)
    else:
        route_name = "provided"
    scored = rank_docs(question, docs, params=p)
    evidence = []
    for rank, (score, i) in enumerate(scored[:top_k], 1):
        d = docs[i]
        evidence.append({
            "rank": rank,
            "locator_id": d["locator_id"],
            "source_id": d["source_id"],
            "kind": d["kind"],
            "evidence_level": d["level"],
            "score": round(score, 4),
            "quote": best_quote(d["text"], question),
        })
    return {
        "method": f"bm25+level_bonus+route:{route_name}",
        "params": {"k1": p["k1"], "b": p["b"], "level_bonus": p["level_bonus"]},
        "corpus_units": len(docs),
        "evidence": evidence,
    }


def _cite_refs(locators: list[str], question: str, warnings: list[str] | None = None) -> list[dict]:
    """把 locator id 解析成带引文的 source_refs；找不到的跳过并记入 warnings。"""
    docs = {d["locator_id"]: d for d in corpus_docs()}
    refs = []
    for loc in locators:
        d = docs.get(loc)
        if not d:
            msg = f"引文不存在：{loc}"
            print(f"[warn] {msg}", file=sys.stderr)
            if warnings is not None:
                warnings.append(msg)
            continue
        refs.append({
            "locator_id": d["locator_id"],
            "source_id": d["source_id"],
            "evidence_level": d["level"],
            "quote": best_quote(d["text"], question, limit=120),
        })
    return refs


def scope_answer(entry: dict, question: str, warnings: list[str] | None = None) -> dict:
    """范围声明命中：明确说不代答，并给出能支撑“为什么”的原文（如果有）。"""
    return {
        "verdict": entry["verdict"],
        "scenario": "scope",
        "summary": entry["answer"],
        "points": [entry["answer"], "范围说明：" + entry["reason"]],
        "missing_fields": [],
        "source_refs": _cite_refs(entry.get("cite", []), question, warnings),
    }


def province_answer(question: str, evidence: dict, warnings: list[str] | None = None) -> dict:
    notes = _load_json("province_notes.json").get("provinces", {})
    key = "zhejiang" if "浙江" in question else "guangdong" if "广东" in question else None
    if not key:
        return {
            "verdict": "province_pointer",
            "scenario": "province",
            "summary": "已收录浙江、广东两省细则；江苏等省暂未收录全文。",
            "points": ["已收录浙江（PV-ZJ-2026-015）、广东（PV-GD-2025-002）两省细则；"
                       "其他省份暂未收录正式文本，不能代答。请补充省份与项目结构化字段，或先看候选证据。"],
            "missing_fields": [],
            "source_refs": [],
        }
    prov = notes[key]
    return {
        "verdict": "province_summary",
        "scenario": "province",
        "summary": f"{prov['label']}细则要点（PV-{'ZJ-2026-015' if key == 'zhejiang' else 'GD-2025-002'}）："
                   + prov["points"][0],
        "points": prov["points"] + ["规则覆盖：" + prov["rule_note"]],
        "missing_fields": [],
        "source_refs": _cite_refs(prov["cites"], question, warnings),
    }


def _conclusion(assessment: dict, field: str):
    for c in assessment.get("conclusions", []):
        if c.get("field") == field:
            return c.get("value"), c.get("rule_id"), c.get("case_id")
    return None, None, None


def build_answer(question: str, scenario: str, evidence: dict, assessment: dict | None,
                 project: dict | None = None) -> dict:
    points: list[str] = []
    missing: list[str] = []
    verdict = "evidence_only"

    if assessment:
        missing = assessment.get("missing_fields", [])
        verdict = assessment.get("result_status", "unknown")
        cls, cls_rule, cls_case = _conclusion(assessment, "project_class")
        modes, modes_rule, _ = _conclusion(assessment, "allowed_grid_modes")
        legacy, legacy_rule, _ = _conclusion(assessment, "legacy_policy_applies")

        if scenario == "classification" and cls:
            points.append(f"判定：{CLASS_LABELS.get(cls, cls)}（规则 {cls_rule}，命中 {cls_case}）。")
        if scenario == "grid_mode" and modes:
            label = "、".join(MODE_LABELS.get(m, m) for m in modes)
            points.append(f"可选上网模式：{label}（规则 {modes_rule}）。")
            points.append("若涉及自发自用，用户与项目应位于同一用地红线内。")
        if scenario == "legacy" and legacy is not None:
            text = "仍可按原有政策执行" if legacy else "不能按原有政策执行"
            points.append(f"新老划断结论：{text}（规则 {legacy_rule}）。")
            points.append("边界：备案不晚于 2025-01-23（含当日），并网不晚于 2025-05-01（含当日）且须全容量并网。")
        if verdict == "insufficient" and not (scenario == "classification" and cls):
            points.append("现有信息不足，不能下结论；缺字段：" + ("、".join(missing) if missing else "见 reasons"))
        if verdict == "unclassified" or (scenario == "classification" and cls == "unclassified"):
            if cls_case == "zhejiang_66kv_centralized":
                points.append("浙江细则第三条在大型工商业中只列 35 千伏和 110 千伏，没有国家办法的“（66千伏）”；"
                              "第四条把容量、电压等级超出第三条的项目按集中式光伏电站备案管理。"
                              "因此不按分布式四类归类，但不等于不允许建设；应按集中式口径备案管理。")
            else:
                points.append("字段齐备但不符合四类条件：不等于“不允许建设”，需按其他政策或人工复核。")
                cap = (project or {}).get("capacity_mw")
                kv = (project or {}).get("connection_voltage_kv")
                if cap and cap > 6 and kv is not None and kv <= 20:
                    points.append(f"容量 {cap:g} MW 超过一般工商业“原则上不超过 6 MW”的上限，"
                                  "按四类定义不能直接归入一般工商业；国家办法没有“超限即转集中式”的明文，"
                                  "是否按集中式备案由地方规定或备案机关认定（浙江细则第四条明确超限按集中式备案）。")
        if (scenario == "classification" and cls == "large_industrial_business"
                and cls_case == "large_industrial_business_110kv"
                and (project or {}).get("province") == "zhejiang"
                and (project or {}).get("connection_voltage_kv") == 66
                and (assessment.get("temporal_scope") or {}).get("policy_as_of") is None):
            points.append("注意：未提供 policy_as_of，当前按国家办法的 66 千伏分支判为大型工商业；"
                          "浙江细则自 2026-08-03 起未把 66 千伏列入大型工商业，补上 policy_as_of 后会按集中式口径提示。")
    else:
        if scenario == "classification":
            points.append("政策把分布式光伏分为自然人户用、非自然人户用、一般工商业、大型工商业四类（第四条）。"
                          "要判定某个项目属于哪类，需要补充投资者类型、场地、电压、容量口径、连接方式等信息。")
        elif scenario == "grid_mode":
            points.append("政策列出的上网模式有三种：全额上网、全部自发自用、自发自用余电上网（第五条）。"
                          "户用两类三种都可选；一般工商业只能全自发自用或余电上网；大型工商业原则上全部自发自用，"
                          "现货市场连续运行地区可余电上网。具体范围要结合项目类型判定。")
        elif scenario == "legacy":
            points.append("能否按原有政策，看两个日期和一个状态：备案不晚于 2025-01-23（含当日）、"
                          "并网不晚于 2025-05-01（含当日）、且必须是全容量并网。")
        elif scenario == "capacity":
            points.append("政策里的容量指交流侧容量，也就是逆变器额定输出功率之和（第十三条）。"
                          "备案容量、装机容量、交流侧容量不是一回事，具体看证据引用。")
        elif scenario == "effective":
            points.append("本办法自发布之日起施行，有效期五年（第四十三条）；废止 2013 年暂行办法。")
        else:
            points.append("未提供项目结构化字段；本轮给候选证据，供人工核对。")

    if evidence["evidence"]:
        points.append("最相关原文：" + evidence["evidence"][0]["quote"])

    if assessment and scenario == "effective":
        points.append("施行/有效期以正文第四十三条为准；具体日期见证据引用。")
    if assessment and scenario == "capacity":
        points.append("容量口径按第十三条：交流侧容量，即逆变器额定输出功率之和。")

    points.append("证据（候选，供人工核对）：" + "；".join(
        f"[{e['rank']}] {e['locator_id']}（{e['evidence_level']}）" for e in evidence["evidence"][:3]))

    return {
        "verdict": verdict,
        "scenario": scenario,
        "summary": points[0] if points else "",
        "points": points,
        "missing_fields": missing,
        "source_refs": [
            {"locator_id": e["locator_id"], "source_id": e["source_id"],
             "evidence_level": e["evidence_level"], "quote": e["quote"]}
            for e in evidence["evidence"]
        ],
    }


def ask(question: str, project: dict | None = None, policy_as_of: str | None = None,
        top_k: int | None = None, request_id: str | None = None) -> dict:
    if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20):
        raise ValueError("top_k 必须是 1..20 的整数")
    warnings: list[str] = []
    scenario = route(question)
    evidence = retrieve(question, top_k=top_k)
    scope = match_scope(question)
    assessment = None
    if project and not scope:
        assessment = rule_eval.assess_project(project, policy_as_of=policy_as_of)
    if scope:
        scenario = "scope"
        answer = scope_answer(scope, question, warnings)
    elif scenario == "province":
        answer = province_answer(question, evidence, warnings)
    else:
        answer = build_answer(question, scenario, evidence, assessment, project)
    result = {
        "schema_version": SCHEMA_VERSION,
        "question": question,
        "scenario": scenario,
        "retrieval": evidence,
        "assessment": assessment,
        "answer": answer,
        "warnings": warnings,
    }
    if request_id is not None:
        result["request_id"] = request_id
    return result


ALLOWED_FIELDS = {"question", "project", "policy_as_of", "top_k", "request_id"}


class RequestError(Exception):
    """输入不合法；CLI 统一转成 ask/v1 错误对象。"""


def read_json_file(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise RequestError(f"读不到文件 {path}：{e}") from e
    except json.JSONDecodeError as e:
        raise RequestError(f"文件不是合法 JSON：{path}（{e}）") from e


def collect_requests(args) -> list[dict]:
    single = (args.question is not None or args.project or args.policy_as_of is not None
              or args.top_k is not None)
    if args.input:
        if single:
            raise RequestError("--input 不能与 --question/--project/--policy-as-of/--top-k 同时使用")
        if args.input == "-":
            text = sys.stdin.read()
        else:
            try:
                text = Path(args.input).read_text(encoding="utf-8")
            except OSError as e:
                raise RequestError(f"读不到输入文件 {args.input}：{e}") from e
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RequestError(f"输入不是合法 JSON：{e}") from e
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
        raise RequestError("输入必须是 JSON 对象或对象数组")

    if not args.question:
        raise RequestError("缺少 --question 或 --input")
    req: dict = {"question": args.question}
    if args.project:
        req["project"] = read_json_file(args.project)
    if args.policy_as_of is not None:
        req["policy_as_of"] = args.policy_as_of
    if args.top_k is not None:
        try:
            req["top_k"] = int(args.top_k)
        except ValueError as e:
            raise RequestError("--top-k 必须是整数") from e
    return [req]


def validate_requests(items: list[dict]) -> list[dict]:
    problems: list[dict] = []

    def add(index: int, field: str, reason: str) -> None:
        problems.append({"index": index, "field": field, "reason": reason})

    for i, obj in enumerate(items):
        if not isinstance(obj, dict):
            add(i, "$", "每条请求必须是 JSON 对象")
            continue
        for field in sorted(set(obj) - ALLOWED_FIELDS):
            add(i, field, "未知字段（ask/v1 拒绝未知字段，避免拼写错误被静默忽略）")
        q = obj.get("question")
        if not isinstance(q, str) or not q.strip():
            add(i, "question", "必填，且必须是非空字符串")
        project = obj.get("project")
        if project is not None and not isinstance(project, dict):
            add(i, "project", "必须是 JSON 对象或省略")
        pa = obj.get("policy_as_of")
        if pa is not None:
            if not isinstance(pa, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pa):
                add(i, "policy_as_of", "必须是 YYYY-MM-DD 字符串或省略")
            else:
                try:
                    datetime.date.fromisoformat(pa)
                except ValueError:
                    add(i, "policy_as_of", "不是有效日期")
        tk = obj.get("top_k")
        if tk is not None and (isinstance(tk, bool) or not isinstance(tk, int) or not 1 <= tk <= 20):
            add(i, "top_k", "必须是 1..20 的整数或省略")
        rid = obj.get("request_id")
        if rid is not None and not isinstance(rid, str):
            add(i, "request_id", "必须是字符串或省略")
    return problems


def fail(args, message: str, code: str = "invalid_request", details=None, exit_code: int = 2) -> int:
    payload = {"schema_version": SCHEMA_VERSION, "error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    if args.json or args.input:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"错误：{message}", file=sys.stderr)
        if details:
            print(json.dumps(details, ensure_ascii=False, indent=2), file=sys.stderr)
    return exit_code


def print_human(result: dict) -> None:
    ans = result["answer"]
    print(f"问题：{result['question']}")
    if result.get("request_id") is not None:
        print(f"request_id：{result['request_id']}")
    print(f"路由：{result['scenario']}；结论状态：{ans['verdict']}")
    for p in ans["points"]:
        print(f"- {p}")
    if ans["missing_fields"]:
        print(f"缺字段：{', '.join(ans['missing_fields'])}")
    if result.get("warnings"):
        print("提示：")
        for w in result["warnings"]:
            print(f"  - {w}")
    print("\n证据：")
    for e in result["retrieval"]["evidence"]:
        print(f"[{e['rank']}] {e['locator_id']}  level={e['evidence_level']}  score={e['score']}")
        print(f"    {e['quote']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="端到端问答：检索 + 规则判定 + 引用（ask/v1）")
    ap.add_argument("--question")
    ap.add_argument("--input", help="JSON 文件路径，或 - 表示 stdin；对象=单条，数组=批量")
    ap.add_argument("--project", help="项目 JSON 文件（仅单条模式）")
    ap.add_argument("--policy-as-of", default=None)
    ap.add_argument("--top-k", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dump-schema", action="store_true")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)

    if args.version:
        print(SCHEMA_VERSION)
        return 0
    if args.dump_schema:
        p = ROOT / "interface" / "ask.schema.json"
        if not p.exists():
            return fail(args, f"缺少契约文件：{p.relative_to(ROOT)}")
        print(p.read_text(encoding="utf-8").rstrip())
        return 0

    try:
        items = collect_requests(args)
    except RequestError as e:
        return fail(args, str(e))
    problems = validate_requests(items)
    if problems:
        return fail(args, "请求字段不合法", details=problems)

    try:
        results = [
            ask(
                obj["question"],
                project=obj.get("project"),
                policy_as_of=obj.get("policy_as_of"),
                top_k=obj.get("top_k"),
                request_id=obj.get("request_id"),
            )
            for obj in items
        ]
    except Exception as e:  # noqa: BLE001 - 稳定接口要返回结构化错误，不能把栈直接抛给调用方
        import traceback
        traceback.print_exc(file=sys.stderr)
        return fail(args, f"内部错误：{e}", code="internal_error", exit_code=1)

    if args.json or args.input:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "results": results}, ensure_ascii=False, indent=2))
        return 0
    for i, result in enumerate(results):
        if i:
            print("\n" + "=" * 48)
        print_human(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
