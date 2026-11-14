# relations/v1 接口冻结（2026-11-13）

范围：`tools/ontology_tool.py relations` 的 4 个子命令。冻结的是**命令形状、JSON 键、退出码和语义**，不是内部的遍历实现。

版本字段：`impact --json` 和 `ancestors --json` 都返回 `"schema_version": "relations/v1"`。

## 1. 命令与退出码

| 命令 | 输出 | exit 0 | exit 1 |
| --- | --- | --- | --- |
| `relations list` | 纯文本，每条边一行，末行“共 N 条关系边” | 文件存在 | `knowledge/relations.jsonl` 不存在 |
| `relations show <节点ID 或 O-REL-xxx>` | 纯文本，边/note/引用 | 边存在；或节点存在但无边（明确提示） | 节点和边都不存在 |
| `relations impact <节点ID> --json` | JSON（见 §2） | 节点类型可识别且存在 | 节点不存在/类型不可识别 |
| `relations ancestors <entity_id> --json` | JSON（见 §3） | 实体存在（无祖先也返回空数组） | 非 entity，或实体不存在 |

不带 `--json` 时 `impact`/`ancestors` 输出人类可读文本；脚本调用一律用 `--json`。

## 2. impact JSON

键固定为：

```json
{
  "schema_version": "relations/v1",
  "node": "PV-ZJ-2026-015#clause=第四条",
  "kind": "locator",
  "direct": ["O-R-004"],
  "related_rules": ["O-R-002"],
  "parent_clauses": ["PV-NAT-2025-007-ATTACH#clause=第五条"],
  "child_clauses": []
}
```

语义：

- `direct`：真正直接受影响的规则。
  - locator：`source_refs` 直接引用该 locator 的规则。
  - entity：引用该实体定义条款（`entity.source_refs`）的规则。
  - fact：**只沿 `constrains` 出边**指向的规则；不沿 fact 自己的 `source_refs` 扩散（共享引用不等于依赖事实）。
  - rule：规则自身。
  - 所有节点：再加该节点 `constrains` 出边指向的规则。
- `related_rules`：与本节点有 `refines` 关系的**上级/下级条款**所引用的规则，去掉 `direct` 后剩下的候选。**明确不代表受影响**，必须人工判断。
- `parent_clauses`：本节点通过 `refines` 指向的上级条款（本节点细化它）。
- `child_clauses`：通过 `refines` 指向本节点的下级细化条款。

## 3. ancestors JSON

```json
{
  "schema_version": "relations/v1",
  "node": "O-E-005",
  "kind": "entity",
  "ancestors": ["O-E-001"]
}
```

语义：沿 `subclass_of` 向上取全部祖先，按广度优先顺序；`seen` 防重复。只接受 entity。没有分类上级时返回 `[]` 且 exit 0。

## 4. 兼容规则

- **允许**新增 JSON 键（向后兼容）；调用方必须忽略未知键。
- **必须升 v2**：删除/重命名键、改变键的类型、改变 `direct`/`related_rules` 的语义、改变退出码。
- 升 v2 时同步改：`tools/relations_tests.py`、`knowledge/relation_coverage.md`、`reviews/17`，并重跑 `run_all`。
- 当前 `list`/`show` 是人工查看接口，不承诺机器可解析；脚本不要解析它们。

## 5. 已验证的契约

`tools/relations_tests.py` 覆盖：版本字段、4 个分类祖先（CQ5）、4 个 impact 场景（浙江第四/广东第二/国家第五/国家第四）、5 条事实的 direct 命中、6 条删除测试（006/009/008/010/013/004），共 25 项。运行：

```powershell
cd "E:\deepseek harness\项目\新能源Ontology"
python tools\relations_tests.py
```

## 6. 已知边界

- `validate` 现在会拒绝 `subclass_of` 多节点环（负例 `rel-cycle`），并要求每个 fact 至少有一条 `constrains` 出边（负例 `rel-fact-without-constrains`）；但 `impact` 不做跨 `refines` 的传递闭包，只上下各一跳，这是有意的“候选”设计。
- `temporal_scope` 只校验格式和起止先后，不自动与来源发布日期联动。
- `relations.jsonl` 仍不在 `evidence_audit.py` 的引用回检范围内（当前 75 条含规则/事实/实体/约束引用）；关系正确性靠 quote 子串检查 + 独立复验。
