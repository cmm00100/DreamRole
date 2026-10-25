"""上下文治理：token 预算、自动压缩、三层记忆。

为什么这件事必须做：agent 的上下文是「只涨不跌」的——每读一个文件、每跑一条命令
都往 messages 里加东西，到第 20 步左右必然撞上限。不做治理的 agent 表现是
「聊得越久越笨，最后直接报错」，而用户不知道发生了什么。

这里的策略是：
1. **留尾巴**：最近 N 轮完整保留（工具调用链不能截断，否则 API 直接 400）；
2. **压中间**：中间历史交给模型压成一段摘要，替换成一条 system 消息；
3. **保系统**：system prompt 永远不动。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import config
from .llm import ChatClient, LLMError, estimate_messages, estimate_tokens
from .paths import DATA, MEMORY

KEEP_TAIL_MESSAGES = 10          # 尾部保留的消息条数（尽量保住完整工具链）
COMPACT_TRIGGER_TOKENS = 40_000  # 兜底阈值，配置里的 compact_ratio 优先


# 用户能选的几档。给的是「模型窗口有多大」，不是「能用多少」——
# 真正留多少由 compact_ratio 决定，两个数分开才好讲清楚。
WINDOW_PRESETS = {
    "32k": 32_000,
    "64k": 64_000,
    "128k": 128_000,
    "200k": 200_000,
    "1m": 1_000_000,
}


def context_limit(model: str) -> int:
    """这个模型的窗口有多大。

    优先用用户选的（设置里「上下文窗口」）。没选就按模型名猜 ——
    猜错的代价很实在：猜大了 API 直接 400，所以宁可猜小。
    """
    chosen = str(config.get().get("context_window") or "").strip().lower()
    if chosen in WINDOW_PRESETS:
        return WINDOW_PRESETS[chosen]
    m = (model or "").lower()
    if "128k" in m or "gpt-4o" in m or "qwen-max" in m or "glm-4" in m:
        return 128_000
    if "32k" in m or "moonshot-v1-32k" in m:
        return 32_000
    if "gemini-2.5" in m:
        return 1_000_000
    return 64_000


def should_compact(messages: list[dict], model: str, ratio: float | None = None) -> bool:
    limit = context_limit(model)
    ratio = ratio if ratio is not None else float(config.get().get("compact_ratio") or 0.72)
    used = estimate_messages(messages)
    if used > limit * ratio:
        return True
    # 兜底只在「窗口是猜的」时才生效：用户明确选了 1M 就该按 1M 来，
    # 否则 40k 一到照样压 —— 那这个选项等于不存在。
    explicit = str(config.get().get("context_window") or "").strip().lower() in WINDOW_PRESETS
    return (not explicit) and used > COMPACT_TRIGGER_TOKENS


def _split_for_compact(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """切成「该压的」和「必须留的」。

    关键约束：尾部不能从一个 tool 消息开始 —— 那意味着它对应的 assistant(tool_calls)
    被压掉了，OpenAI 协议会拒。所以要往前找到一条安全的切点。
    """
    if len(messages) <= KEEP_TAIL_MESSAGES + 1:
        return [], messages

    cut = max(1, len(messages) - KEEP_TAIL_MESSAGES)
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    return messages[1:cut], messages[cut:]


def compact(messages: list[dict], model: str, client: ChatClient | None = None) -> tuple[list[dict], str]:
    """返回 (新 messages, 摘要文本)。摘要为空表示没压。"""
    head, tail = _split_for_compact(messages)
    if not head:
        return messages, ""

    transcript_parts: list[str] = []
    for msg in head:
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if role == "tool":
            content = content[:300]
        elif role == "assistant" and msg.get("tool_calls"):
            names = ", ".join((c.get("function") or {}).get("name", "?") for c in msg["tool_calls"])
            content = f"{content}\n[调用了工具：{names}]"
        if content.strip():
            transcript_parts.append(f"<{role}> {content[:1200]}")
    transcript = "\n".join(transcript_parts)[-24_000:]

    summary = ""
    try:
        client = client or ChatClient()
        summary = client.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "你在压缩一段 agent 与用户的工作记录。输出一段结构化摘要，必须包含：\n"
                        "1) 用户的目标与已确认的约束；2) 已完成的事与产出文件路径；\n"
                        "3) 关键决策及理由；4) 尚未解决的问题。\n"
                        "不要复述工具输出的细节，不要编造。只输出摘要正文。"
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            max_tokens=900,
        )
    except (LLMError, Exception):  # noqa: BLE001 - 压缩失败不能拖垮主流程
        summary = ""

    if not summary:
        # 退化成机械摘要：至少保住「做过什么」，别把上下文丢了
        summary = "（自动压缩，模型摘要失败）此前的关键动作：\n" + "\n".join(
            line[:160] for line in transcript_parts[-24:]
        )

    head_msg = {
        "role": "system",
        "content": "以下是本次会话较早部分的压缩摘要，作为背景使用：\n\n" + summary,
    }
    return [messages[0], head_msg, *tail], summary


# ---------------------------------------------------------------------------
# 三层记忆：项目级 MEMORY.md / 每日记录 / 用户偏好
# ---------------------------------------------------------------------------

def _memory_root() -> Path:
    MEMORY.mkdir(parents=True, exist_ok=True)
    return MEMORY


def project_memory_path(project: str) -> Path:
    """按项目路径哈希分文件，避免不同项目的记忆互相污染。"""
    import hashlib

    digest = hashlib.sha256(str(project or "default").encode("utf-8")).hexdigest()[:12]
    return _memory_root() / f"project-{digest}.md"


def read_project_memory(project: str) -> str:
    path = project_memory_path(project)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:6000]
    except OSError:
        return ""


def append_project_memory(project: str, text: str) -> Path:
    path = project_memory_path(project)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    entry = f"\n- [{stamp}] {text.strip()}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        if path.stat().st_size == 0 if path.exists() else True:
            fh.write(f"# 项目记忆\n\n工作目录：`{project}`\n")
        fh.write(entry)
    return path


def memory_brief(project: str, limit: int = 1500) -> str:
    """注入 system prompt 的那一小段：只给最近的项目记忆 + 用户偏好，避免占满上下文。

    每日记录不进来：它是流水，按天攒，越写越长。真正该跨会话生效的结论
    早就被 agent 用 memory_write 沉淀到项目记忆里了。
    """
    parts: list[str] = []
    text = read_project_memory(project).strip()
    if text:
        lines = [line for line in text.splitlines() if line.strip().startswith("- [")]
        if lines:
            parts.append("项目记忆（此前在这个目录里确认过的事，可直接采用）：\n"
                         + "\n".join(lines[-12:])[:limit])

    try:
        from . import memory

        pref = memory.pref_brief()
        if pref:
            parts.append(pref)
    except Exception:  # noqa: BLE001 - 偏好读不出来不该影响对话
        pass

    return "\n\n".join(parts)
