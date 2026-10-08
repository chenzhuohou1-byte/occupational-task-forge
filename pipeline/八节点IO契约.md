# 数据产线 · 八节点 I/O 契约（TODO3）

> ⚠️ **本文件第三节的判分输出契约已被 `../判分契约_v1.0.md` 取代。**
> 差异：通过标志为 **`passed`（bool）**，不是 `pass_fail`（当前代码还输出 `"pass"`/`"fail"` 字符串）；
> 致命问题为 **`errors[]`（数组）**，不是单数 `error`；夹具四档不是两档；
> `evaluation` schema 见契约 §3。代码尚未改，改动清单见 `ALE调研/TODO3_数据产线Demo搭建.md` §八。
> 本文件的节点 I/O 部分（第四节）仍然有效。

本文件定义产线各节点的输入/输出格式与判分契约，目的是**别人照着能接第二条数据**。
代码在 `pipeline/`，一条数据在 `tasks/<领域>/<任务名>/`。


---

## 一、一条数据的物理形态

```
tasks/<领域>/<任务名>/
├── task_card.json          # 元数据 + prompt + 交付要求 + evaluation(判分声明)
├── main.py                 # 任务契约：load / start / golden / neg / evaluate
└── assets/
    ├── input/              # 派给 agent 的真实工作文件（start 时铺进沙箱 input/）
    └── reference/          # 标准答案，agent 全程不可见，仅判分时注入
```

运行时四目录铁律：`input/` 只读、`software/` 预装、`output/` **agent 唯一可写**、`reference/` 仅判分注入。

## 二、任务 main.py 契约（每条数据都实现这 5 个子命令）

| 子命令 | 作用 | I/O |
|---|---|---|
| `load` | 打印任务卡 | stdout: task_card.json |
| `start --work <dir>` | 铺 input/、建空 output/、**结尾做"答案已隐藏"自检** | 写 `<dir>/input`、`output`、`software` |
| `golden --out <dir>` | 正例：标准答案原样提交 | 写正确 output/，判分应得 **1.0** |
| `neg --out <dir>` | 负例：刻意做坏（触发硬闸门） | 写错误 output/，判分应得 **0.0** |
| `evaluate --output <dir> --reference <dir>` | 判分 | stdout: 判分契约 JSON |

## 三、判分输出契约（全组统一，照 ALE/TODO2）

```
evaluate --output <产出目录> --reference <隐藏答案目录>
  → 往 stdout 打一个 JSON：
    {
      "score": 0.0~1.0,
      "pass_fail": true|false,
      "errors": ["<硬闸门未过的原因>", ...],
      "details": [{"path","expected","observed","correct"}, ...]
    }
```

**gate-and-score 规则**（反作弊主手段）：
- **硬闸门（集合/格式层）**：缺交付文件 / 缺必需列或键 / **行键集合不一致（漏报或多报）** → 整题 **0**，`errors[]` 写明原因，不给部分分。
- **部分分（字段值层）**：过闸后逐字段/逐值比对，`score = 正确数 / 总数`，数值带容差。
- `evaluate` 遇"无产出/格式不对"**返回 0 分，不抛异常**；只有基础设施故障才抛。
- 判分器**独立复算**，不 import 造附件/生成器代码，只共享 reference 里的标准答案。

## 四、八个节点的输入 / 输出

| 节点 | 输入 | 输出 | 代码 |
|---|---|---|---|
| **N1 选材** | 职业名 `occupation` | `{occupation, faces:[工作面,...]}` | `generate.n1_select` |
| **N2a 事实核查** | 断言 `claim` | 证据链 `[{数值,来源URL,原文引用,取数日期,_ok,_problems}]` + 是否全部可核查 | `generate.n2a_factcheck` / `verify_evidence` |
| **N2b 口径裁定** | 工作面 + 存疑口径（人工） | 约定口径清单（写进题面材料） | 人工，接口占位 |
| **N3~N5 起草** | occupation + face + conventions | 草案 `{prompt, 交付要求, 正解要点, 附件计划}` | `generate.n3_5_draft` |
| **落成骨架** | 草案 spec | 可跑任务目录（task_card+main.py+assets 占位）+ todos | `scaffold.draft_to_spec` / `scaffold_task` |
| **N4 造附件** | 附件规格（声明式 JSON）+ 正解 | `assets/input/*` 真实 Excel/Word/PDF/CSV/MD/TXT（支持多层目录）、`assets/reference/expected/*`（标准答案仍人工填） | `attachments.materialize`，`scaffold_task` 见 spec 带「附件规格」即自动调用 |
| **N5 同步字段schema** | reference/expected/ | 反推字段名/表头写回 task_card「交付要求」 | `scaffold.sync_schema` |
| **N6 三道校验** | 一条任务 | `{checks:[标准答案通过, 判分可复现, 判分器独立, 数据已填]}` | `qc.n6_verify` |
| **N7 盲解** | 一条任务 + LLM | `{score, pass_fail, 写出文件数}`（基线通过率） | `qc.n7_blind_solve` |
| **N8 地板审计** | 一条任务 | `{floor_score, 通过}`（空解得分应≤0） | `qc.n8_floor_audit` |

> **N5 关键纪律**：判分器按字段名精确匹配。脚手架题填完标准答案后**必须跑 `sync_schema`**，
> 把交付文件的确切字段名/表头（含 JSON 数组元素键）写回题面「交付要求」，否则复杂输出会因
> 模型用同义词键（如"延期里程碑总数" vs "延期里程碑数"）被硬闸门判 0——这是题面缺 schema、非难度问题。

### 节点间数据流

```
N1 选材 ─faces─▶ N3~N5 起草 ─draft─▶ scaffold 落成骨架 ─todos─▶ N4 填 input+expected
                     ▲                                                    │
           N2a 证据链 / N2b 口径（正解只许用表里的数字）              N5 sync_schema 钉死字段
                                                                          ▼
                                                  N6 三道校验 ─进库?─▶ N7 盲解 + N8 地板审计
```

- **一条命令出分**：`python -m pipeline.cli run <任务> --solver golden` → 打印 score。
- **正负例自证**：`--solver golden` 得 1.0、`--solver neg` 得 0.0（写进 `evaluation.fixtureScores`）。
- **批量质检**：`python -m pipeline.cli qc --all` → 每条跑 N6+N8，输出可否进库。
- **Web 控制台**：`python -m pipeline.server`。

## 五、task_card 的 evaluation 字段（结构化，必填）

```json
"evaluation": {
  "type": "code",
  "scheme": "gate-and-score",
  "tolerance": 0.01,
  "passThreshold": 1.0,
  "hardGates": ["缺交付文件→0", "行键集合不一致(漏报/多报)→0", "缺必需键/列→0"],
  "fixtureScores": {"output_test_pos": "1.0", "output_test_neg": "0.0"},
  "outputContract": "score / pass_fail(bool) / errors[] / details[{path,expected,observed,correct}]"
}
```

## 六、四条产线纪律（写数据必守）

1. 判分器不 import 生成器，独立复算，只共享 reference 标准答案。
2. 标准答案 agent 不可见，`start()` 结尾做"答案已隐藏"自检。
3. `evaluate` 遇无产出/格式错返回 0，不抛异常。
4. 正负例自证是交付的一部分：正例 1.0、负例 0.0，预期分写进 `fixtureScores`。
