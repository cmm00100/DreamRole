"""深度思考：四档，以及每家的参数怎么翻译。

为什么要有这一层：**「思考」不是一个统一参数**。
OpenAI 叫 `reasoning_effort`（low/medium/high）；通义叫 `enable_thinking` + `thinking_budget`；
智谱把开关塞在 `thinking.type` 里；DeepSeek 干脆是「换个模型」（`deepseek-reasoner`）；
OpenRouter 又把它包进 `reasoning.effort`。如果每处 if/elif，加一家就要回来改一次。

所以这里是一张**翻译表**：`payload(provider, model, level) -> (kwargs, note)`。
调不通也不报错，理由和 `_completion` 里 stream_options 的处理一致 ——
兼容端点常常「收下参数然后不理」，或者干脆 400。退回去的代价只是「这次没思考」，
把这件事写进 note 让界面说出来，比抛一个用户看不懂的异常好。

**为什么默认 off**：思考会显著变贵变慢。用户第一次用的时候应该拿到快的那个版本，
想要更深的推理时自己调 —— 而不是每次都等它想十秒。
"""
from __future__ import annotations

LEVELS: dict[str, dict] = {
    "off": {"label": "不思考", "effort": None, "budget": 0,
            "hint": "直接回答，最快最便宜。"},
    "low": {"label": "浅想", "effort": "low", "budget": 1024,
            "hint": "先想一句再答，适合日常改写和小问题。"},
    "medium": {"label": "想一想", "effort": "medium", "budget": 4096,
               "hint": "默认推荐。会先理一遍思路再动手。"},
    "high": {"label": "深想", "effort": "high", "budget": 16000,
             "hint": "复杂改动、架构取舍用它。慢，但错得少。"},
}

ORDER = ["off", "low", "medium", "high"]

DEFAULT = "off"

# 每家支不支持「可调的思考」。写成数据，不写成分支。
#   param: 传参方式；models: 对哪些模型生效（空 = 该家全部）
CAPABILITIES: dict[str, dict] = {
    "openai": {"param": "effort", "models": ("o1", "o3", "o4", "gpt-5"), "budget": False},
    "dashscope": {"param": "qwen", "models": (), "budget": True},
    "zhipu": {"param": "zhipu", "models": ("glm-4.5", "glm-4.6"), "budget": False},
    "openrouter": {"param": "openrouter", "models": (), "budget": False},
    "deepseek": {"param": "model", "models": ("deepseek-reasoner",), "budget": False},
}


def normalize(level: str | None) -> str:
    text = str(level or "").strip().lower()
    return text if text in LEVELS else DEFAULT


def describe() -> dict:
    return {
        "level": DEFAULT,
        "levels": [
            {"id": key, "label": LEVELS[key]["label"], "hint": LEVELS[key]["hint"]}
            for key in ORDER
        ],
        "support": {pid: {"param": meta["param"], "budget": meta["budget"]}
                    for pid, meta in CAPABILITIES.items()},
    }


def supported(provider: str, model: str = "") -> bool:
    """这一家 + 这个模型能不能调思考深度。"""
    meta = CAPABILITIES.get(provider)
    if not meta:
        return False
    if meta["param"] == "model":
        # DeepSeek 是靠换模型实现的：只要目录里有 reasoner 就算支持，
        # 具体换不换在 payload() 里决定。
        return True
    names = meta.get("models") or ()
    if not names:
        return True
    lower = str(model or "").lower()
    return any(name in lower for name in names)


def payload(provider: str, model: str, level: str) -> tuple[dict, str]:
    """返回 ({请求要多带的参数}, 给人看的说明)。说明为空表示「按你说的来」。"""
    level = normalize(level)
    if level == "off":
        return {}, ""

    if not supported(provider, model):
        return {}, (f"{provider} 这一家（模型 {model or '默认'}）没提供可调的思考深度，"
                    "这次按普通模式回答。")

    meta = CAPABILITIES[provider]
    effort = LEVELS[level]["effort"]
    budget = LEVELS[level]["budget"]
    kind = meta["param"]

    if kind == "effort":
        return {"reasoning_effort": effort}, ""

    if kind == "qwen":
        # 通义（DashScope 兼容模式）：enable_thinking 走 extra_body，
        # thinking_budget 才是真正限制长度的那个
        body: dict = {"enable_thinking": True}
        if meta.get("budget"):
            body["thinking_budget"] = budget
        return {"extra_body": body}, ""

    if kind == "zhipu":
        return {"extra_body": {"thinking": {"type": "enabled"}}}, ""

    if kind == "openrouter":
        return {"extra_body": {"reasoning": {"effort": effort}}}, ""

    if kind == "model":
        # DeepSeek：思考不是参数而是模型。已经有思考能力的模型不重复换。
        target = "deepseek-reasoner"
        if target in str(model or ""):
            return {}, ""
        return {"_swap_model": target}, (f"DeepSeek 的思考要换模型，这次会用 {target} 跑；"
                                         f"它不支持工具调用，所以只在纯问答时才换。")

    return {}, ""


def apply_level(provider: str, model: str, level: str, has_tools: bool) -> tuple[str, dict, str]:
    """把「等级」翻译成「这次请求该怎么做」。

    返回 (实际用的模型, 额外参数, 说明)。第三方调用方只需要这一个函数。
    """
    kwargs, note = payload(provider, model, level)
    actual_model = kwargs.pop("_swap_model", "") or model
    if actual_model != model and has_tools:
        # reasoner 类模型不支持 function calling，带工具时会 400。
        # 与其让用户看到一个 400，不如退回普通模型并把话说清楚。
        return model, {}, "这一轮要用工具，而 DeepSeek 的思考模型不支持工具调用，已退回普通模型。"
    return actual_model, kwargs, note
