# -*- coding: utf-8 -*-
"""v2 检索评测：对齐 Codex 协议的 Top20 + 完整证据题 + 证据组覆盖 + 控制题。

方法：BM25 / 向量 / 混合（RRF）/ 混合+证据等级重排。
只做候选召回对照，不进入 rules/assess。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NORMALIZED = ROOT / "normalized"
OUT_DIR = ROOT / "eval" / "retrieval"
QUESTIONS = OUT_DIR / "questions_v2.jsonl"
MODEL_DIR = Path(os.environ.get("EMBEDDINGGEMMA_DIR", r"E:\hf-cache\models\embeddinggemma-2"))
QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: {title} | text: {text}"
TOP_K = 20
RRF_K = 60
LEVEL_WEIGHT = {"normative": 1.0, "official_explanation": 0.6, "official_qa": 0.2, None: 0.0}
LEVEL_BONUS = 0.05


def norm_text(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", s or "")


def tokens(text: str) -> list[str]:
    text = norm_text(text)
    toks: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    toks.extend(re.findall(r"[A-Za-z0-9]+(?:\.[0-9]+)?", text))
    return toks


def load_corpus() -> list[dict]:
    levels = {}
    roles = {}
    source_files = []
    for fname in ("sources.jsonl", "sources_ext.jsonl"):
        p = ROOT / fname
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            levels[rec["source_id"]] = rec.get("evidence_level")
            roles[rec["source_id"]] = rec.get("role")
            if rec.get("role") == "build":  # eval_only / corroborate 不进检索池
                source_files.append(rec["text_path"])
    docs = []
    seen: set[str] = set()
    for rel in source_files:
        path = ROOT / rel
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("kind") not in {"clause", "qa", "section", "item"}:
                continue
            text = rec.get("text") or ""
            if len(norm_text(text)) < 20:  # 章节标题这类空壳不进检索池
                continue
            if rec["locator_id"] in seen:
                continue
            seen.add(rec["locator_id"])
            docs.append({
                "locator_id": rec["locator_id"],
                "kind": rec["kind"],
                "title": rec.get("section") or rec.get("chapter") or rec.get("value") or "",
                "text": text,
                "level": levels.get(rec["source_id"]),
                "source_id": rec["source_id"],
            })
    return docs



class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.doc_tokens = [tokens(d["text"]) for d in docs]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.avg_len = sum(self.doc_len) / max(len(self.doc_len), 1)
        self.tf = [Counter(t) for t in self.doc_tokens]
        df = Counter()
        for tf in self.tf:
            df.update(tf.keys())
        self.idf = {t: math.log(1 + (len(docs) - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def score(self, query: str) -> list[float]:
        q_toks = tokens(query)
        out = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in q_toks:
                if t in tf:
                    f = tf[t]
                    denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / (self.avg_len or 1))
                    s += self.idf[t] * f * (self.k1 + 1) / denom
            out.append(s)
        return out


def load_embedder():
    import torch
    from transformers import AutoModel, AutoTokenizer
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR), local_files_only=True)
    model = AutoModel.from_pretrained(str(MODEL_DIR), local_files_only=True, dtype=torch.float32)
    model.eval()

    def encode(texts):
        batch = tok(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
        with torch.no_grad():
            hidden = model(**batch).last_hidden_state
        mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
        emb = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        return torch.nn.functional.normalize(emb, p=2, dim=1)

    return encode


def rrf(scores_a, scores_b, k=RRF_K):
    ra = sorted(range(len(scores_a)), key=lambda i: -scores_a[i])
    rb = sorted(range(len(scores_b)), key=lambda i: -scores_b[i])
    rank_a = {i: r + 1 for r, i in enumerate(ra)}
    rank_b = {i: r + 1 for r, i in enumerate(rb)}
    return [1.0 / (k + rank_a[i]) + 1.0 / (k + rank_b[i]) for i in range(len(scores_a))]


def metrics(rank_lists, questions) -> dict:
    """rank_lists: qid -> ordered locator ids (len 20)"""
    n = 0
    complete5 = complete20 = 0
    gold_hit = gold_total = 0
    rr_sum = 0.0
    rows = []
    for q in questions:
        if q.get("control"):
            continue
        n += 1
        gold = q["gold"]
        ranked = rank_lists[q["question_id"]]
        hits5 = [g for g in gold if g in ranked[:5]]
        hits20 = [g for g in gold if g in ranked[:TOP_K]]
        complete5 += 1 if len(hits5) == len(gold) else 0
        complete20 += 1 if len(hits20) == len(gold) else 0
        gold_hit += len(hits20)
        gold_total += len(gold)
        first = next((i + 1 for i, lid in enumerate(ranked) if lid in gold), 0)
        rr_sum += 1.0 / first if first else 0.0
        rows.append({"question_id": q["question_id"], "complete@5": len(hits5) == len(gold),
                     "complete@20": len(hits20) == len(gold), "coverage@20": f"{len(hits20)}/{len(gold)}",
                     "first_relevant_rank": first})
    return {
        "n": n,
        "complete_evidence_at_5": round(complete5 / n, 4),
        "complete_evidence_at_20": round(complete20 / n, 4),
        "evidence_group_coverage_at_20": round(gold_hit / max(gold_total, 1), 4),
        "mrr": round(rr_sum / n, 4),
        "complete_evidence_count_at_5": f"{complete5}/{n}",
        "complete_evidence_count_at_20": f"{complete20}/{n}",
        "evidence_group_count_at_20": f"{gold_hit}/{gold_total}",
        "rows": rows,
    }


def by_category(rank_lists, questions) -> dict:
    cats: dict[str, list] = {}
    for q in questions:
        if q.get("control"):
            continue
        cats.setdefault(q.get("category") or "未分类", []).append(q)
    out = {}
    for cat, qs in cats.items():
        m = metrics(rank_lists, qs)
        out[cat] = {k: m[k] for k in ("n", "complete_evidence_count_at_5", "complete_evidence_count_at_20",
                                      "evidence_group_count_at_20", "mrr")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-embedding", action="store_true")
    ap.add_argument("--out", default="results_v2_corpus9.json")
    args = ap.parse_args()

    docs = load_corpus()
    qs = [json.loads(l) for l in QUESTIONS.read_text(encoding="utf-8").splitlines() if l.strip()]
    scoring = [q for q in qs if not q.get("control")]
    controls = [q for q in qs if q.get("control")]
    print(f"corpus={len(docs)} units; scoring={len(scoring)}; controls={len(controls)}; top_k={TOP_K}")

    bm25 = BM25(docs)
    bm_scores = {q["question_id"]: bm25.score(q["question"]) for q in qs}
    bm_rank = {qid: [docs[i]["locator_id"] for i in sorted(range(len(docs)), key=lambda i: -sc[i])[:TOP_K]]
               for qid, sc in bm_scores.items()}

    result = {
        "batch": ["pv-national-2025-v1", "pv-corpus-2025-v2"],
        "protocol": "codex-aligned-v1",
        "corpus_units": len(docs),
        "corpus_by_kind": dict(Counter(d["kind"] for d in docs)),
        "corpus_by_source": dict(Counter(d["source_id"] for d in docs)),
        "scoring_questions": len(scoring),
        "controls": len(controls),
        "top_k": TOP_K,
        "results": {},
        "note": "候选召回对照；gold 由作者标注，非独立标注；不进入 rules/assess。",
    }
    baseline_path = OUT_DIR / "results_v2.json"
    if baseline_path.exists():
        base = json.loads(baseline_path.read_text(encoding="utf-8"))
        keys = ("complete_evidence_count_at_5", "complete_evidence_count_at_20",
                "evidence_group_count_at_20", "mrr")
        result["baseline_130_units"] = {
            "corpus_units": base.get("corpus_units"),
            "results": {name: {k: m.get(k) for k in keys} for name, m in base.get("results", {}).items()},
        }
    result["results"]["bm25"] = {**metrics(bm_rank, qs), "by_category": by_category(bm_rank, qs),
        "control_top1": {
        q["question_id"]: {"top1": bm_rank[q["question_id"]][0], "max_score": round(max(bm_scores[q["question_id"]]), 4)}
        for q in controls}}
    print("BM25", {k: result["results"]["bm25"][k] for k in
                   ("complete_evidence_count_at_5", "complete_evidence_count_at_20", "evidence_group_count_at_20", "mrr")})

    if not args.no_embedding:
        try:
            encode = load_embedder()
            doc_texts = [DOC_PREFIX.format(title=d["title"] or "none", text=d["text"]) for d in docs]
            doc_emb = encode(doc_texts)
            q_emb = encode([QUERY_PREFIX + q["question"] for q in qs])
            sims = (q_emb @ doc_emb.T).tolist()
            vec_scores = {q["question_id"]: sims[i] for i, q in enumerate(qs)}
            vec_rank = {qid: [docs[i]["locator_id"] for i in sorted(range(len(docs)), key=lambda i: -sc[i])[:TOP_K]]
                        for qid, sc in vec_scores.items()}

            hyb_scores = {qid: rrf(bm_scores[qid], vec_scores[qid]) for qid in bm_scores}
            hyb_rank = {qid: [docs[i]["locator_id"] for i in sorted(range(len(docs)), key=lambda i: -sc[i])[:TOP_K]]
                        for qid, sc in hyb_scores.items()}

            # 证据等级重排：混合分数归一化 + 等级加权
            level_of = {d["locator_id"]: d["level"] for d in docs}
            rr_scores = {}
            for qid, sc in hyb_scores.items():
                lo, hi = min(sc), max(sc)
                span = (hi - lo) or 1.0
                rr_scores[qid] = [((v - lo) / span) + LEVEL_BONUS * LEVEL_WEIGHT.get(level_of[docs[i]["locator_id"]], 0.0)
                                  for i, v in enumerate(sc)]
            rr_rank = {qid: [docs[i]["locator_id"] for i in sorted(range(len(docs)), key=lambda i: -sc[i])[:TOP_K]]
                       for qid, sc in rr_scores.items()}

            for name, rank, scores in (("vector", vec_rank, vec_scores), ("hybrid_rrf", hyb_rank, hyb_scores),
                                       ("hybrid_rerank_level", rr_rank, rr_scores)):
                result["results"][name] = {
                    **metrics(rank, qs),
                    "by_category": by_category(rank, qs),
                    "control_top1": {q["question_id"]: {"top1": rank[q["question_id"]][0],
                                                        "max_score": round(max(scores[q["question_id"]]), 4)}
                                     for q in controls},
                }
                print(name, {k: result["results"][name][k] for k in
                             ("complete_evidence_count_at_5", "complete_evidence_count_at_20",
                              "evidence_group_count_at_20", "mrr")})
        except Exception as exc:
            print(f"[WARN] embedding unavailable: {type(exc).__name__}: {exc}")
            result["embedding_error"] = f"{type(exc).__name__}: {exc}"

    out = OUT_DIR / args.out
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written {out} ({out.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
