"""Flowable REST 引擎适配器（源自 G1 的 scripts/engine.py）。

把 BPMN 候选定义部署到真实 Flowable 6.5.0，按场景起实例、驱动 userTask、
推进短时 timer 副本、读引擎历史，独立重算每个场景是否通过。

一个 case = 一个 test_scenario。CaseResult.passed 由引擎历史重算，
不采信候选自报的 test_results.json（交叉核对在 runner 侧做）。

基建故障（引擎不可达 / 认证失败 / timer 墙钟观测超时）抛 EngineUnavailable。
"""
import base64
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from ...dockercli import docker_bin
from .base import EngineAdapter, EngineUnavailable, CaseResult, DeployResult

DEFAULT_BASE = "http://127.0.0.1:18080/flowable-rest/service/"
DEFAULT_CONTAINER = "ale-flowable-smoke-flowable-rest-1"


def _infra(exc):
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError,
                            http.client.RemoteDisconnected, http.client.BadStatusLine))


class FlowableAdapter(EngineAdapter):
    name = "flowable"

    def __init__(self, base=None, container=None, user=None, password=None, **_):
        self.base = base or os.getenv("FLOWABLE_BASE", DEFAULT_BASE)
        self.container = container or os.getenv("FLOWABLE_CONTAINER", DEFAULT_CONTAINER)
        self.user = user or os.getenv("FLOWABLE_USER", "rest-admin")
        self._password = password or os.getenv("FLOWABLE_PASSWORD")
        self._auth = None
        self.role_vars = [
            "coldChainTeamLead", "opsCoordinator", "keyDecisionMaker", "seniorLead",
            "complianceOfficer", "peerCategoryReviewer", "logisticsCoordinator", "coldChainOwner",
        ]
        self.auto_complete = {"logistics_capacity_confirmation"}

    # ---- 认证：优先环境变量，退路是读本机已有容器的 env（绝不落盘/打印密钥）----
    def _authorization(self):
        if self._auth is None:
            password = self._password
            if password is None:
                import subprocess
                try:
                    raw = subprocess.check_output(
                        [docker_bin(), "inspect", "--format", "{{json .Config.Env}}", self.container],
                        stderr=subprocess.DEVNULL, text=True, timeout=10)
                    env = dict(v.split("=", 1) for v in json.loads(raw) if "=" in v)
                    password = env["FLOWABLE_REST_APP_ADMIN_PASSWORD"]
                except Exception:
                    raise EngineUnavailable("credentials unavailable; set FLOWABLE_PASSWORD") from None
            self._auth = base64.b64encode((self.user + ":" + password).encode()).decode()
        return self._auth

    def _api(self, path, body=None, method=None, raw=None, content_type=None, timeout=20):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(
            self.base + path, data=data, method=method or ("POST" if data is not None else "GET"),
            headers={"Authorization": "Basic " + self._authorization(),
                     "Content-Type": content_type or "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return json.loads(b) if b else None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 502, 503, 504) or path.startswith("management/engine"):
                raise EngineUnavailable("REST infra/auth HTTP " + str(e.code)) from None
            body_txt = ""
            try:
                body_txt = e.read().decode(errors="replace")
            except Exception:
                pass
            raise RuntimeError("HTTP %s on %s: %s" % (e.code, path, body_txt[:500]))
        except Exception as exc:
            if _infra(exc):
                raise EngineUnavailable(str(exc))
            raise

    def _rows(self, path, **params):
        params["size"] = 500
        resp = self._api(path + ("?" + urllib.parse.urlencode(params) if params else ""))
        if isinstance(resp, dict) and "data" in resp:
            if resp.get("total", len(resp["data"])) > 500:
                raise EngineUnavailable("evidence pagination exceeds verified limit")
            return resp["data"]
        return resp

    # ---- 生命周期 ----
    def probe(self):
        timeout = float(os.getenv("FLOWABLE_READY_TIMEOUT", "60"))
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self._api("management/engine")
            except EngineUnavailable:
                if time.monotonic() >= deadline:
                    raise EngineUnavailable("REST readiness timeout (%ss)" % timeout)
                time.sleep(3)

    def _deploy_xml(self, xml_bytes, filename):
        boundary = "exb" + str(int(time.time() * 1000))
        raw = (("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\""
                + filename + ".bpmn20.xml\"\r\nContent-Type: application/xml\r\n\r\n").encode()
               + xml_bytes + ("\r\n--" + boundary + "--\r\n").encode())
        d = self._api("repository/deployments", raw=raw,
                      content_type="multipart/form-data; boundary=" + boundary)
        defs = self._rows("repository/process-definitions", deploymentId=d["id"])
        return d["id"], defs

    def deploy(self, output_dir, task_meta):
        bpmn = os.path.join(output_dir, task_meta.get("bpmn_file", "modified_process.bpmn20.xml"))
        if not os.path.isfile(bpmn):
            return DeployResult(False, detail={"error": "缺交付 %s" % os.path.basename(bpmn)})
        xml_bytes = open(bpmn, "rb").read()
        created = []
        # 保留 original 定义（如提供且引擎上没有）
        orig = task_meta.get("original_bpmn")
        orig_key = task_meta.get("original_key")
        if orig and orig_key and os.path.isfile(orig):
            if not self._rows("repository/process-definitions", key=orig_key):
                od, _ = self._deploy_xml(open(orig, "rb").read(), "orig")
                created.append(od)
        try:
            dep_id, defs = self._deploy_xml(xml_bytes, "candidate")
        except EngineUnavailable:
            raise
        except RuntimeError as exc:
            # schema 不合法 / 部署被引擎拒 = agent 侧问题，非故障
            for c in created:
                self._delete(c)
            return DeployResult(False, detail={"error": str(exc)})
        created.append(dep_id)
        if len(defs) != 1:
            for c in created:
                self._delete(c)
            return DeployResult(False, detail={"definitions": [d.get("key") for d in defs]})
        # 短时 timer 副本（可执行类 timer 真实触发验证；不声称验证原时长流逝）
        short_def = None
        tcfg = task_meta.get("timer")
        if tcfg and tcfg.get("deployed_duration") and tcfg.get("test_duration"):
            dd, td = tcfg["deployed_duration"].encode(), tcfg["test_duration"].encode()
            if dd in xml_bytes:
                sdep, sdefs = self._deploy_xml(xml_bytes.replace(dd, td), "candidate_short")
                created.append(sdep)
                short_def = sdefs[0]["id"] if sdefs else None
        return DeployResult(True, handle={"definition": defs[0]["id"], "short": short_def,
                                          "deployments": created},
                            detail={"deploymentId": dep_id, "definitionId": defs[0]["id"],
                                    "key": defs[0]["key"], "short_definitionId": short_def})

    def _delete(self, dep_id):
        try:
            self._api("repository/deployments/" + dep_id + "?cascade=true", method="DELETE")
        except Exception:
            pass

    def teardown(self, handle):
        for dep in (handle or {}).get("deployments", []) if isinstance(handle, dict) else []:
            self._delete(dep)

    # ---- 单场景驱动（engine-recomputed）----
    def run_case(self, handle, case, task_meta):
        from . import flowable_drive
        definition = handle["short"] if (case.get("advance_timer") and handle.get("short")) else handle["definition"]
        if case.get("advance_timer") and not handle.get("short"):
            return CaseResult(case["id"], False, {"reasons": ["no short timer copy available"]})
        ev = flowable_drive.run_scenario(self, definition, case, task_meta)
        return CaseResult(case["id"], bool(ev.get("pass")), ev)
