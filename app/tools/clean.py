"""清理工具。一律进回收站，规则见 app/junk.py。"""
from __future__ import annotations

from pathlib import Path

from .. import junk, store, trash
from .base import ToolContext

PROTECTED_NAMES = {".env", "settings.json", "requirements.txt", "package.json", "README.md", "agent.spec"}
PROTECTED_DIRS = {".git", ".venv", "venv", "node_modules", "app", "web"}


def _size(target: Path) -> int:
    if target.is_file():
        try:
            return target.stat().st_size
        except OSError:
            return 0
    total = 0
    for entry in target.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def scan_junk(ctx: ToolContext, limit: int = 200) -> str:
    items = junk.scan(ctx.root, limit=limit)
    if not items:
        return "没有发现可清理的垃圾（解释器缓存、临时文件、编辑器备份都没有）。"
    total = sum(i["size"] for i in items)
    lines = [f"发现 {len(items)} 项可清理，共 {trash.human_size(total)}："]
    for item in items[:40]:
        if item["kind"] == "dir":
            lines.append(f"- {item['path']}/  {item['files']} 个文件  {trash.human_size(item['size'])}")
        else:
            lines.append(f"- {item['path']}  {trash.human_size(item['size'])}")
    if len(items) > 40:
        lines.append(f"- …… 还有 {len(items) - 40} 项")
    lines.append("")
    lines.append("确认没用之后调 remove_junk(paths=[...]) 清掉，会进回收站，可还原。")
    return "\n".join(lines)


def remove_junk(ctx: ToolContext, paths: list | None = None, reason: str = "") -> str:
    """只接受 scan_junk 认得的路径，防止模型把正常文件塞进来。"""
    if not paths:
        return "没给 paths。先调 scan_junk 看一眼有什么可清理的。"

    done: list[str] = []
    refused: list[str] = []
    failed: list[str] = []
    freed = 0

    for raw in list(paths)[:40]:
        rel = str(raw).replace("\\", "/").strip().lstrip("/")
        if not rel:
            continue
        if not junk.is_junk(rel):
            refused.append(f"{rel}（不在清理规则内，没删）")
            continue
        try:
            target = ctx.resolve(rel, need="write")
        except ValueError:
            refused.append(f"{rel}（越界）")
            continue
        if target == ctx.root.resolve() or not target.exists():
            failed.append(f"{rel}（不存在）")
            continue
        size = _size(target)
        if trash.to_trash(target):
            done.append(f"{rel}  {trash.human_size(size)}")
            freed += size
        else:
            failed.append(f"{rel}（回收站没接受，可能被占用）")

    lines: list[str] = []
    if done:
        lines.append(f"已移入回收站 {len(done)} 项，共 {trash.human_size(freed)}：")
        lines += [f"- {d}" for d in done]
    if refused:
        lines += ["", "拒绝了这些（不在清理规则内，可能是你的东西）："]
        lines += [f"- {r}" for r in refused]
    if failed:
        lines += ["", "没删成："]
        lines += [f"- {f}" for f in failed]
    if reason:
        lines += ["", f"理由：{reason}"]
    if not lines:
        return "什么都没做。"
    lines += ["", "需要的话可以从系统回收站还原。"]
    return "\n".join(lines)


def delete_file(ctx: ToolContext, path: str, reason: str) -> str:
    """只允许删「本次会话自己写过的」或「通用垃圾」。"""
    if not path:
        return "没给 path。"
    rel = str(path).replace("\\", "/").strip().lstrip("/")
    parts = [p for p in rel.split("/") if p]

    if any(p in PROTECTED_DIRS for p in parts):
        return f"拒绝：{rel} 在受保护目录里，不能删。"
    if parts and parts[-1] in PROTECTED_NAMES:
        return f"拒绝：{rel} 是项目关键文件，不能删。"

    try:
        target = ctx.resolve(rel, need="write")
    except ValueError as exc:
        # 别把原因在这里改写成「不在项目目录内」—— 现在有两种可能：
        # 真的越界，或者落在只读授权目录里。改写了模型就会以为授权没生效，
        # 然后反复重试同一个动作。
        return f"拒绝：{exc}"
    if target == ctx.root.resolve():
        return "拒绝：不能删整个项目目录。"
    if not target.exists():
        return f"文件不存在：{rel}"

    # 改动记录是以**(会话, 绝对路径)** 为键存的（回滚时需要完整路径），
    # 所以这里必须用 resolve 之后的路径去查，拿 rel 查永远是空。
    # 而且必须连同会话一起查 —— 同一个文件别的对话改过，不代表这次能动它。
    item = store.change_for(ctx.cid, str(target))
    if item is None and not ctx.cid:
        item = next((v for v in store.changes().values()
                     if v.get("path") == str(target)), None)
    mine = bool(item) and (not ctx.cid or item.get("cid") == ctx.cid)
    if not (mine or junk.is_junk(rel)):
        return (
            f"拒绝：{rel} 不是这次会话创建的，也不是垃圾文件。\n"
            "只允许删自己产生的临时文件；用户原有的文件请让用户自己决定。"
        )

    size = _size(target)
    if not trash.to_trash(target):
        return f"没删成：{rel}（回收站没接受，可能被占用）"
    note = f"（{reason}）" if reason else ""
    return f"已把 {rel} 移入回收站{note}，释放 {trash.human_size(size)}。可还原。"


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "scan_junk",
            "description": (
                "列出工作区里可以清理的东西（解释器缓存、临时文件、编辑器备份），只读不改。"
                "任务收尾时调一次，看看有没有自己留下的垃圾。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多返回多少项，默认 200"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_junk",
            "description": (
                "把 scan_junk 列出来的东西批量删掉，进系统回收站可还原。"
                "只接受 scan_junk 认得出来的路径，不在规则内的会被拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要删的相对路径，必须来自 scan_junk 的结果",
                    },
                    "reason": {"type": "string", "description": "一句话说明为什么可以删"},
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "删除单个文件，进回收站可还原。**只用来删你在这次会话里自己创建的临时文件**"
                "（调试脚本、中间产物）。用户原有的文件、源码、配置一律不许删。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "reason": {"type": "string", "description": "为什么可以删这个文件"},
                },
                "required": ["path", "reason"],
            },
        },
    },
]

HANDLERS = {"scan_junk": scan_junk, "remove_junk": remove_junk, "delete_file": delete_file}
