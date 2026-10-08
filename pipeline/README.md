# ALE 式数据产线（pipeline）

> ⚠️ **判分契约以 `../判分契约_v1.0.md` 为准**，本文件「判分契约」一节的 `pass_fail` 写法已废止
> （新为 `passed` bool + `errors[]` 数组 + 四档夹具）。代码尚未改，清单见 `ALE调研/TODO3_数据产线Demo搭建.md` §八。
> **`tasks/` 下现有四条数据已降级为产线自测夹具，不作交付数据**（`scoringBasis` 全空、便宜档模型盲解满分）。

把「造一条可自动判分的职业任务数据」从手工作业变成一条能跑通的管线。参照 **ALE（Agents' Last Exam）**：
每条数据 = 真实派活的题面 + 输入文件 + 藏起来的标准答案 + **一段能自动判分、输出 0~1 分的代码**。
本产线是 TODO3「数据产线 Demo」的实现，纯 Python 标准库、无三方依赖、免构建。

## 目录结构

```
ALE任务/
├── tasks/<领域>/<任务名>/       # 数据（每条一个目录）
│   ├── task_card.json           # 元数据 + prompt + 交付要求 + evaluation
│   ├── main.py                  # 任务契约：load/start/golden/neg/evaluate
│   └── assets/                  # input/(给agent) + reference/(标准答案,不可见)
├── pipeline/                    # 产线代码（本目录）
│   ├── config.py                # 全局配置（LLM/并发/阈值，环境变量可覆盖）
│   ├── tasks.py                 # 任务发现与加载
│   ├── harness.py               # 沙箱：布置环境→跑候选解→判分
│   ├── llm.py                   # LLM 抽象：mock / OpenAI 兼容网关（含重试、SSL）
│   ├── cost.py                  # 成本粗估（人民币，以 oneapi /mine 账单为准）
│   ├── generate.py              # N1 选材 / N2a 事实核查+证据链 / N3~5 起草
│   ├── qc.py                    # N6 三道校验 / N7 盲解 / N8 地板审计
│   ├── scaffold.py              # 从草案生成可跑任务骨架
│   ├── orchestrator.py          # 批量编排（并发/重试/落盘）
│   ├── occupations.py           # 中国职业分类码表（选材用）
│   ├── cli.py                   # 命令行入口
│   ├── server.py + web/         # 本地 Web 控制台
│   └── 八节点IO契约.md          # 节点 I/O 与判分契约详解
└── runs/                        # 运行产物（qc/blind/build + cost）
```

## 快速开始

```bash
cd /path/to/ALE任务
python3 -m pipeline.cli list                 # 列出所有任务
python3 -m pipeline.cli run <任务目录> --solver golden   # 跑标准解，应得 1.0
python3 -m pipeline.server                    # 启动 Web 控制台 → http://127.0.0.1:8765
```

## 一条数据的形态与任务契约

每条任务的 `main.py` 实现 5 个子命令：

| 子命令 | 作用 |
|---|---|
| `load` | 打印任务卡 |
| `start --work <dir>` | 铺 input/、建空 output/，**结尾做"答案已隐藏"自检** |
| `golden --out <dir>` | 正例：标准答案原样提交（判分应得 1.0） |
| `neg --out <dir>` | 负例：刻意做坏，触发硬闸门（应得 0.0） |
| `evaluate --output <dir> --reference <dir>` | 判分，向 stdout 打契约 JSON |

四目录铁律：`input/` 只读、`software/` 预装、`output/` agent 唯一可写、`reference/` 仅判分注入。

## 判分契约（gate-and-score）

`evaluate` 输出统一 JSON：

```json
{
  "score": 0.0,
  "pass_fail": true,
  "errors": ["<硬闸门未过原因>"],
  "details": [{"path": "文件#字段", "expected": "...", "observed": "...", "correct": true}]
}
```

- **硬闸门（集合/格式层）**：缺交付文件 / 缺必需列或键 / **行键集合不一致（漏报或多报）** → 整题 **0**。
- **部分分（字段值层）**：过闸后逐字段比对，`score = 正确数 / 总数`，数值带容差。
- 遇"无产出/格式错"返回 0，不抛异常；判分器**独立复算**，不 import 生成器。
- 正负例自证写进 task_card 的 `evaluation.fixtureScores`（正例 1.0、负例 0.0）。

