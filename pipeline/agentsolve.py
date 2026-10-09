"""N7 的 **agent 档**：多步、带文件工具的盲解；用来和单发盲解做对照。

为什么要有这一档：单发盲解＝「一口气把所有交付文件的全文吐在一个 JSON 里」，
它跟最终 benchmark 真正要跑的 agent（能列目录、能按需读文件、能分步写、能回头
自查）不是一回事。入库门槛（契约 §6.3）到底该卡在哪一档，必须拿同一条任务两种
测法对照过才知道——否则按单发口径收下的题，换真 agent 一跑可能轻松做掉。

协议刻意用**纯文本 JSON 动作**，不依赖各家的 function-calling，网关上什么模型都能当考生：
  {"action":"list_files"}                       列出 input/ 下全部文件
  {"action":"read_file","path":"..."}           读一份附件（文本化后给，带长度上限）
  {"action":"write_file","path":"...","content":"..."}  往 output/ 写一个交付文件
  {"action":"finish"}                           交卷
"""
import json
import os
import tempfile

from . import harness, qc

PROTOCOL = """你在一个沙箱里完成任务。只能用下面四个动作，**每轮只回一个 JSON 对象、不要任何其它文字**：
{"action":"list_files"}
{"action":"read_file","path":"相对 input/ 的路径"}
{"action":"write_file","path":"相对 output/ 的路径","content":"该文件的完整文本内容"}
{"action":"finish"}
规则：input/ 只读、output/ 是你唯一能写的目录；交付文件必须严格按「交付要求」的文件名与格式；
写完全部交付文件后再 finish。最多 %d 步，步数用完即交卷。"""


def _listing(inputs):
    return "\n".join("  %s  (%d 字符)" % (k, len(v)) for k, v in inputs.items())


def _first_action(text):
    """从模型回复里抠出动作 JSON。复用 qc 的解析器（围栏/夹带散文都能啃）。"""
    obj = qc._extract_json_obj(text)
    if isinstance(obj, dict) and isinstance(obj.get("action"), str):
        return obj
    return None


