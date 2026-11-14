# -*- coding: utf-8 -*-
"""抓取第二批语料（省级实施意见 + 电力现货基本规则）。

只抓公开官方页面，记录 URL / HTTP 状态 / 字节数 / sha256 / 抓取时间。
不覆盖已有 raw 文件（除非显式 --refresh）。

用法：
    python tools/fetch_corpus_v2.py [--refresh]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "raw"
MANIFEST = RAW / "_fetch_v2.json"
CST = timezone(timedelta(hours=8))

SOURCES = [
    {
        "source_id": "PV-JS-2025-002",
        "url": "https://fzggw.jiangsu.gov.cn/art/2025/5/8/art_51007_11560583.html",
        "filename": "PV-JS-2025-002.html",
        "title": "省发展改革委 国家能源局江苏监管办关于贯彻落实《分布式光伏发电开发建设管理办法》的实施意见",
        "issuer": "江苏省发展改革委、国家能源局江苏监管办",
        "document_number": "苏发改规发〔2025〕2号",
        "issued_at": "2025-04-29",
        "published_at": "2025-05-08",
        "verify": False,  # 该站证书链在本机 Python 下不完整，记录并披露
    },
    {
        "source_id": "PV-GD-2025-002",
        "url": "https://drc.gd.gov.cn/gdsnyj/gkmlpt/content/4/4837/mpost_4837956.html",
        "filename": "PV-GD-2025-002.html",
        "title": "广东省能源局关于贯彻落实《分布式光伏发电开发建设管理办法》的实施意见（试行）",
        "issuer": "广东省能源局",
        "document_number": "粤能规〔2025〕2号",
        "issued_at": "2025-12-31",
        "published_at": "2025-12-31",
        "verify": True,
    },
    {
        "source_id": "PV-SPOT-2023-1217",
        "url": "https://www.gov.cn/gongbao/2023/issue_10846/202311/content_6917342.html",
        "filename": "PV-SPOT-2023-1217.html",
        "title": "电力现货市场基本规则（试行）",
        "issuer": "国家发展改革委、国家能源局",
        "document_number": "发改能源规〔2023〕1217号",
        "issued_at": "2023-09-07",
        "published_at": None,
        "verify": True,
    },
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="覆盖已存在的 raw 文件")
    args = ap.parse_args()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    manifest = {"batch": "pv-corpus-2025-v2", "fetched_at": datetime.now(CST).isoformat(timespec="seconds"),
                "sources": []}
    for spec in SOURCES:
        path = RAW / spec["filename"]
        if path.exists() and not args.refresh:
            data = path.read_bytes()
            status = "cached"
        else:
            resp = requests.get(spec["url"], headers=headers, timeout=60, verify=spec["verify"])
            resp.raise_for_status()
            data = resp.content
            path.write_bytes(data)
            status = "downloaded"
        rec = {
            "source_id": spec["source_id"],
            "url": spec["url"],
            "canonical_url": spec["url"],
            "fetched_url": spec["url"],
            "raw_path": f"raw/{spec['filename']}",
            "raw_sha256": sha256_bytes(data),
            "bytes": len(data),
            "http_status": 200 if status == "downloaded" else None,
            "fetch_status": status,
            "fetched_at": datetime.now(CST).isoformat(timespec="seconds"),
            "title": spec["title"],
            "issuer": spec["issuer"],
            "document_number": spec["document_number"],
            "issued_at": spec["issued_at"],
            "published_at": spec["published_at"],
            "tls_verify": spec["verify"],
        }
        manifest["sources"].append(rec)
        print(f"[{status}] {spec['source_id']} {len(data)} B sha256={rec['raw_sha256'][:16]}…")
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"manifest -> {MANIFEST.relative_to(ROOT)} ({MANIFEST.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
