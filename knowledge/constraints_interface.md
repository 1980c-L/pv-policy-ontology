# constraints/v1 接口冻结（2026-11-13）

范围：`python tools/ontology_tool.py constraints` 的 3 个子命令，以及 `export` 里的约束记录。冻结的是**命令形状、输入/输出 JSON 键、退出码和判定语义**，不是内部实现。

修订：2026-11-13 L1 复验后修正 O-C-001/003/004 的判据与组级锚点输入，版本仍为 v1；v1 尚未发布、无外部调用方，本轮视作发布前内部修正。发布后再改判据必须升 v2。

版本字段：`check --json` 与 `list --json` 返回 `"schema_version": "constraints/v1"`。

## 1. 命令与退出码

| 命令 | 输出 | exit 0 | exit 1 | exit 2 |
| --- | --- | --- | --- | --- |
| `constraints list [--json]` | 纯文本 4 行；或 JSON | 正常 | 约束文件缺失/损坏（异常退出） | — |
| `constraints show O-C-xxx` | JSON（原文记录） | 找到 | 未知约束 ID | 未给 ID |
| `constraints check --group <文件\|-> [--json]` | 纯文本摘要；或 JSON | 结构合法，判定完成 | — | 组结构非法、文件读不了、JSON 解析失败 |

`check` 的 **exit 0 不代表约束通过**：结论看 `result_status`/`status`。输入不合法才是 exit 2。脚本调用一律加 `--json`。

## 2. check 输入

组输入是一个 JSON 对象，字段由 `model/constraint_schema.json` 冻结：

- 必填：`group_id`（非空字符串）、`members`（1–20 个）。
- 可选：`policy_as_of`（ISO 日期 YYYY-MM-DD，组级兜底）、`existing_connection_point_id`（Q31 的“已有连接点”锚点，仅单红线组可用）、`existing_connection_points`（按红线给的锚点集合 `{red_line_id: string 或 [string]}`，与单锚点同时给时优先）、`label`（字符串）。
- 每个 member 的字段 = `model/schema.json` 的 21 个基础字段 + 12 个扩展字段：`red_line_id`、`building_red_line_id`、`ancillary_site_red_line_id`、`uses_ancillary_site`、`building_ancillary_same_red_line`、`project_land_red_line_id`、`user_substation_red_line_id`、`uses_user_substation`、`electricity_account_id`、`grid_connection_point_id`、`investor_id`、`phase_id`。
- 成员自带 `policy_as_of` 时，覆盖组级日期，并同时用于规则时效过滤；输出 `member_assessments[].policy_as_of` 回显实际生效日期。
- 不允许注入 `project_class` 等派生字段；分类由 `src/rule_eval.py` 重算。
- `project_id` 组内唯一；空白字符串按“缺字段”处理。
- 未知 group/member 字段、重复 `project_id`、超过 20 人、坏日期、类型不符、`label` 非字符串、`existing_connection_points` 不是对象或含空数组 → exit 2，逐条打印 `ERROR`。

## 3. check 输出

```json
{
  "schema_version": "constraints/v1",
  "group_id": "G-01-ancillary-same",
  "policy_as_of": "2026-09-01",
  "result_status": "satisfied",
  "constraints": [
    {
      "constraint_id": "O-C-001",
      "title": "附属场所与建筑物应在同一用地红线",
      "status": "satisfied",
      "decision": "same_red_line_confirmed",
      "affected_project_ids": [],
      "missing_fields": [],
      "reasons": ["P-01A: 附属场所与建筑物红线 ID 相同"],
      "source_refs": [{"source_id": "PV-FAQ-20250411", "locator_id": "PV-FAQ-20250411#qa=Q14", "quote": "……", "evidence_level": "official_qa"}]
    }
  ],
  "missing_fields": [],
  "source_refs": [],
  "member_assessments": [
    {"project_id": "P-01A", "policy_as_of": "2026-09-01", "project_class": "general_industrial_business", "result_status": "supported", "missing_fields": []}
  ],
  "summary": {"satisfied": 1, "violated": 0, "insufficient": 0, "conflict": 0, "not_applicable": 3, "total": 4}
}
```

语义：

- `result_status`：组内 4 条约束的最坏状态，优先级 `conflict > violated > insufficient > satisfied > not_applicable`。
- `status`：单条约束的判定，取值同上 5 个。
- `decision`：给人看的决策标签，不是机器枚举之外的承诺；当前含 `same_red_line_confirmed`、`same_red_line_required`、`dedicated_line_exception`、`must_merge`、`may_separate`、`no_new_connection_point`、`existing_point_shared`、`insufficient_information`、`not_applicable`、`conflict`。
- `affected_project_ids`：本条约束判定涉及的成员；`satisfied`/`not_applicable` 通常为空。
- `missing_fields`：组级并集；成员缺字段写成 `project_id.field`，组级缺字段写成 `field`（按红线缺锚点写成 `group.existing_connection_points[red_line_id]`）。
- `source_refs`：按约束去重后的引用；每条约束至少 1 条 normative。
- `member_assessments`：每个成员的单项目规则评估摘要，供解释分类来源；不是约束结论。

## 4. 四条约束的判据

