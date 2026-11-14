#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 raw/ 官方来源解析成 normalized/ 下可定位文本，并生成 sources.jsonl。

用法（项目根执行）:
    python tools/normalize_sources.py

解析范围:
    PV-NAT-2025-007            通知页（元数据 / 正文 / 附件链接）
    PV-NAT-2025-007-ATTACH     通知附件 .doc（办法正文，按"条"切分）
    PV-EXPLAIN-20250123        官方政策解读（按小节切分；解读不是规范正文）
    PV-FAQ-20250411            官方问答 PDF（按问答编号 + 印刷页码切分）
    PV-CASE-20250402           公开咨询案例（eval_only）
    ALT-MOFCOM-PV-NAT-2025-007 商务部转载全文（仅交叉校验 .doc 转换完整性）

输出:
    normalized/<source_id>.jsonl   每行一个可定位记录
    sources.jsonl                  来源清单（字段按方案 §3 建议）
    normalized/_crosscheck.json    .doc 转换 vs 转载全文的逐条比对

纪律: 只读 raw/，不联网；重跑会覆盖上述输出。所有 locator_id 由程序生成，
      不手写，避免定位漂移。正文 .doc 首次运行需 LibreOffice（可用 SOFFICE_PATH
      指定 soffice 路径），转换文本缓存在 .tmp/，删除后自动重建。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from bs4 import BeautifulSoup
import pymupdf

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
NORM = ROOT / "normalized"
BATCH = "pv-national-2025-v1"

CHAPTER_RE = re.compile(r"^(第[〇零一二三四五六七八九十]+章)\s*(.*)$")
CLAUSE_RE = re.compile(r"^(第[〇零一二三四五六七八九十百]+条)\s*(.*)$")
TOP_SECTION_RE = re.compile(r"^([一二三四五六七八九十]+)、")
SUB_SECTION_RE = re.compile(r"^（([一二三四五六七八九十]+)）")
FAQ_TOPIC_RE = re.compile(r"^第[一二三四五六七八九十]+篇")
FAQ_Q_RE = re.compile(r"(?m)^[ \t]*(\d{1,2})[ \t]*[.．][ \t]*")
FAQ_PAGE_NO_RE = re.compile(r"^[ \t]*(\d{1,2})[ \t]*$")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compact(text: str) -> str:
    """引用核验用的压缩形式：去掉全部空白（含全角空格）。

    仅用于“摘录是否确实出现在定位文本里”的判断，不改变落盘文本。
    """
    return re.sub(r"\s+", "", text.replace("\u3000", " "))


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def rec(source_id: str, kind: str, value: str, text: str, **extra) -> dict:
    """构造一条 normalized 记录；locator_id 由 (source_id, kind, value) 生成。"""
    out = {
        "locator_id": f"{source_id}#{kind}={value}",
        "source_id": source_id,
        "kind": kind,
        "value": value,
        "text": text,
    }
    out.update(extra)
    return out


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 通知页

