# 新增一条数据 · Playbook（协作者照此走）

> 唯一权威是 [`判分契约_v1.0.md`](../判分契约_v1.0.md)；节点 I/O 见 [`pipeline/八节点IO契约.md`](../pipeline/八节点IO契约.md)。
> 本文只讲**怎么把一条数据从零做到入库**，不重述契约规则（重述会漂移）。
> 两个范例任务：**造价**（开世界真实价 + 独立复算判分器）、**个税**（闭世界 + 通用判分器 + 跳档/封顶/年终奖加难）。数据在私有树，不在本仓。

## 0. 环境
- 跑真模型：`.env` 配 `ALE_LLM_PROVIDER=openai` + 网关 + key；**务必带 `ALE_LLM_REASONING_EFFORT=low`**，否则推理型模型会把 token 预算烧光、content 返空（见 llm.py 注释）。
- 数据与密钥**一律不入库**。clone 后先 `git config core.hooksPath .githooks` 开防漏钩子。

## 1. 起草（N1–N5）
```
python -m pipeline.cli build --occupation "某职业" --faces 3
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
ALE_LLM_MODEL=glm-5.3-flash   ALE_LLM_REASONING_EFFORT=low N7_RUNS=3 N7_TIER=便宜档 N7_TASK=<id> N7_OUT=runs/n7/<id>-glm.json python3 n7_run.py
ALE_LLM_MODEL=gpt-5.5         ALE_LLM_REASONING_EFFORT=low N7_RUNS=3 N7_TIER=前沿档 ALE_LLM_INTER_RUN_SLEEP=100 N7_TASK=<id> N7_OUT=runs/n7/<id>-gpt.json python3 n7_run.py
```
- **便宜档必须 fullPassRate=0**；前沿档 ≤ 1/3。便宜模型能满分 = 水题、退回加难。
- **加难杠杆**（实测个税 V1 便宜档就满分、靠这些加到不满分）：多步累计 + 跨档；由基数自算并封顶（上下限）；单独计税分支（如年终奖÷12 查另一张表）；多项扣除加总并判上限；埋"数据缺失需对账"陷阱（仿造价 BG-05，漏了整题 0）；拉长输出字段。
- 测完把结果填进 `task_card.measuredDifficulty`（绑 model/toolset/budget/scorerVersion/nRuns）。

## 8. 分工建议
- 可外派：起草、闭世界题构造、通用判分器表格题、难度加硬与 N7 迭代。
- 守住核：开世界易变事实的锚定（或交给带机械校验的 agent，属 R&D）。
