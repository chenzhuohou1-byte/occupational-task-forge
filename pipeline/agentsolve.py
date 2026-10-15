"""N7 的 **agent 档**：多步、带文件工具的盲解；用来和单发盲解做对照。

为什么要有这一档：单发盲解＝「一口气把所有交付文件的全文吐在一个 JSON 里」，
它跟最终 benchmark 真正要跑的 agent（能列目录、能按需读文件、能分步写、能回头
自查）不是一回事。入库门槛（契约 §6.3）到底该卡在哪一档，必须拿同一条任务两种
测法对照过才知道——否则按单发口径收下的题，换真 agent 一跑可能轻松做掉。

协议刻意用**纯文本 JSON 动作**，不依赖各家的 function-calling，网关上什么模型都能当考生：
  {"action":"list_files"}                       列出 input/ 下全部文件
  {"action":"read_file","path":"...","offset":0}  读一份附件（分页；返回剩余字符数）
  {"action":"write_file","path":"...","content":"..."}  往 output/ 写一个交付文件
  {"action":"finish"}                           交卷

read_file 为什么要分页：真实附件动辄几万字符（ALE 的最大单份附件中位 16221、P75 26 万），
一次整份塞回去要么撑爆上下文、要么被 ALE_N7_INPUT_LIMIT 悄悄截断——而截断会让题变成不可解、
测出来的难度是假的。分页把「附件多大」和「单次返回多大」解耦，附件规模才能往真实量级走。
"""
import json
import os
import tempfile

from . import harness, qc

PAGE_CHARS = int(os.environ.get("ALE_N7_PAGE_CHARS", "6000"))   # read_file 单次返回上限
# agent 档把整份附件读进内存、靠分页发给模型，所以这里的上限可以远高于单发档的 8000
FILE_LIMIT = int(os.environ.get("ALE_N7_FILE_LIMIT", "150000"))

PROTOCOL = """你在一个沙箱里完成任务。只能用下面四个动作，**每轮只回一个 JSON 对象、不要任何其它文字**：
{"action":"list_files"}
{"action":"read_file","path":"相对 input/ 的路径","offset":0}
{"action":"write_file","path":"相对 output/ 的路径","content":"该文件的完整文本内容"}
{"action":"finish"}
规则：input/ 只读、output/ 是你唯一能写的目录；交付文件必须严格按「交付要求」的文件名与格式；
写完全部交付文件后再 finish。最多 %d 步，步数用完即交卷。
read_file 单次最多返回 """ + str(PAGE_CHARS) + """ 字符：`offset` 省略即从 0 开始，返回里会告诉你
这份文件共多少字符、本次给到哪、还剩多少；没读完就用 `offset` 接着读，**别在没读完的材料上就动手算**。"""


def _page(name, text, offset):
    """按 offset 切一页，并把「总长/本次区间/剩余」明确告诉模型——它得知道自己有没有读完。"""
    total = len(text)
    off = max(0, min(int(offset or 0), total))
    chunk = text[off:off + PAGE_CHARS]
    end = off + len(chunk)
    left = total - end
    head = "%s 第 %d-%d 字符（共 %d）" % (name, off, end, total)
    if left > 0:
        head += "；还剩 %d 字符，用 {\"action\":\"read_file\",\"path\":\"%s\",\"offset\":%d} 继续读" % (
            left, name, end)
    else:
        head += "；已到文件末尾"
    return head + "：\n" + chunk


