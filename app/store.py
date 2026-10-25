"""落盘：一条会话一个轨迹文件 + 一个侧车 meta。

为什么不是一个巨大的数据库：这个产品的核心阅读方式就是「打开一条会话看它怎么长出来的」，
一个文件一个会话最符合这个心智，而且天然可分享、可拷贝、可用文本工具 grep。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .events import Event, EventType
from .paths import STATE, TRACES, ensure_dirs

# 这个模块里的账本都是「读一份 JSON → 改 → 整份写回」。两个会话线程同时这么干，
# 后写的会把先写的整段覆盖掉（实测 8 个线程写 200 条，只剩 21 条）。
# 并行子代理、多个会话同时跑，都是真会发生的事，所以所有读改写都排进这把锁。
_LOCK = threading.RLock()


def safe_name(name: str) -> str:
    """cid 会变成文件名，只留安全字符。"""
    keep = "".join(c for c in (name or "") if c.isalnum() or c in "-_")
    return keep[:64]


# ---------------------------------------------------------------------------
# 范围（scope）：同一批会话按「谁在聊」分目录
# ---------------------------------------------------------------------------

SCOPES_FILE = STATE / "trace-scopes.json"
_scopes: dict[str, str] | None = None


def _load_scopes() -> dict[str, str]:
    global _scopes
    if _scopes is None:
        data = read_json(SCOPES_FILE) or {}
        _scopes = {str(k): str(v) for k, v in (data.get("items") or {}).items() if v}
    return _scopes


def _remember_scope(cid: str, scope: str) -> None:
    """把 cid 落在哪个子目录记下来。

    为什么要一本索引，而不是「到处 rglob 找一遍」：轨迹文件会被大量读取
    （列表、导出、回放、产物），每次都全盘递归扫一遍在会话攒到几百条以后
    会明显变慢，而且扫不到时无法区分「没这条会话」和「找错地方了」。
    """
    with _LOCK:
        index = _load_scopes()
        scope = (scope or "").strip()
        if not scope:
            if index.pop(cid, None) is None:
                return
        else:
            if index.get(cid) == scope:
                return
            index[cid] = scope
        write_json(SCOPES_FILE, {"updated": time.time(), "items": index})


def scope_of(cid: str) -> str:
    return _load_scopes().get(cid, "")


def _folder(scope: str) -> Path:
    scope = safe_name(scope)
    return TRACES / scope if scope else TRACES


def trace_path(cid: str) -> Path:
    return _folder(scope_of(cid)) / f"{safe_name(cid)}.json"


def meta_path(cid: str) -> Path:
    return _folder(scope_of(cid)) / f"{safe_name(cid)}.meta.json"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_json(path: Path, data) -> None:
    """原子的 JSON 落盘。

    两个 Windows 上真实会遇到的坑：
    1. `os.replace` 会偶发 WinError 5 —— 杀软正在扫这个文件、或者搜索索引刚好
       摸了一下。它不是逻辑错误，退几十毫秒再来就好，所以这里重试三次。
    2. 临时文件名如果固定成 `.tmp`，两个线程同时写同一个路径会互相踩。带上线程号。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    blob = json.dumps(data, ensure_ascii=False, indent=1)
    tmp.write_text(blob, encoding="utf-8")

    for attempt, wait in enumerate((0.0, 0.05, 0.15, 0.35)):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 3:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(wait)


# ---------------------------------------------------------------------------
# 轨迹
# ---------------------------------------------------------------------------

def save_trace(cid: str, events: list[Event], meta: dict) -> Path:
    ensure_dirs()
    data = [e.to_dict() for e in events]
    scope = str(meta.get("assistant") or scope_of(cid) or "")
    _remember_scope(cid, scope)
    path = trace_path(cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)

    existing = read_json(meta_path(cid)) or {}
    merged = {
        **existing,
        **meta,
        "cid": cid,
        "trace": path.name,
        "events": len(data),
        "created": existing.get("created") or meta.get("created") or time.time(),
        "updated": time.time(),
    }
    # 用户改过标题就别被第一句话盖回去；置顶/归档同理
    for keep in ("title", "pinned", "archived"):
        if existing.get(keep):
            merged[keep] = existing[keep]
    write_json(meta_path(cid), merged)
    return path


def load_trace(cid: str) -> list[dict]:
    data = read_json(trace_path(cid))
    return data if isinstance(data, list) else []


def load_meta(cid: str) -> dict:
    return read_json(meta_path(cid)) or {}


def patch_meta(cid: str, **fields) -> dict:
    meta = load_meta(cid)
    meta.update(fields)
    write_json(meta_path(cid), meta)
    return meta