def parse_notice(fetch: dict) -> tuple[list[dict], dict]:
    path = ROOT / fetch["raw_path"]
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    sid = fetch["source_id"]

    meta = {}
    for tr in soup.find_all("tr"):
        tds = [td.get_text("", strip=True) for td in tr.find_all("td")]
        if len(tds) == 2 and tds[0].endswith(":"):
            meta[tds[0].rstrip(":")] = tds[1]
    if not all(k in meta for k in ("公开事项名称", "索引号", "主办单位", "制发日期")):
        # 该页把“标签 / 值”拆成相邻的单元格或独立 div，按扁平序列重新配对
        flat = [el.get_text("", strip=True) for el in soup.find_all(["td", "div"])]
        flat = [t for t in flat if t]
        for i, t in enumerate(flat[:-1]):
            if t.endswith(":"):
                key = t.rstrip(":")
                if key in ("公开事项名称", "索引号", "主办单位", "制发日期"):
                    meta.setdefault(key, flat[i + 1])

    assert meta.get("制发日期") == "2025-01-17", meta
    assert meta.get("索引号") == "000019705/2025-00006", meta

    meta_text = "\n".join(f"{k}: {meta[k]}" for k in ("公开事项名称", "索引号", "主办单位", "制发日期") if k in meta)
    records = [rec(sid, "metadata", "公开事项信息", meta_text, fields=meta)]

    ps = [p.get_text("", strip=True) for p in soup.find_all("p")]
    ps = [t for t in ps if t]
    start = next(i for i, t in enumerate(ps) if "国家能源局关于印发" in t and "通知" in t)
    end = next(i for i, t in enumerate(ps) if t.startswith("附件"))
    body = "\n".join(ps[start:end + 1])
    records.append(rec(sid, "section", "正文", body))

    link = soup.find("a", href=re.compile(r"\.doc(\?|$)"))
    if link is not None:
        href = link["href"]
        if href.startswith("/"):
            href = "https://www.nea.gov.cn" + href
        records.append(rec(sid, "link", "附件", link.get_text("", strip=True), url=href))

    assert "国能发新能规" in compact(body), body[:200]
    assert "2025年1月17日" in compact(body)
    info = {"records": len(records), "body_chars": len(body), "fields": meta}
    return records, info


# ---------------------------------------------------------------- 正文 .doc

def parse_measure(text: str, sid: str) -> tuple[list[dict], dict]:
    lines = text.replace("\ufeff", "").splitlines()
    records: list[dict] = []
    chapters: list[str] = []
    current_chapter = None
    pending: dict | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            pending["text"] = "\n".join(pending.pop("_lines"))
            records.append(rec(sid, "clause", pending["clause"], pending["text"],
                               chapter=pending["chapter"], ordinal=pending["ordinal"]))
            pending = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if pending is None and line == "分布式光伏发电开发建设管理办法":
            continue
        m = CHAPTER_RE.match(line)
        if m:
            flush()
            chapters.append(norm_ws(line))
            current_chapter = chapters[-1]
            continue
        m = CLAUSE_RE.match(line)
        if m:
            flush()
            if current_chapter is None:
                raise AssertionError(f"条文前没有章标题: {line[:40]}")
            label = m.group(1)
            pending = {
                "clause": label,
                "chapter": current_chapter,
                "ordinal": len(records) + 1,
                "_lines": [label + (" " + m.group(2) if m.group(2) else "").rstrip()],
            }
            continue
        if pending is not None:
            pending["_lines"].append(line)
        # 条文前的通知/落款等文字：不进入正文记录

    flush()
    labels = [r["value"] for r in records]
    assert len(records) == 43, f"条文数 {len(records)} != 43"
    assert labels[0] == "第一条" and labels[-1] == "第四十三条", labels[:1] + labels[-1:]
    assert len(set(labels)) == 43, "条文编号重复"
    assert len(chapters) == 7, chapters
    for r in records:
        assert r["text"].strip(), r["locator_id"]
    return records, {"clauses": len(records), "chapters": chapters}


# ---------------------------------------------------------------- 政策解读

