# -*- coding: utf-8 -*-
"""第 3 步实验：检索技术路由（只借零件，不改规则判定）。

对照五组：
  M0_global            —— 全语料 BM25 + 证据等级加分（加省级细则后的现状）
  M1_routed            —— 按提问省名收窄候选（国家问题只用国家来源）
  M2_routed_section    —— M1 + 同章相邻条轻微加权（所有问题）
  M3_routed_intent     —— M1 + 仅定义/分类意图做同章相邻加权
  M4_routed_exact      —— M1 + 问题含“第X条”时精确条号置顶

评测集：eval/retrieval/questions_v2.jsonl 的 36 道计分题（控制题不计分）。
结论写 reviews/14；是否接入由结果与真实问法回归共同决定。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import retrieval_eval2 as ret  # noqa: E402
import ask  # noqa: E402
from ask import route_docs  # noqa: E402

TOP_K = ret.TOP_K
PARAMS = ask.retrieval_params()   # 与 ask/evidence_audit 共用，避免实验与线上两套口径
LEVEL_BONUS = PARAMS["level_bonus"]
SECTION_BONUS = 0.03
SECTION_TOP = 5
INTENT_WORDS = ("定义", "是指", "含义", "包括", "类型", "几类", "几种", "分类", "区别")


def load_meta() -> dict:
    """locator_id -> {chapter, ordinal, value}，用来找同章相邻条。"""
    meta = {}
    for path in (ROOT / "normalized").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            meta[rec["locator_id"]] = {
                "chapter": rec.get("chapter") or "",
                "ordinal": rec.get("ordinal"),
                "value": rec.get("value") or "",
            }
    return meta


def level_scores(docs: list[dict], raw: list[float]) -> list[float]:
    lo, hi = min(raw), max(raw)
    span = (hi - lo) or 1.0
    return [((v - lo) / span) + LEVEL_BONUS * ret.LEVEL_WEIGHT.get(docs[i]["level"], 0.0)
            for i, v in enumerate(raw)]


def rank_from_scores(docs, scores, top_k=TOP_K):
    return [docs[i]["locator_id"] for i in sorted(range(len(docs)), key=lambda i: -scores[i])[:top_k]]


def section_scores(docs, meta, base, only_intent: bool = False) -> list[float]:
    order = sorted(range(len(docs)), key=lambda i: -base[i])
    bonus = [0.0] * len(docs)
    for rank, i in enumerate(order[:SECTION_TOP], 1):
        d, m = docs[i], meta.get(docs[i]["locator_id"]) or {}
        if m.get("ordinal") is None:
            continue
        for j, dj in enumerate(docs):
            mj = meta.get(dj["locator_id"]) or {}
            if dj["source_id"] != d["source_id"]:
                continue
            if mj.get("chapter") != m.get("chapter") or mj.get("ordinal") is None:
                continue
            if abs(mj["ordinal"] - m["ordinal"]) == 1:
                bonus[j] = max(bonus[j], SECTION_BONUS / rank)
    return [base[i] + bonus[i] for i in range(len(docs))]


CN_NUM = "一二三四五六七八九十百零〇两"
ART_RE = re.compile(r"第([" + CN_NUM + r"]+)条")


def exact_scores(docs, meta, question, base):
    arts = {"第" + m + "条" for m in ART_RE.findall(question or "")}
    if not arts:
        return list(base)
    out = list(base)
    top = max(base)
    for i, d in enumerate(docs):
        if (meta.get(d["locator_id"]) or {}).get("value") in arts:
            out[i] = top + 0.5
    return out


def main() -> int:
    all_docs = ret.load_corpus()
    meta = load_meta()
    qs = [json.loads(l) for l in (ROOT / "eval/retrieval/questions_v2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    scoring = [q for q in qs if not q.get("control")]
    print(f"corpus={len(all_docs)} units; scoring={len(scoring)}")

    index_cache: dict[tuple, ret.BM25] = {}

    def index(docs):
        key = tuple(d["locator_id"] for d in docs)
        if key not in index_cache:
            index_cache[key] = ret.BM25(docs, k1=PARAMS["k1"], b=PARAMS["b"])
        return index_cache[key]

    names = ["M0_global", "M1_routed", "M2_routed_section", "M3_routed_intent", "M4_routed_exact"]
    ranks = {name: {} for name in names}
    for q in qs:
        qid, text = q["question_id"], q["question"]
        is_intent = any(w in text for w in INTENT_WORDS)

        global_docs = all_docs
        g_base = level_scores(global_docs, index(global_docs).score(text))
        ranks["M0_global"][qid] = rank_from_scores(global_docs, g_base)

        docs, _route = route_docs(all_docs, text)
        base = level_scores(docs, index(docs).score(text))
        ranks["M1_routed"][qid] = rank_from_scores(docs, base)
        ranks["M2_routed_section"][qid] = rank_from_scores(docs, section_scores(docs, meta, base))
        ranks["M3_routed_intent"][qid] = rank_from_scores(
            docs, section_scores(docs, meta, base) if is_intent else base)
        ranks["M4_routed_exact"][qid] = rank_from_scores(docs, exact_scores(docs, meta, text, base))

    out = {"corpus_units": len(all_docs), "scoring_questions": len(scoring),
           "retrieval_params": {"k1": PARAMS["k1"], "b": PARAMS["b"], "level_bonus": LEVEL_BONUS},
           "results": {}}
    for name in names:
        m = ret.metrics(ranks[name], qs)
        out["results"][name] = {**m, "by_category": ret.by_category(ranks[name], qs)}
        print(name, {k: m[k] for k in ("complete_evidence_count_at_5", "complete_evidence_count_at_20",
                                       "evidence_group_count_at_20", "mrr")})
    out["note"] = ("实验对照；gold 由作者标注；M1 按 knowledge/retrieval_routes.json 收窄候选。"
                   "参数取 knowledge/retrieval_params.json（与 ask 同口径）。M2/M3/M4 未接入主链路。")
    p = ROOT / "eval/retrieval/router_results.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("written", p, p.stat().st_size, "B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
