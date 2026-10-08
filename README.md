# occupational-task-forge

把「造一条可自动判分的职业任务数据」从手工作业变成一条能跑通的产线。

面向的数据形态：给 AI agent 出的**真实职业工作考卷** —— 一段派活的话、一批真实工作文件、
一个明确要交的产物、一份藏起来的标准答案，外加**一段能自动判分、输出 0~1 分的代码**。
参照 ALE（Agents' Last Exam, arXiv:2606.05405v2）的数据形态，但**判分契约由本项目自己定**
（见下）。纯 Python 标准库，无三方依赖，免构建。

## 为什么不直接沿用 ALE 的契约

对 ALE 仓库 120 个带 `argparse` 的判分脚本做过普查：同一个「产出目录」概念有 9 种以上
flag 命名（`--output-dir` 36 次 / `--output` 14 / `--agent` 14 / `--submission-dir` 7 …），
通过标志 `passed` 103 次、`pass_fail` 12 次。论文承诺的「统一契约」只指一件事：
VM 侧脚本通过 stdout JSON 通信、绝不写 `output/`。

所以本项目不是「沿用 ALE」，而是**把 ALE 想做但没做到的事做到**：
一份强制的判分契约 + 强制结构化的任务卡 schema + 可机器校验的出题人自证义务。

契约全文 → [`判分契约_v1.0.md`](判分契约_v1.0.md)。**它是唯一权威，改契约只改那一个文件。**

## 快速开始

```bash
python3 -m pipeline.cli list                          # 列出所有任务
python3 -m pipeline.cli run <任务目录> --solver golden  # 跑标准解，应得 1.0
python3 -m pipeline.cli qc --all                      # 进库校验（N6 + N8）
python3 -m pipeline.server                            # Web 控制台 → 127.0.0.1:8765
```

## 一条数据的形态

```
tasks/<领域>/<任务名>/
├── task_card.json       元数据 + prompt + 交付要求 + evaluation + scoringBasis
├── main.py              load / start / golden / neg / evaluate
├── scripts/
│   ├── score_outputs.py       判分器（可独立运行，离线可重跑）
│   ├── recompute_reference.py 从附件独立复算标准答案（出题人侧）
│   ├── make_fixtures.py       生成四档夹具
│   └── run_selftest.sh        四档夹具实测 == 任务卡声明
└── assets/
    ├── input/           派给 agent 的真实工作文件
    └── reference/       标准答案 + 四档夹具（agent 全程不可见）
```

运行时四目录铁律：`input/` 只读、`software/` 预装、`output/` **agent 唯一可写**、
`reference/` 仅判分注入。

## 八个节点

```
N1 选材 → N2a 事实核查(证据链+机械校验) / N2b 口径裁定 → N3~5 起草
→ scaffold 落骨架 → N4 造附件(声明式规格 → 真实 xlsx/pdf/docx)
→ N5 sync_schema 钉死字段名 → N6 进库校验 + N8 地板审计 → N7 盲解(实测难度)
```

逐节点 I/O 见 [`pipeline/八节点IO契约.md`](pipeline/八节点IO契约.md)。

**唯一不能自动化的一步是「关键数值对不对」。** 这类数据的正解由行业惯例、合同条款、
真实价格决定，答案在模型之外 —— 模型能生成一个完全自洽的错误数字而无法自纠。
所以 N2a/N2b 把「查得到的」和「查不到的」分开，前者自动化并强制留证据链，后者交给人。

## 三条最值得抄的设计

**1. 闸门与部分分的分界**

```
「该有哪些条目 / 格式对不对 / 集合一致吗」  →  硬闸门，整题 0，不给部分分
「每个条目的值算得对不对」                  →  连续分，逐字段给部分分
```

推论：刻意埋的漏报项，agent 没识别出来 **整题 0**，不是扣 1/N —— 因为「发现漏报」就是考点。

**2. 地板不是空解，是同结构随机假答案**

空解得 0 只是最低门槛。结构化表格类任务最常见的白拿分口子是「文件名/表头/行键/字段全对、
数值全错」，空解审计挡不住它。所以夹具是四档（定性型五档）而不是两档：

| 夹具 | 期望分 | 证明什么 |
|---|---|---|
| `output_test_pos` | 1.0 | 不误杀正确答案 |
| `output_test_neg` | 0.0 | 闸门真会拦 |
| `output_test_random` | 0.0 | **地板** |
| `output_test_partial` | 声明一个 0<x<1 | 部分分真在给 |
| `output_test_fabricated` | 0.0 | 反幻觉的证据回溯在工作（定性型） |

实测分由自测脚本回填任务卡的 `fixtureScores[*].observed`，`observed != expected` 即判分器不合格。

**3. 判分依据必须能溯源**

每个判分点在任务卡 `scoringBasis[]` 里标出来源：`material`（哪个附件哪一段）或
`external`（url + 逐字原文 + 取数日期，且过机械校验）。**标不出来源的判分点必须删。**
`scoringBasis` 为空 = 不可进库。

联网核查会长出一种新失败模式 —— **幻觉引用**（域名、标准号都像真的，原文没这句），
比编错数字更危险，必须机械挡，不能靠人抽查。

## 难度：只能事后测

ALE 的 165 张任务卡里**没有任何 `difficulty` / `tier` 字段** —— 它的三层是按实测通过率
事后切的。所以本项目禁止手写 `L1-基础` 这类标签，难度拆成两个字段：

- `priorComplexity` —— 先验复杂度，全是客观计数（步数/文档数/跨文档关联数/埋点数/输出字段数）
- `measuredDifficulty[]` —— 实测难度，每条必须同时钉住 **model / toolset / budget /
  scorerVersion / nRuns**。缺任一项的测量结果不算数。

入库门槛：**便宜档模型 3 次 `fullPassRate` 必须 = 0**；前沿档 ≤ 1/3。
廉价模型能满分的任务没有区分度，直接退回。

## 数据不在这个仓库里

`tasks/` 与 `runs/` 被 `.gitignore` 挡掉：真实题面、附件、标准答案是工作产出，
实测通过率是内部评测数据。框架开源、数据私有，用环境变量把数据目录指到仓库外：

```bash
export ALE_TASKS_DIR=/path/to/private/tasks
export ALE_RUNS_DIR=/path/to/private/runs
```

网关价目表（含结算倍率）同理放在不入库的 `pipeline/pricing.local.json`，
缺这个文件时产线照常跑、所有模型记为「未计价」，不静默按 0。

> ⚠️ **已知缺口**：因为 `tasks/` 为空，clone 下来跑 `qc --all` 会是零条任务。
> 需要补一条 `tasks/_demo/` 玩具任务作公开样例（`.gitignore` 已为它放行）。

## 许可与引用

- 参照的 ALE 仓库：`agents-last-exam` @ `d10fb61`，代码 Apache-2.0，数据 CC BY 4.0
- 本项目的判分器模板与样例为自行编写，仅在范式上参考 ALE，未复制其代码
- 引用 ALE 论文数字务必标版本：v1 是 59/55/35、公开 150 条；v2 是 67/55/38、公开 152 条
