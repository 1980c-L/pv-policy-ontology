# 跨文档一致性对照表（consistency/v1）接口说明

> 状态：v1 已实现并通过校验（2026-11-13）。结构冻结，只允许加数据，不允许改结构；要改结构或改派生规则必须升 `consistency/v2` 并另写评审记录。

## 0. 一句话

这张表回答的问题是：**同一件事，几份文件各说了什么？是同文复述、省级细化，还是实质冲突？**

它只做"登记与标注"，不改任何一条规则的判定结果。发现"规则没覆盖"或"有效期没建模"，只在 `risk` 字段登记并写清影响，留给下一轮决定要不要修。

## 1. 它和关系层不是一回事

| | `knowledge/relations.jsonl` | `knowledge/consistency.json` |
| --- | --- | --- |
| 粒度 | 条款 → 条款 | 事项（claim）→ 多份文件的陈述 |
| 用途 | 导航：这条规则细化/引用了哪条 | 对表：同一事项各文件说法是否一致 |
| 方向 | 有向边（refines / constrains / subclass_of …） | 无序集合 + 一个对照基准 `canonical` |
| 跨省差异 | 一条边只能连两个节点 | 一条 claim 可同时收广东、浙江、国家三份陈述 |

两者通过 `refines_edges` 互相引用：claim 可以指向说明同一事项的 `refines` 边，免得同一判断写两遍。

## 2. 数据与校验入口

- 数据文件：`knowledge/consistency.json`
- 结构校验：`python tools/ontology_tool.py validate`（内部 `validate_consistency`）
- 查询：`python tools/ontology_tool.py consistency list|show …`
- 回归：`tools/consistency_tests.py`（27 条断言，含覆盖与 CLI 契约）、`tools/negative_tests.py` 中 13 条 claim 负例
- 全量：`tools/run_all.py` 第 8 步 `consistency`（共 15 步）

## 3. 字段

| 字段 | 必填 | 含义与取值 |
| --- | --- | --- |
| `schema_version` | 是 | 固定 `consistency/v1` |
| `batch` / `note` | 否 | 批次名与人读说明 |
| `claims[]` | 是 | 非空数组；一条 claim = 一个可回答的法律/事实问题 |
| `claim_id` | 是 | `O-K-NNN`，唯一 |
| `key` | 是 | 稳定英文键，如 `classification.four_types`，唯一 |
| `label` | 是 | 人读标题 |
| `relation` | 是 | `same_text` / `refined` / `conflict` / `single_source`，必须与 statements 派生结果一致 |
| `temporal` | 是 | `{kind, at, note}`；`kind ∈ none/expiring/expired`，`kind=none` 时 `at` 必须为 null |
| `canonical` | 是 | 对照基准，必须是 statements 里某个 locator，且该条 `effect=same` |
| `statements[]` | 是 | 每份文件一条：`source_id` / `locator_id` / `quote`（≤40 字原文子串）/ `evidence_level` / `effect` / 可选 `note` |
| `rule_refs` | 否 | 相关规则 id，必须存在 |
| `fact_refs` | 否 | 相关事实 id，必须存在 |
| `refines_edges` | 否 | 相关 `refines` 边 id，必须存在且类型为 refines |
| `risk` | 是 | `none` / `rule_uncovered` / `rule_scope_missing` / `expiry_unmodeled`；非 none 时 `note` 必须含「影响」 |
| `note` | 是 | 人读说明：差异点、为什么这样判、风险影响 |

## 4. 两个标签维度

**statement 级 `effect`**（每份文件相对基准的性质）：

| effect | 含义 |
| --- | --- |
| `same` | 与基准同文复述（允许省略无关句） |
| `provincial_narrower` | 省级口径更窄（如浙江删掉 66 千伏） |
| `provincial_broader` | 省级口径更宽（如浙江不要求现货市场即可余电上网） |
| `interpretive` | 解释性文件给出可执行口径（如"发布之日"= 公布日） |
| `different` | 与基准结论互斥 |

**claim 级 `relation`**（由 statements 派生，校验器强制一致）：

```
有 different                      → conflict
只有 1 条 statement               → single_source
含 provincial_* 或 interpretive   → refined
其余                              → same_text
```

`conflict` 是严格门槛：同级、同地、同时点，结论互斥，且不能用上级授权留白解释。跨省差异一律不算冲突，记 `refined`。

## 5. 校验门禁（13 条）

