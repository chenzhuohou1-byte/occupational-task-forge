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

还有一档 engine（toolset=agent-file-engine，判分契约 §9.5）：在 python 基础上多一个
engine_selftest 动作，让 agent 把自己造的可执行产物丢到真实引擎上跑自己写的场景（像真工程师
那样部署自测）。**它与另两档不可比、不参与难度定级**（qc 只认 agent-file-python/-tools）——
拿到引擎反馈再做的题，难度和盲测是两回事。隔离与红线见 pipeline.executable.selftest。

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

TOOLSETS = {"python": "agent-file-python", "file": "agent-file-tools",
            "engine": "agent-file-engine"}
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


# engine 档：python 全部动作 + engine_selftest（把候选产物丢真实引擎跑自带场景）
PROTOCOL_ENGINE = """你在一个沙箱里完成任务。只能用下面六个动作，**每轮只回一个 JSON 对象、不要任何其它文字**：
{"action":"list_files"}
{"action":"read_file","path":"相对 input/ 的路径","offset":0}
{"action":"write_file","path":"相对 output/ 的路径","content":"该文件的完整文本内容"}
{"action":"run_python","code":"完整的 Python 3 脚本"}
{"action":"engine_selftest","scenarios":[{"id":"...","input_variables":{...},"expected_end_event":"...","must_activities":[...],"forbidden_activities":[...],"advance_timer":false}]}
{"action":"finish"}
规则：
- 工作目录下有 input/（只读）与 output/（唯一能写的交付目录）。read_file 分页，run_python 无网络、独立执行。
- engine_selftest 把你当前 output/ 里的可执行产物部署到**真实引擎**，跑你**自己写的**场景并返回每个场景
  pass/reasons。这像真工程师部署自测：先把结构/部署问题暴露出来。注意：
  · 评测用的隐藏场景与标准答案你看不到，这里只跑你自己提供的场景；
  · 每轮自测即起即删、相互隔离；自测次数与单次场景数都有上限，别把它当免费算力刷；
  · 先确保 output/ 已写出可部署的产物，再自测。
- 交付文件必须严格按「交付要求」的文件名与格式；写完全部交付文件、自测通过后再 finish。
最多 %d 步，步数用完即交卷。"""


def _protocol(tools):
    return {"file": PROTOCOL, "python": PROTOCOL_PY, "engine": PROTOCOL_ENGINE}[tools]



def _split_paths(name):
    return [os.path.realpath(os.path.expanduser(p))
            for p in os.environ.get(name, "").split(os.pathsep) if p.strip()]


def _link_hops(path):
    """解析 path 途经的每一个符号链接本身（文件或目录分量）。sandbox 逐跳解析链接，
    只放行最终目标不够：uv 的 venv python → cpython-3.12-…（目录链接）→ 3.12.14，中间那跳读不到就 exec 失败。"""
    hops, todo, seen = set(), [os.path.abspath(os.path.expanduser(path))], set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        parts = p.split(os.sep)
        for i in range(2, len(parts) + 1):
            cur = os.sep.join(parts[:i])
            if os.path.islink(cur):
                hops.add(cur)
                tgt = os.path.join(os.path.dirname(cur), os.readlink(cur))
                todo.append(os.path.normpath(os.sep.join([tgt] + parts[i:])))
                break
    return hops


_RUNTIME_CACHE = {}


