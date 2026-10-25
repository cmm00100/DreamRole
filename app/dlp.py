"""出站防泄漏：把要发给模型服务商的内容里，看起来像凭据的东西先打码。

为什么在**出站**这一侧做：真正的风险不是「本机文件里有 key」（那本来就在用户自己机器上），
而是「这段内容被原样发到第三方的接口上，并且在对方的日志里留下一份」。
所以检查点只有一个 —— 请求出站之前。

为什么是「打码」而不是「拦截」：拦截会让用户莫名其妙地发不出去（他不知道哪一句踩了），
打码只是让那串字符变成 `sk-a1b2…（已打码）`，模型照样能理解这段代码，但拿不到真钥匙。

默认开。关掉它的场景是真实的：你在调试一个密钥管理脚本，模型必须看到真值才能帮你改。
"""
from __future__ import annotations

import re
from typing import Any

from . import config

# 每条：(名字, 正则)。顺序有讲究 —— 先匹配长而具体的，再匹配宽松的兜底。
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("私钥块", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,4000}?-----END [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI/DeepSeek key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("AWS Access Key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("Bearer 头", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}")),
    ("密码赋值", re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api_key|apikey)\b\s*[:=]\s*[\"']?([^\s\"',}]{8,})")),
]


def enabled() -> bool:
    return bool(config.get().get("dlp", True))


def _placeholder(name: str, hit: str) -> str:
    keep = hit[:4] if len(hit) > 8 else ""
    return f"{keep}…（已打码·{name}）"


def mask_text(text: str) -> tuple[str, list[str]]:
    """返回 (打码后的文本, 命中的类型清单)。"""
    if not text:
        return text, []
    hits: list[str] = []
    out = text
    for name, pattern in PATTERNS:
        def swap(match: re.Match) -> str:
            hits.append(name)
            whole = match.group(0)
            # 「password=xxx」这种要保住前缀，否则读起来就不知道那行在干什么了
            if match.re.groups >= 2 and match.group(2):
                return whole.replace(match.group(2), _placeholder(name, match.group(2)))
            return _placeholder(name, whole)

        out = pattern.sub(swap, out)
    return out, hits


def scan(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """扫一遍要发出去的 messages，返回 (可能打过码的新列表, 命中次数)。

    只在真的有命中时才复制一份 —— 没有命中时原对象直接返回，
    避免每一轮都白复制一遍几十条消息。
    """
    if not enabled():
        return messages, 0

    total = 0
    changed: list[dict[str, Any]] = []
    touched = False
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not content:
            changed.append(message)
            continue
        masked, hits = mask_text(content)
        if not hits:
            changed.append(message)
            continue
        total += len(hits)
        touched = True
        changed.append({**message, "content": masked})
    return (changed if touched else messages), total


def describe() -> dict:
    return {
        "enabled": enabled(),
        "patterns": [
            {"name": name, "sample": pattern.pattern[:60]}
            for name, pattern in PATTERNS
        ],
    }