1. `schema_version` = `consistency/v1`
2. `claim_id` 形如 `O-K-\d{3}` 且唯一
3. `key` 非空且唯一
4. `label` / `note` 非空
5. `relation` / `risk` / `effect` / `temporal.kind` 在枚举内
6. `temporal`：`kind≠none` 时 `at` 为 `YYYY-MM-DD` 且 `note` 非空；`kind=none` 时 `at` 为 null
7. statements 非空，逐条过 `validate_refs`（来源 build、等级与来源一致、locator 存在、kind/value 匹配、quote ≤40 字且为去空白后的子串）
8. `canonical` ∈ statements 且该条 `effect=same`（不要求是上级文件：两省分叉、上位法留白时取其中一份为基准）
9. `relation` 与 effect 派生结果一致
10. `relation=conflict` 时至少 1 条 normative 对照
11. `rule_refs` / `fact_refs` 可解析；`refines_edges` 是现存 refines 边
12. `risk≠none` 时 `note` 含「影响」
13. **覆盖门禁**：规则和事实引用到的每个 locator（当前 17 个）都必须至少出现在一条 claim 的 statements 里

## 6. CLI 契约

```bash
python tools/ontology_tool.py consistency list          # 19 行：id / relation / risk / 条数 / label
python tools/ontology_tool.py consistency list --json   # {"schema_version", "claims":[{claim_id,key,label,relation,risk,temporal,statements}]}
python tools/ontology_tool.py consistency show O-K-001  # 单条完整 JSON（也可用 key）
python tools/ontology_tool.py consistency show K-01     # 未命中 → 退出码 1
```

## 7. 现状（2026-11-14）

19 条 claim：`same_text` 5 / `refined` 6 / `single_source` 8 / `conflict` 0。

登记的风险 3 条（O-K-006、O-K-013、O-K-014 已随规则补齐转为 `none`，见 §7.1）：

| claim | risk | 事由 |
| --- | --- | --- |
| O-K-015 | rule_uncovered | 浙江超容量电压按集中式备案，规则只判分布式四类 |
| O-K-016 | rule_uncovered | 浙江红线外道路/边坡按集中式管理，无对应分支 |
| O-K-019 | expiry_unmodeled | 国家办法五年有效期（2030-01-23 届满）未进规则 `effective_to` |

### 7.1 已解决的规则缺口

| claim | 处理 | 依据 |
| --- | --- | --- |
| O-K-006 | `O-R-002` 新增 `zhejiang_large_industrial_two_modes` 分支：浙江大型工商业 `policy_as_of >= 2026-08-03` 时可直接余电上网，不再要求 `spot_market_region`；细则生效前、或省名/日期缺失时仍按国家口径判或返回 insufficient | 浙江第四条；reviews/37 |
| O-K-013 | `O-R-001` 新增 `guangdong_rural_public_non_natural_household` 分支（2026-02-10～2028-02-09，可选布尔 `rural_public_site`，`blocks_on_unknown`）：广东农村公共建筑归非自然人户用；缺字段/日期/省份返回 insufficient；期间外不适用 | 广东第一节、第六节；reviews/39、reviews/40（独立复验通过，阻断 0） |
| O-K-014 | `O-R-001` 新增 A/B 两支（`guangdong_factory_multi_line_reference_large`、`guangdong_factory_full_self_consumption_reference_large`，2026-02-10～2028-02-09，`blocks_on_unknown`）：广东厂房 2 回及以上 10kV（每回≤6MW、总≤50MW）或全部自发自用且并网电压不高于既有线路，参照大型工商业；缺字段返回 insufficient | 广东第一节；reviews/41、reviews/42（独立复验需修、阻断 0）、reviews/43（N1 窄复核通过，阻断 0） |

时点三条（均记**最后有效日**，失效自次日起）：广东试行期 2028-02-09（O-K-017）、浙江细则 2030-01-16（O-K-018）、国家办法 2030-01-23（O-K-019，按《民法典》第201条起算日次日起算，用户 2026-11-14 裁决）。

已知口径差异（登记不修）：广东那条按同一算法（起算日次日 2026-02-11 起满两年）应为 2028-02-10，但规则层 `gd_policy_expired` 与 26 个评估案例都用 2028-02-09；统一需要同时改规则、案例与行为测试，本轮不动，等规则层下一轮一并处理。

## 8. 边界（明确不做）

- 不判断"哪份文件对"，不输出法律结论；`conflict` 只是登记标签。
- 不从自然语言自动抽 claim；新 claim 由人工编写，靠覆盖门禁兜住漏项。
- 不参与检索排序、不改 `ask` 的回答路径。
- 不改规则 `temporal_scope`；时点只作提醒，失效判定仍在规则层（如 O-R-004 的 `gd_policy_expired`）。
- 时点口径以 `temporal.at` = 最后有效日为准；广东那条与规则层阈值差一天（见 §7），未统一前不要在提醒文案里给出“精确到日”的法律结论。
