# 新增一条数据 · Playbook（协作者照此走）

> 唯一权威是 [`判分契约_v1.0.md`](../判分契约_v1.0.md)；节点 I/O 见 [`pipeline/八节点IO契约.md`](../pipeline/八节点IO契约.md)。
> 本文只讲**怎么把一条数据从零做到入库**，不重述契约规则（重述会漂移）。
> 两个范例任务：**造价**（开世界真实价 + 独立复算判分器）、**个税**（闭世界 + 通用判分器 + 跳档/封顶/年终奖加难）。数据在私有树，不在本仓。

## 0. 环境
- 跑真模型：`.env` 配 `ALE_LLM_PROVIDER=openai` + 网关 + key；**务必带 `ALE_LLM_REASONING_EFFORT=low`**，否则推理型模型会把 token 预算烧光、content 返空（见 llm.py 注释）。
- 数据与密钥**一律不入库**。clone 后先 `git config core.hooksPath .githooks` 开防漏钩子。

## 1. 起草（N1–N5）
```
python -m pipeline.cli build --subdomain "6.1" --faces 3
```
得到 N1 工作面 + 草稿 + 可跑骨架（task_card 已是契约 v1 schema、含独立判分器）。也可手写 spec 调 `scaffold.scaffold_task`。骨架自带 TODO 清单。

## 2. 定事实来源（N2，核心纪律）
判分依据只能来自两处，`scoringBasis` 每条必须标出处，标不出就删这个判分点：
- **闭世界/口径法**：把判分要用的规则、费率、税率表**写进题面材料**（input/）。答案由材料唯一推出、可自验——首选，最省心（个税即此法）。
- **开世界/机检法**：答案挂外部真实事实时，走证据链 `数值|URL|原文|日期` + **机械校验**（真访问 + grep 原文），防幻觉引用。事实机器取不到又没法写进材料的 → 规避这道题。

## 3. 造附件 + 写标准答案（N3/N4）
- 真实附件：声明式 `附件规格` 交 `attachments.materialize` 造 xlsx/pdf/docx；或手放 input/。
- 标准答案**必须用脚本复算**（仿 `recompute_reference.py`）、再**手工抽验 1–2 条**，**绝不手抄**。答案放 `assets/reference/expected/`，agent 全程不可见。

## 4. 判分器（契约 §1）
- 表格类直接用骨架生成的 `scripts/score_outputs.py`（逐字段比对，行键只对齐不计分）。
- 需独立复算的仿**造价**自己写。铁律：**只读 `--output`/`--reference`，不 import 造附件/生成器**。

## 5. 四档夹具（契约 §4）
仿 `make_fixtures.py` 从 expected 派生：`pos`=1.0 / `neg`=0.0（触硬闸门）/ `random`=0.0（同结构假数、地板）/ `partial`=声明一个实测的 0<x<1。定性题加第五档 `fabricated`=0.0。

## 6. 钉字段名 + 质检
```
python -m pipeline.cli sync-schema <任务目录>   # 把确切表头/键写回交付要求，防同义词被判0
python -m pipeline.cli qc --all                 # 要 进库=True（六条件：四档夹具/独立/可复现/无TODO/scoringBasis非空/难度）
```

## 7. 难度体检（契约 §6.3，决定算不算有效数据）
```
ALE_LLM_MODEL=glm-5.3-flash   ALE_LLM_REASONING_EFFORT=low N7_MODE=agent N7_TOOLS=python N7_RUNS=3  N7_TIER=便宜档 N7_TASK=<id> N7_OUT=runs/n7/<id>-glm.json python3 n7_run.py
ALE_LLM_MODEL="Claude Sonnet 5.5" ALE_LLM_REASONING_EFFORT=low N7_MODE=agent N7_TOOLS=python N7_RUNS=10 N7_TIER=前沿档 ALE_LLM_INTER_RUN_SLEEP=100 N7_TASK=<id> N7_OUT=runs/n7/<id>-sonnet.json python3 n7_run.py
```
- **分级（契约 §6.3 第二版）**：便宜档 3 跑 ≥ 2/3 满分 = L1；其余由前沿档（Sonnet 5.5，至少 10 跑）满分率定：
  >30% L2 / 10–30% L3 / ≤10% L4，对标 ALE 三层的严格通过率。想做 L3/L4 就要让前沿档也做不出来。
