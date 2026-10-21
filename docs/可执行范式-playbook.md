# 可执行范式 playbook（手搓 spec + 产线判分节点）

> 面向**可执行产物范式**（`executable_artifact`，判分契约 §9.5）的建题步骤。数值/表格范式走
> `docs/新增一条数据-playbook.md` 与 `build-case`，不是这条。边界与分工见
> `docs/协作者指南-边界与分工.md`。样例：`G1`（Flowable 引擎）、`E1`/`H3`（容器）。

## 这条线什么时候走

交付物**要在真实工具链上跑起来才算完成**（BPMN 要能部署、脚本要能对隐藏输入跑出正确结果）
→ 走这条。只需交一组数 → 走数值 `build-case`。

一句话分工：**产线给骨架 + runner/适配器判分编排 + 容器/引擎沙箱 + qc 闸 + N7 测难；
你手工出 spec、真实数据+provenance、cases/golden/negatives、host_score 打分口径。**
②（填 reference）没有命令、是真正花人力的地方，也是难度的来源。

## 六步总览

| 步 | 做什么 | 命令 / 手工 |
|---|---|---|
| ① 写 spec | 手写任务 spec JSON，范式标死 | 手工 |
| ② scaffold | 产线出骨架 | `scaffold --spec` |
| ③ 填 reference | cases/golden/negatives/host_score/provenance | **手工（核心）** |
| ④ 判分自检 | golden 必须 1.0 | `main.py golden/evaluate` |
| ⑤ 质检 | N6 + N8 + 难度分级 | `qc` |
| ⑥ 测难度 | 便宜档→前沿档 | `n7_run.py` |

---

## ① 写 spec（手工）

关键是 `"范式": "executable_artifact"` 和 `engine`。最小骨架：

```json
{
  "范式": "executable_artifact",
  "领域": "医疗保健医师技师",
  "任务名": "...",
  "task_id": "...",
  "prompt": "见 assets/input/TASK_PROMPT.md。交付 audit.py，python audit.py <session_dir> 输出契约 JSON。",
  "交付要求": {"audit.py": "纯标准库/numpy/scipy，无网络，60s；字段见 output_contract.json"},
  "engine": {"adapter": "container"},
  "selfReportCrossCheck": false,
  "用途": "测试集",
  "材料来源": {"class": "real_derived", "生成方式": "handmade",
             "dataset": "...", "license": "...", "抓取日期": "YYYY-MM-DD", "doi": "...",
             "synthetic_overlays": true},
  "task_meta": {
    "deliverable": "audit.py",
    "cases_dir": "assets/reference/cases",
    "host_scorer": "scripts/host_score.py",
    "cases_manifest": "cases.json",
    "case_timeout": 60
  }
}
```

- `engine.adapter`：`container`（交付脚本，docker 跑）或 `flowable`（交付 BPMN，引擎跑）。
- `host_scorer`：开**答案不可见**判分（候选容器只挂当前 case，expected 留宿主侧重算）。
- `selfReportCrossCheck: true` 时必须有一条 `faked_results` 负例（自报全绿、引擎实算不认）。

## ② scaffold 出骨架

```bash
python3 -m pipeline.cli scaffold --spec 你的spec.json
```

`scaffold_task` 见 `范式=executable_artifact` 自动转 `scaffold_executable_task`，产出薄壳 `main.py`、
`scripts/score_outputs.py`（读 `evaluation.engine`+`task_meta` 调 `pipeline.executable.runner`，**不用改**）、
空的 `assets/input/`、`assets/reference/{golden/output,negatives}/`、task_card 骨架。

## ③ 填 reference（手工核心，无命令）

对着 `H3`（数据仓 `医疗保健医师技师/H3PhysioNetECG节律与信号质量审计/`）照填：

- `assets/input/`：`TASK_PROMPT.md`、`output_contract.json`、公开示例、`provenance.json`
  （逐文件 url+bytes+sha256）、`SOURCE_LICENSE`。B 类真实源在这步抓取/冻结/记校验值。
- `assets/reference/cases/<id>/`：每个隐藏 case 一个目录，`session/`（输入）+ `expected.json`（标准答案）。
  一个 case 瞄一个考点。
- `assets/reference/cases.json`：case id 清单（runner 按它逐例迭代；id 必须与目录名一致）。
- `assets/reference/golden/output/<deliverable>`：参考交付（如正确的 audit.py），要判 1.0。
- `scripts/host_score.py`：可信宿主判分，暴露 `score_case(got, expected, session) -> {"score","passed","info"}`。
  **判分分层写在这里**（哪些字段计分、部分分、硬闸）。
- `assets/reference/negatives/<name>/output/` + `manifest.json`：每条负例一份坏交付 + 一条记录
  （`target_check` / `faked_results` / `floor`）。无自报的题至少给一条 `floor:true` 地板负例。
