"""检查点：把「一轮对话改过的所有文件」当成一个可以整体退回的快照。

为什么不是 Git：用户的项目未必在版本控制里，而且他要退的是「agent 刚才那一轮」，
不是某个 commit。一次 agent 回合可能改五个文件，逐个回滚要靠人记着改过哪些 ——
那就等于没有回滚。

为什么按 (会话, 轮次) 聚合，而不是按时间窗口：时间是猜的，轮次是确定的。
一轮对话就是一次「它想干什么」的最小单位，退回去正好是「当我没说过这句话」。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import store, textio, trash


def _key(cid: str, turn: int) -> str:
    return f"{cid}#{turn}"


def list_all(limit: int = 60, cid: str = "") -> list[dict]:
    """从后往前扫，同一轮只出一张卡。

    给 `cid` 就只出这条对话的 —— 整轮回退现在挂在「改动 → 对话 → 产物」那一栏里，
    用户已经站在某条对话上了，再让他从全量清单里挑一次自己，是多余的。
    """
    grouped: dict[str, dict] = {}
    for item in store.history():
        owner = str(item.get("cid") or "")
        turn = int(item.get("turn") or 0)
        if not owner or (cid and owner != cid):
            continue
        key = _key(owner, turn)
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "id": key,
                "cid": owner,
                "turn": turn,
                "ts": float(item.get("ts") or 0),
                "files": [],
                "project": item.get("project") or "",
            }
            grouped[key] = entry
        else:
            entry["ts"] = min(entry["ts"], float(item.get("ts") or 0))
        path = str(item.get("path") or "")
        if path and all(f["path"] != path for f in entry["files"]):
            entry["files"].append({
                "path": path,
                "name": Path(path).name,
                "existed": bool(item.get("existed", True)),
                "exists_now": Path(path).is_file(),
            })

    items = sorted(grouped.values(), key=lambda entry: -entry["ts"])[:limit]
    for entry in items:
        entry["count"] = len(entry["files"])
        # 这条对话还在不在。检查点也必须跟对话绑死：对话删了还留着「退回这里」，
        # 点下去会去写一批已经不属于任何对话的文件，那是越权。
        entry["alive"] = store.session_exists(entry["cid"])
        # 这一轮之后，同一个文件又被改过没有？改过就不能简单退 —— 会连带抹掉后面的改动
        entry["stacked"] = _stacked_after(entry)
    return items


def _stacked_after(entry: dict) -> bool:
    paths = {f["path"] for f in entry["files"]}
    for item in store.history():
        if str(item.get("cid")) != entry["cid"]:
            continue
        if int(item.get("turn") or 0) <= entry["turn"]:
            continue
        if str(item.get("path")) in paths:
            return True
    return False


def restore(checkpoint_id: str) -> dict:
    """把这一轮涉到的每个文件写回它在这一轮之前的样子。

    同一文件在一轮里被改多次时，history 里有多条；只保留**最早那一份 before**
    才是「这一轮开始前」的状态 —— 逆序写会让最后一次改动的内容留在盘上。
    """
    cid, _, turn_text = (checkpoint_id or "").partition("#")
    try:
        turn = int(turn_text)
    except ValueError:
        return {"ok": False, "error": "检查点标识不对"}

    entries = [
        item for item in store.history()
        if str(item.get("cid")) == cid and int(item.get("turn") or 0) == turn
    ]
    if not entries:
        return {"ok": False, "error": "没有这个检查点"}
    # 历史里还留着、会话已经没了 —— 这种检查点不再是「入口」，只是一条旧记录
    if not store.session_exists(cid):
        return {"ok": False,
                "error": "这个检查点所属的对话已经删除，不能再退回。"}

    first_for_path: dict[str, dict] = {}
    for item in entries:                      # history 是按时间追加的，第一条就是最早的
        path = str(item.get("path") or "")
        if path and path not in first_for_path:
            first_for_path[path] = item

    restored, failed = [], []
    for path, item in first_for_path.items():
        target = Path(path)
        try:
            if bool(item.get("existed", True)):
                textio.write_text(target, str(item.get("before") or ""))
                restored.append(path)
            else:
                # 这一轮新建的文件，退回就该消失；但「消失」要走回收站 ——
                # 整个产品的原则是「删东西一律进回收站」，检查点没有理由例外
                # （`changes.restore` 早就这么做了，这里当时漏了）。
                trash.to_trash(target)
                restored.append(path)
        except (OSError, textio.EncodingError) as exc:
            failed.append({"path": path, "error": str(exc)})

    for item in entries:
        item["restored_ts"] = time.time()
    _mark(entries)

    return {"ok": not failed, "restored": restored, "failed": failed,
            "count": len(restored)}


def _mark(entries: list[dict]) -> None:
    """在历史条目上打个标记，列表里就能显示「已退回」。

    不删条目：删了就没法知道「这一轮曾经发生过」。历史是账本，不是待办清单。
    """
    seen = {(item.get("path"), item.get("ts")) for item in entries}
    data = store.history()
    for item in data:
        if (item.get("path"), item.get("ts")) in seen:
            item["restored"] = True
    store.write_json(store.HISTORY_FILE, {"updated": time.time(), "items": data})
