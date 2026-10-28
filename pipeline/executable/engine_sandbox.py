"""即用即弃 Flowable+PostgreSQL 沙箱（判分契约 §9.5 agent-file-engine 不可信自测）。

为什么要它：共享长驻引擎（ale-flowable-smoke）上，不同部署只靠 deployment-id cascade
删除隔开——那只是生命周期清理，不是安全隔离：恶意 BPMN 经 service task / 脚本 / 共享库
仍能读改别的部署。给不可信 agent 开自测，必须把爆炸半径收进一套一次性引擎+库里。

本模块每次起一套独立栈（独立 compose project / bridge 网络 / PG 数据卷 / 随机 loopback
端口 / 随机口令），就绪后交出一个指向它的 FlowableAdapter；用完 `compose down -v` 连卷
一起销毁。任何环节起不来 / 超时 → 抛 EngineUnavailable（基建故障，记测量故障、不记成
agent 做错）。close() 幂等，析构与异常路径都兜底销毁，绝不残留跑着的不可信引擎。
"""
import os
import secrets
import socket
import subprocess
import time

from ..dockercli import docker_bin
from .engines import get_adapter
from .engines.base import EngineUnavailable

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = os.path.join(HERE, "sandbox", "docker-compose.yml")


def _free_loopback_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class DisposableFlowable:
    """一次性 Flowable 栈。用作上下文管理器或手动 start()/close()。

    start() 返回 {"base","user","password","container","project","port"}；
    adapter() 返回一个已指向本沙箱的 FlowableAdapter。
    """

    def __init__(self, ready_timeout=None, up_timeout=None,
                 flowable_image=None, pg_image=None):
        self.project = "ale-engine-sbx-" + secrets.token_hex(6)
        self.port = _free_loopback_port()
        self._admin_pw = secrets.token_hex(16)
        self._db_pw = secrets.token_hex(16)
        self.user = os.getenv("FLOWABLE_USER", "rest-admin")
        self.ready_timeout = float(ready_timeout or os.getenv("SANDBOX_READY_TIMEOUT", "240"))
        self.up_timeout = float(up_timeout or os.getenv("SANDBOX_UP_TIMEOUT", "180"))
        self.flowable_image = flowable_image or os.getenv("SANDBOX_FLOWABLE_IMAGE",
                                                          "ale/flowable-rest:6.5.0-local")
        self.pg_image = pg_image or os.getenv("SANDBOX_PG_IMAGE", "docker.io/library/postgres:13")
        self.base = "http://127.0.0.1:%d/flowable-rest/service/" % self.port
        self.container = self.project + "-flowable-rest-1"
        self._started = False
        self._closed = False

    # ---- 注入给 compose 的环境（口令只在进程内存里，不落盘）----
    def _env(self):
        env = dict(os.environ)
        env.update({
            "FLOWABLE_HOST_PORT": str(self.port),
            "FLOWABLE_DB_PASSWORD": self._db_pw,
            "FLOWABLE_PASSWORD": self._admin_pw,
            "FLOWABLE_USER": self.user,
            "SANDBOX_FLOWABLE_IMAGE": self.flowable_image,
            "SANDBOX_PG_IMAGE": self.pg_image,
        })
        return env

    def _compose(self, *args, timeout=60, check=True):
        cmd = [docker_bin(), "compose", "-p", self.project, "-f", COMPOSE, *args]
        try:
            r = subprocess.run(cmd, env=self._env(), capture_output=True, text=True,
                               timeout=timeout)
        except FileNotFoundError as e:
            raise EngineUnavailable("docker compose 不可用：%s" % e)
        except subprocess.TimeoutExpired:
            raise EngineUnavailable("docker compose %s 超时(%ss)" % (args[0], timeout))
        if check and r.returncode != 0:
            raise EngineUnavailable("docker compose %s 失败：%s"
                                    % (args[0], (r.stderr or r.stdout)[:400]))
        return r

    def start(self):
        if self._started:
            return self._conn()
        try:
            self._compose("up", "-d", timeout=self.up_timeout)
            self._started = True   # 一旦起过就标记，确保 close 一定执行销毁
            self._await_ready()
        except Exception:
            self.close()
            raise
        return self._conn()

    def _await_ready(self):
        """轮询 REST 直到引擎应答或超时；超时附上 compose 日志尾巴便于诊断。"""
        adapter = self.adapter()
        deadline = time.monotonic() + self.ready_timeout
        last = ""
        while True:
            try:
                adapter.probe()
                return
            except EngineUnavailable as exc:
                last = str(exc)
                if time.monotonic() >= deadline:
                    logs = ""
                    try:
                        logs = self._compose("logs", "--tail", "30", "flowable-rest",
                                             timeout=30, check=False).stdout[-800:]
                    except EngineUnavailable:
                        pass
                    raise EngineUnavailable("沙箱引擎就绪超时(%ss)：%s\n--- flowable 日志尾 ---\n%s"
                                            % (self.ready_timeout, last, logs))
                time.sleep(3)

    def adapter(self):
        """指向本沙箱的 FlowableAdapter（显式传 base/口令，不去 docker inspect 共享栈）。"""
        return get_adapter("flowable", base=self.base, container=self.container,
                           user=self.user, password=self._admin_pw)

    def _conn(self):
        return {"base": self.base, "user": self.user, "password": self._admin_pw,
                "container": self.container, "project": self.project, "port": self.port}

    def close(self):
        if self._closed:
            return
        self._closed = True
        if not self._started:
            # 可能 up 中途失败残留部分容器，仍尝试销毁一次
            pass
        try:
            self._compose("down", "-v", "--remove-orphans", "-t", "10",
                          timeout=120, check=False)
        except EngineUnavailable:
            pass   # 销毁本身失败不再抛（调用方已在清理路径上）；残留由下次 prune 兜底

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False
