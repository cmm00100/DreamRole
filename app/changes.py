"""改动记录的归属，以及「这一条还能不能退回」。

这个模块单独存在的理由：一条改动能不能退，取决于**四件互相独立的事** ——
谁改的、那条对话还在不在、对话是不是正跑着、磁盘上的东西还是不是这次改出来的。
把这四条散在各个接口里，加一条新原因就要改三处，而且判定顺序一乱，
给用户看的理由就变成假话（比如「已经退回过了」其实是因为对话被删了）。

所以集中在这里，按固定顺序判，只报第一个说得出口的理由。
界面拿 `blocked` 决定按钮灰不灰，接口拿同一份判定决定拒不拒 ——
**界面置灰是提示，接口校验才是防线**，两者必须用同一个函数，否则迟早对不上。
"""
from __future__ import annotations

import difflib
import time
from pathlib import Path

from . import store, textio

# 每一种「退不了」都要能直接说给用户听。不要写「操作失败」——
# 用户看到失败时的第一个问题永远是「为什么」。
REASONS = {
    "no_owner": "这条记录没有归属对话（旧版本留下的），找不回是哪次改动",
    "no_session": "所属对话已删除，这条改动不能再退回",
    "running": "所属对话正在跑，等这一轮结束再退",
    "reverted": "这条改动已经退回过了",
    "drifted": "磁盘上的内容已经被改过，退回会覆盖后来的改动",
    "no_record": "找不到这条改动记录",
    "not_mine": "这条改动不属于这个对话",
}

# 判定顺序就是这份列表的顺序。no_owner 必须排在 no_session 前面 ——
# 一条没有 cid 的记录，问「它的对话还在吗」是没有意义的。
_ORDER = ("no_owner", "no_session", "running", "reverted", "drifted")


def _running(cid: str) -> bool:
    from . import session as session_module

    live = session_module.get(cid)
    return bool(live and live.running)


def _sessions() -> dict[str, dict]:
    """所有着会话的元信息，含已归档的。

    已归档的会话**照样能退回** —— 归档是「列表里收起来」，不是「作废」。
    把它算成失效，用户会以为归档等于删除，从此不敢点。
    """
    return {str(meta.get("cid") or ""): meta
            for meta in store.list_traces(include_archived=True)}


def _row(key: str, item: dict, meta: dict, alive: bool) -> dict:
    path = str(item.get("path") or "")
    target = Path(path)
    exists = target.is_file()
    after = str(item.get("after") or item.get("before") or "")

    drifted = False
    if exists:
        try:
            drifted = textio.read_text(target) != after
        except OSError:
            drifted = False

    owner = str(item.get("cid") or "")
    facts = {
        "no_owner": not owner,
        "no_session": bool(owner) and not alive,
        "running": bool(owner) and alive and _running(owner),
        "reverted": item.get("status") == "reverted",
        "drifted": drifted,
    }
    # 「磁盘漂移」不是硬拦，是「再确认一下」：它还能退，但得用户明说 force。
    # 其余几种退不了就是退不了 —— 给个 force 就能过的校验，不叫校验。
    blocked = next((code for code in _ORDER if code != "drifted" and facts[code]), "")
    needs_force = facts["drifted"] and not blocked
    reason = REASONS.get(blocked) or (REASONS["drifted"] if needs_force else "")

    return {
        "id": key,
        "path": path,
        "name": target.name,
        "cid": owner,
        "project": item.get("project") or "",
        "status": item.get("status") or "pending",
        "ts": item.get("ts"),
        "reverted_ts": item.get("reverted_ts"),
        "exists": exists,
        "drifted": drifted,
        # 会话这边的现状，界面要用来分组和标注
        "session_alive": alive,
        "session_title": str(meta.get("title") or ""),
        "session_archived": bool(meta.get("archived")),
        "session_pinned": bool(meta.get("pinned")),
        # 能不能退，以及为什么不能
        "blocked": blocked,
        "reason": reason,
        "revertible": not blocked,
        "needs_force": needs_force,
    }


