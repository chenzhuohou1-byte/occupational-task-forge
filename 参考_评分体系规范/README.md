# 我们的评分体系规范 · 分享包

本包包含**交付物 2（评分体系规范）的简报与完整文档**，以及**可直接运行的评分系统源代码**。

---

## 30 秒看懂

我们对照 UC Berkeley RDI 的 **ALE（Agents' Last Exam）** benchmark 判分体系，定下了自己的一套评分规范：

1. **一套统一的判分器接口契约** —— `--output / --reference` + stdout JSON
2. **一个强制结构化的 `evaluation` 字段 schema** —— 不继承 ALE 的散文债
3. **三份按答复形态分类的判分器模板** —— 数值型 / 集合型 / 定性型
4. **一套「容差与权重怎么定」的方法** —— 补上 ALE 论文留下的空白

**核心骨架是一句话：先过硬闸门，不过直接 0 分；过了才算连续分。**

---

## 先看哪份

| 你的目的 | 打开 |
|---|---|
| **快速了解**（推荐先看这个） | `交付物2_简报.md` —— 含完整评分流程框架图 + 关键代码 |
| **照着写代码** | `交付物2_评分体系规范.md` —— 完整契约与 schema |
| **直接改来用** | `templates/` 三份模板，每份顶部有 `CONFIG` 段 |
| **验证判分器对不对** | `判分器样例/` 跑一遍自测 |

---

## 跑一遍自测（30 秒）

```bash
cd 判分器样例
bash run_selftest.sh
```

预期输出：

```
[正例 output_test_pos]      score = 1.0   passed = True    闸门 6 道全过
[负例 output_test_neg]      score = 0.0   passed = False   闸门拦下
[诊断例 output_test_wrong]  score = 0.0   evidence_grounding = 0.0   ← 编造证据被抓住
[诊断例 output_test_partial] score = 0.6  evidence_grounding = 1.0   ← 部分分真的在给
```

只依赖 Python 3 标准库，无需安装任何第三方包。

**为什么有 4 个夹具而不是 2 个**：正例 1.0 / 负例 0.0 只能证明判分器"能区分"，证明不了
「反幻觉」和「部分分」两个机制在工作。后两个诊断例专门用来证明这两件事。

---

## 目录结构

```
.
├── README.md                     本文件
├── 交付物2_简报.md                简报：评分流程框架 + 关键代码
├── 交付物2_评分体系规范.md          完整规范
├── templates/                    三份判分器模板（改 CONFIG 段即可用）
│   ├── score_numeric.py          数值型：逐字段比对 + 部分分，关键项全对才 passed
│   ├── score_set.py              集合型：集合一致是硬闸门，条目值才给部分分
│   └── score_qualitative.py      定性型：结构化结论 + 证据逐字回溯 + 加权 rubric
└── 判分器样例/                    可直接运行的定性型判分器实例
    ├── score_outputs.py          判分器本体
    ├── run_selftest.sh           自测脚本
    └── fixtures/
        ├── reference/            隐藏答案 + 输入材料
        │   ├── findings.json     标准答案：3 个争议焦点
        │   └── inputs/           三份输入材料（施工合同 / 结算申报书 / 监理月报）
        ├── output_test_pos/      正例：应得 1.0
        ├── output_test_neg/      负例：应得 0.0
        ├── output_test_wrong/    诊断例：编造证据 -> 0.0
        └── output_test_partial/  诊断例：档位判错 -> 部分分
```

---

## 三个最值得看的设计

### 1. 闸门与部分分的位置

```
「该有哪些条目 / 格式对不对 / 集合一致吗」  →  硬闸门，不给部分分，不过就是 0
「每个条目的值算得对不对」                  →  连续分，给部分分
```

这条决定了「刻意埋的漏报项」的口径：**agent 没识别出漏报项 → 整题 0 分**，而不是扣一个比例分。
因为"发现漏报"正是考点。

### 2. 证据逐字回溯（反幻觉，核心只有几行）

```python
bad = [e for e in ev if not any(str(e) in t for t in input_texts)]
ok = bool(ev) and not bad
```

每条证据必须是输入材料里的**逐字原文**，不是文件名引用。有一条找不到，这条结论整个不算数。
**一个子串检查，就把"模型有没有瞎编证据"变成了机械可判。**

### 3. 为什么由我们定，而不是沿用 ALE

实测（对 120 个带 `argparse` 的 ALE 判分脚本普查）：

- 表示"产出目录"的 flag 有 **7 种以上**命名（`--output-dir` / `--output` / `--agent` / `--submission-dir` / `--out` / `--candidate-dir` / `--agent-dir`）
- 表示"过没过"的键，主流是 **`passed`（103 次）**，`pass_fail` 只有 **12 次**

**ALE 的接口根本不统一。** 论文承诺的"统一契约"只指一件事：VM 侧脚本通过 stdout JSON 通信、绝不写 `output/`。

所以这份规范不是"沿用 ALE"，而是"把 ALE 想做但没做到的事做到"。

---

## 引用说明

- ALE 代码仓库：`agents-last-exam` @ `d10fb61`，Apache-2.0，可引用
- 论文：`2606.05405v1`，判分体系见 Section 3.3 与 Appendix C.3
- 本包中的模板与样例为**我们自行编写**，仅在范式上参考 ALE，未复制其代码
