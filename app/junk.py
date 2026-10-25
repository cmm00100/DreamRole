"""找出「一看就是垃圾」的东西。

故意保守：宁可漏掉，也不能把用户的东西当垃圾建议删。
只认三类硬规则，不做任何「看起来像临时文件」的猜测。
"""
from __future__ import annotations

import os
from pathlib import Path

JUNK_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    ".cache",
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".idea", "dist", "build"}
JUNK_SUFFIXES = {".pyc", ".pyo", ".tmp", ".temp", ".bak", ".orig", ".rej", ".swp", ".swo"}
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
MAX_DEPTH = 6
MAX_ITEMS = 300


def _rel(target: Path, root: Path) -> str:
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(target).replace("\\", "/")


def _stats(path: Path) -> tuple[int, int]:
    count = size = 0
    for base, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            try:
                size += (Path(base) / name).stat().st_size
                count += 1
            except OSError:
                continue
    return count, size


def scan(root: Path, limit: int = MAX_ITEMS) -> list[dict]:
    root = Path(root).resolve()
    found: list[dict] = []

    for base, dirs, files in os.walk(root):
        depth = len(Path(base).relative_to(root).parts)
        if depth >= MAX_DEPTH:
            dirs[:] = []
            continue

        keep: list[str] = []
        for name in dirs:
            if name in SKIP_DIRS:
                continue
            if name in JUNK_DIRS:
                target = Path(base) / name
                count, size = _stats(target)
                found.append({"path": _rel(target, root), "kind": "dir", "files": count, "size": size})
                continue
            keep.append(name)
        dirs[:] = keep

        for name in files:
            target = Path(base) / name
            if name in JUNK_NAMES or target.suffix.lower() in JUNK_SUFFIXES:
                try:
                    size = target.stat().st_size
                except OSError:
                    continue
                found.append({"path": _rel(target, root), "kind": "file", "files": 1, "size": size})

        if len(found) >= limit:
            break

    found.sort(key=lambda x: x["size"], reverse=True)
    return found[:limit]


def is_junk(rel_path: str) -> bool:
    """这条路是不是扫描规则认得出来的垃圾（用来校验模型给的路径）。"""
    parts = [p for p in str(rel_path).replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    if any(part in JUNK_DIRS for part in parts):
        return True
    name = parts[-1]
    return name in JUNK_NAMES or Path(name).suffix.lower() in JUNK_SUFFIXES
