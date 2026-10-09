# 协作者上手指南（CONTRIBUTING）

欢迎。本仓是「职业任务数据产线」**框架**。数据不在仓里（私有）；你贡献的是：用这套框架**独立构造**出合格的职业任务数据，各自做各自的，汇到同一个数据集。

## 一、一次性准备
1. 接受仓库邀请 → `git clone`。
2. **开防漏钩子**（必做）：仓根执行 `git config core.hooksPath .githooks`。它拦住误入提交的 `tasks/ runs/ .env *.local.json`，防私有数据/密钥进公开仓。
3. **配环境**：复制 `.env.example` 为 `.env`，填你自己的网关地址与 key。跑真实模型时**务必设 `ALE_LLM_REASONING_EFFORT=low`**，否则推理型模型会把 token 预算烧光、返空（细节见 `pipeline/llm.py` 注释）。
4. **拉私有数据仓**：数据不在本框架仓，在私有仓 `chenzhuohou1-byte/occupational-task-forge-data`（已把你加进去）。`git clone` 它，把 `.env` 里的 `ALE_TASKS_DIR` 指向这个 clone 的路径。范例任务「造价 / 个税」就在里面。
5. **读两份权威**：判分唯一权威 [`判分契约_v1.0.md`](判分契约_v1.0.md)；操作流程 [`docs/新增一条数据-playbook.md`](docs/新增一条数据-playbook.md)。

## 二、造一条数据（照 playbook 走）
`build` 起草 → 定事实来源（**闭世界法最稳**：规则/费率/税率表写进题面材料，答案由材料唯一推出）→ 造附件 + **脚本复算标准答案并手工抽验（绝不手抄）** → 判分器（表格类直接用生成的 `scripts/score_outputs.py`）→ 四档夹具 → `qc --all` 要 `进库=True` → **N7 难度体检**。

> **一条数据算数的硬线**：`qc` 进库=True **且** N7 **便宜档 fullPassRate=0**、前沿档 ≤1/3。便宜模型都能满分 = 水题，必须加硬（加难杠杆按强弱排序见 playbook §7）。
>
> 1009 起 `qc` 的 `进库` 已经把**难度门槛**一起判了（N6 ∧ N8 ∧ §6.3），不再需要人去看 `measuredDifficulty`。所以：
> - **没测过难度的任务，`进库` 一定是 False**（显示「难度未测」），这是对的、不是 bug；
> - **改了判分器就要同步改 `evaluation.scorerVersion` 并重测难度** —— 旧判分器测的数字配新判分器不算数，qc 会拦；
> - N8 地板现在跑两档：你造的 `output_test_random` **加上**机器从 golden 自动合成的同结构假答案，取较高分，都必须是 0。地板不为 0 ＝ 判分器在给「格式对、内容错」送分，先修判分器；
> - 题面里不许出现标准答案里的数（照抄即得分）；确属必须给出的已知量，写进 `元数据.题面允许数值` 并注明理由。

## 三、提交

### 提 PR 之前先跑自测

```bash
python3 tests/run_all.py          # 5/5 通过才提 PR；不花钱、不触网
```

覆盖造附件、程序化地板、agent 档动作循环、N7 runner 控制流，以及对自带
`tasks/_demo` 的 qc 闸门行为核对。CI（`.github/workflows/ci.yml`）跑的就是这一条，
外加一条信息边界检查（`tasks/` 只许有 `_demo`；`runs/`、`.env`、`*.local.json` 不许入库）。


- 在**分支**上做，开 **PR**，等 1 个评审合并——main 不能直推。
- **框架/代码**改动走**本仓**（occupational-task-forge）PR。
- **你的任务数据提交到私有数据仓** `occupational-task-forge-data`（不是本仓）；`tasks/ runs/` 在本仓已被忽略、钩子也拦，别往本仓塞数据。

## 四、协调（避免撞车）
- 开工前**认领**你要做的职业/领域，别和别人重复。
- `task_id` 用职业前缀 + 编号，保证全局唯一。
- 在自己任务卡 `task_card.json` 加一行 `"负责人": "你的名字"`——进度看板会显示，认领一眼可见。
- **看进度看板**（谁在做什么、每条 qc 进库没、N7 两档数字）：在框架仓目录跑
  ```
  cd <occupational-task-forge 目录>
  python3 -m pipeline.server      # 注意是 python3；打印出 http://127.0.0.1:8765
  ```
  浏览器打开那个地址 → 点「进度看板」。（想换端口：`ALE_PORT=8799 python3 -m pipeline.server`）

## 五、红线
- 数据 / 密钥 / 模型跑分**绝不进公开仓**。
- 判分依据只能来自**题面材料**或**有可核查链接+原文的外部事实**；每个判分点在 `scoringBasis` 标出处，标不出就删。
