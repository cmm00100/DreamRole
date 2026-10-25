"""三层记忆：项目长期记忆 / 每日记录 / 用户偏好。

为什么要分三层，而不是一个大文件：
- **项目长期记忆**按工作目录哈希分开 —— 两个项目各自的约定绝不能串味，
  「这个仓库用 pnpm」跑到另一个仓库就是错的。
- **每日记录**按天分开 —— 它天生是流水，越写越长。混进长期记忆里，
  注入提示词时会把真正重要的约定挤出去。
- **用户偏好**不分项目 —— 「回答别用 emoji」「少解释多动手」这类话，
  换个目录依然成立；跟着项目存反而丢得快。

三层的写入时机也不同：长期记忆靠 agent 自己判断（memory_write），
每日记录靠人翻（面板里编辑），偏好是慢变量，偶尔改一次。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from .paths import MEMORY

KINDS = ("project", "daily", "pref")

KIND_LABEL = {
    "project": "项目记忆",
    "daily": "每日记录",
    "pref": "用户偏好",
}


def _root() -> Path:
    MEMORY.mkdir(parents=True, exist_ok=True)
    return MEMORY


def _digest(project: str) -> str:
    return hashlib.sha256(str(project or "default").encode("utf-8")).hexdigest()[:12]


def path_of(kind: str, project: str = "", name: str = "") -> Path:
    """条目 id 就是文件名的稳定映射，不做数据库 —— 记忆本来就该能用记事本改。"""
    root = _root()
    if kind == "pref":
        return root / "preferences.md"
    if kind == "daily":
        day = name if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name or "") else time.strftime("%Y-%m-%d")
        return root / f"daily-{day}.md"
    return root / f"project-{_digest(project)}.md"


def item_id(kind: str, project: str = "", name: str = "") -> str:
    """id 是给前端用的稳定标识：换个进程、换个时间都得一样。"""
    if kind == "pref":
        return "pref"
    if kind == "daily":
        return "daily:" + (name or time.strftime("%Y-%m-%d"))
    return "project:" + _digest(project)


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except OSError:
        return ""


def _stat(path: Path) -> dict:
    try:
        info = path.stat()
        return {"updated": info.st_mtime, "size": info.st_size}
    except OSError:
        return {"updated": 0, "size": 0}


def read(item: str, project: str = "") -> dict:
    """按 id 读一条。id 解析失败就返回空内容，不抛异常 —— 面板不该因为
    一条记忆的文件被手动删了就打不开。"""
    kind, _, name = (item or "").partition(":")
    if kind not in KINDS:
        return {"ok": False, "error": "不认识的记忆类型"}
    if kind == "daily" and not name:
        name = time.strftime("%Y-%m-%d")
    path = path_of(kind, project, name)
    return {
        "ok": True,
        "id": item,
        "kind": kind,
        "label": KIND_LABEL[kind],
        "name": name or Path(path).stem,
        "path": str(path),
        "text": _read(path),
        **_stat(path),
    }


def write(item: str, text: str, project: str = "") -> dict:
    kind, _, name = (item or "").partition(":")
    if kind not in KINDS:
        return {"ok": False, "error": "不认识的记忆类型"}
    if kind == "daily" and not name:
        name = time.strftime("%Y-%m-%d")
    path = path_of(kind, project, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (text or "").rstrip() + "\n"
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"写不进去：{exc}"}
    return {"ok": True, "id": item, "path": str(path), "size": len(body.encode("utf-8"))}


def append(item: str, text: str, project: str = "") -> dict:
    """追加一条。项目记忆的正文格式是「- [时间] 结论」，跟 agent 自己写的一致，
    否则面板加一条、agent 加一条，长出来两种样子。"""
    kind, _, name = (item or "").partition(":")
    if kind not in KINDS:
        return {"ok": False, "error": "不认识的记忆类型"}
    line = (text or "").strip()
    if not line:
        return {"ok": False, "error": "没写内容"}
    path = path_of(kind, project, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read(path)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as fh:
        if not existing.strip():
            if kind == "project":
                fh.write(f"# 项目记忆\n\n工作目录：`{project}`\n")
            elif kind == "daily":
                fh.write(f"# {name}\n")
            else:
                fh.write("# 用户偏好\n")
        fh.write(f"\n- [{stamp}] {line}\n")
    return {"ok": True, "id": item, "path": str(path)}


def delete(item: str, project: str = "") -> dict:
    kind, _, name = (item or "").partition(":")
    if kind == "pref":
        return {"ok": False, "error": "用户偏好不能删，清空内容即可"}
    path = path_of(kind, project, name)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"删不掉：{exc}"}
    return {"ok": True, "id": item}


def list_all(project: str = "") -> list[dict]:
    """列出所有条目，最近的排前面。

    每日记录会越攒越多，所以只返回最近 30 天 —— 面板是「翻一翻」的地方，
    不是归档系统；真要全量，文件就在 MEMORY 目录里。
    """
    root = _root()
    items: list[dict] = []

    for kind, path, name in [
        ("project", path_of("project", project), "本项目"),
        ("pref", path_of("pref"), "用户偏好"),
    ]:
        info = _stat(path)
        items.append({
            "id": item_id(kind, project, name),
            "kind": kind,
            "label": KIND_LABEL[kind],
            "name": name,
            "path": str(path),
            "size": info["size"],
            "updated": info["updated"],
            "lines": _count_lines(path),
            "exists": path.is_file(),
        })

    try:
        days = sorted(
            (p for p in root.glob("daily-*.md")),
            key=lambda p: p.name,
            reverse=True,
        )[:30]
    except OSError:
        days = []

    # 今天永远占一格 —— 否则「今天还没记」这件事在界面里是不存在的，
    # 用户就没法从面板起头写第一条。
    today = time.strftime("%Y-%m-%d")
    if not any(path.stem == f"daily-{today}" for path in days):
        days.insert(0, path_of("daily", name=today))

    for path in days:
        day = path.stem.replace("daily-", "")
        info = _stat(path)
        items.append({
            "id": "daily:" + day,
            "kind": "daily",
            "label": KIND_LABEL["daily"],
            "name": day,
            "path": str(path),
            "size": info["size"],
            "updated": info["updated"],
            "lines": _count_lines(path),
            "exists": path.is_file(),
        })

    return items


def _count_lines(path: Path) -> int:
    text = _read(path, 400_000)
    if not text:
        return 0
    return sum(1 for line in text.splitlines() if line.strip().startswith("- ["))


def append_daily(project: str, text: str) -> Path:
    """给会话收尾用：把这一轮做了什么写进当天的流水。"""
    item = item_id("daily")
    append(item, text, project)
    return path_of("daily")


def pref_brief(limit: int = 600) -> str:
    """注入提示词的那一小段偏好。只取条目行，不要标题和空行。"""
    text = _read(path_of("pref")).strip()
    if not text:
        return ""
    lines = [line for line in text.splitlines() if line.strip().startswith("- [")]
    if not lines:
        return ""
    return "用户偏好（长期有效，优先遵守）：\n" + "\n".join(lines[-10:])[:limit]
