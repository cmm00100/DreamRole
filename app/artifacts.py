"""一个对话往磁盘上写了什么。

为什么不另建一本「产物账」：轨迹本来就是这产品的阅读对象（打开一条会话看它
怎么长出来的），再造一份清单，迟早会和轨迹对不上 —— 而两边对不上是最难查的
那类问题。所以产物从两个已有的地方读：

- **轨迹里的工具调用** —— 谁在什么时候写了什么，一轮一轮都在；
- **改动历史**（store.history）—— 只有它知道「这个文件之前存不存在」，
  以及能复现「这一轮之前长什么样」。

两处按路径对齐：轨迹给结构和轮次，历史给新建/修改的判定和 diff 所需的快照。
历史被 1200 条上限裁掉时，轨迹这边仍然兜得住（只是看不到 diff）。

三类产物分开标，因为用户最先想知道的是「哪些是我原来没有的」，
而不是「动了几个文件」。
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from . import store

# 会落到磁盘上的写文件工具。run_command 也算写，但它写什么、写到哪，
# 从参数里看不出来 —— 与其猜，不如不列，免得给出一个看起来完整其实漏的清单。
WROTE = ("write_file", "edit_file")

# screenshot 的返回值形如「已截图（整个屏幕，1920×1080）→ D:\x\shots\shot-1.png」
SHOT_RE = re.compile(r"→\s*(.+?\.(?:png|jpe?g|webp|bmp))", re.IGNORECASE)


def _abs(project: str, raw: str) -> Path | None:
    """按 tools/base.py 里 resolve 的规则收敛：相对路径挂在工作区下。"""
    raw = str(raw or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    if not project:
        return None
    return (Path(project) / candidate).resolve()


def _rel(project: str, target: Path) -> str:
    try:
        return str(target.resolve().relative_to(Path(project).resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return str(target)


def collect(cid: str, project: str = "") -> dict:
    """这一条会话的产物清单。"""
    events = store.load_trace(cid)
    hist = [item for item in store.history() if str(item.get("cid") or "") == cid]

    # 路径 -> 这一条会话里最早/最晚的那次改动
    first: dict[str, dict] = {}
    last: dict[str, dict] = {}
    turns: dict[str, list[int]] = {}
    writes: dict[str, int] = {}
    for item in hist:
        path = str(item.get("path") or "")
        if not path:
            continue
        first.setdefault(path, item)
        last[path] = item
        writes[path] = writes.get(path, 0) + 1
        turn = int(item.get("turn") or 0)
        if turn and turn not in turns.setdefault(path, []):
            turns[path].append(turn)

    found: dict[str, dict] = {}
    shots: dict[str, dict] = {}

    for event in events:
        kind = str(event.get("type") or "")
        if kind == "action":
            payload = event.get("payload") or {}
            tool = str(event.get("tool") or "")
            if tool in WROTE:
                args = payload.get("arguments") or {}
                target = _abs(project, args.get("path"))
                if target is None:
                    continue
                key = str(target)
                if key in found:
                    continue   # 路径已由历史判定过，别被覆盖成「修改」
                turns_here = [int(event.get("step_id") or 0)]
                found[key] = {
                    "path": key,
                    "name": target.name,
                    "rel": _rel(project, target),
                    "kind": "written",
                    "turns": [t for t in turns_here if t],
                    "ts": float(event.get("ts") or 0),
                }
        elif kind == "result":
            tool = str(event.get("tool") or "")
            if tool != "screenshot":
                continue
            matched = SHOT_RE.search(str(event.get("content") or ""))
            if not matched:
                continue
            target = _abs(project, matched.group(1).strip())
            if target is None:
                continue
            key = str(target)
            shots[key] = {
                "path": key,
                "name": target.name,
                "rel": _rel(project, target),
                "kind": "shot",
                "turns": [int(event.get("step_id") or 0)] if event.get("step_id") else [],
                "ts": float(event.get("ts") or 0),
            }

    # 历史那边有的，以它为准（只有它知道 existed 和 before/after）
    for path, item in last.items():
        target = Path(path)
        entry = found.get(path) or {
            "path": path,
            "name": target.name,
            "rel": _rel(project, target),
            "kind": "written",
            "turns": turns.get(path, []),
            "ts": float(item.get("ts") or 0),
        }
        entry["kind"] = "created" if not first[path].get("existed", True) else "modified"
        entry["turns"] = sorted(set(turns.get(path, entry.get("turns") or [])))
        entry["writes"] = writes.get(path, 1)
        entry["has_diff"] = True
        entry["ts"] = float(item.get("ts") or entry.get("ts") or 0)
        found[path] = entry

    items: list[dict] = []
    for path, entry in list(found.items()) + list(shots.items()):
        target = Path(path)
        alive = target.is_file()
        items.append({
            **entry,
            "kind": entry.get("kind") or "written",
            "exists": alive,
            "ext": target.suffix.lower().lstrip("."),
            "size": target.stat().st_size if alive else 0,
            "writes": entry.get("writes", 1),
            "has_diff": bool(entry.get("has_diff")),
        })

    # 新建排在改动前面，同类里新的在前 —— 「这次多出来了什么」通常是第一眼要看的
    order = {"created": 0, "modified": 1, "written": 2, "shot": 3}
    items.sort(key=lambda one: (order.get(one["kind"], 9), -float(one["ts"] or 0)))

    counts = {"created": 0, "modified": 0, "written": 0, "shot": 0}
    for one in items:
        counts[one["kind"]] = counts.get(one["kind"], 0) + 1

    return {
        "ok": True,
        "cid": cid,
        "project": project,
        "items": items,
        "counts": counts,
        "total": len(items),
    }


def diff_text(cid: str, path: str) -> str | None:
    """这一条会话对这个文件做过什么：第一次之前 → 最后一次之后。

    用这条路而不是 /api/changes/diff：后者只留每个文件的最后一次改动，
    换了别的会话再改一遍，diff 就成了别人的。
    """
    mine = [item for item in store.history()
            if str(item.get("cid") or "") == cid and str(item.get("path") or "") == path]
    if not mine:
        return None
    before = str(mine[0].get("before") or "")
    after = str(mine[-1].get("after") or "")
    name = Path(path).name
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{name}（这次对话之前）",
        tofile=f"{name}（现在）",
        n=3,
    )
    return "".join(diff) or "（内容完全一致）"