def parse_explain(fetch: dict) -> tuple[list[dict], dict]:
    path = ROOT / fetch["raw_path"]
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    sid = fetch["source_id"]
    page_text = soup.get_text("\n", strip=True)
    m = re.search(r"发布时间[:：]\s*(\d{4}-\d{2}-\d{2})", page_text)
    assert m, "解读页找不到发布时间"
    published = m.group(1)
    meta_text = f"《分布式光伏发电开发建设管理办法》政策解读\n发布时间：{published}\n来源：国家能源局"
    records = [rec(sid, "metadata", "元数据", meta_text, published_at=published)]

    ps = [p.get_text("", strip=True) for p in soup.find_all("p")]
    ps = [t for t in ps if t]

    intro = []
    top: dict | None = None
    top_count = 0
    sub_count = 0
    for t in ps:
        mt = TOP_SECTION_RE.match(t)
        if mt:
            flush_top(records, top, sid)
            top_count += 1
            top = {"value": mt.group(1), "text": t, "heading": True}
            continue
        ms = SUB_SECTION_RE.match(t)
        if ms and top is not None:
            sub_count += 1
            value = f"{top['value']}({ms.group(1)})"
            records.append(rec(sid, "section", value, t,
                               parent_section=top["value"], heading_level=2))
            top["text"] += "\n" + t
            continue
        if top is None:
            intro.append(t)
        else:
            top["text"] += "\n" + t
    flush_top(records, top, sid)

    if intro:
        records.insert(1, rec(sid, "section", "引言", "\n".join(intro), heading_level=1))
    assert top_count == 4, top_count
    assert sub_count == 9, sub_count
    return records, {"sections_top": top_count, "sections_sub": sub_count, "published_at": published}


def flush_top(records: list[dict], top: dict | None, sid: str) -> None:
    if top is None:
        return
    records.append(rec(sid, "section", top["value"], top["text"], heading_level=1))


# ---------------------------------------------------------------- 问答 PDF

def parse_faq(fetch: dict) -> tuple[list[dict], dict]:
    path = ROOT / fetch["raw_path"]
    doc = pymupdf.open(path)
    page_count = doc.page_count
    pages = [doc[i].get_text() for i in range(page_count)]
    doc.close()
    sid = fetch["source_id"]
    assert page_count == 40, page_count

    def clean_page_text(raw: str) -> str:
        lines = [ln.rstrip() for ln in raw.splitlines()]
        kept = [ln for ln in lines if ln.strip() and not FAQ_PAGE_NO_RE.match(ln)]
        return "\n".join(kept)

    page_texts = [clean_page_text(p) for p in pages]
    body = "\n".join(page_texts[8:])

    # 篇标题位置 -> 给每个问题分配所属篇
    topic_positions = [(m.start(), m.group(0)) for m in re.finditer(r"(?m)^第[一二三四五六七八九十]+篇[^\n]*", body)]
    assert len(topic_positions) == 7, topic_positions
    matches = list(FAQ_Q_RE.finditer(body))
    numbers = [int(m.group(1)) for m in matches]
    assert numbers == list(range(1, 74)), numbers

    records: list[dict] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        item = body[m.end():end]
        item = re.sub(r"\n?[ \t]*第[一二三四五六七八九十]+篇[^\n]*[ \t]*$", "", item)
        question_text, sep, answer_text = item.partition("答：")
        assert sep, f"Q{i + 1} 缺 答："
        question = re.sub(r"\s*\n\s*", "", question_text).strip()
        answer = re.sub(r"\s*\n\s*", "", answer_text).strip()
        section = [name for pos, name in topic_positions if pos < m.start()][-1]
        # 印刷页码：该问题起点落在哪一页（body 从物理第 9 页开始，印刷第 1 页）
        printed = None
        pdf_page = None
        offset = 0
        for idx, pt in enumerate(page_texts[8:]):
            if offset <= m.start() < offset + len(pt) + 1:
                printed = idx + 1
                pdf_page = idx + 9
                break
            offset += len(pt) + 1
        assert printed is not None, f"Q{i + 1} 无法定位页码"
        records.append(rec(sid, "qa", f"Q{i + 1}", f"{question}\n答：{answer}",
                           question=question, answer=answer, section=section,
                           printed_page_start=printed, pdf_page_start=pdf_page, ordinal=i + 1))

    for idx in range(8, page_count):
        printed = idx - 8 + 1
        records.append(rec(sid, "page", f"p.{printed}", page_texts[idx], printed_page=printed))

    sections = {}
    for r in records:
        if r["kind"] == "qa":
            sections[r["section"]] = sections.get(r["section"], 0) + 1
    info = {"pages": page_count, "printed_pages": page_count - 8, "qa": len(matches), "sections": sections}
    return records, info