def describe(cid: str = "", include_dead: bool = True) -> list[dict]:
    """列出改动记录。给 cid 就只看这条对话的。

    `include_dead=False` 用来在界面上把「对话都没了」的那些收起来 ——
    它们已经不是可操作的东西了，混在能点的行里只会让人误点。
    """
    index = _sessions()
    rows: list[dict] = []
    for key, item in store.changes().items():
        owner = str(item.get("cid") or "")
        if cid and owner != cid:
            continue
        # 存亡用文件判定，不用索引 —— 索引是从文件建的，但用户手删 data 目录里的
        # 某一个文件时，两者的更新时间可能差一拍。文件在才叫在。
        alive = store.session_exists(owner)
        row = _row(key, item, index.get(owner) or {}, alive)
        if not include_dead and row["blocked"] == "no_session":
            continue
        rows.append(row)
    rows.sort(key=lambda row: -float(row.get("ts") or 0))
    return rows


def summary(cid: str = "") -> dict:
    rows = describe(cid)
    return {
        "total": len(rows),
        "revertible": sum(1 for row in rows if row["revertible"]),
        "dead": sum(1 for row in rows if row["blocked"] == "no_session"),
        "reverted": sum(1 for row in rows if row["status"] == "reverted"),
    }


def diff(cid: str, path: str, stage: int | None = None) -> tuple[str, str]:
    """(diff 文本, 错误)。错误非空时文本无意义。

    对话没了，它的改动详情也不再给看 —— 这不是小气：那条对话删掉之后，
    这里只剩一份没有上下文的文本差异，留着只会让人以为「还能撤」。

    `stage` 给了就看时间线上**那一次**改动的差异（第 k 阶段 = 第 k 次改动干的事），
    不给就看这个文件最近一次。界面上点哪一行就该看哪一行，不该只剩最后一次能看。
    """
    if not store.session_exists(cid):
        return "", REASONS["no_session"]

    if stage is not None:
        entries, _contents, _truncated, _disk, _exists = _stage_source(cid, path)
        try:
            index = int(stage)
        except (TypeError, ValueError):
            return "", "阶段号要是整数"
        if index <= 0 or index > len(entries):
            return "", "没有这个阶段"
        item = entries[index - 1]
        name = Path(path).name
        text = "".join(difflib.unified_diff(
            str(item.get("before") or "").splitlines(keepends=True),
            str(item.get("after") or "").splitlines(keepends=True),
            fromfile=f"{name}（第 {index} 次改动前）",
            tofile=f"{name}（第 {index} 次改动后）", n=3,
        ))
        return text or "（内容完全一致）", ""

    item = store.changes().get(store.change_id(cid, path))
    if item is None:
        owner = _owner_of(path)
        if owner and owner != cid:
            return "", REASONS["not_mine"]
        return "", REASONS["no_record"]
    before = str(item.get("before") or "")
    after = str(item.get("after") or "")
    name = Path(path).name
    text = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"{name}（改之前）", tofile=f"{name}（改之后）", n=3,
    ))
    return text or "（内容完全一致）", ""


def _owner_of(path: str) -> str:
    for item in store.changes().values():
        if str(item.get("path") or "") == path:
            return str(item.get("cid") or "")
    return ""


def revert(cid: str, path: str, force: bool = False) -> dict:
    """退回一条改动。

    `force` 只对「磁盘漂移」有效 —— 别的原因（对话没了、正在跑、已退回）
    是真的不该退，给个 force 就能过的校验不叫校验。
    """
    # 先问对话在不在。对话没了，不管账上还有没有那条记录，答案都是「不能再退」——
    # 这比「找不到记录」准确：记录确实可能已经被级联搬走了。
    if not store.session_exists(cid):
        return {"ok": False, "code": "no_session", "error": REASONS["no_session"]}

    key = store.change_id(cid, path)
    item = store.changes().get(key)
    if item is None:
        owner = _owner_of(path)
        if owner and owner != cid:
            return {"ok": False, "code": "not_mine", "error": REASONS["not_mine"]}
        return {"ok": False, "code": "no_record", "error": REASONS["no_record"]}

    index = _sessions()
    row = _row(key, item, index.get(cid) or {}, True)

    if row["blocked"]:
        return {"ok": False, "code": row["blocked"], "error": row["reason"]}
    if row["needs_force"] and not force:
        return {"ok": False, "code": "drifted", "error": REASONS["drifted"]}

    target = Path(path)
    had_file = target.is_file()
    try:
        textio.write_text(target, str(item.get("before") or ""))
    except (OSError, textio.EncodingError) as exc:
        return {"ok": False, "code": "io", "error": f"写不回去：{exc}"}

    item["status"] = "reverted"
    item["reverted_ts"] = time.time()
    item["reverted_by"] = cid
    store.save_changes()
    return {
        "ok": True,
        "path": path,
        "cid": cid,
        "recreated": not had_file,
        "note": "" if had_file else "这个文件之前已经不在磁盘上了，退回等于把它重建出来。",
    }


