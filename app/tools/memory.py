"""记忆工具：让 agent 自己决定「这件事值得记住」。

为什么要给 agent 一个写记忆的工具，而不是自动全记：
自动全记会把上下文很快塞满噪声。让模型显式判断「这是长期有效的约定/偏好/坑」，
它才会真的只记该记的。

三个 scope 的取舍在 memory.py 顶部写了；这里只负责把模型的意图翻译成调用。
"""
from __future__ import annotations

from .. import context, memory
from .base import ToolContext

SCOPE_ALIAS = {
    "": "project",
    "project": "project",
    "项目": "project",
    "daily": "daily",
    "day": "daily",
    "每日": "daily",
    "pref": "pref",
    "preference": "pref",
    "偏好": "pref",
}


def _scope_item(ctx: ToolContext, scope: str) -> str:
    kind = SCOPE_ALIAS.get((scope or "").strip().lower(), "project")
    return memory.item_id(kind, ctx.project or str(ctx.root))


def memory_read(ctx: ToolContext, limit: int = 4000, scope: str = "project") -> str:
    item = _scope_item(ctx, scope)
    data = memory.read(item, ctx.project or str(ctx.root))
    text = (data.get("text") or "").strip() if data.get("ok") else ""
    if not text:
        return "这个项目还没有记忆。可以用 memory_write 记下值得长期保留的约定。"
    head = {"pref": "用户偏好：\n", "daily": "今天的记录：\n"}.get(data.get("kind"), "")
    return (head + text)[: max(200, min(int(limit or 4000), 8000))]


def memory_write(ctx: ToolContext, text: str, scope: str = "project") -> str:
    text = (text or "").strip()
    if not text:
        return "没给内容。"
    if len(text) > 800:
        return "太长了（>800 字符）。记忆只留结论和约定，细节用文件记录。"
    item = _scope_item(ctx, scope)
    result = memory.append(item, text, ctx.project or str(ctx.root))
    if not result.get("ok"):
        return f"没记上：{result.get('error')}"
    label = {"pref": "用户偏好", "daily": "今天的记录"}.get(
        memory.read(item, ctx.project or str(ctx.root)).get("kind"), "项目记忆")
    return f"已记入{label}：{text}\n（文件：{result.get('path')}）"


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "memory_read",
            "description": "读这个项目的历史记忆（此前确认过的约定、偏好、踩过的坑）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["project", "daily", "pref"],
                        "description": "project=项目长期约定（默认）；pref=用户偏好",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": (
                "把一条**长期有效**的信息记进记忆，例如「本项目用 pnpm 不用 npm」"
                "「测试命令是 pytest -q」「用户不喜欢 emoji 注释」。"
                "不要记一次性的中间结果。一条不超过 800 字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要记住的一句话结论"},
                    "scope": {
                        "type": "string",
                        "enum": ["project", "daily", "pref"],
                        "description": (
                            "project=只对这个目录成立的约定（默认）；"
                            "pref=跟目录无关、换项目也该遵守的用户偏好；"
                            "daily=今天的流水账"
                        ),
                    },
                },
                "required": ["text"],
            },
        },
    },
]

HANDLERS = {"memory_read": memory_read, "memory_write": memory_write}