# ---------------------------------------------------------------- 咨询案例

def parse_case(fetch: dict) -> tuple[list[dict], dict]:
    path = ROOT / fetch["raw_path"]
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    sid = fetch["source_id"]
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
    title_idx = next(i for i, ln in enumerate(lines) if "关于取得备案证" in ln)
    text = "\n".join(lines[title_idx:])
    published = None
    m = re.search(r"发布时间[:：]\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        published = m.group(1)
    assert "新能源司答复" in text
    records = [
        rec(sid, "metadata", "元数据", f"发布时间：{published}\n来源：国家能源局", published_at=published),
        rec(sid, "section", "咨询及答复", text),
    ]
    return records, {"published_at": published, "chars": len(text)}


# ---------------------------------------------------------------- 转载全文（交叉校验）

def parse_alt(fetch: dict) -> tuple[list[dict], dict]:
    path = ROOT / fetch["raw_path"]
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    text = soup.get_text("\n", strip=True)
    # 去掉页面尾部的“推荐阅读/免责声明/主办单位”等站点外壳，避免被当成条文内容
    for marker in ("\n推荐阅读", "\n免责声明", "\n顶部\n"):
        if marker in text:
            text = text.split(marker, 1)[0]
    sid = fetch["source_id"]
    records, info = parse_measure(text, sid)
    info["published_date_claim"] = re.search(r"【发布日期】\s*\n?(\d{4}-\d{2}-\d{2})", text) is not None
    return records, info


# ---------------------------------------------------------------- sources.jsonl

def build_sources(fetch: list[dict], infos: dict) -> list[dict]:
    base = {
        "PV-NAT-2025-007": dict(
            title="国家能源局关于印发《分布式光伏发电开发建设管理办法》的通知",
            issuer="国家能源局", document_number="国能发新能规〔2025〕7号",
            issued_at="2025-01-17", published_at="2025-01-23",
            role="build", role_detail="建设语料；办法正文及发文元数据（HTML 通知页）",
            evidence_level="normative",
            parse_status="parsed", parse_tool="BeautifulSoup 4.15.0/HTML",
            locator_kinds=["metadata", "section", "link"],
            note="网页标注制发日期 2025-01-17；页面发布于 2025-01-23（见 FAQ Q71）。",
        ),
        "PV-NAT-2025-007-ATTACH": dict(
            title="分布式光伏发电开发建设管理办法（通知附件全文）",
            issuer="国家能源局", document_number="国能发新能规〔2025〕7号",
            issued_at="2025-01-17", published_at="2025-01-23",
            role="build", role_detail="建设语料；办法正文（通知页附件旧版 .doc）",
            evidence_level="normative",
            parse_status="parsed",
            parse_tool="LibreOffice 26.8.2 --convert-to txt:Text (encoded):UTF8",
            locator_kinds=["clause"],
            note="source_id 为对方案 §3 表格的扩展（与对端 Wiki 项目同款拆分，raw_sha256 一致）；正文按条切分 43 条。",
        ),
        "PV-EXPLAIN-20250123": dict(
            title="《分布式光伏发电开发建设管理办法》政策解读",
            issuer="国家能源局", document_number=None,
            issued_at=None, published_at="2025-01-23",
            role="build", role_detail="建设语料；官方政策解读，不能冒充规范正文",
            evidence_level="official_explanation",
            parse_status="parsed", parse_tool="BeautifulSoup 4.15.0/HTML",
            locator_kinds=["metadata", "section"],
            note="引用时 evidence_level 必须标 official_explanation。",
        ),
        "PV-FAQ-20250411": dict(
            title="分布式光伏发电开发建设管理办法问答（2025 年版）",
            issuer="国家能源局", document_number=None,
            issued_at=None, published_at="2025-04-11",
            role="build", role_detail="建设语料；官方解释与问答",
            evidence_level="official_qa",
            parse_status="parsed", parse_tool=f"pymupdf {pymupdf.__version__}/PDF",
            locator_kinds=["qa", "page"],
            note="PDF 共 40 页；正文印刷页 1-32（PDF 第 9 页起）。引用时 evidence_level 标 official_qa。",
        ),
        "PV-CASE-20250402": dict(
            title="关于取得备案证及接入方案的项目，但因建设周期原因，未能于5月1日前并网投产后续如何处置的咨询",
            issuer="国家能源局", document_number=None,
            issued_at=None, published_at="2025-04-02",
            role="eval_only", role_detail="eval_only；公开咨询案例，不并入首批建设语料",
            evidence_level=None,
            parse_status="parsed", parse_tool="BeautifulSoup 4.15.0/HTML",
            locator_kinds=["metadata", "section"],
            note="仅用于评测，不得作为规则或事实的来源。",
        ),
        "ALT-MOFCOM-PV-NAT-2025-007": dict(
            title="国家能源局关于印发《分布式光伏发电开发建设管理办法》的通知（2025修订）转载",
            issuer="商务部（转载国家能源局文件）", document_number="国能发新能规〔2025〕7号",
            issued_at="2025-01-17", published_at=None,
            role="corroborate", role_detail="交叉校验；商务部法规库转载全文，不并入建设语料",
            evidence_level=None,
            parse_status="parsed", parse_tool="BeautifulSoup 4.15.0/HTML",
            locator_kinds=["clause"],
            corroborates="PV-NAT-2025-007-ATTACH",
            note="只用于核对 .doc 转换完整性；不得作为规则或事实的来源。",
        ),
    }
    out = []
    for f in fetch:
        sid = f["source_id"]
        assert sid in base, f"未登记来源 {sid}"
        row = {
            "source_id": sid,
            "canonical_url": f["url"],
            "fetched_url": f["url"],
            "retrieved_at": f["fetched_at"],
            "raw_path": f["raw_path"],
            "raw_sha256": f["raw_sha256"],
            "text_path": f"normalized/{sid}.jsonl",
            "batch": BATCH,
        }
        row.update({k: v for k, v in base[sid].items()})
        if sid in infos:
            row["normalized_summary"] = infos[sid]
        out.append(row)
    out.sort(key=lambda r: r["source_id"])
    return out


# ---------------------------------------------------------------- main

SOFFICE_FALLBACKS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
]


