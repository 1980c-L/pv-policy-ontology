#!/usr/bin/env python3
"""登记浙江省实施细则：docx -> locator 记录，并写入 sources_ext.jsonl。

用法：
    python tools/normalize_zj.py

注意：tools/normalize_corpus_v2.py 会整体重写 sources_ext.jsonl；重跑全链路时，
本脚本必须排在其后，否则浙江来源记录会被覆盖。
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SOURCE_ID = "PV-ZJ-2026-015"
RAW_PATH = ROOT / "raw" / "PV-ZJ-2026-015.docx"
OUT_PATH = ROOT / "normalized" / "PV-ZJ-2026-015.jsonl"
SOURCES_EXT = ROOT / "sources_ext.jsonl"
NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CHAPTERS = ["总则", "行业管理", "备案管理", "建设管理", "电网接入", "运行管理", "附则"]
CLAUSE_RE = re.compile(r"^第([一二三四五六七八九十]+)条")
CN_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九", start=0)}

EXPECTED_SHA256 = "8de2159105e206abc3e233672054326fc5ae373ba3b4f129bba37822b23ab0b6"
EXPECTED_PARAGRAPHS = 89
EXPECTED_CLAUSES = 55


def read_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    out = []
    for para in root.iter(NS + "p"):
        text = "".join(t.text or "" for t in para.iter(NS + "t"))
        text = text.replace("\u00a0", " ").strip()
        if text:
            out.append(text)
    return out


def cn_to_int(text: str) -> int:
    total, section = 0, 0
    for ch in text:
        if ch == "十":
            section = 10 if section == 0 else section * 10
        else:
            section += CN_DIGITS[ch]
    return total + section


def build_records(paras: list[str]) -> list[dict]:
    assert paras[0] == "附件", paras[:3]
    assert paras[1] == "浙江省分布式光伏发电开发建设管理", paras[:3]
    assert paras[2] == "实施细则", paras[:3]

    records: list[dict] = []
    front = "\n".join(paras[:3])
    records.append(
        {
            "source_id": SOURCE_ID,
            "kind": "metadata",
            "value": "front-matter",
            "locator_id": f"{SOURCE_ID}#metadata=front-matter",
            "text": front,
            "order": -1,
        }
    )

    sections: list[dict] = []
    clauses: list[dict] = []
    chapter = None
    ordinal = 0
    current = None
    for para in paras[3:]:
        compact = re.sub(r"\s+", "", para)
        if compact in CHAPTERS:
            chapter = para
            assert compact not in {s["value"] for s in sections}, chapter
            sections.append({"heading": para, "value": compact, "texts": []})
            current = None
            continue
        assert chapter is not None, para
        match = CLAUSE_RE.match(para)
        if match:
            ordinal += 1
            value = f"第{match.group(1)}条"
            assert cn_to_int(match.group(1)) == ordinal, (value, ordinal)
            current = {
                "source_id": SOURCE_ID,
                "kind": "clause",
                "value": value,
                "locator_id": f"{SOURCE_ID}#clause={value}",
                "text": para,
                "ordinal": ordinal,
                "chapter": chapter,
            }
            clauses.append(current)
        else:
            assert current is not None, para
            current["text"] += "\n" + para
        sections[-1]["texts"].append(para)

    assert ordinal == EXPECTED_CLAUSES, ordinal
    assert len(sections) == len(CHAPTERS), len(sections)

    for i, sec in enumerate(sections):
        value = sec["value"]
        records.append(
            {
                "source_id": SOURCE_ID,
                "kind": "section",
                "value": value,
                "locator_id": f"{SOURCE_ID}#section={value}",
                "text": "\n".join([sec["heading"]] + sec["texts"]),
                "order": i,
                "title": sec["heading"],
            }
        )
    records.extend(clauses)
    return records


def upsert_source(record: dict) -> None:
    rows = []
    if SOURCES_EXT.exists():
        rows = [json.loads(line) for line in SOURCES_EXT.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [row for row in rows if row["source_id"] != record["source_id"]]
    rows.append(record)
    SOURCES_EXT.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    raw_bytes = RAW_PATH.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    assert digest == EXPECTED_SHA256, digest
    paras = read_paragraphs(RAW_PATH)
    assert len(paras) == EXPECTED_PARAGRAPHS, len(paras)

    records = build_records(paras)
    OUT_PATH.write_text(
        "\n".join(json.dumps(rec, ensure_ascii=False) for rec in records) + "\n",
        encoding="utf-8",
    )

    kinds: dict[str, int] = {}
    for rec in records:
        kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
    retrieved = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    source = {
        "source_id": SOURCE_ID,
        "batch": "pv-corpus-2025-v3",
        "title": "浙江省分布式光伏发电开发建设管理实施细则",
        "canonical_url": "https://fzggw.zj.gov.cn/col/col1229123366/art/2026/art_88d61ad2526d40f6821d8122a4649eb4.html",
        "fetched_url": "https://fzggw.zj.gov.cn/col/col1229123366/art/2026/art_88d61ad2526d40f6821d8122a4649eb4.html",
        "issuer": "浙江省能源局、国家能源局浙江监管办公室",
        "document_number": "浙能源〔2026〕15号",
        "issued_at": "2026-08-03",
        "published_at": "2026-08-18",
        "retrieved_at": retrieved,
        "role": "build",
        "role_detail": "provincial_expansion",
        "evidence_level": "normative",
        "raw_path": "raw/PV-ZJ-2026-015.docx",
        "raw_sha256": digest,
        "text_path": "normalized/PV-ZJ-2026-015.jsonl",
        "parse_status": "parsed",
        "parse_tool": "tools/normalize_zj.py",
        "locator_kinds": ["metadata", "section", "clause"],
        "normalized_summary": {
            "records": len(records),
            "kinds": kinds,
            "bytes": OUT_PATH.stat().st_size,
        },
        "current_effectiveness": "in_effect",
        "notes": "第三批语料：浙江正式细则（2026-08-03 印发，2026-08-18 发布，有效期至 2030-01-16）。浙江第四条对一般工商业/大型工商业余电上网比例不作要求，与广东 30% 口径相反；浙江只进来源层，不建规则。",
    }
    upsert_source(source)
    print(f"wrote {OUT_PATH.relative_to(ROOT)}: {len(records)} records {kinds}")
    print(f"upserted {SOURCE_ID} in sources_ext.jsonl (retrieved_at={retrieved})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
