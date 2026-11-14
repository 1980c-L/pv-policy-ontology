"""取回本批官方来源原文到 raw/（只读保存）。

用途：新能源 Ontology 第一批语料（pv-national-2025-v1）的可复跑获取入口。
输出：raw/ 下原始文件 + raw/_fetch.json 获取清单（URL、字节数、sha256、获取时间）。
注意：只抓取方案 §3 列出的官方入口，不扩展抓取范围；不写项目外目录。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
CST = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

NOTICE_URL = "https://www.nea.gov.cn/20250123/112c5b199c5f45dd8e7ac93c9f5e4eaf/c.html"
PLAN = [
    # (source_id, url_or_None, filename, kind, note)
    ("PV-NAT-2025-007", NOTICE_URL, "PV-NAT-2025-007__notice.html", "notice", "通知页，正文在附件 .doc"),
    ("PV-NAT-2025-007-ATTACH", None, "PV-NAT-2025-007__measure.doc", "measure_fulltext", "通知页附件 .doc（URL 从通知页解析）"),
    (
        "PV-EXPLAIN-20250123",
        "https://www.nea.gov.cn/20250123/d38e5436b4d04159863ddbc10a6ede10/c.html",
        "PV-EXPLAIN-20250123__explain.html",
        "official_explain",
        "政策解读",
    ),
    (
        "PV-FAQ-20250411",
        "https://www.nea.gov.cn/20250411/dca28f9186e848dda1d690d192fa50f8/20250411dca28f9186e848dda1d690d192fa50f8_113000de00999043e8a7fe4446dd6d97d8.pdf",
        "PV-FAQ-20250411__faq2025.pdf",
        "official_faq",
        "问答 2025 年版 PDF",
    ),
    (
        "PV-CASE-20250402",
        "https://www.nea.gov.cn/20250402/1c56ebc576504b4e8acb2ae9f5a3ee9b/c.html",
        "PV-CASE-20250402__case.html",
        "consult_case",
        "公开咨询案例，eval_only",
    ),
    (
        "ALT-MOFCOM-PV-NAT-2025-007",
        "https://policy.mofcom.gov.cn/claw/clawContent.shtml?id=101798",
        "ALT-MOFCOM-PV-NAT-2025-007.html",
        "reprint_fulltext",
        "商务部转载全文，仅用于交叉校验 .doc 转换完整性",
    ),
]


def fetch(url: str, referer: str | None = None) -> requests.Response:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
            if resp.status_code == 200 and resp.content:
                return resp
            last = f"HTTP {resp.status_code}, {len(resp.content)} bytes"
        except requests.RequestException as exc:  # 网络异常重试一次
            last = f"{type(exc).__name__}: {exc}"
        if attempt < 3:
            time.sleep(2 * attempt)
    raise RuntimeError(f"fetch failed: {url} :: {last}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    manifest = []
    notice_html: bytes | None = None

    for source_id, url, filename, kind, note in PLAN:
        target = RAW / filename
        if target.exists():
            data = target.read_bytes()
            fetched_url = url or "(cached)"
            status = "cached"
        else:
            if source_id == "PV-NAT-2025-007-ATTACH":
                if notice_html is None:
                    raise RuntimeError("通知页未取回，无法解析附件 URL")
                html = notice_html.decode("utf-8", errors="replace")
                found = re.findall(r'href="([^"]+\.doc)"', html, flags=re.I)
                if not found:
                    raise RuntimeError("通知页中未找到 .doc 附件链接")
                fetched_url = urljoin(NOTICE_URL, found[0])
                url = fetched_url
            else:
                fetched_url = url
            resp = fetch(url, referer=NOTICE_URL if source_id.startswith("PV-NAT") else None)
            data = resp.content
            target.write_bytes(data)
            status = resp.status_code
            if source_id == "PV-NAT-2025-007":
                notice_html = data
        rec = {
            "source_id": source_id,
            "kind": kind,
            "url": fetched_url,
            "raw_path": str(target.relative_to(ROOT)).replace("\\", "/"),
            "raw_bytes": len(data),
            "raw_sha256": sha256(data),
            "fetched_at": datetime.now(CST).isoformat(timespec="seconds"),
            "status": status,
            "note": note,
        }
        manifest.append(rec)
        print(f"{source_id}: {status}, {len(data)} B, {rec['raw_sha256'][:16]}…")

    out = RAW / "_fetch.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"manifest -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
