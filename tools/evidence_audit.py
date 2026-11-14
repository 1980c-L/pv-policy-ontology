# -*- coding: utf-8 -*-
"""证据回检：逐条检查规则/事实/实体引用的原文能否被检索到。

检查项（结构 → 可检索 → 唯一性）：
  1. locator 存在        引用指向的条款/段落必须真实存在，否则 exit 1
  2. quote 是原文子串    去空白后必须是 locator 正文的子串，否则 exit 1
  3. 可检索性           用 quote 当查询，在“国家 + 该省”候选池里按运行时参数
                        （knowledge/retrieval_params.json）跑 BM25 + 等级加分，
                        看引用自己的 locator 排第几（Top20 外记 miss）
  4. 唯一性             quote 在多少段落里出现；出现太多说明引文太泛、钉不住 locator

输出：eval/evidence_audit.json + 终端摘要（可疑清单带 rank 与原因）。
只读，不改规则；用于人工复核，不判规则结论真假。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ask  # noqa: E402
import retrieval_eval2 as ret  # noqa: E402

TOP_N = 20
GENERIC_HITS = 5
SHORT_QUOTE = 10


def norm(s: str) -> str:
    return ret.norm_text(s)


def load_texts() -> dict:
    texts = {}
    for path in (ROOT / "normalized").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            texts[rec["locator_id"]] = rec
    return texts


def collect_refs() -> list[dict]:
    out = []
    rules = json.loads((ROOT / "knowledge/rules.json").read_text(encoding="utf-8"))
    for rule in rules.get("rules", []):
        for ref in rule.get("source_refs", []):
            out.append({"owner": rule["rule_id"], "owner_type": "rule", **ref})
    constraints_path = ROOT / "knowledge/constraints.json"
    if constraints_path.exists():
        constraints = json.loads(constraints_path.read_text(encoding="utf-8"))
        for c in constraints.get("constraints", []):
            for ref in c.get("source_refs", []):
                out.append({"owner": c["constraint_id"], "owner_type": "constraint", **ref})
    for name, key in (("facts.jsonl", "fact_id"), ("entities.jsonl", "entity_id")):
        path = ROOT / "knowledge" / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            for ref in rec.get("source_refs", []):
                out.append({"owner": rec[key], "owner_type": "fact" if name.startswith("facts") else "entity", **ref})
    return out


def route_prefix(source_id: str, prefixes: list[str]) -> str | None:
    for p in prefixes:
        if source_id.startswith(p):
            return p
    return None


def main() -> int:
    docs = ret.load_corpus()
    by_loc = {d["locator_id"]: d for d in docs}
    texts = load_texts()
    routes = json.loads((ROOT / "knowledge/retrieval_routes.json").read_text(encoding="utf-8"))
    prefixes = [r["source_prefix"] for r in routes.get("provinces", [])]

    index_cache: dict[tuple, ret.BM25] = {}

    def scope_docs_for(source_id: str) -> list[dict]:
        p = route_prefix(source_id, prefixes)
        if not p:
            return [d for d in docs if not route_prefix(d["source_id"], prefixes)]
        return [d for d in docs if (not route_prefix(d["source_id"], prefixes)) or d["source_id"].startswith(p)]

    refs = collect_refs()
    params = ask.retrieval_params()
    hard, items = [], []
    for ref in refs:
        loc = ref["locator_id"]
        quote = ref.get("quote") or ""
        rec = texts.get(loc)
        item = {"owner": ref["owner"], "owner_type": ref["owner_type"], "locator_id": loc,
                "source_id": ref.get("source_id"), "quote": quote}
        if rec is None:
            item.update(status="missing_locator", reason="locator 不在 normalized 语料中")
            hard.append(item)
            items.append(item)
            continue
        if norm(quote) not in norm(rec.get("text") or ""):
            item.update(status="quote_mismatch", reason="quote 去空白后不是 locator 正文子串")
            hard.append(item)
            items.append(item)
            continue

        hits = sum(1 for d in docs if norm(quote) in norm(d["text"]))
        item["quote_doc_hits"] = hits

        if loc not in by_loc:
            item.update(status="not_indexed", rank=None,
                        reason="来源不在检索池（role 非 build）")
            items.append(item)
            continue

        scoped = scope_docs_for(ref.get("source_id") or "")
        key = tuple(d["locator_id"] for d in scoped)
        if key not in index_cache:
            index_cache[key] = ret.BM25(scoped, k1=params["k1"], b=params["b"])
        order = ask.rank_docs(quote, scoped, bm25=index_cache[key], params=params)
        rank = next((r + 1 for r, (_score, i) in enumerate(order) if scoped[i]["locator_id"] == loc), None)
        item["rank"] = rank
        reasons, notes = [], []
        if rank is None or rank > TOP_N:
            reasons.append(f"Top{TOP_N} 未召回")
        elif rank > 3 and len(norm(quote)) <= SHORT_QUOTE:
            reasons.append("引文过短，排名靠后")
        sources_with_quote = {d["source_id"] for d in docs if norm(quote) in norm(d["text"])}
        item["quote_source_hits"] = len(sources_with_quote)
        if len(sources_with_quote) >= 3:
            notes.append(f"同文出现在 {len(sources_with_quote)} 个来源（{', '.join(sorted(sources_with_quote))}）")
        item["status"] = "suspicious" if reasons else ("note" if notes else "ok")
        item["reason"] = "；".join(reasons + notes)
        items.append(item)

    ok = [i for i in items if i.get("status") == "ok"]
    suspicious = [i for i in items if i.get("status") == "suspicious"]
    noted = [i for i in items if i.get("status") == "note"]
    not_indexed = [i for i in items if i.get("status") == "not_indexed"]
    summary = {
        "refs_total": len(items),
        "ok": len(ok),
        "note": len(noted),
        "suspicious": len(suspicious),
        "not_indexed": len(not_indexed),
        "hard_failures": len(hard),
        "top1": sum(1 for i in items if i.get("rank") == 1),
        "top5": sum(1 for i in items if i.get("rank") and i["rank"] <= 5),
        "top20": sum(1 for i in items if i.get("rank") and i["rank"] <= TOP_N),
        "miss": sum(1 for i in items if i.get("rank") is None and i.get("status") != "not_indexed"),
    }
    out = {"summary": summary, "items": items}
    p = ROOT / "eval/evidence_audit.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"证据回检：{summary['refs_total']} 条引用；结构校验 "
          f"{summary['refs_total'] - len(hard)}/{summary['refs_total']} 通过")
    print(f"  可检索：Top1 {summary['top1']} / Top5 {summary['top5']} / Top20 {summary['top20']} / "
          f"未召回 {summary['miss']}；未入池 {summary['not_indexed']}")
    print(f"  可疑 {len(suspicious)} 条：")
    for i in suspicious:
        print(f"    - [{i['owner']}] {i['locator_id']} rank={i.get('rank')}：{i['reason']}")
    print(f"  提示 {len(noted)} 条（同文跨来源，不影响结构校验）：")
    for i in noted:
        print(f"    - [{i['owner']}] {i['locator_id']}：{i['reason']}")
    if hard:
        print("  结构失败（必须修）：")
        for i in hard:
            print(f"    - [{i['owner']}] {i['locator_id']}：{i['reason']}")
    print(f"written {p} ({p.stat().st_size} B)")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
