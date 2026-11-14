# 分布式光伏政策 Ontology

把政策条款转成可执行的项目规则。输入省份、政策日期、装机容量和并网电压等字段，返回项目分类、上网模式、缺失信息、判定理由和原文出处。

项目围绕国家政策与江苏、浙江、广东的部分实施条款，展示规则、实体关系、多项目约束和跨文档一致性的建模与校验。

## 已实现

| 能力 | 实现 |
|---|---|
| 项目规则判定 | 4 条规则，覆盖项目分类、上网模式、新旧政策和设施农业比例 |
| 多项目约束 | 4 条约束，检查同一用地红线、户号合并与公共连接点 |
| 实体与关系 | 7 个实体、5 个事实、13 条关系；支持祖先查询与条款影响追溯 |
| 跨文档一致性 | 19 条对照主张，连接国家和省级文件的 42 条 statement 引用 |
| 本地问答接口 | `ask/v1`，支持单条、批量 JSON 和工具定义 |

## 快速运行

使用 Python 3.10+，在仓库根目录运行：

```bash
# 按问题查原文证据
python tools/ask.py --question "分布式光伏分几类？" --json
# 带结构化项目字段执行规则判定
python tools/ask.py --question "这个项目还能按原政策吗？" --project eval/project.example.json --policy-as-of 2025-06-01 --json
# 查看第五条关联的规则影响
python tools/ontology_tool.py relations impact "PV-NAT-2025-007-ATTACH#clause=第五条"
# 检查多项目约束
python tools/ontology_tool.py constraints check --group eval/constraint_group.example.json
```

本地主流程使用 Python 标准库。项目字段模板见 [`eval/project.example.json`](eval/project.example.json)，完整输入输出见 [接口说明](interface/README.md)。

## 关键设计

**缺信息时准确报告待补字段。** 例如，浙江大型工商业项目的上网模式依赖政策日期；日期缺失时返回 `insufficient` 和缺失字段，补全后再选择适用分支。

**检索与规则分别运行。** BM25 检索帮助找到条款；规则引擎依据结构化字段和条件表给出结论。返回结果保留两部分证据，便于定位错误发生在检索、输入还是规则。

**政策变化可以追溯。** 通过条款细化、实体分类和事实约束关系，列出已建关系内受变化影响的规则。

## 运行验证

```bash
python tools/run_all.py
```

统一入口依次运行结构校验、67 个规则案例、多项目约束、实体关系、接口与更新演练等 15 步检查。详细结果见 [验证记录](docs/VALIDATION.md)。

| 目录 | 内容 |
|---|---|
| [`model/`](model/) | 项目字段、实体关系和约束输入结构 |
| [`knowledge/`](knowledge/) | 规则、关系、约束、一致性对照与检索参数 |
| [`src/`](src/) | 规则与约束求值引擎 |
| [`tools/`](tools/) | 查询、追溯、校验和回归入口 |
| [`interface/`](interface/) | `ask/v1` 输入输出结构与 agent 工具定义 |
| [`eval/`](eval/) | 案例、项目样例和评测参照 |
| [`raw/`](raw/) · [`normalized/`](normalized/) | 公开政策快照与可定位条文 |

[新能源知识库](https://github.com/1980c-L/new-energy-knowledge-base) 提供原文、Wiki 与规则层整合后的 Dify 展示；本仓库集中呈现规则和本体实现。

数据范围和规则解释见 [使用范围](docs/SCOPE.md)，来源与使用说明见 [NOTICE](NOTICE.md)。