def ensure_measure_txt(raw_rel: str) -> str:
    """把通知附件 .doc 转成 UTF-8 纯文本。

    优先复用 .tmp 下的转换缓存；缓存不存在时调用 LibreOffice（headless）重建，
    这样 fresh checkout 只需装有 raw/ 与 LibreOffice 即可复现，不依赖手工预处理。
    """
    dest = ROOT / ".tmp" / "PV-NAT-2025-007__measure.txt"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest.read_text("utf-8")

    candidates = [os.environ.get("SOFFICE_PATH", ""), *SOFFICE_FALLBACKS, shutil.which("soffice") or ""]
    soffice = next((c for c in candidates if c and Path(c).is_file()), None)
    if not soffice:
        raise SystemExit(
            "找不到 LibreOffice soffice，无法从 .doc 重建正文；"
            "请设置 SOFFICE_PATH 或安装 LibreOffice 后重跑。"
        )

    tmp = ROOT / ".tmp"
    tmp.mkdir(exist_ok=True)
    profile = tmp / "lo-profile"
    profile.mkdir(exist_ok=True)
    cmd = [
        soffice,
        "--headless",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        "txt:Text (encoded):UTF8",
        "--outdir",
        str(tmp),
        str(ROOT / raw_rel),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise SystemExit(f"LibreOffice 转换未产出预期文件: {dest}")
    return dest.read_text("utf-8")


def main() -> int:
    fetch = json.loads((RAW / "_fetch.json").read_text("utf-8"))
    by_id = {f["source_id"]: f for f in fetch}
    infos: dict = {}

    for f in fetch:
        if f["source_id"].startswith("ALT-"):
            continue
        actual = sha256_file(ROOT / f["raw_path"])
        assert actual == f["raw_sha256"], f"raw 哈希不符: {f['source_id']}"

    notice_records, infos["PV-NAT-2025-007"] = parse_notice(by_id["PV-NAT-2025-007"])
    write_jsonl(NORM / "PV-NAT-2025-007.jsonl", notice_records)

    measure_txt = ensure_measure_txt(by_id["PV-NAT-2025-007-ATTACH"]["raw_path"])
    measure_records, info = parse_measure(measure_txt, "PV-NAT-2025-007-ATTACH")
    infos["PV-NAT-2025-007-ATTACH"] = info
    write_jsonl(NORM / "PV-NAT-2025-007-ATTACH.jsonl", measure_records)

    explain_records, infos["PV-EXPLAIN-20250123"] = parse_explain(by_id["PV-EXPLAIN-20250123"])
    write_jsonl(NORM / "PV-EXPLAIN-20250123.jsonl", explain_records)

    faq_records, infos["PV-FAQ-20250411"] = parse_faq(by_id["PV-FAQ-20250411"])
    write_jsonl(NORM / "PV-FAQ-20250411.jsonl", faq_records)

    case_records, infos["PV-CASE-20250402"] = parse_case(by_id["PV-CASE-20250402"])
    write_jsonl(NORM / "PV-CASE-20250402.jsonl", case_records)

    alt_records, infos["ALT-MOFCOM-PV-NAT-2025-007"] = parse_alt(by_id["ALT-MOFCOM-PV-NAT-2025-007"])
    write_jsonl(NORM / "ALT-MOFCOM-PV-NAT-2025-007.jsonl", alt_records)

    # 交叉校验：同一批 43 条，逐条比对压缩文本与首句。
    # .doc 用弯引号 “ ”，转载页用直引号 "；先把引号统一再比，避免把排版差异当成内容差异。
    def canon(s: str) -> str:
        s = compact(s)
        for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")):
            s = s.replace(a, b)
        return s

    doc = {r["value"]: r["text"] for r in measure_records}
    alt = {r["value"]: r["text"] for r in alt_records}
    assert set(doc) == set(alt), (set(doc) ^ set(alt))
    mismatches = []
    first_sentence_mismatch = []
    for label in doc:
        if canon(doc[label]) != canon(alt[label]):
            mismatches.append(label)
        d_first = canon(doc[label]).split("。")[0]
        a_first = canon(alt[label]).split("。")[0]
        if d_first != a_first:
            first_sentence_mismatch.append(label)
    crosscheck = {
        "doc_source_id": "PV-NAT-2025-007-ATTACH",
        "alt_source_id": "ALT-MOFCOM-PV-NAT-2025-007",
        "clauses_compared": len(doc),
        "compare_method": "去空白后比对，并把弯引号/直引号视为等价（仅排版差异）",
        "full_text_equal": len(doc) - len(mismatches),
        "full_text_mismatch_labels": mismatches,
        "first_sentence_equal": len(doc) - len(first_sentence_mismatch),
        "first_sentence_mismatch_labels": first_sentence_mismatch,
    }
    dump_json(NORM / "_crosscheck.json", crosscheck)

    sources = build_sources(fetch, infos)
    with (ROOT / "sources.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for row in sources:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    print(json.dumps({
        "ok": True,
        "infos": infos,
        "crosscheck": crosscheck,
        "outputs": {
            str(p.relative_to(ROOT)): {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
            for p in sorted(list(NORM.glob("*.jsonl")) + [ROOT / "sources.jsonl", NORM / "_crosscheck.json"])
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
