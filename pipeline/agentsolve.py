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

两种工具集（`tools` 参数，记进 measuredDifficulty.toolset，换了就不可比）：
  python（默认，toolset=agent-file-python）：上面四个 + run_python，read_file 可按 offset 分页
  file  （旧考法，toolset=agent-file-tools）：只有上面四个，保留只为与旧测量对照

为什么默认要能跑代码：ALE 的 agent 在沙箱里有完整 shell（vm_mcp_server.run_command），
157 张任务卡里 99 张把 Python 列进 software。只给读写文件，测出来的「难」有一部分是
「心算出错」（EDA 那条两跑 0.99 都只错在总额少加 1 元）—— 那是我们考法的负担，不是题难。

read_file 为什么要分页：真实附件动辄几万字符（ALE 的最大单份附件中位 16221、P75 26 万），
一次整份塞回去要么撑爆上下文、要么被 ALE_N7_INPUT_LIMIT 悄悄截断——而截断会让题变成不可解、
测出来的难度是假的。分页把「附件多大」和「单次返回多大」解耦，附件规模才能往真实量级走。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import harness, qc

TOOLSETS = {"python": "agent-file-python", "file": "agent-file-tools"}
# read_file 每页字符数。默认沿用 ALE_N7_INPUT_LIMIT（n7_run 记进 budget.inputLimit），
# 保证 10-15 起的 agent-file-python 测量口径不变；ALE_N7_PAGE_CHARS 可单独覆盖。
PAGE_CHARS = int(os.environ.get("ALE_N7_PAGE_CHARS") or os.environ.get("ALE_N7_INPUT_LIMIT") or "8000")
# 旧考法（file）整份返回、不分页，超过这个上限即截断并拒跑（截断＝题不可解）
FILE_LIMIT = int(os.environ.get("ALE_N7_FILE_LIMIT", "150000"))

PROTOCOL = """你在一个沙箱里完成任务。只能用下面四个动作，**每轮只回一个 JSON 对象、不要任何其它文字**：
{"action":"list_files"}
{"action":"read_file","path":"相对 input/ 的路径"}
{"action":"write_file","path":"相对 output/ 的路径","content":"该文件的完整文本内容"}
{"action":"finish"}
规则：input/ 只读、output/ 是你唯一能写的目录；交付文件必须严格按「交付要求」的文件名与格式；
写完全部交付文件后再 finish。最多 %d 步，步数用完即交卷。"""


def _page(name, text, offset):
    """按 offset 切一页，并把「总长/本次区间/剩余」明确告诉模型——它得知道自己有没有读完。"""
    total = len(text)
    try:
        off = max(0, min(int(offset or 0), total))
    except (TypeError, ValueError):
        off = 0
    chunk = text[off:off + PAGE_CHARS]
    end = off + len(chunk)
    left = total - end
    head = "%s 第 %d-%d 字符（共 %d）" % (name, off, end, total)
    if left > 0:
        head += "；还剩 %d 字符，用 {\"action\":\"read_file\",\"path\":\"%s\",\"offset\":%d} 继续读，或 run_python 直接解析原文件" % (
            left, name, end)
    else:
        head += "；已到文件末尾"
    return head + "：\n" + chunk


PROTOCOL_PY = """你在一个沙箱里完成任务。只能用下面五个动作，**每轮只回一个 JSON 对象、不要任何其它文字**：
{"action":"list_files"}
{"action":"read_file","path":"相对 input/ 的路径","offset":0}
{"action":"write_file","path":"相对 output/ 的路径","content":"该文件的完整文本内容"}
{"action":"run_python","code":"完整的 Python 3 脚本"}
{"action":"finish"}
规则：
- 工作目录下有 input/（只读，附件原文件，含 xlsx/docx 等二进制）与 output/（唯一能写的交付目录）。
- read_file 返回文本化后的附件；长文件分页，按提示的 offset 继续读。
- run_python 在工作目录里执行脚本，返回退出码与 stdout/stderr（过长会截断，只 print 你要看的）。
  无网络；每次运行独立、不保留变量；可用标准库，pandas/openpyxl 等视环境而定，import 失败就退回标准库。
  脚本可以直接把交付文件写进 output/。
- 交付文件必须严格按「交付要求」的文件名与格式；写完全部交付文件后再 finish。
最多 %d 步，步数用完即交卷。"""


