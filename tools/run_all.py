# -*- coding: utf-8 -*-
"""统一 runner：按固定顺序跑完校验/判定/演练链，用退出码汇总。

用法：
  python tools/run_all.py              # 验证链（不联网、不改语料）
  python tools/run_all.py --with-corpus  # 先抓取+切分语料，再跑验证链

约定：
- 任一步非零退出即整体失败（但仍把后续步骤跑完，便于一次看全问题）。
- 每步输出原样转发（UTF-8），并记录耗时；末尾打印汇总表。
- 报告写到 .tmp/run_all-report.json。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ontology_tool.py"
REPORT = ROOT / ".tmp" / "run_all-report.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

VERIFY_STEPS = [
    ("validate", [str(TOOL), "validate", "--samples"]),
    ("assess", [str(TOOL), "assess", "--cases", str(ROOT / "eval" / "cases.jsonl")]),
    ("query", [str(TOOL), "query", "--all"]),
    ("export", [str(TOOL), "export"]),
    ("negative", [str(ROOT / "tools" / "negative_tests.py")]),
    ("relations", [str(ROOT / "tools" / "relations_tests.py")]),
    ("constraints", [str(ROOT / "tools" / "constraint_tests.py")]),
    ("consistency", [str(ROOT / "tools" / "consistency_tests.py")]),
    ("behavior", [str(ROOT / "tools" / "rule_behavior_tests.py")]),
    ("scenario", [str(ROOT / "tools" / "scenario_tests.py")]),
    ("realq", [str(ROOT / "tools" / "real_question_tests.py")]),
    ("contract", [str(ROOT / "tools" / "ask_contract_tests.py")]),
    ("audit", [str(ROOT / "tools" / "evidence_audit.py")]),
    ("conflict", [str(ROOT / "tools" / "conflict_drill.py")]),
    ("update", [str(ROOT / "tools" / "update_drill.py")]),
]

CORPUS_STEPS = [
    ("fetch_sources", [str(ROOT / "tools" / "fetch_sources.py")]),
    ("fetch_corpus_v2", [str(ROOT / "tools" / "fetch_corpus_v2.py")]),
    ("normalize_sources", [str(ROOT / "tools" / "normalize_sources.py")]),
    # 注意：normalize_corpus_v2 会整体重写 sources_ext.jsonl，必须排在 normalize_zj 之前
    ("normalize_corpus_v2", [str(ROOT / "tools" / "normalize_corpus_v2.py")]),
    ("normalize_zj", [str(ROOT / "tools" / "normalize_zj.py")]),
]


def run_step(name: str, argv: list[str]) -> dict:
    started = time.time()
    proc = subprocess.run(
        [sys.executable, *argv],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=CHILD_ENV,
    )
    secs = round(time.time() - started, 1)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(f"\n===== {name} (exit={proc.returncode}, {secs}s) =====")
    print(out.strip()[:6000])
    if len(out) > 6000:
        print(f"...（输出其余 {len(out) - 6000} 字符已截断）")
    return {"step": name, "exit": proc.returncode, "seconds": secs}


def main(argv: list[str]) -> int:
    steps = list(VERIFY_STEPS)
    if "--with-corpus" in argv:
        steps = CORPUS_STEPS + steps
        print("注意：--with-corpus 会联网抓取并重写 sources_ext.jsonl / normalized。")
    if "--list" in argv:
        for name, _ in steps:
            print(name)
        return 0

    results = [run_step(name, args) for name, args in steps]
    failed = [r for r in results if r["exit"] != 0]

    print("\n===== 汇总 =====")
    for r in results:
        mark = "PASS" if r["exit"] == 0 else "FAIL"
        print(f"[{mark}] {r['step']:<18} exit={r['exit']:<3} {r['seconds']:>6}s")
    print(f"\n{len(results) - len(failed)}/{len(results)} 步通过")
    if failed:
        print("失败步骤：" + ", ".join(r["step"] for r in failed))
        print("提示：先看对应步骤上方输出；数据/引用问题优先看 validate，判定问题看 assess。")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "with_corpus": "--with-corpus" in argv,
                "results": results,
                "passed": len(results) - len(failed),
                "total": len(results),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"报告：{REPORT.relative_to(ROOT)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
