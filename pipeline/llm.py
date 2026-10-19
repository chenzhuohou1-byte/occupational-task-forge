"""LLM 客户端抽象：可插拔 provider，支持离线 mock。

真实调用走 OpenAI 兼容 HTTP 接口，仅用标准库 urllib（无三方依赖）。
无网络 / 联调产线控制流时用 MockLLMClient。
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


def _ssl_context(verify=True):
    """构造 SSL 上下文。python.org 版 Python 默认找不到系统 CA，故优先用 certifi 的证书包；
    内网自签网关可把 verify 关掉。"""
    if not verify:
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    reasoning_tokens: int = 0


class LLMClient:
    def chat(self, messages, **kw) -> "LLMResult":
        raise NotImplementedError


class MockLLMClient(LLMClient):
    """离线桩：不产生真实内容，仅回可解析的占位 JSON，用于跑通控制流与成本统计。

    可注入 scripted={关键字: 文本} 让特定节点返回预设内容（便于本地演示/测试）。
    """
    def __init__(self, model="mock", scripted=None):
        self.model = model
        self.scripted = scripted or {}

    def chat(self, messages, **kw):
        last = messages[-1]["content"] if messages else ""
        for key, val in self.scripted.items():
            if key in last:
                return LLMResult(text=val, prompt_tokens=len(last) // 4,
                                 completion_tokens=len(val) // 4, model=self.model)
        text = json.dumps({"mock": True, "note": "无真实LLM，占位输出"}, ensure_ascii=False)
        return LLMResult(text=text, prompt_tokens=len(last) // 4,
                         completion_tokens=8, model=self.model)


class OpenAICompatClient(LLMClient):
    def __init__(self, cfg):
        self.cfg = cfg
        # 优先用页面/配置里填入的 key，其次回退到环境变量
        self.api_key = getattr(cfg, "api_key", "") or os.environ.get(cfg.api_key_env, "")

    def chat(self, messages, **kw):
        payload = {
            "model": self.cfg.model, "messages": messages,
            "temperature": kw.get("temperature", self.cfg.temperature),
        }
        # 不显式给 max_tokens 时，部分 OpenAI 兼容网关的默认上限很低（常见 4096/8000），
        # 推理型模型会把预算全烧在 reasoning 上、content 返空 → 盲解写出 0 文件、被静默记 0 分。
        # 故显式给足；默认 16000。注意：预算给太大时某些网关会因生成时间过长而 504。
        max_tokens = int(os.environ.get("ALE_LLM_MAX_TOKENS", "16000"))
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        # 不少「始终思考」型模型不支持关闭思考，但认 reasoning_effort（flat 字段，如 low/high/max）。
        # 不降档则推理占满 token 预算 → content 返空；设 low 可让它尽快收束、把答案写进 content。
        effort = os.environ.get("ALE_LLM_REASONING_EFFORT", "").strip()
        if effort:
            payload["reasoning_effort"] = effort
        # 流式（默认开）：网关对非流式请求约 5 分钟无响应就 504，长输出（casegen 写代码 32000 token、
        # 推理档 agent 单步）稳定撞上；流式边生成边回包，连接不空闲就不超时（10-19 实测：
        # 非流式 Sonnet 5.5 写代码 3 次全 504，流式 5000 token 64s 正常收完）。ALE_LLM_STREAM=0 关掉。
        stream = os.environ.get("ALE_LLM_STREAM", "1") != "0"
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.cfg.api_base.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key})
        ctx = _ssl_context(getattr(self.cfg, "verify_ssl", True))
        # 网关偶发 connection reset / 超时，自动重试几次（指数退避）。
        # 超时给到 900s：盲解一条真实任务的输出动辄两万多 completion token，
        # 原来写死的 180s 会在推理型模型上稳定超时（实测 N7 造价那条撞到过）。
        attempts = int(os.environ.get("ALE_LLM_RETRIES", "3"))
        timeout = int(os.environ.get("ALE_LLM_TIMEOUT", "900"))
        last = None
        for i in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    data = _read_stream(r) if stream else json.loads(r.read())
                # 网关偶发回 200 但 choices 为 null（错误信封，如上游超时）：当瞬时故障重试，
                # 而不是让 `None[0]` 抛 TypeError 绕过重试（实测 ward 那条一个槽位连挂三次）
                if not (isinstance(data, dict) and data.get("choices")):
                    raise ConnectionError("网关返回无 choices：%s" % json.dumps(data, ensure_ascii=False)[:300])
                usage = data.get("usage") or {}
                choice = data["choices"][0]
                detail = usage.get("completion_tokens_details") or {}
                return LLMResult(
                    text=choice["message"].get("content") or "",
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    model=data.get("model", self.cfg.model),
                    finish_reason=choice.get("finish_reason", ""),
                    reasoning_tokens=detail.get("reasoning_tokens", 0))
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError, ValueError) as e:
                last = e
                if i < attempts - 1:
                    wait = 1.5 * (i + 1)
                    # 429 限流：短退避没用，认 Retry-After 头，否则指数退避（封顶 60s）
                    if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                        ra = e.headers.get("Retry-After") if e.headers else None
                        wait = float(ra) if (ra and ra.isdigit()) else min(60.0, 8.0 * (2 ** i))
                    time.sleep(wait)
        raise RuntimeError("网关请求失败（重试 %d 次仍失败）：%s" % (attempts, last))


def _read_stream(resp):
    """把 SSE 流（data: {...} 行）拼回非流式响应的形状，下游解析不用改。"""
    text, fin, usage, model, n = [], "", {}, "", 0
    for raw in resp:
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("data:"):
            continue
        d = line[5:].strip()
        if d == "[DONE]":
            break
        j = json.loads(d)
        if j.get("error"):
            raise ConnectionError("网关流内报错：%s" % json.dumps(j["error"], ensure_ascii=False)[:300])
        n += 1
        model = j.get("model") or model
        if j.get("usage"):
            usage = j["usage"]
        for c in j.get("choices") or []:
            text.append((c.get("delta") or {}).get("content") or "")
            fin = c.get("finish_reason") or fin
    if not n:
        raise ConnectionError("网关流式响应为空")
    return {"model": model, "usage": usage,
            "choices": [{"message": {"content": "".join(text)}, "finish_reason": fin}]}


def get_client(cfg):
    if cfg.provider == "openai":
        return OpenAICompatClient(cfg)
    return MockLLMClient(cfg.model)
