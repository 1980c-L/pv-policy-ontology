# -*- coding: utf-8 -*-
"""把第二批语料（江苏/广东实施意见、电力现货规则）切成 locator 单元。

输入：tools/fetch_corpus_v2.py 抓到的 raw/*.html + raw/_fetch_v2.json
输出：normalized/<source_id>.jsonl、sources_ext.jsonl

所有 locator 文本都来自原文，不做改写；只做去空白和分段。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "raw"
MANIFEST = RAW / "_fetch_v2.json"
NORMALIZED = ROOT / "normalized"
SOURCES_EXT = ROOT / "sources_ext.jsonl"

CN_NUM = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(text: str) -> int:
    """第一章/第一条 里的中文数字 -> int（支持到 999，够本批用）。"""
    text = text.strip()
    total = 0
    section = 0
    for ch in text:
        if ch in ("十", "百"):
            unit = 10 if ch == "十" else 100
            if section == 0:
                section = 1
            total += section * unit
            section = 0
        elif ch in CN_NUM:
            section = CN_NUM[ch]
        else:
            raise ValueError(f"无法解析的中文数字: {text!r}")
    return total + section


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_text(path: Path) -> str:
    html = path.read_text(encoding="utf-8-sig", errors="ignore")
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def record(source_id: str, kind: str, value: str, text: str, order: int, **extra) -> dict:
    rec = {
        "locator_id": f"{source_id}#{kind}={value}",
        "source_id": source_id,
        "kind": kind,
        "value": value,
        "text": text,
        "order": order,
    }
    rec.update(extra)
    return rec


def normalize_js(source_id: str, text: str) -> list[dict]:
    start = text.find("各设区市发展改革委")
    end = text.find("分享至：", start)
    assert start >= 0 and end > start, "江苏：找不到正文边界"
    body = text[start:end]
    lines = body.split("\n")
    top_re = re.compile(r"^([一二三四五六七八九十]+)、(.+)$")
    item_re = re.compile(r"^（([一二三四五六七八九十]+)）(.+)$")

    # 先按顶层段切，再在段内按（N）切
    chunks: list[tuple[str, str]] = []  # (head, line)
    for line in lines:
        chunks.append((line, line))

    records: list[dict] = []
    order = 0
    sections: list[tuple[str, list[str]]] = []
    front: list[str] = []
    cur_section: list[str] | None = None
    for head, line in chunks:
        m = top_re.match(line)
        if m:
            cur_section = [line]
            sections.append((m.group(1), cur_section))
        elif cur_section is None:
            if line:
                front.append(line)
        else:
            cur_section.append(line)
    assert len(sections) == 5, f"江苏：顶层段数 {len(sections)} != 5"

    for cn, sec_lines in sections:
        heading = sec_lines[0]
        rest = sec_lines[1:]
        # 段首、（N）条目、尾段
        pre: list[str] = []
        items: list[tuple[str, list[str]]] = []
        tail: list[str] = []
        cur: list[str] | None = None
        for line in rest:
            if not line:
                continue
            m = item_re.match(line)
            if m:
                cur = [line]
                items.append((m.group(1), cur))
            elif cur is None:
                pre.append(line)
            else:
                cur.append(line)
        # 最后一段若是“本实施意见由…”之类的收尾，归入 tail
        if items:
            last = items[-1][1]
            while len(last) > 1 and ("本实施意见由" in last[-1] or "2025年4月29日" in last[-1]
                                     or last[-1].startswith("江苏省发展改革委")):
                tail.insert(0, last.pop())
        sec_text = "\n".join([heading, *pre]).strip()
        records.append(record(source_id, "section", cn, sec_text, order,
                              heading=heading, section=cn))
        order += 1
        for idx, (num, item_lines) in enumerate(items):
            item_text = "\n".join(item_lines).strip()
            records.append(record(source_id, "item", f"（{num}）", item_text, order,
                                  heading=item_lines[0], section=cn, item=num))
            order += 1
        if tail:
            pass  # 尾部另行记录
    # 前言与尾部
    records.insert(0, record(source_id, "metadata", "front-matter", "\n".join(front).strip(), -1))
    tail_text = "\n".join(tail).strip()
    if tail_text:
        records.append(record(source_id, "metadata", "document-tail", tail_text, order))
    return records


def normalize_gd(source_id: str, text: str) -> list[dict]:
    start = text.find("各地级以上市发展改革局")
    end = text.find("主办：广东省能源局", start)
    assert start >= 0 and end > start, "广东：找不到正文边界"
    body = text[start:end]
    lines = [ln for ln in body.split("\n") if ln]
    top_re = re.compile(r"^([一二三四五六七八九十]+)、(.+)$")
    records: list[dict] = []
    order = 0
    front: list[str] = []
    cur: tuple[str, list[str]] | None = None
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        m = top_re.match(line)
        if m:
            cur = (m.group(1), [line])
            sections.append(cur)
        elif cur is None:
            front.append(line)
        else:
            cur[1].append(line)
    assert len(sections) == 6, f"广东：顶层段数 {len(sections)} != 6"
    records.append(record(source_id, "metadata", "front-matter", "\n".join(front).strip(), -1))
    for cn, sec_lines in sections:
        texts = list(sec_lines)
        # 文末落款单独记
        tail: list[str] = []
        while len(texts) > 1 and (texts[-1].startswith("广东省能源局") or re.match(r"^20\d\d年", texts[-1])
                                 or texts[-1] == "发布"):
            tail.insert(0, texts.pop())
        sec_text = "\n".join(texts).strip()
        records.append(record(source_id, "section", cn, sec_text, order,
                              heading=texts[0], section=cn))
        order += 1
        if cn == sections[-1][0] and tail:
            records.append(record(source_id, "metadata", "document-tail", "\n".join(tail), order))
    return records


def normalize_spot(source_id: str, text: str) -> list[dict]:
    end = text.find("附件：名词解释")
    idx = [m.start() for m in re.finditer("第一章", text)]
    assert end > 0 and len(idx) >= 2, "现货规则：找不到正文边界"
    body = text[idx[-1]:end]
    chapter_re = re.compile(r"^第([一二三四五六七八九十]+)章\s*(.*)$")
    clause_re = re.compile(r"^第([一二三四五六七八九十百零〇]+)条$")
    records: list[dict] = []
    order = 0
    chapters: list[dict] = []
    clauses: list[dict] = []
    cur_chapter: dict | None = None
    cur_clause: dict | None = None
    for line in (ln for ln in body.split("\n") if ln):
        mc = chapter_re.match(line)
        mk = clause_re.match(line)
        if mc:
            cur_chapter = {"no": cn_to_int(mc.group(1)), "cn": mc.group(1), "heading": line, "lines": [line], "clauses": []}
            chapters.append(cur_chapter)
            cur_clause = None
        elif mk:
            cur_clause = {"no": cn_to_int(mk.group(1)), "cn": mk.group(1), "heading": line, "lines": [line]}
            clauses.append(cur_clause)
            assert cur_chapter is not None
            cur_chapter["clauses"].append(cur_clause)
        elif cur_clause is not None:
            cur_clause["lines"].append(line)
        elif cur_chapter is not None:
            cur_chapter["lines"].append(line)
    assert len(chapters) == 13, f"现货规则：章数 {len(chapters)} != 13"
    assert len(clauses) == 129, f"现货规则：条数 {len(clauses)} != 129（原文 101–109 用〇编号）"
    assert [c["no"] for c in chapters] == list(range(1, 14)), "章号不连续"
    assert [c["no"] for c in clauses] == list(range(1, 130)), "条号不连续"
    for ch in chapters:
        # 章文本只到第一条之前（避免把整章重复一遍）
        lines = []
        for ln in ch["lines"]:
            if clause_re.match(ln):
                break
            lines.append(ln)
        records.append(record(source_id, "section", f"第{ch['cn']}章", "\n".join(lines).strip(), order,
                              heading=ch["heading"], chapter=ch["no"]))
        order += 1
        for cl in ch["clauses"]:
            ctext = "\n".join(cl["lines"]).strip()
            records.append(record(source_id, "clause", f"第{cl['cn']}条", ctext, order,
                                  heading=cl["heading"], chapter=ch["no"]))
            order += 1
    return records


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    NORMALIZED.mkdir(exist_ok=True)
    sources: list[dict] = []
    summary_all: dict[str, dict] = {}
    for spec in manifest["sources"]:
        sid = spec["source_id"]
        raw_path = ROOT / spec["raw_path"]
        actual = sha256_file(raw_path)
        assert actual == spec["raw_sha256"], f"{sid}: raw sha256 与 manifest 不符"
        text = get_text(raw_path)
        if sid.startswith("PV-JS"):
            recs = normalize_js(sid, text)
            kinds = ["metadata", "section", "item"]
        elif sid.startswith("PV-GD"):
            recs = normalize_gd(sid, text)
            kinds = ["metadata", "section"]
        else:
            recs = normalize_spot(sid, text)
            kinds = ["section", "clause"]
        ids = [r["locator_id"] for r in recs]
        assert len(ids) == len(set(ids)), f"{sid}: locator_id 重复"
        out = NORMALIZED / f"{sid}.jsonl"
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
        counts: dict[str, int] = {}
        for r in recs:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        summary_all[sid] = {"records": len(recs), "kinds": counts, "bytes": out.stat().st_size}
        sources.append({
            "source_id": sid,
            "batch": "pv-corpus-2025-v2",
            "title": spec["title"],
            "canonical_url": spec["canonical_url"],
            "fetched_url": spec["fetched_url"],
            "issuer": spec["issuer"],
            "document_number": spec["document_number"],
            "issued_at": spec["issued_at"],
            "published_at": spec["published_at"],
            "retrieved_at": spec["fetched_at"],
            "role": "build",
            "role_detail": "corpus_expansion",
            "evidence_level": "normative",
            "raw_path": spec["raw_path"],
            "raw_sha256": spec["raw_sha256"],
            "text_path": f"normalized/{sid}.jsonl",
            "parse_status": "parsed",
            "parse_tool": "tools/normalize_corpus_v2.py",
            "locator_kinds": kinds,
            "normalized_summary": summary_all[sid],
            "current_effectiveness": "not_checked",
            "notes": "第二批语料：只扩来源层，不扩规则；规则暂不引用本来源。",
        })
        print(f"{sid}: {len(recs)} records {counts} -> {out.relative_to(ROOT)}")
    SOURCES_EXT.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in sources), encoding="utf-8")
    print(f"sources_ext -> {SOURCES_EXT.relative_to(ROOT)} ({SOURCES_EXT.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