## 八节点（造数据流水线）

```
N1 选材 → N2a 事实核查(证据链) / N2b 口径裁定 → N3~5 起草 → scaffold 落成骨架
→ N4 填 input+标准答案 → N6 三道校验 + N8 地板审计 →(进库)→ N7 盲解(基线难度)
```

逐节点输入/输出格式见 `八节点IO契约.md`。

## CLI 命令

| 命令 | 作用 |
|---|---|
| `list` | 列出所有任务 |
| `run <任务> --solver golden\|neg\|empty` | 跑单任务并打印判分 |
| `qc [<任务>\|--all]` | N6 三道校验 + N8 地板审计 |
| `blind [<任务>\|--all]` | N7 盲解（用 LLM 当考生，估基线通过率） |
| `build --occupation "<职业>" [--faces N]` | N1 选材 → N3~5 起草 → 落成骨架 |
| `scaffold --spec <spec.json>` | 从 spec 生成任务骨架 |
| `occupations [--domain <领域>]` | 中国职业分类码表（选材用） |

## Web 控制台（`python -m pipeline.server`）

任务列表（紧凑单行）+ 每条的 标准解/负例/地板/校验/盲解/编辑数据；顶栏：全量 QC、全量盲解、
运行记录、看板/统计、**选材(N1)**、**造新任务**、**⚙ 设置(LLM)**。可在页面上直接编辑
`assets/` 的输入与标准答案并「保存全部并校验」，全程不离开浏览器。

> 注意：改了 **server.py（后端接口）** 必须重启服务；只改前端（页面/JS）刷新浏览器即可。
> 页面报 `not found` 基本都是服务没重启。

## LLM 配置

默认 `mock`（离线、不花钱、返回占位）。切真实网关：

```bash
# 方式一：环境变量（重启不丢）
export ALE_LLM_PROVIDER=openai
export ALE_LLM_API_BASE=https://<你的 OpenAI 兼容网关>/v1
export ALE_LLM_MODEL=DeepSeek-V4-Flash
export ALE_LLM_API_KEY=sk-xxx

# 可选：把数据目录放到仓库外（框架开源、数据私有时必须这么用）
export ALE_TASKS_DIR=/path/to/private/tasks
export ALE_RUNS_DIR=/path/to/private/runs
```

方式二：Web 页面 ⚙ 设置(LLM) 填写（key 仅存进程内存、不落盘、返回脱敏）。
网关请求带自动重试（默认 3 次）；证书用 certifi，可通过 `verify_ssl` 关闭校验。

## 成本

`cost.py` 按网关价目做**人民币粗估**（DeepSeek 按高峰价、未计缓存）；**实际费用以网关账单为准**。
价目表查不到的模型标"未计价"，不静默按 0。

> **价目表不在代码里。** 它是内部商务信息（含结算倍率），放在 `pipeline/pricing.local.json`，
> **不入版本库**。缺这个文件时产线照常跑，所有模型记为「未计价」。路径可用 `ALE_PRICING_FILE` 覆盖。


## 新增一条任务

1. **脚手架**：`build` / 页面「选材→造新任务」生成骨架 → 填 `assets/input/*` 与
   `assets/reference/expected/*` → **`sync-schema` 从标准答案把字段说明写回题面**（关键，别漏）→ `qc` 进库。
   - `python -m pipeline.cli sync-schema <任务>`，或页面「编辑数据」里的「从标准答案同步字段说明」按钮。
   - 属 **N5（写 prompt）** 的收尾：判分器精确匹配字段名，题面不钉死 schema 会让复杂输出因同义词被判 0。
2. **手写**：仿 `tasks/工程造价/综合单价复核/`，需要"独立复算"式判分时自己写 `evaluate()`（在 prompt 里手写字段 schema）。

### N4 造附件（`attachments.py`）

`assets/input/` 里的附件不必手工一份份做：给 spec 加一段**声明式**「附件规格」，`scaffold_task`
会调 `attachments.materialize` 直接落成真实 `xlsx/pdf/docx/csv/md/txt`，支持任意层目录嵌套、
xlsx 多 sheet、一份规格里用 `常量` 统一单位名/人名/日期（字符串里写 `{工程名称}` 引用）。

