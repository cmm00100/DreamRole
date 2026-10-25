"""专家：一份可版本化的「运行时人格包」。

它跟「技能」不是一回事，这点最容易做混：
- **技能**是能力（会做 PPT、会查专利），按需加载，回答「能不能」。
- **专家**是立场（用什么方法看问题、什么算做完、先问什么），常驻，回答「怎么看」。
所以技能是插进上下文的说明书，专家是改写系统提示的身份 ——
一个专家可以配几个技能，但技能本身不改变你是谁。

存储用 JSON 而不是 Markdown 目录：专家是一份**结构化配置**（还给工具白名单、
默认模式、默认权限），不是一篇文档。用户改的是字段，不是正文。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .paths import DATA

FILE = DATA / "experts.json"

BUILTIN: list[dict] = [
    {
        "id": "builder",
        "name": "执行者",
        "emoji": "🛠",
        "desc": "少说多做，直接把事做完",
        "prompt": (
            "你是一个以「把事做完」为唯一标准的执行者。\n"
            "- 先确认目标，然后动手；能用一条命令验证的，就不要靠推测。\n"
            "- 每做完一步就给一行结果，不要写长段落解释你打算做什么。\n"
            "- 卡住了就说卡在哪，不要编造成功。"
        ),
        "mode": "craft",
        "tools": None,
    },
    {
        "id": "reviewer",
        "name": "代码审阅",
        "emoji": "🔍",
        "desc": "只挑问题，不替你重写",
        "prompt": (
            "你是一个严格的代码审阅者。\n"
            "- 只读，不改文件。给出问题、位置、为什么是问题、怎么改（一句话）。\n"
            "- 按严重程度排序：会出错的 > 会误导下一个人的 > 风格。\n"
            "- 没看出问题时明确说「没看出问题」，不要为了凑数编小毛病。"
        ),
        "mode": "ask",
        "tools": None,
    },
    {
        "id": "teacher",
        "name": "带练导师",
        "emoji": "🎓",
        "desc": "一步一步教，每步都让你自己动手",
        "prompt": (
            "你是一个带练型导师。\n"
            "- 每次只讲一个概念，讲完立刻给一个可以马上做的小练习。\n"
            "- 用「为什么」开头解释设计取舍，而不是罗列 API。\n"
            "- 学生做错时先问他怎么想的，再指出偏差在哪。\n"
            "- 阶段之间停下来等他确认。"
        ),
        "mode": "teach",
        "tools": None,
    },
    {
        "id": "analyst",
        "name": "数据分析",
        "emoji": "📊",
        "desc": "先问口径，再算数",
        "prompt": (
            "你是一个数据分析师。\n"
            "- 动手算之前先确认口径：时间范围、分母是什么、缺失值怎么算。\n"
            "- 每个结论都要能指回具体数字，不写「显著提升」这种没有量的话。\n"
            "- 给出结论时顺带说一句「这个结论在什么情况下会不成立」。"
        ),
        "mode": "craft",
        "tools": None,
    },
    {
        "id": "writer",
        "name": "技术写作",
        "emoji": "✍️",
        "desc": "先搭骨架，再填肉",
        "prompt": (
            "你是一个技术写作者。\n"
            "- 动笔前先给三级标题骨架，确认结构再展开。\n"
            "- 一段只讲一件事；能用例子说清的，不要用形容词。\n"
            "- 删掉「值得注意的是」「众所周知」这类填充词。"
        ),
        "mode": "craft",
        "tools": None,
    },
]


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": []}


def _save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def _slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", "-", (name or "").lower()).strip("-")
    return (text or "expert")[:32]


def list_all() -> list[dict]:
    """内置 + 自定义。同名 id 时自定义覆盖内置 —— 这样用户能「改造」内置专家，
    而不用去改程序目录里的东西（那会被下一次更新冲掉）。"""
    custom = _load().get("items") or []
    by_id: dict[str, dict] = {}
    for item in BUILTIN:
        by_id[item["id"]] = {**item, "builtin": True, "hidden": False}
    for item in custom:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        base = by_id.get(item["id"], {})
        by_id[item["id"]] = {**base, **item, "builtin": bool(base), "custom": True}
    items = list(by_id.values())
    items.sort(key=lambda item: (bool(item.get("hidden")), not item.get("builtin"), item.get("name") or ""))
    return items


def get(expert_id: str) -> dict | None:
    if not expert_id:
        return None
    for item in list_all():
        if item.get("id") == expert_id:
            return item
    return None


def save(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "专家得有个名字"}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "方法论是专家的本体，不能空着"}

    data = _load()
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    expert_id = str(payload.get("id") or "").strip() or _slug(name)

    entry = {
        "id": expert_id,
        "name": name[:24],
        "emoji": str(payload.get("emoji") or "🧩")[:4],
        "desc": str(payload.get("desc") or "").strip()[:80],
        "prompt": prompt[:6000],
        "mode": payload.get("mode") if payload.get("mode") in
                ("craft", "plan", "ask", "teach") else None,
        "tools": payload.get("tools") if isinstance(payload.get("tools"), list) else None,
        "hidden": bool(payload.get("hidden")),
        "updated": time.time(),
    }

    replaced = False
    for index, item in enumerate(items):
        if item.get("id") == expert_id:
            items[index] = {**item, **entry}
            replaced = True
            break
    if not replaced:
        items.append(entry)
    data["items"] = items
    _save(data)
    return {"ok": True, "expert": get(expert_id)}


def remove(expert_id: str) -> dict:
    data = _load()
    items = [item for item in (data.get("items") or [])
             if isinstance(item, dict) and item.get("id") != expert_id]
    if len(items) == len(data.get("items") or []):
        # 内置专家没有自定义副本，删不掉；把它藏起来等同于「不在列表里出现」
        for item in BUILTIN:
            if item["id"] == expert_id:
                return save({**item, "hidden": True})
        return {"ok": False, "error": "没有这个专家"}
    data["items"] = items
    _save(data)
    return {"ok": True}


def prompt_block(expert_id: str) -> str:
    """注入系统提示的那一段。放在模式片段之后、技能之前 ——
    身份比能力优先：先定「你是谁」，再告诉它「你会什么」。"""
    item = get(expert_id)
    if not item:
        return ""
    lines = [f"你现在的身份是「{item.get('name')}」。"]
    if item.get("desc"):
        lines.append(f"（{item['desc']}）")
    lines.append("")
    lines.append(str(item.get("prompt") or "").strip())
    tools = item.get("tools")
    if isinstance(tools, list) and tools:
        lines.append("")
        lines.append("这个身份只用这些工具：" + "、".join(str(t) for t in tools[:40]) + "。")
    return "\n".join(line for line in lines if line is not None)


def allowed_tools(expert_id: str, names: set[str]) -> set[str]:
    """专家声明了工具白名单就取交集。没声明（None）= 不额外限制。"""
    item = get(expert_id)
    if not item:
        return names
    tools = item.get("tools")
    if not isinstance(tools, list) or not tools:
        return names
    return names & {str(t) for t in tools}


def default_mode(expert_id: str) -> str | None:
    item = get(expert_id)
    mode = (item or {}).get("mode")
    return mode if mode in ("craft", "plan", "ask", "teach") else None