- **加难杠杆按实测强弱排序**（强的先用）：
  1. **跨文档对账 + 漏项要自己挖出来**，且漏了整题 0（造价 BG-05：申报表编号跳号、取消项无对应申报行）。这是目前唯一把前沿档均分压到 0.5 以下的配方。
  2. **输出字段多、附件多**（造价 22 份附件 / 14 个输出字段）。
  3. 多步累计 + 跨档；由基数自算并封顶（上下限）；单独计税分支（如年终奖÷12 查另一张表）；多项扣除加总并判上限。
  4. 拉长输出。
  对照：只靠第 3 类做出来的个税，两档均分 0.86/0.89，刚过线、区分度弱。
- **前沿档稳定同错 ⇒ 先怀疑答案和题面，不是先认定题难**（2026-10-16 两条实证）。
  前沿模型 N 跑错在**同一组字段、同一个值**，几乎一定是确定性原因：
  ① 标准答案漏算 —— 造价那条复算器只在一个事项上套了合同工程量偏差条款，前沿档 6 跑都按合同字面
  对另外两项也套了，改正答案后 6/6 满分；**复算器必须对每一条规则遍历它适用的全部事项，
  不能只在出题人想到的那一项上用**；② 题面没约定 —— grants 那条 `monitoring_level` 只写 `str`，
  答案却是材料里没出现过的缩写标签，`immediate_total` 的构成只写在 scoringBasis 里，
  前沿档 10 跑全错在这 6 个字段上。判定顺序：看错项能否在题面/材料里找到唯一依据，找不到就是我们的错。
- **别把「判分器扣没约定过的东西」当难度。** pmo 那条原来两档 fullPassRate 都是 0，看着达标；
  等把判分器里「数组按下标比（题面从没规定顺序）」和「行键列计分」两个缺陷修掉，
  两档立刻都能满分 → 退回加硬。**难度要来自题，不要来自判分器的噪声。**
- 测完把结果填进 `task_card.measuredDifficulty`（绑 model/toolset/budget/scorerVersion/nRuns）。
  **改过判分器就要同步改 `evaluation.scorerVersion` 并重测**——qc 会拦住「新判分器配旧难度数字」。
- **定级测量用 agent 档**：`N7_MODE=agent`（默认工具集 `N7_TOOLS=python`＝能跑代码，对齐 ALE
  沙箱；`N7_TOOLS=file` 是旧考法、只作对照）。`N7_MODE=blind` 单发盲解只作便宜预筛、不定级（契约 §6.3）。
  **能跑代码之后，算术、费率、封顶、跳档这类「计算复杂度」基本不再是难度**（上面第 3 类杠杆），
  难度只能来自「找得到、对得上、判得准」：海量材料里挖漏项、跨文档冲突取舍、需要判断的结论。
- **每次测量都会留轨迹**（默认开，`N7_NO_TRACE=1` 关）：`<N7_OUT 同名>_trace/runN/` 下有
  `prompt.txt`（喂进去的完整题面+附件文本）、`response_raw.txt`（模型原始响应）、
  `output/`（它交的文件）、`score.json`（判分器完整输出，含逐字段 expected/observed）；
  agent 档另有 `transcript.json`（每一步的问答）与 `actions.json`（动作序列）。
  **分数看着不对先翻轨迹**，别凭猜改题。测量故障那几次也会留（最该复盘的就是它们）。
- `qc` 的夹具逐字段明细落在 `runs/<run_id>/fixture_details/<task_id>.json` ——
  夹具实测分和声明对不上时，看这个就知道是哪一项判错了。


## 8. 分工建议
- 可外派：起草、闭世界题构造、通用判分器表格题、难度加硬与 N7 迭代。
- 守住核：开世界易变事实的锚定（或交给带机械校验的 agent，属 R&D）。