def impact(cid: str) -> dict:
    """删掉这条对话会连带作废什么。删除确认框就靠这个说实话。"""
    rows = describe(cid)
    history = [item for item in store.history() if str(item.get("cid") or "") == cid]
    turns = {int(item.get("turn") or 0) for item in history}
    return {
        "cid": cid,
        "changes": len(rows),
        "revertible": sum(1 for row in rows if row["revertible"]),
        "files": [row["path"] for row in rows],
        "checkpoints": len(turns),
        "running": _running(cid),
    }


# ---------------------------------------------------------------------------
# 三级下钻：对话 → 文件产物 → 改动时间线
#
# 为什么是三级而不是平铺：「退回」这个动作的对象从来不是一条改动记录，
# 而是「那个对话里的那个文件的那个阶段」。平铺成一张清单时，这三个问题
# 被压成了一行文字，用户得自己在脑子里重建归属关系 —— 于是就有了
# 「改动和对话没关联上」这个感受。层级不是装饰，它就是这个关系的形状。
# ---------------------------------------------------------------------------

# 一个文件最多展示多少个阶段。超出的部分会被裁掉，只留最近的 ——
# 一个被改过 200 次的文件，时间线再长也没人翻到底，倒会让接口变慢。
MAX_STAGES = 40


def threads(include_dead: bool = True) -> list[dict]:
    """第一级：哪些对话留下过改动。

    归属以**历史账本**为准，不只看按路径那本账 —— 按路径的每个文件只留最后一条，
    一条对话如果只改过 A 文件而 A 后来又被别的对话改过，它就整条从清单里消失了。
    """
    index = _sessions()
    hist: dict[str, list[dict]] = {}
    for item in store.history():
        hist.setdefault(str(item.get("cid") or ""), []).append(item)

    rows = describe("", include_dead=True)
    by_cid: dict[str, list[dict]] = {}
    for row in rows:
        by_cid.setdefault(row["cid"], []).append(row)

    out: list[dict] = []
    for cid in set(hist) | set(by_cid):
        group = by_cid.get(cid) or []
        entries = hist.get(cid) or []
        alive = store.session_exists(cid) if cid else False
        if not alive and not include_dead:
            continue
        meta = index.get(cid) or {}
        paths = {row["path"] for row in group} | {
            str(item.get("path") or "") for item in entries if item.get("path")
        }
        stamps = [float(row.get("ts") or 0) for row in group]
        stamps += [float(item.get("ts") or 0) for item in entries]
        out.append({
            "cid": cid,
            "title": str(meta.get("title") or "")
                     or ("（对话已删除）" if cid else "（旧记录，没有归属）"),
            "alive": alive,
            "archived": bool(meta.get("archived")),
            "pinned": bool(meta.get("pinned")),
            "project": (group[0]["project"] if group else
                        str(entries[0].get("project") or "") if entries else ""),
            "files": len(paths),
            "changes": len(entries) or len(group),
            "revertible": sum(1 for row in group if row["revertible"]),
            "dead": sum(1 for row in group if row["blocked"] == "no_session"),
            "turns": len({int(item.get("turn") or 0) for item in entries}),
            "last_ts": max(stamps) if stamps else 0.0,
        })
    out.sort(key=lambda item: -float(item["last_ts"] or 0))
    return out


