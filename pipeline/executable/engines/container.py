"""容器跑脚本适配器（源自 E1 的 score_outputs.py + container_runner.py）。

把候选交付脚本丢进 `docker run --network none` 的隔离容器，对所有隐藏 case
跑一遍、逐例比对输出。一个 case = 一个隐藏输入样例。

与 Flowable 适配器的差异：容器类一次 docker 调用就把全部 case 跑完（起容器开销大，
不按 case 反复起），所以 deploy 阶段就批量执行、缓存逐例结果，run_case 只做查表。
这样仍贴合 base 的 per-case 接口，又不浪费容器启动开销。

基建故障（docker 不可用 / 镜像缺失 / 容器超时 / 执行器没吐合法 JSON）抛 EngineUnavailable。
"""
import json
import os
import shutil
import subprocess
import tempfile

from ...dockercli import docker_bin
from .base import EngineAdapter, EngineUnavailable, CaseResult, DeployResult

DEFAULT_IMAGE = "python:3.12-slim"
# 单次调用的默认参数：与扩展前的行为一致（只把会话目录传给交付脚本）
DEFAULT_INVOCATIONS = [{"name": "main", "args": ["{session}"]}]


def _render(args, mapping):
    """把 invocation 的参数模板展开成容器内路径（{session}/{out}/{case}）。"""
    out = []
    for a in args:
        s = str(a)
        for k, v in mapping.items():
            s = s.replace("{%s}" % k, v)
        out.append(s)
    return out


def _call_scorer(module, got, expected, session, runs, artifacts):
    """兼容两代 host_scorer 签名。

    老签名 `score_case(got, expected, session)` 继续可用（got = 首次调用的 stdout JSON）；
    新签名可声明 `runs` / `artifacts` 形参，拿到全部调用的输出与产物目录做隐藏校验。
    """
    import inspect
    try:
        params = inspect.signature(module.score_case).parameters
    except (TypeError, ValueError):       # 内建/C 实现拿不到签名，按老签名调
        return module.score_case(got, expected, session)
    kwargs = {}
    if "runs" in params:
        kwargs["runs"] = runs
    if "artifacts" in params:
        kwargs["artifacts"] = artifacts
    return module.score_case(got, expected, session, **kwargs)