def _sandbox_profile(wd, scratch):
    """macOS sandbox-exec 规则：无网络、读不了家目录（答案在任务目录、密钥在 .env，都在 ~ 下）、
    只能写 output/ 与草稿目录。Python 自身与用户 site-packages 放行读。"""
    home = os.path.realpath(os.path.expanduser("~"))
    allow_read = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, wd)}
    try:
        import site
        allow_read.add(os.path.realpath(site.getusersitepackages()))
    except Exception:  # noqa: BLE001
        pass
    allow_read.add(os.path.join(home, "Library", "Python"))
    q = lambda p: '"%s"' % p.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    return "\n".join([
        "(version 1)", "(allow default)", "(deny network*)",
        "(deny file-read* (subpath %s))" % q(home),
        "(allow file-read* (literal %s) %s)" % (q(home), " ".join(
            "(subpath %s)" % q(p) for p in sorted(allow_read))),
        "(deny file-write*)",
        '(allow file-write* (subpath %s) (subpath %s) (literal "/dev/null") (regex #"^/dev/tty"))'
        % (q(os.path.join(wd, "output")), q(scratch)),
    ])


def run_python(wd, code, tag, timeout=None, out_limit=None):
    """在工作目录里跑一段模型写的脚本，返回给模型看的观察文本。

    隔离：macOS 上走 sandbox-exec（无网、读不了 ~、只能写 output/）；环境变量清空（不带密钥）。
    没有沙箱的机器（如 CI 的 Linux）默认**拒跑**并抛测量故障 —— 不隔离就让模型代码碰得到
    reference/ 与 .env，测出来的不是难度；离线自测可设 ALE_RUN_PYTHON_UNSANDBOXED=1 放行。
    """
    timeout = timeout or int(os.environ.get("ALE_RUN_PYTHON_TIMEOUT", "120"))
    out_limit = out_limit or int(os.environ.get("ALE_RUN_PYTHON_OUTPUT_LIMIT", "6000"))
    wd = os.path.realpath(wd)
    scratch = os.path.join(wd, "_scratch")
    os.makedirs(scratch, exist_ok=True)
    script = os.path.join(scratch, "%s.py" % tag)
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "LANG": "en_US.UTF-8",
           "TMPDIR": scratch, "MPLCONFIGDIR": scratch}
    cmd = [sys.executable, "-X", "utf8", script]
    if shutil.which("sandbox-exec"):
        prof = os.path.join(scratch, "_sandbox.sb")
        with open(prof, "w", encoding="utf-8") as f:
            f.write(_sandbox_profile(wd, scratch))
        cmd = ["sandbox-exec", "-f", prof] + cmd
    elif os.environ.get("ALE_RUN_PYTHON_UNSANDBOXED", "") != "1":
        raise RuntimeError("run_python 需要沙箱（macOS sandbox-exec）；本机没有，拒跑以免模型代码"
                           "读到答案或密钥。离线自测可设 ALE_RUN_PYTHON_UNSANDBOXED=1。")
    out_dir = os.path.join(wd, "output")
    before = {p: os.path.getmtime(os.path.join(out_dir, p)) for p in _out_files(out_dir)}
    try:
        r = subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        head = "退出码 %d" % r.returncode
        text = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    except subprocess.TimeoutExpired as e:
        head = "超时（%ds）被终止" % timeout
        text = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    if len(text) > out_limit:
        half = out_limit // 2
        text = "%s\n…[中间省略 %d 字符]…\n%s" % (text[:half], len(text) - out_limit, text[-half:])
    changed = [p for p in _out_files(out_dir)
               if before.get(p) != os.path.getmtime(os.path.join(out_dir, p))]
    tail = ("\n[output/ 新写或改动：%s]" % ", ".join(changed)) if changed else ""
    return "run_python %s\n%s%s" % (head, text, tail), changed