def files(cid: str) -> dict:
    """第二级：这条对话动过哪些文件产物。"""
    index = _sessions()
    meta = index.get(cid) or {}
    rows = {row["path"]: row for row in describe(cid)}

    grouped: dict[str, list[dict]] = {}
    for item in store.history():
        if str(item.get("cid") or "") != cid:
            continue
        path = str(item.get("path") or "")
        if path:
            grouped.setdefault(path, []).append(item)

    out: list[dict] = []
    for path in set(rows) | set(grouped):
        if not path:
            continue
        row = rows.get(path) or {}
        entries = sorted(grouped.get(path) or [], key=lambda item: float(item.get("ts") or 0))
        target = Path(path)
        try:
            size = target.stat().st_size if target.is_file() else 0
        except OSError:
            size = 0
        out.append({
            "path": path,
            "name": target.name,
            "dir": str(target.parent),
            "exists": target.is_file(),
            "size": size,
            "stages": len(entries) + (1 if entries else 0),
            "changes": len(entries) or 1,
            "first_ts": float(entries[0].get("ts") or 0) if entries else row.get("ts"),
            "last_ts": float(entries[-1].get("ts") or 0) if entries else row.get("ts"),
            "revertible": bool(row.get("revertible")),
            "blocked": row.get("blocked", ""),
            "reason": row.get("reason", "") or ("" if entries else REASONS["no_record"]),
            "needs_force": bool(row.get("needs_force")),
            "drifted": bool(row.get("drifted")),
            "status": row.get("status", ""),
        })
    out.sort(key=lambda item: -float(item["last_ts"] or 0))
    return {
        "cid": cid,
        "title": str(meta.get("title") or "") or ("（对话已删除）" if cid else "（旧记录）"),
        "alive": store.session_exists(cid) if cid else False,
        "archived": bool(meta.get("archived")),
        "files": out,
    }


def _stage_source(cid: str, path: str) -> tuple[list[dict], list[str], bool, str, bool]:
    """(历史条目, 每个阶段的内容, 是否被裁过, 磁盘当前内容, 文件是否在)。

    contents 比 entries 多一个：第 0 个是「最初」（第一条的 before），
    之后第 k 个是第 k 次改动之后的 after。
    """
    entries = [
        item for item in store.history()
        if str(item.get("cid") or "") == cid and str(item.get("path") or "") == path
    ]
    entries.sort(key=lambda item: float(item.get("ts") or 0))
    truncated = len(entries) > MAX_STAGES
    if truncated:
        entries = entries[-MAX_STAGES:]

    target = Path(path)
    exists = target.is_file()
    disk = ""
    if exists:
        try:
            disk = textio.read_text(target)
        except OSError:
            exists = False

    contents: list[str] = []
    if entries:
        contents.append(str(entries[0].get("before") or ""))
    for item in entries:
        contents.append(str(item.get("after") or ""))
    return entries, contents, truncated, disk, exists


def timeline(cid: str, path: str) -> dict:
    """第三级：这个文件被改过哪几个阶段，现在停在哪一个。

    阶段 0 是「最初」—— 对话第一次碰它之前的样子。它的内容只存在于
    第一条记录的 `before` 里；如果这个文件本来就是 agent 新建的，那阶段 0 就是「不存在」。
    """
    entries, contents, truncated, disk, exists = _stage_source(cid, path)
    row = next((item for item in describe(cid) if item["path"] == path), {})

    stages: list[dict] = []
    if entries:
        first = entries[0]
        stages.append({
            "stage": 0,
            "kind": "origin",
            "label": "最初",
            "ts": float(first.get("ts") or 0),
            "turn": 0,
            "bytes": len(contents[0]),
            "existed": bool(first.get("existed", True)),
        })
    for index, item in enumerate(entries, start=1):
        kind = str(item.get("kind") or "edit")
        stages.append({
            "stage": index,
            "kind": kind,
            "label": "退回到第 " + str(_restore_target(item)) + " 阶段" if kind == "restore"
                     else "第 " + str(index) + " 次改动",
            "ts": float(item.get("ts") or 0),
            "turn": int(item.get("turn") or 0),
            "bytes": len(contents[index]),
            "existed": True,
        })

    current = None
    if exists:
        for index in range(len(contents) - 1, -1, -1):
            if contents[index] == disk:
                current = index
                break
    for stage in stages:
        stage["current"] = stage["stage"] == current

    return {
        "cid": cid,
        "path": path,
        "name": Path(path).name,
        "exists": exists,
        "drifted": exists and current is None,
        "current": current,
        "truncated": truncated,
        "changes": len(entries),
        "blocked": row.get("blocked", ""),
        "reason": row.get("reason", "") or ("" if entries else REASONS["no_record"]),
        "revertible": bool(row.get("revertible")),
        "stages": stages,
    }


