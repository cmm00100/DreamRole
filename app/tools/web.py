"""联网工具。边界在 app/net.py。"""
from __future__ import annotations

from .. import net
from .base import ToolContext


def web_search(ctx: ToolContext, query: str, count: int = 5) -> str:
    """网络错误也当结果返回，别让异常打断整个循环。"""
    try:
        return net.search(query, count=count)
    except net.NetError as exc:
        return f"搜索失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 解析第三方页面，什么都可能发生
        return f"搜索出错：{type(exc).__name__}: {exc}"


def web_fetch(ctx: ToolContext, url: str, max_chars: int = 6000) -> str:
    try:
        return net.fetch(url, max_chars=max_chars)
    except net.NetError as exc:
        return f"抓取失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return f"抓取出错：{type(exc).__name__}: {exc}"


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "上网搜一条，返回标题/链接/摘要。适合查「最新版本」「官网文档」「今天发生了什么」"
                "这类记忆里可能过时的东西。拿到链接后用 web_fetch 读正文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，像人在搜索引擎里打的那样"},
                    "count": {"type": "integer", "description": "要几条，默认 5，最多 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "抓一个网页/接口。HTML 自动抽正文（去标签/脚本/样式），JSON 原样给。"
                "只支持公网 http/https，内网和本机地址会被拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整地址，含 https://"},
                    "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 6000"},
                },
                "required": ["url"],
            },
        },
    },
]

HANDLERS = {"web_search": web_search, "web_fetch": web_fetch}