```bash
python3 -m pipeline.attachments 附件规格.json assets/input   # 也可单独跑
python3 -m pipeline.attachments_selftest                     # 自测：六格式 × 三层嵌套 + 降级
```

- 规格里**不许有生成逻辑**（无表达式、无代码字符串）——数据与排版分离，照
  `02_造价变更索赔` 的 `case_data.py`(纯数据) / `gen_all.py`(纯排版) 那条线。
- 规格形状与行为契约见 `attachments.materialize` 的 docstring，最小完整示例见
  `attachments_selftest.py` 的 `TOY_SPEC`。
- spec 不带「附件规格」时行为不变：还是写占位符 + 挂 todos 等人填。
- 三方库只经 `kw_*` 底座间接用（kw_xlsx→openpyxl、kw_pdf→reportlab、kw_docx→macOS textutil），
  缺依赖时只让那一份附件进 `errors[]`，不抛异常、不影响同批其余文件。

## 依赖与边界

- 依赖：Python 3.9+，纯标准库；真实网关调用建议装 `certifi`（证书）。
- N4 造附件要出真 xlsx/pdf 时需 `openpyxl`/`reportlab`，且只经 `KW数据构造_通用工具/kw_*`
  调用（目录位置可用 `KW_TOOLS_DIR` 覆盖）；缺了照跑，那几份附件记进 `errors[]`。
- 边界：本产线是**打样 Demo**——只做办公文档形态（Excel/Word/PDF/文本，读文件比数值），
  不碰 GUI/商业软件/大规模数据；成本为粗估。生产级扩量属二期。

## 架构图

```
                    ┌──────────────── 造数据流水线（generate + scaffold）─────────────┐
  职业码表           │  N1 选材 ──▶ N3~5 起草 ──▶ scaffold 落成骨架 ──▶ 人/agent 填    │
 occupations.py ───▶│    ▲                                              assets/       │
                    │  N2a 证据链 / N2b 口径（正解只许用表里的数字）        │            │
                    └───────────────────────────────────────────────────┼───────────┘
                                                                         ▼
   tasks/<领域>/<任务名>/                                        ┌──────────────┐
   ├── task_card.json   元数据+evaluation                        │  质检 (qc.py) │
   ├── main.py          load/start/golden/neg/evaluate  ◀──────▶ │ N6 三道校验   │
   └── assets/                                                   │ N8 地板审计   │
       ├── input/       给 agent（start 铺进沙箱）                └──────┬───────┘
       └── reference/   标准答案（仅判分注入）                      进库? │
                                                                         ▼
   harness.py：布置环境 → 跑候选解(golden/neg/empty/LLM) → evaluate 判分   N7 盲解(基线难度)
        │                                                                 │
        ▼                                                                 ▼
   CLI (cli.py) / Web 控制台 (server.py + web/)         LLM (llm.py: mock|网关) · 成本 (cost.py)
```

## 示例走查（真实输出）

以 `tasks/工程造价/综合单价复核/` 为例，全部为实跑结果：

**① 跑标准解（正例，应 1.0）**
```
$ python3 -m pipeline.cli run tasks/工程造价/综合单价复核 --solver golden
{ "score": 1.0, "pass_fail": "pass",
  "details[0]": {"path": "综合单价汇总.csv#挖土方.综合单价",
                 "expected": 25.63, "observed": 25.63, "correct": true} }
```

**② 跑负例（刻意漏报"模板"分项，硬闸门判 0）**
```
$ python3 -m pipeline.cli run tasks/工程造价/综合单价复核 --solver neg
{ "score": 0.0, "pass_fail": "fail",
  "error": "分项集合不一致（漏报/多报直接判0）：漏报=['模板'] 多报=无" }
```

**③ QC 质检（四项 + 地板审计全过 → 可进库）**
```
$ python3 -m pipeline.cli qc tasks/工程造价/综合单价复核
可进库 True
  标准答案通过         True
  判分可复现           True
  判分器独立(不读input) True
  数据已填(无TODO占位)  True
  N8 地板审计 floor_score 0.0
```

一条数据只有走到 **③ 可进库**，才算一条能用的评测数据。