def _restore_target(item: dict) -> int:
    """一条「退回」记录当时退到了第几阶段。记不上就退回 0，别猜。"""
    try:
        return int(item.get("restore_to", 0))
    except (TypeError, ValueError):
        return 0


def restore(cid: str, path: str, stage: int, force: bool = False) -> dict:
    """把文件写回时间线上的第 `stage` 个阶段。

    和 `revert()` 的区别：revert 只能撤**最近一次**，restore 能挑任何一段 ——
    这正是「退回到哪个阶段」要的能力。两个动作共用同一套「能不能退」的判定，
    所以对话删了、正在跑、磁盘漂移这些理由，表现完全一样。
    """
    if not store.session_exists(cid):
        return {"ok": False, "code": "no_session", "error": REASONS["no_session"]}
    if _running(cid):
        return {"ok": False, "code": "running", "error": REASONS["running"]}

    entries, contents, _truncated, disk, exists = _stage_source(cid, path)
    if not entries:
        owner = _owner_of(path)
        if owner and owner != cid:
            return {"ok": False, "code": "not_mine", "error": REASONS["not_mine"]}
        return {"ok": False, "code": "no_record", "error": REASONS["no_record"]}

    try:
        index = int(stage)
    except (TypeError, ValueError):
        return {"ok": False, "code": "bad_stage", "error": "阶段号要是整数"}
    if index < 0 or index >= len(contents):
        return {"ok": False, "code": "bad_stage", "error": "没有这个阶段，刷新一下看看现在有几个"}

    content = contents[index]
    origin_existed = bool(entries[0].get("existed", True))
    # 退到「最初」而且那时候这个文件根本不存在 → 该删掉，而不是写一个空文件
    remove = index == 0 and not origin_existed

    if remove:
        if not exists:
            return {"ok": False, "code": "same", "error": "这个文件已经不在磁盘上了，不用再退"}
    else:
        if exists and disk == content:
            return {"ok": False, "code": "same", "error": "磁盘上现在就是这个阶段的内容"}

    # 漂移：磁盘上的东西跟时间线最后一阶段对不上，说明中间有人动过。
    # 退到「当前所在阶段」不算覆盖，不用 force。
    head = contents[-1]
    drifted = exists and disk != head and not (exists and disk == content)
    if drifted and not force:
        return {"ok": False, "code": "drifted", "error": REASONS["drifted"]}

    target = Path(path)
    try:
        if remove:
            from . import trash

            trash.to_trash(target)
        else:
            textio.write_text(target, content)
    except (OSError, textio.EncodingError) as exc:
        return {"ok": False, "code": "io", "error": f"写不回去：{exc}"}

    # 退回本身也是一次改动，记进账本：
    # 不记的话，按路径那本账还停在「最新内容」，下次漂移判定就会开始说假话。
    project = str(entries[-1].get("project") or "")
    new_text = "" if remove else content
    store.record_change(path, disk if exists else "", new_text, cid, project,
                        turn=int(entries[-1].get("turn") or 0),
                        existed=exists, kind="restore")
    # 在历史条目上补一个「退到第几阶段」，时间线才能把它画成「退回」而不是「又改了一次」
    _tag_restore(cid, path, new_text, index)

    note = ""
    if remove:
        note = "这个文件在对话开始前并不存在，已经把它移进回收站。"
    elif not exists:
        note = "这个文件之前已经不在磁盘上了，退回等于把它重建出来。"
    return {
        "ok": True,
        "cid": cid,
        "path": path,
        "stage": index,
        "action": "removed" if remove else "written",
        "bytes": len(new_text),
        "note": note,
    }


def _tag_restore(cid: str, path: str, content: str, target_stage: int) -> None:
    """给刚追加的那条历史盖上「退到第几阶段」。

    靠 (会话, 路径, after) 的回溯最近一条来定位，而不是拿时间戳去猜 ——
    同一毫秒里连着写两次的情况是真会发生的。
    """
    items = store.history()
    for item in reversed(items):
        if str(item.get("cid") or "") != cid:
            continue
        if str(item.get("path") or "") != path:
            continue
        if str(item.get("kind") or "") != "restore":
            continue
        if str(item.get("after") or "") == content:
            item["restore_to"] = int(target_stage)
            store.write_json(store.HISTORY_FILE, {"updated": time.time(), "items": items})
            return
