# ask/v1 稳定接口

面向本项目的本地问答契约：**问题 → 检索候选原文 →（可选）结构化规则判定 → 带 locator 引用的回答**。
不联网、不调用模型、不依赖第三方服务；纯 Python 标准库 + 本仓库代码。

## 1. 快速开始

```powershell
# 在仓库根目录运行

# 单条，人类可读
python tools\ask.py --question "10千伏8兆瓦的项目属于一般工商业吗？"

# 单条，机器可读（始终返回 results 数组）
python tools\ask.py --question "分布式光伏分几类？" --json

# 带项目字段做规则判定
python tools\ask.py --question "这个项目还能按原政策吗？" --project eval\project.example.json --policy-as-of 2025-06-01 --json

# 批量：对象或数组，从 stdin 或文件读
echo '{"question":"分布式光伏分几类？","top_k":3}' | python tools\ask.py --input - --json
python tools\ask.py --input requests.json --json

# 契约与版本
python tools\ask.py --version        # ask/v1
python tools\ask.py --dump-schema    # 打印 interface/ask.schema.json
```

## 2. 输入

单条请求对象，或请求对象数组（按数组顺序返回）。

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `question` | 是 | string | 用户原话，非空 |
| `project` | 否 | object | 项目结构化字段，口径见 `model/schema.json`；不传只给候选证据，不做判定 |
| `policy_as_of` | 否 | string | `YYYY-MM-DD`，判断规则是否在有效期内 |
| `top_k` | 否 | integer | 1–20，默认取 `knowledge/retrieval_params.json` 的 5 |
| `request_id` | 否 | string | 原样回传，便于批量对账 |

约束：

- **拒绝未知字段**（避免拼写错误被静默忽略）。
- 批量请求里**任意一条不合法，整批拒绝**，不会返回“半批结果”。
- `--input` 不能与 `--question/--project/--policy-as-of/--top-k` 同时使用。
- CLI 的 `--project` 只接受文件路径，仅单条模式；批量请把 project 对象放进 JSON。

## 3. 输出

成功（单条也包在数组里）：

```json
{
  "schema_version": "ask/v1",
  "results": [
    {
      "schema_version": "ask/v1",
      "request_id": "可选，原样回传",
      "question": "原问题",
      "scenario": "classification",
      "retrieval": {
        "method": "bm25+level_bonus+route:national",
        "params": {"k1": 1.2, "b": 0.3, "level_bonus": 0.18},
        "corpus_units": 260,
        "evidence": [
          {"rank": 1, "locator_id": "...", "source_id": "...", "kind": "clause",
           "evidence_level": "normative", "score": 1.108, "quote": "..."}
        ]
      },
      "assessment": {"result_status": "supported", "...": "规则判定原始输出或 null"},
      "answer": {
        "verdict": "supported",
        "scenario": "classification",
        "summary": "第一句结论",
        "points": ["..."],
        "missing_fields": [],
        "source_refs": [
          {"locator_id": "...", "source_id": "...", "evidence_level": "normative", "quote": "..."}
        ]
      },
      "warnings": ["引文缺失等非致命问题"]
    }
  ]
}
```

要点：

- `assessment` 只在传了 `project` 且问题未命中范围声明时非 null；规则判定逻辑在 `src/rule_eval.py`，接口层不复制。
- `retrieval` 只是候选：`evidence` 按分数排序，**不参与规则判定**。
- `answer.source_refs` 可能少于 `evidence`——缺失的 locator 会跳过并写进 `warnings`。
- `verdict` 枚举：`supported / insufficient / conflict / out_of_scope / unclassified / evidence_only / province_summary / province_pointer / abolished_document`。
- `scenario` 枚举：`legacy / grid_mode / classification / effective / capacity / province / evidence / scope`。

## 4. 错误

失败时返回统一错误对象，**不把 Python 栈抛给调用方**：

```json
{
  "schema_version": "ask/v1",
  "error": {
    "code": "invalid_request",
    "message": "请求字段不合法",
    "details": [{"index": 0, "field": "top_k", "reason": "必须是 1..20 的整数或省略"}]
  }
}
```

| code | exit | 触发 |
| --- | ---: | --- |
| `invalid_request` | 2 | 缺 question、未知字段、top_k 越界/非整数、日期不合法、batch 中任一对象不合法、JSON 解析失败 |
| `internal_error` | 1 | 未预期异常；stderr 保留 traceback，stdout 仍是结构化错误 |

人类可读模式（不加 `--json` 且不用 `--input`）下，错误写 stderr、exit code 不变。

## 5. 版本策略

- `schema_version` 是契约版本，当前 `ask/v1`。
- **兼容变更**（新增可选字段、新增 verdict 枚举值、修正文案）留在 v1，但新增枚举值算客户端可见变化，需在 `reviews` 记录。
- **破坏变更**（改字段名/删字段/改语义/改错误码）升 `ask/v2`，旧 schema 文件保留。
- 客户端应检查 `schema_version` 与 `results`/`error` 二选一，不要依赖字段顺序。
- 契约文件：
  - `interface/ask.schema.json`：响应
  - `interface/ask-input.schema.json`：输入
  - `interface/ask-tool.json`：agent tool 定义（name `ask_new_energy_policy`）

## 6. 边界

- 不自动从自然语言抽项目字段；分类/上网模式/新老划断必须传 `project`，否则 `evidence_only`。
- 省名路由只认字面“浙江/广东/江苏”；省题收益未被 36 道国家题评测覆盖。
- 省级覆盖目前只有浙江、广东要点；其他省给 `province_pointer`，不代答。
- 检索参数在 `knowledge/retrieval_params.json`，`ask.py` 与 `evidence_audit.py` 共用；改参数要重跑 `run_all.py`。
- 不是法律意见；`insufficient` / `unclassified` 不等于“不允许”。
- 没有 HTTP 服务、没有鉴权、没有限流；需要时在进程外包装，不要改本契约。

## 7. 验证

```powershell
python tools\ask_contract_tests.py   # 18 项契约测试
python tools\run_all.py             # 15 步，含 contract、constraints、consistency
```

契约测试覆盖：版本、schema dump、单条、批量 stdin、带 project、范围声明、未知字段、top_k 边界（0/21/"5"/true）、非法日期、缺 question、空批量、标量输入、参数冲突、人类可读模式、坏 JSON、输入 schema。