def list_traces(project: str | None = None, include_archived: bool = False,
                assistant: str | None = None) -> list[dict]:
    """列会话。`assistant` 给值时只看这个助理名下的（含群聊）。"""
    ensure_dirs()
    items: list[dict] = []
    # 轨迹可能落在根目录，也可能落在某个助理的子目录里
    for file in TRACES.rglob("*.json"):
        if file.name.endswith(".meta.json"):
            continue
        cid = file.stem
        meta = load_meta(cid)
        if not meta:
            raw = read_json(file) or []
            first_user = next(
                (e.get("content", "") for e in raw if e.get("type") == "user"), "未命名会话"
            )
            meta = {
                "cid": cid,
                "title": " ".join(str(first_user).split())[:60],
                "project": "",
                "created": file.stat().st_mtime,
                "events": len(raw) if isinstance(raw, list) else 0,
                "legacy": True,
            }
        if project and str(Path(meta.get("project") or "")) != str(Path(project)):
            continue
        if assistant is not None:
            if str(meta.get("assistant") or "") != assistant:
                continue
        if meta.get("archived") and not include_archived:
            continue
        meta.setdefault("trace", file.name)
        # 侧车文件可能被人手改过、也可能只有 title 没有 cid。列表的键必须是真 cid，
        # 否则按 cid 建索引的地方（改动归属、检查点）会全部落到空串那一格上。
        meta["cid"] = cid
        items.append(meta)

    items.sort(key=lambda m: (not m.get("pinned"), -float(m.get("created") or 0)))
    return items


def delete_trace(cid: str) -> list[Path]:
    """返回删掉的文件清单（由调用方决定是直接删还是进回收站）。"""
    paths = [p for p in (trace_path(cid), meta_path(cid)) if p.is_file()]
    _remember_scope(cid, "")
    return paths


# ---------------------------------------------------------------------------
# 改动记录（agent 写过哪些文件，供 diff / 回滚）
#
# **改动挂在对话下面，不是一个全局的按路径账本。**
# 为什么键必须是 (cid, path) 而不是 path：同一个文件完全可能被 A 会话改过、
# 又被 B 会话改过。只按路径记，第二次就把第一次顶掉了 —— A 那个「回滚」按钮
# 会变成「回滚成 B 改之前的样子」。那不是回滚，是替 B 撤销了一次它不知道的改动。
# ---------------------------------------------------------------------------

CHANGES_FILE = STATE / "changes.json"
HISTORY_FILE = STATE / "change-history.json"
_changes: dict[str, dict] | None = None


def change_id(cid: str, path: str) -> str:
    """改动记录的主键。cid 可能是空的（旧数据），用 `-` 占位，不要留空串。"""
    return f"{cid or '-'}::{path}"


def split_change_id(key: str) -> tuple[str, str]:
    cid, _, path = (key or "").partition("::")
    return ("" if cid == "-" else cid), path


def _load_changes() -> dict[str, dict]:
    global _changes
    with _LOCK:
        if _changes is None:
            data = read_json(CHANGES_FILE) or {}
            _changes = {}
            for item in data.get("items") or []:
                path = str(item.get("path") or "")
                if not path:
                    continue
                # 旧版本按路径存，键里没有 `::`。这里直接重建成新键 ——
                # 每一行本来就带 cid，不需要额外的迁移脚本。
                key = change_id(str(item.get("cid") or ""), path)
                item["key"] = key
                _changes[key] = item
        return _changes


def changes() -> dict[str, dict]:
    """改动账的只读视图。

    返回的是映射的浅拷贝：调用方大多在遍历（`for key, item in store.changes().items()`），
    直接遍历缓存本身的话，另一个线程往 `record_change` 里加一条就会抛
    `dictionary changed size during iteration`。条目本身仍是同一批 dict，
    所以 `revert()` 那种「改条目再 save」的写法照常生效。
    """
    return dict(_load_changes())


def change_for(cid: str, path: str) -> dict | None:
    """这条会话在这个文件上的改动记录。查不到就是查不到，不跨会话兜底。"""
    store = _load_changes()
    return store.get(change_id(cid, path))


def save_changes() -> None:
    with _LOCK:
        write_json(CHANGES_FILE, {"updated": time.time(), "items": list(_load_changes().values())})


def record_change(path: str, before: str, after: str, cid: str, project: str,
                  turn: int = 0, existed: bool = True, kind: str = "edit") -> None:
    if not path or before == after:
        return
    with _LOCK:
        store = _load_changes()
        key = change_id(cid, path)
        # 这里原来有一支「上一笔已退回、而且它的 after 等于这次的 before，就把
        # before 赋成 prev["after"]」——但那个条件成立时两条本来就相等，赋值是空操作。
        # 真正想要的语义已经是天然成立的：退回之后用户再改，这一笔的 before 就是
        # 退回后的内容，账本直接覆盖旧条目即可，不需要特殊分支。
        store[key] = {
            "key": key,
            "path": path,
            "before": before,
            "after": after,
            "cid": cid,
            "project": project,
            "status": "pending",
            "kind": kind,
            "ts": time.time(),
        }
        save_changes()
        append_history(path, before, after, cid, project, turn, existed, kind=kind)