def _listing(inputs):
    """清单带字符数与页数——模型据此估步数预算，不然它不知道一份 4 万字符的表要读 7 次。"""
    return "\n".join(
        "  %s  (%d 字符，需 read_file %d 次)" % (k, len(v), max(1, -(-len(v) // PAGE_CHARS)))
        for k, v in inputs.items())


def pages_needed(inputs):
    """读完全部附件所需的 read_file 次数。maxSteps 要按页数算，不是按文件数。"""
    return sum(max(1, -(-len(v) // PAGE_CHARS)) for v in inputs.values())


def _first_action(text):
    """从模型回复里抠出动作 JSON。复用 qc 的解析器（围栏/夹带散文都能啃）。"""
    obj = qc._extract_json_obj(text)
    if isinstance(obj, dict) and isinstance(obj.get("action"), str):
        return obj
    return None


def agent_solve(task, llm, cost=None, max_steps=20, trace_dir=None):
    """让模型以 agent 形态做题：多步、可读附件、可分次写交付文件，最后判分。

    诚实性同 N7 单发（契约地基）：**不是「不会做」的 0 分一律抛成测量故障** ——
    一个有效动作都没解析出来（协议噪声）、步数耗尽却一个文件都没写（预算不够）、
    判分器自己崩了，都算故障。

    给了 trace_dir 就把完整对话（每一步的提问与回答）、动作序列、产出文件、
    判分器完整输出落盘；多步任务不留轨迹等于事后无法复盘它在哪一步走偏。
    """
    wd = tempfile.mkdtemp(prefix="ale_n7agent_")
    harness.setup_env(task, wd)
    inputs, truncated = qc._read_inputs(os.path.join(wd, "input"), limit=FILE_LIMIT)
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
    try:
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
                    if p in inputs:
                        obs = _page(p, inputs[p], act.get("offset", 0))
                    else:
                        obs = "没有这个文件。清单：\n" + _listing(inputs)
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
                    obs = ("不认识的 action=%r，只许 list_files/read_file/write_file/finish。"
                           % kind)
            trace.append({"step": step + 1, "action": (act or {}).get("action", "<解析失败>"),
                          "path": (act or {}).get("path"),
                          # 多页读取的轨迹不记 offset 事后没法复盘它到底读了哪一段
                          **({"offset": (act or {}).get("offset", 0)}
                             if (act or {}).get("action") == "read_file" else {})})
            left = max_steps - step - 1
            msgs.append({"role": "user", "content": "%s\n[还剩 %d 步；步数用完即交卷，"
                         "务必留足步数把交付文件写出来]" % (obs, left)})
    finally:
        # 不管正常结束还是抛故障都落盘：最该复盘的恰恰是出故障那几次
        qc._save_trace(trace_dir, transcript=msgs, actions=trace,
                       meta={"max_steps": max_steps, "有效动作": valid,
                             "协议噪声步": bad_steps, "write次数": written,
                             "work_dir": wd})


    if valid == 0:
        raise RuntimeError("agent 档全程没解析出一个有效动作（%d 步全是协议噪声）" % bad_steps)
    # 步数用完、一个交付文件都没写出来 ＝ **预算不够**，不是不会做（实测：造价 22 份附件，
    # maxSteps=20 光读文件就用完了）。这种 0 分不作为难度信号，记测量故障、让调用方加预算。
    if written == 0 and len(trace) >= max_steps:
        raise RuntimeError(
            "agent 档 %d 步预算耗尽、一个交付文件都没写（光读附件就用完了）：这不是难度，"
            "是预算不够。调大 N7_MAX_STEPS（附件份数 + 交付文件数 + 一半余量）。" % max_steps)
    r = harness.evaluate(task, wd)
    qc._save_trace(trace_dir, work_dir=wd,
                   score=dict(r.raw or {}, _score=r.score, _passed=r.passed,
                              _errors=r.errors, _fault=r.fault))
    if r.fault:
        raise RuntimeError("判分器故障，本跑不计入难度：" + r.fault[:300])
    return True, {"task_id": task.task_id, "score": r.score, "passed": r.passed,
                  "写出文件数": len(os.listdir(out_dir)) if os.path.isdir(out_dir) else 0,
                  "步数": len(trace), "write次数": written, "协议噪声步": bad_steps,
                  "trace": trace, "trace_dir": trace_dir}


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

    # 分页：按 offset 切片、告知总长与剩余、读到末尾不再提示继续、offset 越界不崩
    long_text = "".join("第%05d行,数值%d\n" % (i, i) for i in range(2000))   # 约 2.8 万字符
    p1 = _page("大表.csv", long_text, 0)
    body1 = p1.split("：\n", 1)[1]
    assert len(body1) == PAGE_CHARS, "首页长度应为 %d，实际 %d" % (PAGE_CHARS, len(body1))
    assert body1 == long_text[:PAGE_CHARS], "首页内容不是文件开头"
    assert ("共 %d" % len(long_text)) in p1 and "还剩" in p1 and '"offset":%d' % PAGE_CHARS in p1, p1
    p2 = _page("大表.csv", long_text, PAGE_CHARS)
    assert p2.split("：\n", 1)[1] == long_text[PAGE_CHARS:PAGE_CHARS * 2], "第二页接得不对"
    last = _page("大表.csv", long_text, len(long_text) - 10)
    assert "已到文件末尾" in last and "还剩" not in last, last
    over = _page("大表.csv", long_text, len(long_text) + 999)
    assert "已到文件末尾" in over and over.endswith("：\n"), "offset 越界应返回空内容而不是崩"
    n_pages = max(1, -(-len(long_text) // PAGE_CHARS))
    assert pages_needed({"大表.csv": long_text}) == n_pages
    print("agentsolve 自测③read_file 分页 OK（%d 字符 → %d 页，单页 %d 字符）"
          % (len(long_text), n_pages, PAGE_CHARS))

    # 分页后 agent 能跨页读完同一份附件：两次 read_file 拿到不同内容
    ok, r = agent_solve(t, _ScriptedClient([
        json.dumps({"action": "read_file", "path": "采购清单.csv", "offset": 0}),
        json.dumps({"action": "read_file", "path": "采购清单.csv", "offset": 5}),
        json.dumps({"action": "finish"}),
    ]), max_steps=6)
    obs = [s for s in r["trace"] if s["action"] == "read_file"]
    assert len(obs) == 2 and [s["offset"] for s in obs] == [0, 5], obs
    print("agentsolve 自测④跨页读取 OK（offset 生效并记进轨迹：%s）"
          % [s["offset"] for s in obs])
    return True


if __name__ == "__main__":
    _selftest()


