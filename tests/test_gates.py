#!/usr/bin/env python3
"""难度分级 / 用途闸门 / 大模型判分多数票 的单元自测（纯函数，不触网、不花钱）。

为什么要有：2026-10-14 口径变更把「难度门槛」改成「难度标签」、把 §7 的 scoringBasis
闸门按 `用途` 拆开、给大模型判分项的「判分两次必须一致」开了多数票例外。这三处都是
**判定逻辑**，错了的后果是静默的 —— 水题被当成 L4、训练集闸门把测试集也放宽了、
不稳定的判分器被当成稳定。qc --all 只覆盖 tasks/ 里实际存在的那几种组合，
分级函数的边界（2/3、1/3、擦线、单发不算、判分器改版）一条都碰不到，所以单独写。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import qc  # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAILURES.append(label)


class FakeTask:
    """difficulty_tier / task_purpose 只读 task.card 与 task.task_id，不碰磁盘。"""

    def __init__(self, card, task_id="fake-001"):
        self.card = card
        self.task_id = task_id
        self.dir = os.path.join(ROOT, "tasks", "_demo")


def md(model, fpr, n=3, toolset="agent-file-tools", sv="1.0", at="2026-10-14"):
    return {"model": model, "toolset": toolset, "budget": {"maxSteps": 20},
            "scorerVersion": sv, "nRuns": n, "fullPassRate": fpr,
            "meanScore": 0.5, "measuredAt": at}


def card(entries, sv="1.0", **extra):
    return {"evaluation": {"scorerVersion": sv}, "measuredDifficulty": entries, **extra}


def tier_of(entries, **kw):
    ok, d = qc.difficulty_tier(FakeTask(card(entries, **kw)))
    return ok, d.get("tier"), d.get("级别来源"), d


def test_tiers():
    print("难度分级（契约 §6.3 新口径）")
    CHEAP, FRONT = "glm-5.3-flash", "Claude Sonnet 5.5"

    ok, t, src, _ = tier_of([md(CHEAP, 1.0)])
    check((ok, t, src) == (True, "L1", "实测"), "便宜档 fpr=1.0 → L1 实测（前沿档不必跑）")

    ok, t, src, _ = tier_of([md(CHEAP, 1.0 / 3)])
    check((ok, t, src) == (True, "L2", "实测"), "便宜档 fpr=1/3 → L2 实测（便宜档单独定级）")

    ok, t, src, d = tier_of([md(CHEAP, 2.0 / 3)])
    check((ok, t, src) == (True, "L1", "推定") and d.get("待补"),
          "便宜档 fpr 正落在 L1|L2 边界 2/3 且 nRuns=3 → L1 但降为推定级、带待补")

    ok, t, src, _ = tier_of([md(CHEAP, 2.0 / 3, n=6)])
    check((ok, t, src) == (True, "L1", "实测"), "同上但补到 6 跑 → L1 实测")

    ok, t, src, _ = tier_of([md(CHEAP, 0.0), md(FRONT, 1.0)])
    check((ok, t, src) == (True, "L3", "实测"), "便宜档 0 + 前沿档 1.0 > 1/3 → L3 实测")

    ok, t, src, _ = tier_of([md(CHEAP, 0.0), md(FRONT, 0.0)])
    check((ok, t, src) == (True, "L4", "实测"), "便宜档 0 + 前沿档 0 ≤ 1/3 → L4 实测")

    ok, t, src, d = tier_of([md(CHEAP, 0.0), md(FRONT, 1.0 / 3)])
    check((ok, t, src) == (True, "L4", "推定") and d.get("待补"),
          "前沿档正落在 L3|L4 边界 1/3 且 nRuns=3 → L4 但降推定级")

    ok, t, src, d = tier_of([md(CHEAP, 0.0)])
    check((ok, t, src) == (True, "L3+", "推定"),
          "便宜档 0、缺前沿档 → 推定级 L3+（不是未测，§6.5 省钱路线）")


def test_tier_rejects():
    print("难度分级：不算数的测量条目（三条规矩一个字没放松）")
    CHEAP, FRONT = "glm-5.3-flash", "Claude Sonnet 5.5"

    ok, t, _, d = tier_of([])
    check((ok, t) == (False, None) and "未测" in d["说明"], "measuredDifficulty 为空 → 未测，定不出级")

    ok, t, _, d = tier_of([md(CHEAP, 0.0, toolset="single-shot-blind")])
    check((ok, t) == (False, None) and "单发" in str(d), "只有单发盲解 → 定不出级（§6.3 只认 agent 档）")

    ok, t, _, d = tier_of([md(CHEAP, 0.0, sv="1.0")], sv="1.1")
    check((ok, t) == (False, None) and "改版" in str(d), "判分器已改版（1.0 vs 1.1）→ 旧数字作废")

    bad = md(CHEAP, 0.0)
    bad.pop("budget")
    ok, t, _, _ = tier_of([bad])
    check((ok, t) == (False, None), "条目缺 budget → 不算数（换预算就不可比，§6.2）")

    ok, t, _, _ = tier_of([md(CHEAP, 0.0), md(FRONT, 0.0, sv="0.9")])
    check((ok, t) == (True, "L3+"), "前沿档是旧判分器版本 → 只当缺前沿档，退推定级 L3+")

    ok, t, _, d = tier_of([md(CHEAP, 0.0, at="2026-10-01"), md(CHEAP, 1.0, at="2026-10-14")])
    check((ok, t) == (True, "L1"), "同档多次测量取 measuredAt 最近的一条")


def test_purpose():
    print("§7 用途：训练集放宽 / 测试集严格 / 漏填按严的来")
    check(qc.task_purpose(FakeTask({})) == ("测试集", False),
          "未声明 用途 → 按测试集严判，且标出「未声明」")
    check(qc.task_purpose(FakeTask({"用途": "训练集"})) == ("训练集", True), "声明训练集 → 训练集")
    check(qc.task_purpose(FakeTask({"用途": "瞎写"})) == ("测试集", False),
          "用途 取值非法 → 不静默接受，按测试集严判")


def test_majority_repro():
    """大模型判分项的「多数票稳定」（§9.4 例外）。stub 掉 harness，不真跑判分器。"""
    print("§9.4 大模型判分：判分两次一致 → 多数票稳定")

    class R:
        def __init__(self, s):
            self.score, self.fault = s, ""

    def stub(seq):
        box = {"i": -1}

        def _ev(task, wd):
            box["i"] += 1
            return R(seq[box["i"] % len(seq)])
        return _ev

    orig = (qc.harness.setup_env, qc.harness.golden_solver, qc.harness.evaluate)
    qc.harness.setup_env = lambda *a, **k: None
    qc.harness.golden_solver = lambda *a, **k: None
    try:
        qc.harness.evaluate = stub([0.8, 0.9])
        ok, d = qc._check_reproducible(FakeTask({}))
        check(not ok and len(d["score"]) == 2,
              "纯代码判分两判不同分 → 不通过（这道闸门对代码判分原样保留）")

        qc.harness.evaluate = stub([0.8, 0.8])
        ok, _ = qc._check_reproducible(FakeTask({}))
        check(ok, "纯代码判分两判同分 → 通过")

        llm = {"evaluation": {"llmJudged": True}}
        qc.harness.evaluate = stub([0.8, 0.9, 0.8])
        ok, d = qc._check_reproducible(FakeTask(llm))
        check(ok and d["票数"] == 2 and len(d["score"]) == 3,
              "大模型判分 3 判 2 胜 → 通过（旧闸门会在这里把它判成不合格）")

        qc.harness.evaluate = stub([0.1, 0.5, 0.9])
        ok, _ = qc._check_reproducible(FakeTask(llm))
        check(not ok, "大模型判分 3 判全不同 → 仍不通过（放宽的是判据、不是纪律）")
    finally:
        qc.harness.setup_env, qc.harness.golden_solver, qc.harness.evaluate = orig


def main():
    for fn in (test_tiers, test_tier_rejects, test_purpose, test_majority_repro):
        fn()
    if FAILURES:
        print("\n✗ %d 项不符预期：\n  - %s" % (len(FAILURES), "\n  - ".join(FAILURES)))
        return 1
    print("\n✓ 难度分级/用途/多数票 全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
