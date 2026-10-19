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

from .base import EngineAdapter, EngineUnavailable, CaseResult, DeployResult

DEFAULT_IMAGE = "python:3.12-slim"


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
            v = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
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

        cmd = ["docker", "run", "--rm", "--network", "none",
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
        """只挂载候选和单 case session；trusted scorer/expected 永不进入容器。"""
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
                name = 'ale-candidate-' + uuid.uuid4().hex
                cmd = ['docker', 'run', '--rm', '--name', name, '--network', 'none',
                       '--user', '65534:65534', '--read-only', '--cap-drop', 'ALL',
                       '--security-opt', 'no-new-privileges', '--memory', '512m',
                       '--cpus', '1', '--pids-limit', '64', '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
                       '--ulimit', 'fsize=1048576:1048576',
                       '-e', 'OPENBLAS_NUM_THREADS=1', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                       '-v', str(public) + ':/work:ro', '--entrypoint', 'python', self.image,
                       '/work/solution/audit.py', '/work/session']
                # 日志落临时文件，限制文件大小，不在宿主内存中无限 capture_output。
                with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                    try:
                        result = subprocess.run(cmd, stdout=stdout, stderr=stderr,
                                                timeout=int(task_meta.get('case_timeout', 60)))
                        stdout.seek(0); stderr.seek(0)
                        raw = stdout.read(1048577)
                        err = stderr.read(2000).decode(errors='replace')
                        if result.returncode in (125, 126, 127):
                            raise EngineUnavailable('docker 执行环境错误：' + err)
                        if result.returncode:
                            record = {'score': 0.0, 'passed': False, 'err': '候选异常退出：' + err}
                        elif len(raw) > 1048576:
                            record = {'score': 0.0, 'passed': False, 'err': '候选输出超过 1MiB'}
                        else:
                            try:
                                got = json.loads(raw)
                            except (ValueError, UnicodeError):
                                record = {'score': 0.0, 'passed': False, 'err': '候选未输出合法 JSON'}
                            else:
                                record = module.score_case(got, expected, str(session))
                    except subprocess.TimeoutExpired:
                        record = {'score': 0.0, 'passed': False, 'err': '候选执行超时'}
                    finally:
                        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
                by_case[case.name] = dict(record, name=case.name)
        if not by_case:
            raise EngineUnavailable('隐藏 cases 为空')
        return DeployResult(True, handle={'by_case': by_case},
                            detail={'image': self.image, 'ran_cases': len(by_case),
                                    'expected_mount': False, 'host_scored': True})

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