def append_history(path: str, before: str, after: str, cid: str,
                   project: str, turn: int = 0, existed: bool = True,
                   kind: str = "edit") -> None:
    """追加一条「按轮」的变更历史。

    这本账有两个用处，所以不能只当检查点的底稿：
    1. 检查点 —— 按 (会话, 轮次) 聚成「退回到这一轮之前」；
    2. 改动时间线 —— 按 (会话, 路径) 聚成「这个文件被改过几次、每次改成什么样」。

    按路径的那本账每个文件只留最后一次，做不了第 2 件事；
    所以这里**每一条都留 before 和 after**。`kind` 区分是正常改动还是「退回到某个阶段」，
    时间线上要把后者画成另一种节点，否则用户会以为那是 agent 又改了一次。

    上限只是防止文件无限长；滚动清理会丢最老的条目，时间线也因此可能不完整。
    """
    with _LOCK:
        data = read_json(HISTORY_FILE) or {}
        items = data.get("items") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        items.append({
            "path": path,
            "before": before,
            "after": after,
            "cid": cid,
            "project": project,
            "turn": int(turn or 0),
            "existed": bool(existed),
            "kind": kind,
            "ts": time.time(),
        })
        if len(items) > 1200:
            items = items[-1200:]
        write_json(HISTORY_FILE, {"updated": time.time(), "items": items})


# 老名字留着：别的地方还在按私有名调用，改名不该顺手把调用点一起改炸
_append_history = append_history


def history() -> list[dict]:
    data = read_json(HISTORY_FILE) or {}
    items = data.get("items") if isinstance(data, dict) else None
    return [item for item in (items or []) if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# 会话的存亡：改动能不能退回，第一步就是问这个
# ---------------------------------------------------------------------------

def session_exists(cid: str) -> bool:
    """这条会话还在不在。

    不能只看 `load_trace()` 返回空：一条刚建好还没说话的会话本来就是空的。
    轨迹文件和侧车文件都在，才算这条会话立在这儿。
    """
    if not cid:
        return False
    return trace_path(cid).is_file() or meta_path(cid).is_file()


# ---------------------------------------------------------------------------
# 会话删掉之后，它名下的改动和检查点记录怎么办
#
# 之前是搬进「墓园」（另一个 json 文件）：界面上不出现、撤回不生效，但证据留着。
# 后来发现这个设计是错的 —— 用户点「删除对话」时想的是「这些都别留了」，
# 结果账本里还压着一份谁也看不见的副本，只有占地方这一个效果。
# 现在改成**真删**：对话没了，它名下的改动和轮次记录一起删掉。
#
# 代价是「那条对话到底改过什么」再也查不到了。这个代价由删除确认框来承担：
# 点删除之前会先把「要清掉 N 条改动、M 个检查点」说清楚（app/changes.py 的 impact）。
# 先算代价、再动文件、最后清账本 —— 顺序反了就会留下「会话在、改动没了」。
# ---------------------------------------------------------------------------

def drop_changes(cid: str) -> int:
    """删掉这条会话的改动记录，返回删了几条。"""
    if not cid:
        return 0
    with _LOCK:
        store = _load_changes()
        keys = [key for key, item in store.items() if str(item.get("cid") or "") == cid]
        if not keys:
            return 0
        for key in keys:
            store.pop(key, None)
        save_changes()
        return len(keys)


def forget_cid(cid: str) -> dict:
    """会话删除时的级联：改动记录 + 轮次历史一起删掉。

    只清 changes 不清 history 会留下一个更坏的状态：时间线里「第 3 轮」
    还挂着，点「退回这里」却会去写一个已经不属于任何对话的文件。
    """
    if not cid:
        return {"changes": 0, "history": 0}

    dropped = drop_changes(cid)

    with _LOCK:
        data = read_json(HISTORY_FILE) or {}
        items = data.get("items") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        mine = [item for item in items if str(item.get("cid") or "") == cid]
        if mine:
            rest = [item for item in items if str(item.get("cid") or "") != cid]
            write_json(HISTORY_FILE, {"updated": time.time(), "items": rest})

    return {"changes": dropped, "history": len(mine)}


def purge_orphans() -> dict:
    """清掉「主人已经不在」的记录：没有 cid 的、cid 对应的会话已经删掉的。

    为什么需要单独一个入口：真删之后仍然会有孤儿 —— 比如用户在文件管理器里
    直接删了 data/traces 里的文件，或者手改坏了侧车文件。
    这些记录留在界面上是灰的、点不动的，属于纯噪音。
    """
    store = _load_changes()
    with _LOCK:
        doomed = [
            key for key, item in store.items()
            if not str(item.get("cid") or "") or not session_exists(str(item.get("cid") or ""))
        ]
        for key in doomed:
            store.pop(key, None)
        if doomed:
            save_changes()

        items = history()
        kept = [item for item in items
                if str(item.get("cid") or "") and session_exists(str(item.get("cid") or ""))]
        dropped = len(items) - len(kept)
        if dropped:
            write_json(HISTORY_FILE, {"updated": time.time(), "items": kept})

    return {"changes": len(doomed), "history": dropped}