- 可选 `scripts/checks_task.py` 的 `extra_checks(output_dir, task_meta)`：范式专属静态检查。

**强烈建议交叉复算**：用第二份独立实现（oracle）复算出 `expected.json`，两实现一致才落盘，
防 golden 本身算错（H3 的 `_build/independent.py` 即此用途）。

> 负例不必"只错一个点"——程序型交付的负例天然会错多个 case，这是正常的（别为此纠结）。
> 判分严格性靠「多 case 各探一点 + golden 全过 + 每条负例被判负」。

## ④ 判分自检（要 docker）

```bash
python3 main.py golden --out /tmp/g
python3 main.py evaluate --output /tmp/g --reference assets/reference   # golden 必须 score=1.0
```

## ⑤ 质检（要 docker）

```bash
ALE_TASKS_DIR=<tasks根> python3 -m pipeline.cli qc <任务目录>
```

N6（golden==1.0 / 判分可复现 / 每条负例被判负 / 不泄答案 / 材料来源齐）+ N8（空交付与地板被否决）
自动走可执行分支，你不写判分逻辑。**进库 = N6 ∧ N8 ∧ 难度定得出级。**

## ⑥ 测难度（要花钱，便宜档先行）

```bash
# 便宜档
ALE_LLM_PROVIDER=openai ALE_LLM_MODEL=glm-5.3-flash \
N7_TASK=<task_id或任务目录> N7_TIER=便宜档 N7_MODE=agent N7_RUNS=10 \
N7_OUT=runs/n7/cheap.json ALE_TASKS_DIR=<tasks根> python3 n7_run.py

# 前沿档（便宜档不满分才上；≥10 跑才能切 L2/L3/L4）
ALE_LLM_PROVIDER=openai ALE_LLM_MODEL="Claude Sonnet 5.5" \
N7_TASK=<...> N7_TIER=前沿档 N7_MODE=agent N7_RUNS=10 \
N7_OUT=runs/n7/frontier.json ALE_TASKS_DIR=<...> python3 n7_run.py

python3 n7_run.py --write-card --from runs/n7/frontier.json   # 回填进 task_card（默认只测不写卡）
```

- 便宜档满分率 ≥2/3 → L1；便宜档 0 分时前沿档 >30%/10–30%/≤10% → L2/L3/L4。
- 单轮只能给推定级（`pending_review`）；要定死级别前沿档需 ≥10 有效跑。
- `ALE_LLM_MODEL` 必须显式给（别用默认 gpt-5.5，网关常 503，改 `"Claude Sonnet 5.5"`）。

## 入库

`qc` 显示 **N6∧N8∧难度定得出级** 才可进库。B 类真实源单实例跑不了 200 世界 crosscheck，
`releaseStatus` 走 `pending_review`。数据进**数据仓**（私有），不进公开框架仓。

## 安全（默认就位，知道即可）

可执行范式候选跑在容器里（非 root、`--network none`、`--read-only`、`--cap-drop ALL`、
标准答案不进候选挂载点）；给不可信 agent 的引擎自测起一次性 Flowable+PG 沙箱、用完销毁。
没 docker / 没沙箱相关步骤会**拒跑**而非裸奔，这是纪律不是 bug。

### 生题侧沙箱解决机密性，不解决完整性（重要边界）

数值 casegen 那条线把模型写的代码（`参数生成器/solve/solve2/物化`）关进 `sandbox-exec`
子进程：读不到 `.env`、连不上网、只能写 scratch——这是**机密性**。

但**判定本身也在同一个不可信进程里算**：`冒烟 / 难度门 / crosscheck` 都由 worker 执行并回报
`stage`。模型代码完全可以 monkeypatch 掉 `crosscheck.crosscheck` 让 worker 直接回
`stage="ok"`。换句话说，**「双实现一致」这件事目前实际上由不可信进程自报**。

宿主侧确实有兜底，而且这些都在可信进程里跑、伪造不了：

- **N6**（golden 实测 ==1.0 / 每条负例被判负 / 题面不泄答案 / 材料来源溯源齐）
- **N8**（空交付与地板被正确否决）
- **落盘路径校验**：worker 回传的文件名一律过 `_safe_under()`——拒绝绝对路径与 `..`，
  realpath 后必须仍在 `assets/input` / `assets/reference/expected` 之下。
  否则模型让 `render_inputs()` 返回 `"../../../x"` 就能借宿主的完整权限写到任意位置，
  等于绕过沙箱的写限制。
- **worker 超时按返修处理**：超时统一转成 `RuntimeError`，不会让死循环打崩整条生成循环。

所以现阶段的准确说法是：**机密性有保证，案例逻辑自洽这一层不防伪造**。要补完整性，
得把判定搬回宿主侧重算（宿主只收 worker 吐的 world/answer 原始数据、自己做比对），属后续改造。