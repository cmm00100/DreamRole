"""文件工具。

两条和「我的agent」不同的地方，都是冲着「能不能真改代码」去的：

1. `read_file` 支持 offset/limit，并且**返回带行号的文本**——模型据此才能说清「第 42 行」；
2. `edit_file` 做精确替换，不要求模型重写整篇。这是决定 agent 能否改真实项目的那一步。
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import textio
from .base import ToolContext

MAX_READ_CHARS = 60_000
MAX_LINES = 2000


def _safe_text(path: Path) -> str:
    return textio.read_text(path)


def list_dir(ctx: ToolContext, path: str = ".", depth: int = 1) -> str:
    try:
        base = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not base.is_dir():
        return f"不是目录：{path}"
    # 这里原来有 `if base == ctx.root: root = base else: root = base` —— 两支一模一样，
    # 大概是当初想「在根目录时列全量、在子目录时只列这一层」，后来规则变了没删干净。
    root = base

    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".mypy_cache"}
    lines: list[str] = []

    def walk(folder: Path, level: int) -> None:
        if level > max(1, min(depth, 3)):
            return
        try:
            entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            lines.append(f"{'  ' * level}(读不了：{exc})")
            return
        for entry in entries:
            if entry.name in skip or entry.name.startswith("."):
                continue
            mark = "dir " if entry.is_dir() else "file"
            size = "" if entry.is_dir() else f"  {entry.stat().st_size}B"
            lines.append(f"{'  ' * (level - 1)}{mark} {entry.name}{size}")
            if entry.is_dir():
                walk(entry, level + 1)

    walk(root, 1)
    return "\n".join(lines) if lines else "(空目录)"


def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 400) -> str:
    """按行读，返回带行号的内容，方便后续 edit_file 精确替换。"""
    try:
        target = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.is_file():
        return f"文件不存在：{path}"

    size = target.stat().st_size
    if size > 4_000_000:
        return f"文件太大（{size} 字节），先确认要读哪一段再带 offset/limit 读。"

    text = _safe_text(target)
    lines = text.splitlines()
    total = len(lines)

    start = max(1, int(offset or 1))
    limit = max(1, min(int(limit or 400), MAX_LINES))
    chunk = lines[start - 1: start - 1 + limit]

    body = "\n".join(f"{start + i:>5}\t{line}" for i, line in enumerate(chunk))
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + "\n…（本段过长已截断，请缩小 limit）"

    header = f"[{ctx.relative(target)}] 共 {total} 行"
    if start > 1 or start - 1 + len(chunk) < total:
        end = start - 1 + len(chunk)
        header += f"，本次显示 {start}-{end} 行（用 offset/limit 继续读）"
    return f"{header}\n{body}"


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    """整篇写入。新建文件或确实需要重写时用；改几行请用 edit_file。"""
    try:
        target = ctx.resolve(path, need="write")
    except ValueError as exc:
        return f"拒绝：{exc}"
    if target.is_dir():
        return f"{path} 是个目录，不能写入。"
    try:
        textio.write_text(target, content)
    except textio.EncodingError as exc:
        # 明确说「没写」，模型才知道要换个写法，而不是以为写成功了
        return f"没有写入：{exc}"
    except OSError as exc:
        return f"写入失败：{exc}"
    return f"已写入 {ctx.relative(target)}（{len(content)} 字符，{len(content.splitlines())} 行）"


def edit_file(
    ctx: ToolContext,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """精确替换。old_string 必须在文件里唯一出现，否则拒绝并说明。"""
    try:
        target = ctx.resolve(path, need="write")
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.is_file():
        return f"文件不存在：{path}"

    text = _safe_text(target)
    if not old_string:
        return "old_string 不能为空。要整篇重写请用 write_file。"

    count = text.count(old_string)
    if count == 0:
        # 给一条能自我纠正的线索：常见原因是缩进/空白不一致
        probe = old_string.strip().splitlines()[0] if old_string.strip() else ""
        hint = ""
        if probe and probe in text:
            hint = f"\n提示：内容存在但空白/缩进不一致，确认 old_string 的缩进（文件用 {'  ' if '  ' in probe[:4] else '可能是 Tab'}）。"
        return f"没找到这段内容，未做修改。{hint}"

    if count > 1 and not replace_all:
        return (
            f"这段内容在文件里出现了 {count} 次，不唯一。\n"
            "请把 old_string 扩大到能唯一定位（多带几行上下文），"
            "或者显式传 replace_all=true 全部替换。"
        )

    updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    try:
        textio.write_text(target, updated)
    except textio.EncodingError as exc:
        return f"没有修改：{exc}"
    except OSError as exc:
        return f"写入失败：{exc}"
    removed = len(text.splitlines())
    added = len(updated.splitlines())
    return (
        f"已替换 {ctx.relative(target)}（{'全部 ' + str(count) + ' 处' if replace_all else '1 处'}，"
        f"行数 {removed} → {added}）"
    )


def grep(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    glob: str = "",
    max_results: int = 60,
    ignore_case: bool = True,
) -> str:
    """按内容找。返回 path:line: 内容，一行一条。"""
    try:
        base = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"

    try:
        regex = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as exc:
        return f"正则写错了：{exc}"

    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", "dist", "build"}
    matches: list[str] = []
    files = [base] if base.is_file() else list(base.rglob(glob or "*"))

    for file in files:
        if not file.is_file():
            continue
        if any(part in skip_dirs for part in file.parts):
            continue
        if file.stat().st_size > 2_000_000:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                snippet = line.strip()[:200]
                matches.append(f"{ctx.relative(file)}:{lineno}: {snippet}")
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break

    if not matches:
        return f"没有匹配「{pattern}」的内容。"
    head = f"匹配「{pattern}」共 {len(matches)} 条" + ("（已达上限）" if len(matches) >= max_results else "")
    return head + "\n" + "\n".join(matches)


def find_files(ctx: ToolContext, pattern: str = "*", path: str = ".", max_results: int = 200) -> str:
    try:
        base = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea"}
    hits: list[str] = []
    for file in base.rglob(pattern):
        if any(part in skip_dirs for part in file.parts):
            continue
        hits.append(ctx.relative(file))
        if len(hits) >= max_results:
            break
    if not hits:
        return f"没有匹配「{pattern}」的文件。"
    return f"匹配「{pattern}」共 {len(hits)} 个：\n" + "\n".join(sorted(hits))


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容。depth 可到 3 层，默认 1 层。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对项目根的路径，默认当前目录"},
                    "depth": {"type": "integer", "description": "递归深度 1~3，默认 1"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读一个文本文件，返回**带行号**的内容。默认读前 400 行。"
                "大文件用 offset/limit 分段读，别一次读到底。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "从第几行开始（1 起），默认 1"},
                    "limit": {"type": "integer", "description": "读多少行，默认 400，最大 2000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "整篇写入（覆盖）一个文件，父目录会自动创建。"
                "**只是改几行请用 edit_file**，不要整篇重写——那样容易丢掉你没注意到的内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "精确替换文件里的一段内容。old_string 必须在文件中**唯一出现**（无法唯一就多带几行上下文）。"
                "找不到会返回失败且不改文件，据此可以自我纠正。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "要被替换的原内容，注意缩进要和文件完全一致"},
                    "new_string": {"type": "string", "description": "替换成的新内容"},
                    "replace_all": {"type": "boolean", "description": "是否替换所有出现，默认 false"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "按内容搜索（支持正则），返回 文件:行号: 内容。找函数、找变量、找调用点都用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "搜索起始目录，默认项目根"},
                    "glob": {"type": "string", "description": "只搜匹配的文件，例如 *.py"},
                    "max_results": {"type": "integer", "description": "最多返回多少条，默认 60"},
                    "ignore_case": {"type": "boolean", "description": "默认 true"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "按文件名模式找文件，例如 **/*.tsx、*.md。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，默认 *"},
                    "path": {"type": "string", "description": "起始目录，默认项目根"},
                },
            },
        },
    },
]

HANDLERS = {
    "list_dir": list_dir,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "grep": grep,
    "glob": find_files,
}