def agent_solve(task, llm, cost=None, max_steps=20):
    """让模型以 agent 形态做题：多步、可读附件、可分次写交付文件，最后判分。

    诚实性同 N7 单发（契约地基）：**不是「不会做」的 0 分一律抛成测量故障** ——
    一个有效动作都没解析出来（协议噪声）、判分器自己崩了，都算故障。
    """
    wd = tempfile.mkdtemp(prefix="ale_n7agent_")
    harness.setup_env(task, wd)
    inputs, truncated = qc._read_inputs(os.path.join(wd, "input"))
    if truncated and os.environ.get("ALE_N7_ALLOW_TRUNCATION", "") != "1":
        raise RuntimeError("附件被截断，题目不可解、测出来的难度是假的：%s"
                           % json.dumps(truncated, ensure_ascii=False))
    out_dir = os.path.join(wd, "output")
    os.makedirs(out_dir, exist_ok=True)

    msgs = [
        {"role": "system", "content": "你是完成真实工作任务的 agent。" + PROTOCOL % max_steps},
        {"role": "user", "content": "\n".join([
            task.card.get("prompt", ""), "", "## 交付要求",
            json.dumps(task.card.get("交付要求", {}), ensure_ascii=False, indent=2),
            "", "## input/ 文件清单（用 read_file 按需读）", _listing(inputs),
            "", "现在开始，回一个动作 JSON。"])},
    ]

    trace, written, bad_steps, valid = [], 0, 0, 0
    for step in range(max_steps):
        res = llm.chat(msgs)
        if cost:
            cost.add("N7-agent", res)
        if not res.text.strip() or res.finish_reason == "length":
            raise RuntimeError(
                "agent 档第 %d 步无有效产出（finish_reason=%s, reasoning_tokens=%d）"
                % (step + 1, res.finish_reason, res.reasoning_tokens))
        msgs.append({"role": "assistant", "content": res.text})
        act = _first_action(res.text)
        if not act:
            bad_steps += 1
            obs = "动作解析失败：每轮只回一个含 action 字段的 JSON 对象，不要别的文字。"
        else:
            valid += 1
            kind = act.get("action")
            if kind == "list_files":
                obs = "input/ 文件清单：\n" + _listing(inputs)
            elif kind == "read_file":
                p = str(act.get("path", "")).lstrip("/")
                p = p[len("input/"):] if p.startswith("input/") else p
                obs = inputs.get(p) or ("没有这个文件。清单：\n" + _listing(inputs))
                obs = "%s 的内容：\n%s" % (p, obs)
            elif kind == "write_file":
                rel = qc._safe_rel(act.get("path", ""))
                content = act.get("content")
                if not rel or not isinstance(content, str):
                    obs = "write_file 需要 path 与字符串 content。"
                else:
                    dst = os.path.join(out_dir, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "w", encoding="utf-8") as f:
                        f.write(content)
                    written += 1
                    obs = "已写入 output/%s（%d 字符）。" % (rel, len(content))
            elif kind == "finish":
                trace.append({"step": step + 1, "action": "finish"})
                break
            else:
                obs = "不认识的 action=%r，只许 list_files/read_file/write_file/finish。" % kind
        trace.append({"step": step + 1, "action": (act or {}).get("action", "<解析失败>"),
                      "path": (act or {}).get("path")})
        msgs.append({"role": "user", "content": obs})

    if valid == 0:
        raise RuntimeError("agent 档全程没解析出一个有效动作（%d 步全是协议噪声）" % bad_steps)
    r = harness.evaluate(task, wd)
    if r.fault:
        raise RuntimeError("判分器故障，本跑不计入难度：" + r.fault[:300])
    return True, {"task_id": task.task_id, "score": r.score, "passed": r.passed,
                  "写出文件数": len(os.listdir(out_dir)) if os.path.isdir(out_dir) else 0,
                  "步数": len(trace), "write次数": written, "协议噪声步": bad_steps,
                  "trace": trace}


class _ScriptedClient:
    """离线自测用：按脚本依次回动作，不触网、不花钱。"""

    def __init__(self, script):
        self.script, self.i, self.model = script, 0, "scripted"

    def chat(self, messages, **kw):
        from .llm import LLMResult
        text = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return LLMResult(text=text, prompt_tokens=10, completion_tokens=10, model=self.model)


def _selftest():
    """离线自测（CI 跑这个）：验证 agent 档的动作循环与两条诚实性路径。"""
    from .tasks import load_task
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    t = load_task(os.path.join(root, "tasks", "_demo", "采购清单核算"))
    deliver = list(t.card.get("交付要求", {}))[0]

    ok, r = agent_solve(t, _ScriptedClient([
        json.dumps({"action": "list_files"}),
        json.dumps({"action": "read_file", "path": "采购清单.csv"}),
        json.dumps({"action": "write_file", "path": deliver,
                    "content": json.dumps({"故意": "写错的内容"}, ensure_ascii=False)}),
        json.dumps({"action": "finish"}),
    ]), max_steps=8)
    assert r["write次数"] == 1 and r["协议噪声步"] == 0, r
    assert r["步数"] == 4, "动作循环步数不对：%s" % r["步数"]
    assert r["score"] < 1.0, "写错内容却给了满分：%s" % r["score"]
    print("agentsolve 自测①动作循环 OK（步数 %d、写文件 %d、score %.4f）"
          % (r["步数"], r["write次数"], r["score"]))

    try:   # 协议噪声全程无有效动作 → 必须抛测量故障，不能冒充 0 分
        agent_solve(t, _ScriptedClient(["我先分析一下这道题……"]), max_steps=3)
    except RuntimeError as e:
        assert "有效动作" in str(e), e
        print("agentsolve 自测②协议噪声→测量故障 OK")
    else:
        raise AssertionError("全程协议噪声居然没抛测量故障")
    return True


if __name__ == "__main__":
    _selftest()


