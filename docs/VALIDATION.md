# 验证记录

2026-11-15，Windows / Python 3.14.5，在独立发布候选目录运行 `python tools/run_all.py`，15/15 步通过。

| 检查 | 结果 |
|---|---|
| 来源、字段和引用结构 | validate 通过 |
| 规则判定 | 67/67 案例 |
| 能力问题与导出 | query / export 通过 |
| 数据与结构负例 | negative 通过 |
| 实体关系 | relations 通过 |
| 多项目约束 | constraints 通过 |
| 跨文档一致性 | consistency 通过 |
| 规则边界与场景 | behavior / scenario 通过 |
| 问法与接口 | realq / contract 通过 |
| 引用回检 | audit 通过 |
| 冲突与更新演练 | conflict / update 通过 |

逐步退出码与耗时见 [结构化报告](checks/run_all-report.json)，断言明细见 [完整运行输出](checks/run_all.txt)。README 的查询、带字段判定、影响追溯和约束检查命令已实际运行。

本轮检查使用随包公开政策快照和本地案例。可选语料刷新与向量模型实验的环境要求见 [使用范围](SCOPE.md)。
