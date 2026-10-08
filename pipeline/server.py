"""本地 Web 控制台：用 Python 标准库把产线包成可视化交互页面（零三方依赖、免构建）。

与老管线的 React+Vite dashboard 同类，但本产线引擎是 Python（判分/harness 需执行 main.py），
故用「Python 后端 + 原生前端」：后端直接复用 pipeline 各模块，前端是单页原生 JS。

启动：python3 -m pipeline.server   浏览器打开 http://127.0.0.1:8765
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import generate, harness, scaffold, occupations as occ
from .config import PipelineConfig
from .cost import CostTracker
from .llm import get_client
from .orchestrator import blind_batch, qc_batch
from .tasks import discover_tasks

CFG = PipelineConfig()
WEB = os.path.join(os.path.dirname(__file__), "web")


def _task_by_id(tid):
    for t in discover_tasks(CFG.tasks_dir):
        if t.task_id == tid:
            return t
    return None


def _selected(body):
    if body.get("task_id"):
        t = _task_by_id(body["task_id"])
        return [t] if t else []
    return discover_tasks(CFG.tasks_dir)


def _tasks_payload():
    out = []
    for t in discover_tasks(CFG.tasks_dir):
        c = t.card
        out.append({"task_id": t.task_id, "dir": os.path.relpath(t.dir, CFG.tasks_dir),
                    "领域": c.get("领域", ""), "任务名": c.get("任务名", ""),
                    "难度层": c.get("难度层", ""), "prompt": c.get("prompt", "")})
    return out


def _runs_payload():
    root = CFG.runs_dir
    out = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, name)
            if os.path.isdir(d):
                out.append({"run_id": name, "files": sorted(os.listdir(d))})
    return out


def _stats_payload():
    """汇总 runs/ 下历次报告，供看板出图：QC 可进库、盲解基线通过率、累计成本。"""
    root = CFG.runs_dir
    qc, blind, cost_total = [], [], 0.0
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            qf = os.path.join(d, "qc.json")
            if os.path.exists(qf):
                j = json.load(open(qf, encoding="utf-8"))
                qc.append({"run_id": name, "任务数": j.get("任务数", 0),
                           "可进库": j.get("可进库", 0)})
            bf = os.path.join(d, "blind.json")
            if os.path.exists(bf):
                j = json.load(open(bf, encoding="utf-8"))
                blind.append({"run_id": name, "任务数": j.get("任务数", 0),
                              "基线通过率": j.get("基线通过率", 0), "成本RMB": j.get("成本RMB", 0)})
            for fn in os.listdir(d):
                if fn.startswith("cost_") and fn.endswith(".json"):
                    cost_total += json.load(open(os.path.join(d, fn), encoding="utf-8")).get("total_rmb", 0)
    return {"qc": qc, "blind": blind, "cost_total_rmb": round(cost_total, 6),
            "cost_note": cost.COST_NOTE,
            "bill_url": cost.BILL_URL}


def _latest_qc_map():
    """取最近一次 runs/*/qc.json 的 task_id→进库，供看板显示真实进库状态（截至上次 qc 跑）。"""
    root = CFG.runs_dir
    if not os.path.isdir(root):
        return {}, None
    for name in sorted(os.listdir(root), reverse=True):
        qf = os.path.join(root, name, "qc.json")
        if os.path.exists(qf):
            j = json.load(open(qf, encoding="utf-8"))
            m = {}
            for r in j.get("结果", []):
                tid = r.get("task_id") or (r.get("N6") or {}).get("task_id")
                if tid:
                    m[tid] = r.get("进库")
            return m, name
    return {}, None


def _board_payload():
    """进度看板：每条任务的负责人 / 上次 qc 进库 / N7 两档 / scoringBasis / 夹具档数（全读 task_card，不现跑）。"""
    qc_map, qc_run = _latest_qc_map()
    rows = []
    for t in discover_tasks(CFG.tasks_dir):
        c = t.card
        ev = c.get("evaluation", {}) or {}
        md = [{"model": e.get("model", ""), "fullPassRate": e.get("fullPassRate"),
               "meanScore": e.get("meanScore")} for e in (c.get("measuredDifficulty") or [])]
        rows.append({
            "task_id": t.task_id, "领域": c.get("领域", ""), "任务名": c.get("任务名", ""),
            "负责人": c.get("负责人") or (c.get("元数据") or {}).get("负责人") or "",
            "qc进库": qc_map.get(t.task_id),
            "N7": md,
            "scoringBasis": len(c.get("scoringBasis") or []),
            "夹具档数": len(ev.get("fixtureScores") or {}),
        })
    return {"qc_run": qc_run, "tasks": rows}


def _config_payload():
    """当前 LLM 配置（key 脱敏，只回是否已设置与尾 4 位）。"""
    k = CFG.llm.api_key or ""
    return {"provider": CFG.llm.provider, "model": CFG.llm.model,
            "api_base": CFG.llm.api_base, "key_set": bool(k),
            "key_tail": ("****" + k[-4:]) if k else "",
            "verify_ssl": CFG.llm.verify_ssl,
            "concurrency": CFG.concurrency, "pass_threshold": CFG.pass_threshold}


def _asset_path(task, rel):
    """把相对路径安全解析到 <task>/assets 下，防目录穿越。"""
    base = os.path.abspath(os.path.join(task.dir, "assets"))
    p = os.path.abspath(os.path.join(base, rel))
    return p if (p == base or p.startswith(base + os.sep)) else None


def _task_files_payload(task):
    """列出任务 assets/ 下所有文件（相对 assets），文本文件带内容、可编辑。"""
    base = os.path.join(task.dir, "assets")
    files = []
    for root, _, names in os.walk(base):
        for n in sorted(names):
            if n == ".DS_Store":
                continue
            full = os.path.join(root, n)
            rel = os.path.relpath(full, base)
            try:
                content = open(full, encoding="utf-8").read()
                editable = True
            except (UnicodeDecodeError, OSError):
                content, editable = "", False  # 二进制/不可读
            files.append({"rel": rel.replace(os.sep, "/"), "content": content, "editable": editable})
    return {"task_id": task.task_id, "files": files}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html")
        if p.path == "/api/tasks":
            return self._send(200, _tasks_payload())
        if p.path == "/api/runs":
            return self._send(200, _runs_payload())
        if p.path == "/api/stats":
            return self._send(200, _stats_payload())
        if p.path == "/api/board":
            return self._send(200, _board_payload())
        if p.path == "/api/config":
            return self._send(200, _config_payload())
        if p.path == "/api/occupations":
            return self._send(200, occ.load()["positions_798"])
        if p.path == "/api/task":
            t = _task_by_id(parse_qs(p.query).get("task_id", [""])[0])
            if not t:
                return self._send(404, {"error": "任务不存在"})
            return self._send(200, _task_files_payload(t))
        if p.path == "/api/run":
            rid = parse_qs(p.query).get("id", [""])[0]
            d = os.path.join(CFG.runs_dir, rid)
            res = {}
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    if fn.endswith(".json"):
                        res[fn] = json.load(open(os.path.join(d, fn), encoding="utf-8"))
            return self._send(200, res)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        path = urlparse(self.path).path
        try:
            if path == "/api/run":
                t = _task_by_id(body.get("task_id"))
                if not t:
                    return self._send(404, {"error": "任务不存在"})
                solver = harness.golden_solver if body.get("solver") == "golden" else harness.empty_solver
                r = harness.run_task(t, solver=solver)
                return self._send(200, {"task_id": r.task_id, "score": r.score,
                                        "passed": r.passed, "details": r.details, "errors": r.errors})
            if path == "/api/qc":
                return self._send(200, qc_batch(_selected(body), CFG))
            if path == "/api/blind":
                return self._send(200, blind_batch(_selected(body), get_client(CFG.llm), CFG))
            if path == "/api/scaffold":
                spec = body.get("spec", body)
                return self._send(200, scaffold.scaffold_task(spec, CFG.tasks_dir))
            if path == "/api/select":
                occupation = body.get("occupation", "").strip()
                if not occupation:
                    return self._send(400, {"error": "缺少 occupation"})
                cost = CostTracker()
                _, sel = generate.n1_select(get_client(CFG.llm), occupation,
                                            int(body.get("n", 10)), cost)
                return self._send(200, {"occupation": occupation, "faces": sel["faces"],
                                        "provider": CFG.llm.provider, "成本RMB": cost.total(),
                                        "未计价模型": cost.unpriced_models()})
            if path == "/api/task_file":
                t = _task_by_id(body.get("task_id", ""))
                if not t:
                    return self._send(404, {"error": "任务不存在"})
                dst = _asset_path(t, body.get("rel", ""))
                if not dst or (".." in body.get("rel", "")):
                    return self._send(400, {"error": "非法路径"})
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(body.get("content", ""))
                return self._send(200, {"ok": True, "rel": body.get("rel")})
            if path == "/api/sync_schema":
                t = _task_by_id(body.get("task_id", ""))
                if not t:
                    return self._send(404, {"error": "任务不存在"})
                return self._send(200, scaffold.sync_schema(t.dir))
            if path == "/api/config":
                for f in ("provider", "model", "api_base", "api_key"):
                    if body.get(f) is not None and body.get(f) != "":
                        setattr(CFG.llm, f, body[f])
                if "verify_ssl" in body:
                    CFG.llm.verify_ssl = bool(body["verify_ssl"])
                return self._send(200, _config_payload())
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})


def main():
    port = int(os.environ.get("ALE_PORT", "8765"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("ALE 控制台已启动: http://127.0.0.1:%d" % port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
