"""审计日志：每个工具判定都留一条，用哈希链串起来。

为什么不是普通日志：普通日志能改。而审计的价值恰恰在于「事后能证明没被动过」——
所以每条都带前一条的哈希，改任何一条都会让它后面整串对不上。

    hash = sha256(prev_hash + 规范化后的字段)

这不防住有意的伪造（能改文件的人当然也能整串重算），但它能挡住最常见的两种：
「事后补一条」「悄悄删一条」。要真防篡改得上外部存储，那是另一个量级的事。

落盘是 JSONL，一行一条：追加不用读全文，也不会因为半截写入毁掉整份历史。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from .paths import STATE

AUDIT_FILE = STATE / "audit.jsonl"
MAX_MEMORY = 400        # 内存里只留最近这些条，界面也只展示这么多

_lock = threading.RLock()
_chain: list[dict] | None = None


def _digest(prev: str, payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def _load() -> list[dict]:
    global _chain
    if _chain is not None:
        return _chain
    rows: list[dict] = []
    try:
        text = AUDIT_FILE.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    _chain = rows[-MAX_MEMORY:]
    return _chain


def append(actor: str, action: str, target: str, verdict: str, detail: str = "",
           cid: str = "") -> dict:
    """记一条。返回落盘的那条（带 hash）。"""
    with _lock:
        chain = _load()
        prev = str(chain[-1].get("hash") or "") if chain else ""
        body = {
            "ts": round(time.time(), 3),
            "actor": str(actor or "")[:60],
            "action": str(action or "")[:60],
            "target": str(target or "")[:400],
            "verdict": str(verdict or "")[:20],
            "detail": str(detail or "")[:400],
            "cid": str(cid or "")[:40],
            "prev": prev,
        }
        item = {**body, "hash": _digest(prev, body)}
        chain.append(item)
        if len(chain) > MAX_MEMORY:
            del chain[:-MAX_MEMORY]
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with AUDIT_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return item


def entries(limit: int = 120) -> list[dict]:
    rows = _load()
    return list(rows[-max(1, min(limit, MAX_MEMORY)):])[::-1]


def verify() -> dict:
    """从头到尾重算一遍哈希链。返回 {ok, total, broken_at}。"""
    rows = _load()
    prev = ""
    for index, item in enumerate(rows):
        body = {k: item.get(k) for k in
                ("ts", "actor", "action", "target", "verdict", "detail", "cid", "prev")}
        if str(body.get("prev") or "") != prev:
            return {"ok": False, "total": len(rows), "broken_at": index,
                    "why": "prev 指针对不上（中间有人删过或插入过）"}
        if _digest(prev, body) != str(item.get("hash") or ""):
            return {"ok": False, "total": len(rows), "broken_at": index,
                    "why": "内容与哈希对不上（这一条被改过）"}
        prev = str(item.get("hash") or "")
    return {"ok": True, "total": len(rows), "broken_at": -1, "why": ""}


def stats() -> dict:
    rows = _load()
    counts: dict[str, int] = {}
    for item in rows:
        verdict = str(item.get("verdict") or "?")
        counts[verdict] = counts.get(verdict, 0) + 1
    return {"total": len(rows), "by_verdict": counts, "file": str(AUDIT_FILE)}


def clear() -> int:
    """清空（换机器/日志太长时用）。清空本身也留一条，否则「链突然变短」无法解释。"""
    with _lock:
        global _chain
        rows = _load()
        size = len(rows)
        try:
            if AUDIT_FILE.is_file():
                AUDIT_FILE.unlink()
        except OSError:
            pass
        _chain = []
        append("system", "audit.clear", f"删掉 {size} 条", "allow", "链路从这里重新开始")
        return size