class ContainerAdapter(EngineAdapter):
    name = "container"

    def __init__(self, image=None, runner=None, timeout=900, **_):
        self.image = image or os.getenv("EXEC_CONTAINER_IMAGE", DEFAULT_IMAGE)
        # 容器内执行器脚本路径（由 task_meta 指定；缺省用任务 scripts/container_runner.py）
        self.runner = runner
        self.timeout = int(os.getenv("EXEC_CONTAINER_TIMEOUT", str(timeout)))
        self._work = None

    def probe(self):
        try:
            v = subprocess.run([docker_bin(), "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise EngineUnavailable("docker 调用失败：%s" % e)
        if v.returncode != 0:
            raise EngineUnavailable("docker 不可用：%s" % (v.stderr or v.stdout)[:300])
        return {"docker": (v.stdout or "").strip(), "image": self.image}

    def deploy(self, output_dir, task_meta):
        """把交付脚本丢进隔离容器执行。

        `host_scorer` 模式逐 case 执行：容器只挂载 session，expected 留在宿主机，
        由可信 host scorer 独立比较，候选永远看不到标准答案。旧 runner 模式保持兼容。
        """
        if task_meta.get("host_scorer"):
            return self._deploy_host_scored(output_dir, task_meta)
        deliver = task_meta.get("deliverable", "audit.py")
        sol = os.path.join(output_dir, deliver)
        if not os.path.isfile(sol):
            return DeployResult(False, detail={"error": "缺交付 %s" % deliver})
        cases_dir = task_meta.get("cases_dir")
        runner = self.runner or task_meta.get("container_runner")
        if not cases_dir or not os.path.isdir(cases_dir):
            raise EngineUnavailable("隐藏 cases 目录缺失：%s" % cases_dir)
        if not runner or not os.path.isfile(runner):
            raise EngineUnavailable("容器执行器缺失：%s" % runner)

        work = tempfile.mkdtemp(prefix="exec_container_")
        self._work = work
        os.makedirs(os.path.join(work, "solution"))
        shutil.copy(sol, os.path.join(work, "solution", deliver))
        shutil.copytree(cases_dir, os.path.join(work, "cases"))
        shutil.copy(runner, os.path.join(work, "container_runner.py"))

        cmd = [docker_bin(), "run", "--rm", "--network", "none",
               "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
               "--security-opt", "no-new-privileges",
               "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-v", "%s:/work:ro" % work, self.image,
               "python", "/work/container_runner.py"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise EngineUnavailable("容器超时(>%ss)" % self.timeout)
        if r.returncode != 0 and not r.stdout.strip():
            raise EngineUnavailable("容器异常退出码 %d：%s" % (r.returncode, (r.stderr or r.stdout)[:500]))
        try:
            data = json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            raise EngineUnavailable("容器未输出合法 JSON：%s" % (r.stdout or r.stderr)[:500])
        if not data.get("ok", False):
            raise EngineUnavailable("执行器故障：%s" % data.get("fault", "未知"))
        # 缓存逐例结果供 run_case 查表
        by_case = {c["name"]: c for c in data.get("cases", [])}
        return DeployResult(True, handle={"by_case": by_case, "work": work},
                            detail={"image": self.image, "ran_cases": len(by_case)})

    def _deploy_host_scored(self, output_dir, task_meta):
        """只挂载候选和单 case session；trusted scorer/expected 永不进入容器。

        支持两种交付面：
          ① 单次调用 + stdout（默认，与扩展前完全一致）；
          ② 多次调用 + 产物目录：`task_meta.invocations` 给每个 case 要跑的若干次调用
             （参数模板 {session}/{out}/{case}，考生看不到清单），`task_meta.artifact_dir`
             给一个容器内可写目录名，跑完由宿主侧 scorer 对产物跑隐藏校验。
        """
        import importlib.util
        from pathlib import Path
        import uuid

        deliver = task_meta.get("deliverable", "audit.py")
        sol = Path(output_dir) / deliver
        if not sol.is_file() or sol.is_symlink():
            return DeployResult(False, detail={"error": "缺交付或交付是符号链接"})
        cases_dir = Path(task_meta["cases_dir"])
        scorer_path = Path(task_meta["host_scorer"])
        if not cases_dir.is_dir() or not scorer_path.is_file():
            raise EngineUnavailable("宿主判分资源缺失")
        spec = importlib.util.spec_from_file_location("trusted_host_scorer", scorer_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        invocations = task_meta.get("invocations") or DEFAULT_INVOCATIONS
        if not isinstance(invocations, list) or not all(
                isinstance(i, dict) and i.get("args") for i in invocations):
            raise EngineUnavailable("invocations 配置非法（须是 [{name,args}] 列表）")
        artifact_dirname = task_meta.get("artifact_dir")
        fsize = int(task_meta.get("max_artifact_bytes", 1048576))
        # 不执行候选 import；仅导入作者控制的可信 scorer。
        by_case = {}
        for case in sorted(cases_dir.iterdir()):
            if not case.is_dir():
                continue
            expected = json.loads((case / "expected.json").read_text())
            session = case / "session"
            if not session.is_dir() or any(p.is_symlink() for p in session.rglob('*')):
                raise EngineUnavailable("session 缺失或含符号链接")
            with tempfile.TemporaryDirectory(prefix="exec_public_") as work:
                public = Path(work)
                shutil.copytree(session, public / 'session')
                (public / 'solution').mkdir()
                shutil.copyfile(sol, public / 'solution' / 'audit.py')
                for p in public.rglob('*'):
                    p.chmod(0o755 if p.is_dir() else 0o644)
                public.chmod(0o755)
                arts = None
                if artifact_dirname:
                    # 产物目录建在 public 之外、rw 挂载盖在只读根上；挂载点要先在 public 里
                    # 存在（只读 rootfs 上 docker 没法替我们 mkdir 挂载点）
                    (public / artifact_dirname).mkdir(exist_ok=True)
                    (public / artifact_dirname).chmod(0o755)
                    arts = Path(work + "_out")
                    arts.mkdir(parents=True, exist_ok=True)
                    arts.chmod(0o777)
                runs, record = self._run_invocations(
                    public, arts, artifact_dirname, invocations, task_meta, case.name, fsize)
                if record is None:
                    first = next((r for r in runs.values() if r.get("json") is not None), None)
                    got = first["json"] if first else None
                    if got is None and not artifact_dirname:
                        record = {'score': 0.0, 'passed': False, 'err': '候选未输出合法 JSON'}
                    else:
                        record = _call_scorer(module, got, expected, str(session),
                                              runs, str(arts) if arts else None)
                if arts is not None:
                    shutil.rmtree(arts, ignore_errors=True)
                by_case[case.name] = dict(record, name=case.name)
        if not by_case:
            raise EngineUnavailable('隐藏 cases 为空')
        return DeployResult(True, handle={'by_case': by_case},
                            detail={'image': self.image, 'ran_cases': len(by_case),
                                    'expected_mount': False, 'host_scored': True,
                                    'invocations': [i.get("name") for i in invocations],
                                    'artifact_dir': bool(artifact_dirname)})

    def _run_invocations(self, public, arts, artifact_dirname, invocations, task_meta,
                         case_name, fsize):
        """按清单逐次起容器跑候选产物。返回 (runs, 失败 record 或 None)。"""
        import uuid
        max_bytes = int(task_meta.get('max_output_bytes', 1048576))
        mapping = {"session": "/work/session", "case": case_name,
                   "out": "/work/" + (artifact_dirname or "out")}
        runs = {}
        for inv in invocations:
            name = 'ale-candidate-' + uuid.uuid4().hex
            cmd = [docker_bin(), 'run', '--rm', '--name', name, '--network', 'none',
                   '--user', '65534:65534', '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--memory', '512m',
                   '--cpus', '1', '--pids-limit', '64',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
                   '--ulimit', 'fsize=%d:%d' % (fsize, fsize),
                   '-e', 'OPENBLAS_NUM_THREADS=1', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                   '-v', str(public) + ':/work:ro']
            if arts is not None:
                cmd += ['-v', '%s:%s:rw' % (arts, mapping["out"])]
            cmd += ['--entrypoint', 'python', self.image, '/work/solution/audit.py']
            cmd += _render(inv["args"], mapping)
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                try:
                    result = subprocess.run(cmd, stdout=stdout, stderr=stderr,
                                            timeout=int(task_meta.get('case_timeout', 60)))
                    stdout.seek(0); stderr.seek(0)
                    raw = stdout.read(max_bytes + 1)
                    err = stderr.read(2000).decode(errors='replace')
                    if result.returncode in (125, 126, 127):
                        raise EngineUnavailable('docker 执行环境错误：' + err)
                    if result.returncode:
                        return runs, {'score': 0.0, 'passed': False,
                                      'err': '候选调用 %s 异常退出：%s' % (inv.get("name"), err)}
                    if len(raw) > max_bytes:
                        return runs, {'score': 0.0, 'passed': False,
                                      'err': '候选调用 %s 输出超过 %d 字节' % (inv.get("name"), max_bytes)}
                    try:
                        parsed = json.loads(raw)
                    except (ValueError, UnicodeError):
                        parsed = None
                    runs[inv.get("name") or str(len(runs))] = {
                        "json": parsed, "stdout": raw[:4096].decode(errors='replace'),
                        "args": _render(inv["args"], mapping)}
                except subprocess.TimeoutExpired:
                    return runs, {'score': 0.0, 'passed': False,
                                  'err': '候选调用 %s 执行超时' % inv.get("name")}
                finally:
                    subprocess.run([docker_bin(), 'rm', '-f', name],
                                   capture_output=True, timeout=20)
        return runs, None

    def run_case(self, handle, case, task_meta):
        cid = case["id"] if isinstance(case, dict) else case
        rec = handle["by_case"].get(cid)
        if rec is None:
            return CaseResult(cid, False, {"reasons": ["容器未产出该 case 结果"]})
        return CaseResult(cid, bool(rec.get("passed")),
                          {"score": rec.get("score"), "err": rec.get("err"),
                           "info": rec.get("info")})

    def teardown(self, handle):
        work = (handle or {}).get("work") if isinstance(handle, dict) else self._work
        if work:
            shutil.rmtree(work, ignore_errors=True)