def runtime():
    """考生 run_python 的运行环境：(解释器, 只读放行目录, 追加 PATH, 环境标签)。

    默认 = 产线自身解释器（10-15 起的测量口径）。造真实软件题时用：
      ALE_RUN_PYTHON    考生解释器（如 ~/ale-runtime/py312/bin/python，带 rdkit/geopandas…）
      ALE_RUN_SOFTWARE  额外只读放行的软件目录（os.pathsep 分隔，如 KiCad.app 所在目录）
      ALE_RUN_PATH      追加到 PATH 的目录（如 kicad-cli 所在的 Contents/MacOS）
      ALE_RUN_RUNTIME   环境标签，记进 measuredDifficulty.budget.runtime（换了环境就不可比）
    放行的只有这些目录本身，家目录其余部分（任务目录的 reference/、.env）照旧读不到。

    ALE_RUN_SANDBOX=docker 时换成容器后端（见 pipeline/docker_sandbox.py）：解释器与软件全在镜像里，
    上面四个变量一概不看（它们指的是宿主路径），环境标签由镜像 tag 派生成 `docker-<tag>`。"""
    if sandbox_backend() == "docker":
        from . import docker_sandbox
        return ("python", [], [], docker_sandbox.runtime_tag())
    key = tuple(os.environ.get(k, "") for k in
                ("ALE_RUN_PYTHON", "ALE_RUN_SOFTWARE", "ALE_RUN_PATH", "ALE_RUN_RUNTIME"))
    if key in _RUNTIME_CACHE:
        return _RUNTIME_CACHE[key]
    py = os.path.expanduser(key[0]) if key[0] else sys.executable
    allow = set(_split_paths("ALE_RUN_SOFTWARE")) | set(_split_paths("ALE_RUN_PATH"))
    if key[0]:   # venv 的 prefix 与它指向的基础解释器都要放行
        r = subprocess.run([py, "-c", "import sys;print(sys.prefix);print(sys.base_prefix)"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError("ALE_RUN_PYTHON=%s 起不来：%s" % (py, r.stderr.strip()))
        allow |= {os.path.realpath(p) for p in r.stdout.split()}
        allow.add(os.path.realpath(os.path.dirname(os.path.realpath(py))))
        allow |= _link_hops(py)
    # 默认口径的 PATH 只有 /usr/bin:/bin，不动；指定了考生解释器才把它的 bin 加进去（venv 里的 sumo 等 CLI）
    extra = _split_paths("ALE_RUN_PATH") + ([os.path.dirname(py)] if key[0] else [])
    res = (py, sorted(allow), extra, key[3] or None)
    _RUNTIME_CACHE[key] = res
    return res


def sandbox_backend():
    """考生 run_python 的隔离后端：默认 sandbox-exec（macOS，10-15 起的测量口径），
    ALE_RUN_SANDBOX=docker 换容器。其它取值直接报错，免得拼错一个字母就悄悄退回默认后端。"""
    b = os.environ.get("ALE_RUN_SANDBOX", "").strip().lower() or "sandbox-exec"
    if b not in ("sandbox-exec", "docker"):
        raise ValueError("ALE_RUN_SANDBOX 只认 sandbox-exec / docker，给的是 %r" % b)
    return b


_USER_BASE_CACHE = {}


def _user_base(py):
    """考生解释器在真 HOME 下的用户 site 根目录（run_python 换了假 HOME，要用它钉回去）。"""
    if py not in _USER_BASE_CACHE:
        if py == sys.executable:
            import site
            ub = site.getuserbase()
        else:
            r = subprocess.run([py, "-c", "import site;print(site.getuserbase())"],
                               capture_output=True, text=True, timeout=30)
            ub = r.stdout.strip() if r.returncode == 0 else ""
        _USER_BASE_CACHE[py] = ub
    return _USER_BASE_CACHE[py]


def _sandbox_profile(wd, scratch):
    """macOS sandbox-exec 规则：无网络、读不了家目录（答案在任务目录、密钥在 .env，都在 ~ 下）、
    只能写 output/ 与草稿目录。Python 自身、用户 site-packages、runtime() 声明的软件目录放行读。"""
    home = os.path.realpath(os.path.expanduser("~"))
    allow_read = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, wd)}
    allow_read |= set(runtime()[1])
    try:
        import site
        allow_read.add(os.path.realpath(site.getusersitepackages()))
    except Exception:  # noqa: BLE001
        pass
    allow_read.add(os.path.join(home, "Library", "Python"))
    q = lambda p: '"%s"' % p.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    # 放行目录的各级父目录只给元数据（stat/lstat，不能列目录、不能读内容）：SQLite（PROJ 的 proj.db）
    # 打开文件前会逐级 lstat 解析路径，父目录 stat 不了就报打不开
    parents = set()
    for p in allow_read:
        d = os.path.dirname(p)
        while d.startswith(home + os.sep):
            parents.add(d)
            d = os.path.dirname(d)
    return "\n".join([
        "(version 1)", "(allow default)", "(deny network*)",
        "(deny file-read* (subpath %s))" % q(home),
        "(allow file-read* (literal %s) %s)" % (q(home), " ".join(
            "(subpath %s)" % q(p) for p in sorted(allow_read))),
    ] + (["(allow file-read-metadata %s)" % " ".join("(literal %s)" % q(d) for d in sorted(parents))]
         if parents else []) + [
        "(deny file-write*)",
        '(allow file-write* (subpath %s) (subpath %s) (literal "/dev/null") (regex #"^/dev/tty"))'
        % (q(os.path.join(wd, "output")), q(scratch)),
    ])


