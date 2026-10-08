"""产线全局配置。所有路径基于本文件定位，可用环境变量覆盖运行参数。

**框架与数据是分开的**：本仓库只装框架，真实任题、附件、标准答案、实测产物、网关价目表
都在仓库外，靠 `ALE_TASKS_DIR` / `ALE_RUNS_DIR` / `ALE_PRICING_FILE` 指过去。
这三个变量写在仓库根的 `.env`（不入库，样例见 `.env.example`），本模块 import 时自动读。
"""
import os
from dataclasses import dataclass, field

ALE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path=os.path.join(ALE_ROOT, ".env")):
    """读仓库根的 .env。已存在的环境变量优先，命令行临时覆盖照旧有效。

    自己实现是为了不引第三方依赖（产线声明纯标准库）。格式只认 KEY=VALUE 与 # 注释。
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv()

# 数据目录默认落在仓库内，但仓库里的 tasks/ runs/ 被 .gitignore 挡着，
# 正常用法是用 .env 指到仓库外的私有目录。
TASKS_DIR = os.environ.get("ALE_TASKS_DIR", os.path.join(ALE_ROOT, "tasks"))
RUNS_DIR = os.environ.get("ALE_RUNS_DIR", os.path.join(ALE_ROOT, "runs"))



@dataclass
class LLMConfig:
    # provider=mock 时全程离线，仅联调控制流；provider=openai 时走 OpenAI 兼容 HTTP 接口（含内网网关）
    provider: str = os.environ.get("ALE_LLM_PROVIDER", "mock")
    model: str = os.environ.get("ALE_LLM_MODEL", "gpt-4o-mini")
    api_base: str = os.environ.get("ALE_LLM_API_BASE", "https://api.openai.com/v1")
    api_key_env: str = "ALE_LLM_API_KEY"
    # 页面填入的 key 直接存这里（优先于环境变量）；仅在本进程内存，不落盘
    api_key: str = os.environ.get("ALE_LLM_API_KEY", "")
    verify_ssl: bool = os.environ.get("ALE_LLM_VERIFY_SSL", "1") != "0"
    temperature: float = 0.2


@dataclass
class PipelineConfig:
    concurrency: int = int(os.environ.get("ALE_CONCURRENCY", "8"))
    max_retries: int = int(os.environ.get("ALE_MAX_RETRIES", "2"))
    pass_threshold: float = 1.0     # score>=此值视为通过
    floor: float = 0.0              # 地板审计：空解得分应≤此值
    tasks_dir: str = TASKS_DIR
    runs_dir: str = RUNS_DIR
    llm: LLMConfig = field(default_factory=LLMConfig)
