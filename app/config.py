"""配置与模型目录。

一条设计原则（抄自 PromptOptimizer 的 PROVIDER_ENV_KEYS）：**provider 是数据不是分支**。
新增一家只需要在 PROVIDERS 里加一行，不需要改任何 if/elif。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .paths import SETTINGS_FILE, DATA, ensure_dirs

APP_VERSION = "1.6.8"
APP_TITLE = "DreamRole"

# ---------------------------------------------------------------------------
# 模型目录：全部走 OpenAI 兼容协议，换一家只是换 base_url + key
# ---------------------------------------------------------------------------
PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "key_env": ["DREAMROLE_API_KEY", "ASTER_API_KEY", "DEEPSEEK_API_KEY"],
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "默认。便宜、中文好、工具调用稳定。",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "key_env": ["OPENAI_API_KEY"],
        "models": ["gpt-4o", "gpt-4o-mini", "o4-mini"],
        "note": "",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": ["ZHIPU_API_KEY", "GLM_API_KEY"],
        "models": ["glm-4.6", "glm-4.5-air"],
        "note": "",
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "key_env": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
        "models": ["kimi-k2-0905-preview", "moonshot-v1-32k"],
        "note": "",
    },
    {
        "id": "dashscope",
        "name": "阿里通义",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "note": "",
    },
    {
        "id": "gemini",
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "note": "",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": ["OPENROUTER_API_KEY"],
        "models": ["anthropic/claude-sonnet-4", "google/gemini-2.5-pro"],
        "note": "一个 key 转发几百个模型。",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "key_env": ["SILICONFLOW_API_KEY"],
        "models": ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "note": "",
    },
    {
        "id": "ollama",
        "name": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "key_env": [],
        "models": ["qwen2.5:7b", "llama3.2"],
        "note": "不联网、不要 key，质量取决于本地模型。",
    },
    {
        "id": "custom",
        "name": "自定义端点",
        "base_url": "",
        "key_env": ["DREAMROLE_CUSTOM_API_KEY", "ASTER_CUSTOM_API_KEY"],
        "models": [],
        "note": "任何 OpenAI 兼容服务：vLLM、OneAPI、中转站…",
    },
]

PROVIDER_BY_ID = {p["id"]: p for p in PROVIDERS}

DEFAULTS: dict[str, Any] = {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "optimize_model": "",          # 空 = 用主模型做提示词优化
    "mode": "craft",               # craft | ask | plan | teach
    "permission": "standard",      # readonly | standard | staged | full
    "project": "",                 # 空 = 这台机器还没选过工作区；每个会话自己有
    "recent_projects": [],         # 最近用过的工作区，选目录时一键复用
    "enabled_tools": [],           # 空 = 用注册表的默认值
    "tools_seen": [],              # 记录这份配置见过哪些工具，用于升级时只补一次
    "approvals": {},               # tool -> "always"，记住「总是允许」
    "providers": {},               # id -> {api_key, base_url, model}
    "skills_enabled": [],          # 空 = 全部启用
    # 技能开关记的是「关掉的那些」，不是「开着的那些」——
    # 记开着的，新建的技能就默认是关的，那和「刚写完就看不见」是一回事。
    "skills_off": [],
    # 全局通行证：所有**新**会话建起来时自动抄一份。已经在的会话按它自己的列表来，
    # 改这里不会回头去动它们（否则「这条会话能去哪」会随时变）。
    "global_grants": [],
    "mcp_servers": {},             # name -> {command, args, env, enabled}
    "compact_ratio": 0.72,         # 上下文用到模型的这个比例就压缩
    "context_window": "",          # 空 = 按模型名猜；32k/64k/128k/200k/1m = 用户指定
    "auto_memory": "smart",        # 自动记忆：off | daily | smart（见 automemory.py）
    "engine": "role1",             # 默认底层：role1 = 自带内核；role2 = 本机 DeepSeek Harness
    "dsh_path": "",                # role2 的 DSH 安装位置；空 = 自动找（见 dsh_engine.locate）
    "max_steps": 24,
    "auto_optimize": False,        # 发送前自动优化提示词（默认关，走手动预览）
    "personal_note": "",           # 个性化：总是想让它遵守的几句话，注入系统提示
    "think": "off",                # 深度思考：off | low | medium | high（见 thinking.py）
    "dlp": True,                   # 出站打码：发给模型之前遮住看起来像密钥的内容
    "rules": [],                   # 用户自定义的安全规则（形状同 rules.BUILTIN）
    "runtime_paths": {},           # 运行时手动指定的路径（探测错了一定有救）
    "api_open": False,             # 外部接口默认关：先关着，需要时明确打开
    "api_token": "",               # 外部访问令牌，第一次用到时生成
}

_lock = threading.RLock()
_settings: dict[str, Any] | None = None


def mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:5] + "…" + key[-4:] if len(key) > 12 else "已设置"


def _read_dotenv() -> None:
    """把 .env 读进环境变量（setdefault，已有环境变量优先）。"""
    for candidate in (DATA / ".env", Path(".env")):
        if not candidate.is_file():
            continue
        try:
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            continue


def _env_key(provider_id: str) -> str:
    for env in PROVIDER_BY_ID.get(provider_id, {}).get("key_env", []):
        value = os.environ.get(env, "").strip()
        if value:
            return value
    return ""


def load() -> dict[str, Any]:
    global _settings
    with _lock:
        if _settings is not None:
            return _settings

        _read_dotenv()
        ensure_dirs()
        data = dict(DEFAULTS)
        if SETTINGS_FILE.is_file():
            try:
                data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        # 老版本会把 `data/projects` 当成默认工作区静默写进配置。那是程序自己造的目录，
        # 不是用户选的，必须当成「没选」—— 否则「没设工作区就不许开始」这条永远不生效。
        if is_placeholder_project(data.get("project")):
            data["project"] = ""
        _settings = data
        return _settings


def is_placeholder_project(path: Any) -> bool:
    text = str(path or "").strip()
    if not text:
        return True
    try:
        return Path(text).resolve() == (DATA / "projects").resolve()
    except OSError:
        return False


def remember_project(path: str) -> list[str]:
    """把用过的工作区记进「最近」。只留 8 个，最近的排最前。

    为什么需要这个：工作区改成「每个会话自己选」之后，如果每次都要从磁盘根目录
    一层层点下去，用户很快就会烦到想要那个「静默默认」回来。一行就能复用，
    才谈得上「显式选择」。
    """
    text = str(path or "").strip()
    if not text:
        return list(get().get("recent_projects") or [])
    try:
        normalized = str(Path(text).expanduser().resolve())
    except OSError:
        normalized = text
    recent = [item for item in (get().get("recent_projects") or [])
              if isinstance(item, str) and item and item != normalized]
    recent.insert(0, normalized)
    recent = recent[:8]
    update({"recent_projects": recent, "project": normalized})
    return recent


def get() -> dict[str, Any]:
    return load()


def save() -> None:
    with _lock:
        payload = dict(get())
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)  # 原子替换：半截文件比没有更糟


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """整体替换这些键。

    这里刻意**不做字典合并**：`approvals` 和 `mcp_servers` 这类映射一旦默认合并，
    「取消记住某个工具」「删掉一个 MCP 服务」就再也删不掉了 —— 传进来的新字典
    里没有这个键，合并之后旧键还在。要合并请显式调 `merge_map()`。
    """
    with _lock:
        settings = get()
        for key, value in patch.items():
            settings[key] = value
        save()
        return settings


def merge_map(key: str, mapping: dict[str, Any]) -> dict[str, Any]:
    """把一个映射**并进**某个键（用于「加一条」而不是「换一批」的场景）。"""
    with _lock:
        settings = get()
        merged = dict(settings.get(key) or {})
        merged.update(mapping)
        settings[key] = merged
        save()
        return merged


# ---------------------------------------------------------------------------
# provider 连接信息
# ---------------------------------------------------------------------------

def provider_conn(provider_id: str | None = None) -> dict[str, str]:
    """拿某一家的 {base_url, api_key, model}，缺的用默认/环境变量补。"""
    settings = get()
    pid = provider_id or settings.get("provider") or "deepseek"
    meta = PROVIDER_BY_ID.get(pid, PROVIDER_BY_ID["custom"])
    conf = (settings.get("providers") or {}).get(pid) or {}

    base_url = (conf.get("base_url") or meta.get("base_url") or "").strip()
    api_key = (conf.get("api_key") or "").strip() or _env_key(pid)
    model = (conf.get("model") or "").strip()
    if not model:
        model = settings.get("model") if pid == settings.get("provider") else ""
    if not model:
        model = (meta.get("models") or ["gpt-4o-mini"])[0]

    return {"provider": pid, "base_url": base_url, "api_key": api_key, "model": model}


def has_key() -> bool:
    return bool(provider_conn().get("api_key"))


def can_chat() -> bool:
    """能不能发请求。

    本地模型（Ollama、vLLM）不需要 key，所以不能只看 has_key ——
    否则用本地模型的用户会一直看到「还没填 API key」。
    """
    conn = provider_conn()
    if not conn.get("base_url"):
        return False
    if conn.get("api_key"):
        return True
    base = conn["base_url"]
    return "127.0.0.1" in base or "localhost" in base or "0.0.0.0" in base


def public_settings() -> dict[str, Any]:
    """给前端的版本：key 只回掩码，绝不回原文。"""
    settings = get()
    pid = settings.get("provider") or "deepseek"
    conn = provider_conn(pid)
    providers = {}
    for meta in PROVIDERS:
        c = (settings.get("providers") or {}).get(meta["id"]) or {}
        providers[meta["id"]] = {
            "base_url": c.get("base_url") or meta.get("base_url") or "",
            "api_key_masked": mask_key(c.get("api_key") or _env_key(meta["id"])),
            "model": c.get("model") or "",
            "models": list(meta.get("models") or []),
        }
    return {
        "provider": pid,
        "model": conn["model"],
        "base_url": conn["base_url"],
        "has_key": bool(conn["api_key"]),
        "api_key_masked": mask_key(conn["api_key"]),
        "mode": settings.get("mode"),
        "permission": settings.get("permission"),
        "project": settings.get("project"),
        "auto_optimize": settings.get("auto_optimize"),
        "personal_note": settings.get("personal_note") or "",
        "think": settings.get("think") or "off",
        "recent_projects": list(settings.get("recent_projects") or []),
        "compact_ratio": settings.get("compact_ratio"),
        "context_window": settings.get("context_window") or "",
        "auto_memory": settings.get("auto_memory") or "smart",
        "engine": settings.get("engine") or "role1",
        "dsh_path": settings.get("dsh_path") or "",
        "max_steps": settings.get("max_steps"),
        "providers": providers,
        "provider_catalog": [
            {k: m.get(k) for k in ("id", "name", "base_url", "models", "note")} for m in PROVIDERS
        ],
        "version": APP_VERSION,
    }