def run_python(wd, code, tag, timeout=None, out_limit=None):
    """在工作目录里跑一段模型写的脚本，返回给模型看的观察文本。

    隔离：macOS 上走 sandbox-exec（无网、读不了 ~、只能写 output/）；环境变量清空（不带密钥）。
    没有沙箱的机器（如 CI 的 Linux）默认**拒跑**并抛测量故障 —— 不隔离就让模型代码碰得到
    reference/ 与 .env，测出来的不是难度；离线自测可设 ALE_RUN_PYTHON_UNSANDBOXED=1 放行。
    ALE_RUN_SANDBOX=docker 改走容器（pipeline/docker_sandbox.py，Linux 也能跑）；docker 不可达/镜像缺失/
    容器卡死抛 SandboxUnavailable（RuntimeError），同样记测量故障。
    """
    timeout = timeout or int(os.environ.get("ALE_RUN_PYTHON_TIMEOUT", "120"))
    out_limit = out_limit or int(os.environ.get("ALE_RUN_PYTHON_OUTPUT_LIMIT", "6000"))
    wd = os.path.realpath(wd)
    scratch = os.path.join(wd, "_scratch")
    os.makedirs(scratch, exist_ok=True)
    script = os.path.join(scratch, "%s.py" % tag)
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)
    if sandbox_backend() == "docker":
        from . import docker_sandbox
        return _observe(wd, timeout, out_limit,
                        lambda: docker_sandbox.run(wd, script, timeout))
    py, _allow, extra_path, _tag = runtime()
    # HOME 指到草稿目录里的假家目录：真家目录在沙箱里只读，KiCad/Inkscape 每次调用都去 ~/Library
    # 建配置/字体缓存目录失败，刷满 stderr 吃掉输出额度（C2/C10 实测）。KiCad 不认 HOME，另给
    # KICAD_CONFIG_HOME；PYTHONUSERBASE 钉回真用户目录，免得用户 site-packages 跟着 HOME 丢掉。
    fake_home = os.path.join(scratch, "_home")
    os.makedirs(fake_home, exist_ok=True)
    env = {"PATH": os.pathsep.join(extra_path + ["/usr/bin", "/bin"]),
           "HOME": fake_home, "LANG": "en_US.UTF-8",
           "TMPDIR": scratch, "MPLCONFIGDIR": scratch,
           "KICAD_CONFIG_HOME": os.path.join(fake_home, "kicad"),
           "XDG_CONFIG_HOME": os.path.join(fake_home, ".config"),
           "XDG_CACHE_HOME": os.path.join(fake_home, ".cache"),
           "XDG_DATA_HOME": os.path.join(fake_home, ".local", "share")}
    ub = _user_base(py)
    if ub:
        env["PYTHONUSERBASE"] = ub
    cmd = [py, "-X", "utf8", script]
    if shutil.which("sandbox-exec"):
        prof = os.path.join(scratch, "_sandbox.sb")
        with open(prof, "w", encoding="utf-8") as f:
            f.write(_sandbox_profile(wd, scratch))
        cmd = ["sandbox-exec", "-f", prof] + cmd
    elif os.environ.get("ALE_RUN_PYTHON_UNSANDBOXED", "") != "1":
        raise RuntimeError("run_python 需要沙箱（macOS sandbox-exec）；本机没有，拒跑以免模型代码"
                           "读到答案或密钥。离线自测可设 ALE_RUN_PYTHON_UNSANDBOXED=1。")
    def _exec():
        try:
            r = subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return "退出码 %d" % r.returncode, (r.stdout or "") + (
                ("\n[stderr]\n" + r.stderr) if r.stderr else "")
        except subprocess.TimeoutExpired as e:
            return "超时（%ds）被终止" % timeout, (
                e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
    return _observe(wd, timeout, out_limit, _exec)


def _observe(wd, timeout, out_limit, execute):
    """两种后端共用的观察文本拼装：执行 → 截断过长输出 → 报告 output/ 里新写/改动的文件。
    execute() 返回 (head, text)；基建故障由它直接抛出（不在这里吞掉）。"""
    out_dir = os.path.join(wd, "output")
    before = {p: os.path.getmtime(os.path.join(out_dir, p)) for p in _out_files(out_dir)}
    head, text = execute()
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
    py = tools in ("python", "engine")   # 这两档都带 run_python 与分页读
    eng = tools == "engine"
    wd = tempfile.mkdtemp(prefix="ale_n7agent_")
    harness.setup_env(task, wd)
    selftest = None
    if eng:
        from .executable.selftest import build_selftest
        selftest = build_selftest(task, wd)
        if selftest is None:
            raise ValueError("engine 工具集只对 executable_artifact + flowable 引擎的任务可用；"
                             "本任务 evaluation 不满足，换 tools=python")
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
         + _protocol(tools) % max_steps},
        {"role": "user", "content": "\n".join([
            task.card.get("prompt", ""), "", "## 交付要求",
            json.dumps(task.card.get("交付要求", {}), ensure_ascii=False, indent=2),
            "", "## input/ 文件清单（用 read_file 按需读）", _listing(inputs, py),
            "", "现在开始，回一个动作 JSON。"])},
    ]

    trace, written, bad_steps, valid, py_runs, eng_tests = [], 0, 0, 0, 0, 0
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
                elif kind == "engine_selftest" and eng:
                    eng_tests += 1
                    result = selftest(act.get("scenarios"))
                    obs = "engine_selftest 结果：\n" + json.dumps(result, ensure_ascii=False, indent=2)
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
                    allowed = "list_files/read_file/write_file/"
                    if py:
                        allowed += "run_python/"
                    if eng:
                        allowed += "engine_selftest/"
                    obs = "不认识的 action=%r，只许 %sfinish。" % (kind, allowed)
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
                             "run_python次数": py_runs, "engine_selftest次数": eng_tests,
                             "work_dir": wd})
        # 不可信 engine 自测起过的一次性沙箱必须销毁，绝不残留跑着的引擎
        if selftest is not None and hasattr(selftest, "close"):
            selftest.close()

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
                  "run_python次数": py_runs, "engine_selftest次数": eng_tests,
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

        # ④b 假家目录：HOME/XDG/KICAD_CONFIG_HOME 都在草稿目录里且可写（软件不再刷配置目录报错）
        wd = tempfile.mkdtemp(prefix="ale_sbtest_")
        os.makedirs(os.path.join(wd, "input"))
        os.makedirs(os.path.join(wd, "output"))
        obs, _ = run_python(wd, "\n".join([
            "import os",
            "for k in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'KICAD_CONFIG_HOME'):",
            "    d = os.path.join(os.environ[k], 'probe'); os.makedirs(d, exist_ok=True)",
            "    print('home_ok' if os.path.realpath(d).startswith(os.path.realpath('_scratch')) else 'home_bad', k)"]),
            "homeprobe")
        shutil.rmtree(wd, ignore_errors=True)
        assert obs.count("home_ok") == 4 and "home_bad" not in obs, obs
        print("agentsolve 自测④b 假家目录可写且在草稿目录内 OK")

        # ⑤ 指定考生解释器（ALE_RUN_PYTHON）后：用的是它、标签记得住，且家目录照旧读不到
        saved = {k: os.environ.get(k) for k in ("ALE_RUN_PYTHON", "ALE_RUN_RUNTIME")}
        os.environ["ALE_RUN_PYTHON"], os.environ["ALE_RUN_RUNTIME"] = sys.executable, "selftest-rt"
        try:
            assert runtime()[3] == "selftest-rt", runtime()
            wd = tempfile.mkdtemp(prefix="ale_sbtest_")
            os.makedirs(os.path.join(wd, "input"))
            os.makedirs(os.path.join(wd, "output"))
            obs, _ = run_python(wd, "\n".join([
                "import sys; print('exe', sys.executable)",
                "try: open(%r).read(); print('LEAK read_home')" % probe,
                "except Exception: print('blocked read_home')"]), "rtprobe")
            shutil.rmtree(wd, ignore_errors=True)
            assert "LEAK" not in obs and "blocked read_home" in obs, obs
            print("agentsolve 自测⑤考生解释器放行后 ~ 仍读不到 OK")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

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

    # engine 档守门：数值型任务（非 executable_artifact）开 engine 工具集必须被拒，
    # 免得 agent-file-engine 的非等价测量被误用到普通任务上。
    try:
        agent_solve(t, _ScriptedClient([json.dumps({"action": "finish"})]),
                    max_steps=2, tools="engine")
    except ValueError as e:
        assert "executable_artifact" in str(e), e
        print("agentsolve 自测⑤engine 档对非可执行任务拒绝 OK")
    else:
        raise AssertionError("engine 工具集居然在数值型任务上放行了")
    return True


if __name__ == "__main__":
    _selftest()