| ID | 类型 | 触发 | 结论 |
| --- | --- | --- | --- |
| O-C-001 | site_red_line | 成员 `uses_ancillary_site=true` | 两个红线 ID 相同→satisfied；不同→violated；仅当显式 `building_ancillary_same_red_line` 与两个 ID 矛盾→conflict；缺 ID→insufficient；`false`→not_applicable |
| O-C-002 | site_red_line | 成员 `uses_user_substation=true` | 项目用地与用户变电站红线 ID 相同→satisfied；不同→violated；例外（大型工商业 + 专线）→not_applicable/`dedicated_line_exception`；缺 `connection_type` 或分类未定→insufficient；`false`→not_applicable |
| O-C-003 | account_merge | 同一 `red_line_id` 内 ≥2 个成员 | 同户号子集（如 A/A/B 中的 A/A）→violated/`must_merge`，其余户号可独立；每个户号至多 1 个成员→satisfied/`may_separate`；缺户号/红线的成员单列 insufficient，不掩盖已判定的 must_merge；单成员或不同红线→not_applicable |
| O-C-004 | connection_point | 同一 `red_line_id` 内 ≥2 个工商业成员 | 每条红线的连接点都在该线锚点集合内→satisfied/`existing_point_shared`；任一已给点不在集合内→violated/`no_new_connection_point`；缺该线锚点/连接点/分类的成员单列 insufficient，不掩盖别的红线或已给点的新增；投资人与分期完全相同→not_applicable；非工商业成员不参与，工商业子集继续判 |

要点：

- “必须合并”只由 O-C-003 的**同一户号**触发；“允许独立”是多个户号下的 `may_separate`，不是“建议分开”。
- O-C-003 判 `must_merge` 时，`affected_project_ids` **只列同户号重复的成员**；缺户号或缺红线的成员只在 `missing_fields` 与 `reasons` 里，不得混进受影响集合。没有重复户号、整条约束为 `insufficient` 时，`affected_project_ids` 才指向缺字段成员。
- O-C-004 的 `affected_project_ids` 按“本条结论所指向的成员”列：已给点违反的成员与缺锚点/缺连接点而无法判定的成员会同时出现；调用方若要区分，应结合该成员的 `missing_fields` 判断，不能把 `affected_project_ids` 当成“全部违规”。
- “信息不足”必须是缺**判定所需**字段；不能因为项目整体 `insufficient` 就把约束一律判 insufficient。已能判定的成员/子集照常给出 violated/satisfied，缺字段成员另列。
- O-C-004 的锚点按红线取：`existing_connection_points[red_line_id]` 可给单个或多个既有点；该对象存在时，某红线没给锚点就是缺锚点，不再退回组级单锚点。只有完全没有该对象时，组级单锚点才可用于“组内只有一条红线”的组，不能跨红线复用。
- O-C-004 只判工商业项目；同红线内的非工商业成员不参与，但不会把整条约束压成 not_applicable。unclassified 视为“不属于四类”，按非工商业处理，不是缺字段。
- 红线 ID 是调用方给的不透明字符串；本工具不核验产权证、宗地图。
- O-C-002 的例外只看 `project_class=large_industrial_business` 且 `connection_type=dedicated_line`；缺一不可。

## 5. export 记录

`export` 新增 `record_type: "constraint"`，字段与其它记录同形：`record_id`、`text`、`entity_ids`、`fact_ids`、`rule_ids`、`source_refs`、`conditions`（含 `constraint_type`/`parameters`/`question`）、`temporal_scope`、`claim_status: "supported"`、`knowledge_kind: "derived"`、`batch`。约束不挂 `rule_ids`，避免被误当成规则。

## 6. 兼容规则

- **允许**新增 JSON 键；调用方必须忽略未知键。
- **必须升 v2**：删除/重命名键、改变键类型、改变 `status`/`decision` 语义、改变退出码、改变 4 条约束的判据。**v1 发布后**再改判据必须升 v2；本次是 v1 发布前修正，已在头部记录。
- 升 v2 时同步改：`src/constraint_eval.py`、`model/constraint_schema.json`、`knowledge/constraints.json`、`tools/constraint_tests.py`、`reviews/28`，并重跑 `run_all`。
- `list`/`show` 的纯文本只供人工查看；脚本不要解析。

## 7. 已验证的契约

`tools/constraint_tests.py` 共 225 项：31 个固定组的组级/约束级状态与决策、缺字段与受影响成员的点名（含 O-C-003 `must_merge` 的 affected 精确集合）、8 类结构负例（注入派生字段、重复 `project_id`、超 20 人、坏日期、缺 `group_id`、`label` 非字符串、`existing_connection_points` 非对象/空数组）、6 项 CLI 契约（list/show/check、未知 ID exit 1、非法输入 exit 2）。运行：

```powershell
cd "E:\deepseek harness\项目\新能源Ontology"
python tools\constraint_tests.py
```

`tools/negative_tests.py` 共 32 条，其中 9 条约束负例（类型、未知参数、缺 normative、重复 ID、坏前缀、schema 缺键、pair 同字段、非法目标分类、时间倒置）。

## 8. 已知边界

- 只做同一用地红线内的四条约束；不判断备案主体资格、产权、建设管理全流程、电网接入批复。
- 集中式项目后续管理规则未建；O-C-002 的“按集中式备案”只作为结论提示，不展开后续流程。
- 红线 ID/户号/连接点由调用方提供，工具不做外部核验。
- `temporal_scope` 只校验格式与起止先后，不自动与来源发布日期联动。
- `existing_connection_points` 的键是调用方给的红线标识；本工具不核验它是否与成员的 `red_line_id` 指向同一宗地。
- `export --output` 可指向项目外路径；指向项目内时打印相对路径，项目外打印绝对路径。
- 关系边不在 `evidence_audit.py` 回检范围内；约束引用已纳入（当前 75 条 = 原 63 + 约束 12）。