def _out_files(out_dir):
    res = []
    for root, _, files in os.walk(out_dir):
        for fn in files:
            res.append(os.path.relpath(os.path.join(root, fn), out_dir))
    return sorted(res)


def _listing(inputs, paged=True):
    """清单带字符数与页数——模型据此估步数预算，不然它不知道一份 4 万字符的表要读 7 次。
    旧考法（不分页）只给字符数，保持对照基线不变。"""
    if not paged:
        return "\n".join("  %s  (%d 字符)" % (k, len(v)) for k, v in inputs.items())
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


def agent_solve(task, llm, cost=None, max_steps=20, trace_dir=None, tools="python"):
    """让模型以 agent 形态做题：多步、可读附件、可分次写交付文件、可跑代码，最后判分。

    诚实性同 N7 单发（契约地基）：**不是「不会做」的 0 分一律抛成测量故障** ——
    一个有效动作都没解析出来（协议噪声）、步数耗尽却一个文件都没写（预算不够）、
    判分器自己崩了，都算故障。

    给了 trace_dir 就把完整对话（每一步的提问与回答）、动作序列、产出文件、
    判分器完整输出落盘；多步任务不留轨迹等于事后无法复盘它在哪一步走偏。
    """
    if tools not in TOOLSETS:
        raise ValueError("tools 只认 %s，给的是 %r" % (list(TOOLSETS), tools))
    py = tools == "python"
    wd = tempfile.mkdtemp(prefix="ale_n7agent_")
    harness.setup_env(task, wd)
    if py:   # 分页读，材料不丢 → 不存在「截断成不可解」，附件不必再为 8000 字符压体积
        inputs, _ = qc._read_inputs(os.path.join(wd, "input"), limit=10 ** 9)
    else:
        inputs, truncated = qc._read_inputs(os.path.join(wd, "input"))   # 旧考法口径不动
        if truncated and os.environ.get("ALE_N7_ALLOW_TRUNCATION", "") != "1":
            raise RuntimeError("附件被截断，题目不可解、测出来的难度是假的：%s"
                               % json.dumps(truncated, ensure_ascii=False))
    out_dir = os.path.join(wd, "output")
    os.makedirs(out_dir, exist_ok=True)

    msgs = [
        {"role": "system", "content": "你是完成真实工作任务的 agent。"
         + (PROTOCOL_PY if py else PROTOCOL) % max_steps},
        {"role": "user", "content": "\n".join([
            task.card.get("prompt", ""), "", "## 交付要求",
            json.dumps(task.card.get("交付要求", {}), ensure_ascii=False, indent=2),
            "", "## input/ 文件清单（用 read_file 按需读）", _listing(inputs, py),
            "", "现在开始，回一个动作 JSON。"])},
    ]

    trace, written, bad_steps, valid, py_runs = [], 0, 0, 0, 0
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
                    obs = "input/ 文件清单：\n" + _listing(inputs, py)
                elif kind == "read_file":
                    p = str(act.get("path", "")).lstrip("/")
                    p = p[len("input/"):] if p.startswith("input/") else p
                    if p not in inputs:
                        obs = "没有这个文件。清单：\n" + _listing(inputs, py)
                    elif py:
                        obs = _page(p, inputs[p], act.get("offset", 0))
                    else:
                        obs = "%s 的内容：\n%s" % (p, inputs[p])
                elif kind == "run_python" and py:
                    code = act.get("code")
                    if not isinstance(code, str) or not code.strip():
                        obs = "run_python 需要字符串 code。"
                    else:
                        py_runs += 1
                        obs, _changed = run_python(wd, code, "step%02d" % (step + 1))
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
                    obs = ("不认识的 action=%r，只许 list_files/read_file/write_file/%sfinish。"
                           % (kind, "run_python/" if py else ""))
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
                       meta={"max_steps": max_steps, "toolset": TOOLSETS[tools], "有效动作": valid,
                             "协议噪声步": bad_steps, "write次数": written,
                             "run_python次数": py_runs, "work_dir": wd})

    if valid == 0:
        raise RuntimeError("agent 档全程没解析出一个有效动作（%d 步全是协议噪声）" % bad_steps)
    # 步数用完、一个交付文件都没写出来 ＝ **预算不够**，不是不会做（实测：造价 22 份附件，
    # maxSteps=20 光读文件就用完了）。这种 0 分不作为难度信号，记测量故障、让调用方加预算。
    # 看 output/ 实际有没有文件：run_python 也能写交付物，只数 write_file 会误判。
    if not _out_files(out_dir) and len(trace) >= max_steps:
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
                  "写出文件数": len(_out_files(out_dir)), "toolset": TOOLSETS[tools],
                  "步数": len(trace), "write次数": written, "协议噪声步": bad_steps,
                  "run_python次数": py_runs,
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
    ]), max_steps=8, tools="file")
    assert r["write次数"] == 1 and r["协议噪声步"] == 0, r
    assert r["步数"] == 4, "动作循环步数不对：%s" % r["步数"]
    assert r["score"] < 1.0, "写错内容却给了满分：%s" % r["score"]
    assert r["toolset"] == "agent-file-tools", r["toolset"]
    print("agentsolve 自测①动作循环 OK（步数 %d、写文件 %d、score %.4f）"
          % (r["步数"], r["write次数"], r["score"]))

    # ③ run_python：脚本读 input/ 原文件、把交付物直接写进 output/（不经 write_file）。
    # Linux CI 没有 sandbox-exec，这里显式放行；真测量时没沙箱会拒跑（见 run_python）。
    has_sb = bool(shutil.which("sandbox-exec"))
    if not has_sb:
        os.environ["ALE_RUN_PYTHON_UNSANDBOXED"] = "1"
    code = ("import os\nprint(sorted(os.listdir('input')))\n"
            "open(os.path.join('output', %r), 'w', encoding='utf-8').write('{}')\n" % deliver)
    ok, r = agent_solve(t, _ScriptedClient([
        json.dumps({"action": "run_python", "code": code}),
        json.dumps({"action": "finish"}),
    ]), max_steps=4)
    assert r["toolset"] == "agent-file-python" and r["run_python次数"] == 1, r
    assert r["写出文件数"] == 1 and r["write次数"] == 0, "run_python 写的交付物没被看见：%s" % r
    print("agentsolve 自测③run_python 写交付物 OK（沙箱=%s）" % has_sb)

    if has_sb:   # ④ 沙箱真的在拦：读家目录、写 input/、联网都必须失败
        wd = tempfile.mkdtemp(prefix="ale_sbtest_")
        os.makedirs(os.path.join(wd, "input"))
        os.makedirs(os.path.join(wd, "output"))
        probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentsolve.py")
        obs, _ = run_python(wd, "\n".join([
            "import urllib.request",
            "for name, fn in [('read_home', lambda: open(%r).read()),"
            " ('write_input', lambda: open('input/x', 'w')),"
            " ('net', lambda: urllib.request.urlopen('https://example.com', timeout=5))]:" % probe,
            "    try: fn(); print('LEAK', name)",
            "    except Exception: print('blocked', name)"]), "sbprobe")
        shutil.rmtree(wd, ignore_errors=True)
        assert "LEAK" not in obs and obs.count("blocked") == 3, obs
        print("agentsolve 自测④沙箱隔离 OK（读 ~ / 写 input / 联网 全拦）")

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


