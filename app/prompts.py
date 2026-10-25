"""提示词库：收藏起来、下次还能用的那几句话。

为什么它值得一个独立的小存储，而不是塞进设置里：它是**越用越值钱**的东西。
一条好的指令是改了三五遍才成形的，丢了就得重来一遍。所以：

- 落盘在 `state/prompt_library.json`，一行一个对象，坏了只丢那一条的解析；
- **只存文本，不存用法**。它不是「模板系统」，没有变量、没有版本链 ——
  优化浮层那边的版本链是「这一次改了几版」，这里是「这几句我想留着」，
  两件事混在一起会变成一个谁都不敢改的中间层；
- 上限 300 条。收藏夹的意义是「挑出来的」，不是「全都留着」；
  没有上限的收藏夹三个月后会变成第二个历史记录。

标签存成字符串数组，去重、去空、限长 —— 界面上它们是几枚可点的胶囊。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .paths import STATE, ensure_dirs

FILE = STATE / "prompt_library.json"
MAX_ITEMS = 300

_lock = threading.RLock()
_cache: list[dict] | None = None


def _clean_tags(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [one for one in raw.replace("，", ",").split(",")]
    seen: list[str] = []
    for item in (raw or []):
        name = str(item).strip().lstrip("#").strip()
        if name and name not in seen:
            seen.append(name[:16])
        if len(seen) >= 6:
            break
    return seen


def _load() -> list[dict]:
    global _cache
    with _lock:
        if _cache is None:
            rows: list[dict] = []
            if FILE.is_file():
                try:
                    data = json.loads(FILE.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        rows = [row for row in data if isinstance(row, dict) and row.get("id")]
                except (json.JSONDecodeError, OSError):
                    rows = []
            _cache = rows
        return _cache


def _save() -> None:
    with _lock:
        ensure_dirs()
        tmp = FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_load(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(FILE)


def list_all() -> list[dict]:
    """新的在前。收藏夹看的是「最近收了什么」，不是字母序。"""
    with _lock:
        return sorted((dict(row) for row in _load()),
                      key=lambda row: row.get("created") or 0, reverse=True)


def add(title: str, text: str, tags=None, source: str = "") -> dict:
    body = str(text or "").strip()
    if not body:
        raise ValueError("正文是空的")
    with _lock:
        items = _load()
        # 同一句话收两次没有意义，直接把原来那条顶上来
        for row in items:
            if row.get("text") == body:
                row["created"] = time.time()
                row["title"] = str(title or row.get("title") or "")[:60]
                _save()
                return dict(row)
        if len(items) >= MAX_ITEMS:
            items.sort(key=lambda row: row.get("created") or 0)
            del items[0]
        record = {
            "id": uuid.uuid4().hex[:8],
            "title": str(title or body.splitlines()[0] or "未命名")[:60],
            "text": body[:20000],
            "tags": _clean_tags(tags),
            "source": str(source or "手写")[:20],
            "created": time.time(),
        }
        items.append(record)
        _save()
        return dict(record)


def update(pid: str, patch: dict) -> dict | None:
    with _lock:
        for row in _load():
            if row.get("id") != pid:
                continue
            if "title" in patch:
                row["title"] = str(patch.get("title") or row["title"])[:60]
            if "text" in patch and str(patch.get("text") or "").strip():
                row["text"] = str(patch["text"]).strip()[:20000]
            if "tags" in patch:
                row["tags"] = _clean_tags(patch.get("tags"))
            _save()
            return dict(row)
    return None


def remove(pid: str) -> bool:
    with _lock:
        items = _load()
        before = len(items)
        items[:] = [row for row in items if row.get("id") != pid]
        if len(items) == before:
            return False
        _save()
        return True
