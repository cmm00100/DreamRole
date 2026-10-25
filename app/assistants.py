"""助理：一个「有人设、有自己一间屋子」的常驻角色。

和专家（experts.py）的区别，别混：

- **专家**是「换一种看法」—— 你还是你，只是这次让它按律师的思路读一遍；
- **助理**是「另一个存在」—— 它有自己的名字、自己的说话方式、自己的项目目录、
  自己的对话记录。你切回它，接的是它那间屋子里上次没收尾的事。

所以助理必须有自己的目录，这不是「整理癖」，是功能本身：

```
data/assistants/<id>/
  assistant.json     人设与设置
  workspace/         它干活的地方（产物都写在这里）
  memory/            它自己的长期记忆
  exports/           它导出的东西
data/traces/<id>/    它的对话记录（按助理分目录，见 store.scope_of）
data/assistants/groups/<gid>.json   群聊
```

为什么群聊不是「一条会话多挂几个人」那么简单：几个助理对同一件事各说各话，
如果不规定谁先说、说完算不算数，它就会变成一场没人收尾的讨论。
所以群聊必须有一个**发言规则**（轮流 / 只点名的说 / 有人主持），
规则是一等公民，不是提示词里写一句「请有序发言」。
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from .paths import DATA

HOME = DATA / "assistants"
GROUPS = HOME / "groups"
INDEX = HOME / "index.json"

MAX_NAME = 24
MAX_PERSONA = 6000

# 新建助理时给的三个起手式。不是「模板库」，是三个不同的建议 ——
# 空白表单最难填，给三个具体的样子，用户改两个字就能用。
STARTERS = [
    {
        "name": "私人秘书",
        "emoji": "🗂",
        "tagline": "盯进度、理材料、提醒你还没做完的事",
        "persona": (
            "你是我的私人秘书。你说话简短、直接，不复述我说过的话。\n"
            "你关心三件事：**这件事截止到什么时候**、**现在卡在哪**、**下一步谁做什么**。\n"
            "我给你一堆零散材料时，你的默认动作是整理成清单，而不是总结成一段话。\n"
            "你从不替我拍板，但你会明确说「我建议 X，因为 Y」。"
        ),
    },
    {
        "name": "研究搭档",
        "emoji": "🔍",
        "tagline": "查资料、找反例、把结论的证据摆出来",
        "persona": (
            "你是我的研究搭档。你的第一反应永远是「这个说法的证据是什么」。\n"
            "你会主动找**反例**，而不是只找支持结论的材料。\n"
            "每条结论后面必须跟来源或数据；没有来源的，你会标成「待核实」。\n"
            "你区分「事实」「推断」「猜测」，并且说清哪一条是哪一类。"
        ),
    },
    {
        "name": "代码搭子",
        "emoji": "⌨️",
        "tagline": "读代码、给改法、顺手挑出会炸的地方",
        "persona": (
            "你是我的代码搭子。你先读代码再说话，不猜。\n"
            "给改法时给**最小可行的那一版**，不要顺手重构。\n"
            "你会主动指出：这段在多线程/异常/边界输入下会怎样。\n"
            "你说话像一个懒得客套的资深工程师：短句、有行号、不夸人。"
        ),
    },
]

GROUP_RULES = {
    "round_robin": "轮流说，每人一轮",
    "mention": "只让被点到名的说",
    "host": "由主持人点名，最后主持人收尾",
}


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------

def _ensure() -> None:
    for folder in (HOME, GROUPS):
        folder.mkdir(parents=True, exist_ok=True)


def safe_id(value: str) -> str:
    keep = "".join(c for c in (value or "") if c.isalnum() or c in "-_")
    return keep[:40]


def dirs(aid: str) -> dict[str, Path]:
    base = HOME / safe_id(aid)
    return {
        "base": base,
        "workspace": base / "workspace",
        "memory": base / "memory",
        "exports": base / "exports",
        "config": base / "assistant.json",
    }


def ensure_dirs(aid: str) -> dict[str, Path]:
    layout = dirs(aid)
    for key in ("workspace", "memory", "exports"):
        layout[key].mkdir(parents=True, exist_ok=True)
    return layout


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _blank(aid: str, name: str) -> dict:
    return {
        "id": aid,
        "name": name,
        "emoji": "🙂",
        "tagline": "",
        "persona": "",
        "model": "",        # 空 = 跟全局设置走
        "think": "",        # 空 = 跟全局设置走
        "permission": "",
        "mode": "",
        "created": time.time(),
        "used": 0.0,
        "turns": 0,
        "builtin": False,
    }


def get(aid: str) -> dict | None:
    key = safe_id(aid)
    if not key:
        return None
    data = _read(HOME / key / "assistant.json")
    if not isinstance(data, dict):
        return None
    data.setdefault("id", key)
    data.setdefault("name", key)
    return data


def list_all() -> list[dict]:
    _ensure()
    items: list[dict] = []
    for folder in sorted(HOME.iterdir()) if HOME.is_dir() else []:
        if not folder.is_dir():
            continue
        data = _read(folder / "assistant.json")
        if not isinstance(data, dict):
            continue
        data.setdefault("id", folder.name)
        layout = dirs(folder.name)
        data["workspace"] = str(layout["workspace"])
        data["dir"] = str(layout["base"])
        items.append(data)
    items.sort(key=lambda a: (not a.get("builtin"), -(a.get("used") or 0), a.get("created") or 0))
    return items


def create(name: str, persona: str = "", emoji: str = "🙂", tagline: str = "",
           **options) -> dict:
    _ensure()
    label = " ".join(str(name or "").split())[:MAX_NAME]
    if not label:
        raise ValueError("助理得有个名字")
    # id 和名字脱钩：名字随时能改，目录和轨迹路径不能跟着改。
    # 也不用中文当 id —— 它会出现在 URL、目录名和命令里，多一层转义就多一处坑。
    aid = "a" + secrets.token_hex(4)

    record = _blank(aid, label)
    record["persona"] = str(persona or "").strip()[:MAX_PERSONA]
    record["emoji"] = str(emoji or "🙂")[:4]
    record["tagline"] = str(tagline or "").strip()[:80]
    for key in ("model", "think", "permission", "mode"):
        if options.get(key):
            record[key] = str(options[key])
    ensure_dirs(aid)
    _write(dirs(aid)["config"], record)
    _touch_index(aid)
    return record


def update(aid: str, **fields) -> dict:
    record = get(aid)
    if not record:
        raise ValueError("没有这个助理")
    allowed = {"name", "emoji", "tagline", "persona", "model",
               "think", "permission", "mode"}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "name":
            clean = " ".join(str(value or "").split())[:MAX_NAME]
            if not clean:
                raise ValueError("名字不能空着")
            record["name"] = clean
        elif key == "persona":
            record["persona"] = str(value or "").strip()[:MAX_PERSONA]
        elif key == "emoji":
            record["emoji"] = str(value or "🙂")[:4]
        elif key == "tagline":
            record["tagline"] = str(value or "").strip()[:80]
        else:
            record[key] = str(value or "")
    _write(dirs(aid)["config"], record)
    return record


def remove(aid: str, keep_files: bool = True) -> dict:
    """删助理。默认**不动**它的工作目录和对话记录。

    这是刻意的：助理干了三个月活，误点删除就把整个 workspace 删掉，
    是灾难性的。删的只是「这个角色」，它的东西留在原地，并告诉用户在哪。
    """
    record = get(aid)
    if not record:
        raise ValueError("没有这个助理")
    if record.get("builtin"):
        raise ValueError("内置助理不能删，可以改名或改人设")
    target = dirs(aid)["config"]
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"删不掉：{exc}") from exc
    _touch_index(aid, drop=True)
    layout = dirs(aid)
    return {
        "ok": True,
        "workspace": str(layout["workspace"]),
        "kept": keep_files,
        "trace_dir": str(DATA / "traces" / safe_id(aid)),
    }


def touch(aid: str, turns: int = 1) -> None:
    record = get(aid)
    if not record:
        return
    record["used"] = time.time()
    record["turns"] = int(record.get("turns") or 0) + turns
    _write(dirs(aid)["config"], record)


# ---------------------------------------------------------------------------
# 索引：给「会话归属」用（轨迹目录按助理分，见 store.scope_of）
# ---------------------------------------------------------------------------

def _touch_index(aid: str, drop: bool = False) -> None:
    data = _read(INDEX) or {}
    items = data.get("items") if isinstance(data, dict) else None
    items = list(items or [])
    if drop:
        items = [i for i in items if i.get("id") != aid]
    elif aid not in [i.get("id") for i in items]:
        items.append({"id": aid, "at": time.time()})
    _write(INDEX, {"updated": time.time(), "items": items})


# ---------------------------------------------------------------------------
# 群聊
# ---------------------------------------------------------------------------

def _group_path(gid: str) -> Path:
    return GROUPS / f"{safe_id(gid)}.json"


def group_workspace(gid: str) -> Path:
    """群聊自己的落脚目录。

    为什么群聊也要有工作区：会话层的「没有工作区就不许跑」是一条不变量，
    群聊不该是一条例外 —— 例外一多，这条不变量就形同虚设。
    讨论不写文件，但目录仍然存在，将来「大家一起改一个东西」也用得上。
    """
    folder = GROUPS / safe_id(gid) / "workspace"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def list_groups() -> list[dict]:
    _ensure()
    out: list[dict] = []
    for file in GROUPS.glob("*.json"):
        data = _read(file)
        if not isinstance(data, dict):
            continue
        data.setdefault("id", file.stem)
        data["members"] = [a for a in (data.get("members") or []) if get(a)]
        out.append(data)
    out.sort(key=lambda g: -(g.get("created") or 0))
    return out


def get_group(gid: str) -> dict | None:
    data = _read(_group_path(gid))
    if not isinstance(data, dict):
        return None
    data.setdefault("id", safe_id(gid))
    return data


def create_group(name: str, members: list[str], rule: str = "round_robin",
                 host: str = "", topic: str = "") -> dict:
    _ensure()
    picked = [safe_id(a) for a in (members or []) if get(a)]
    if len(picked) < 2:
        raise ValueError("群聊至少要两个助理，不然那就是单聊")
    rule = rule if rule in GROUP_RULES else "round_robin"
    host = safe_id(host) if host and host in picked else ""
    if rule == "host" and not host:
        host = picked[0]
    gid = "g" + secrets.token_hex(4)
    record = {
        "id": gid,
        "name": " ".join(str(name or "").split())[:32] or "新群聊",
        "members": picked,
        "rule": rule,
        "host": host,
        "topic": str(topic or "").strip()[:400],
        "created": time.time(),
    }
    _write(_group_path(gid), record)
    return record


def update_group(gid: str, **fields) -> dict:
    record = get_group(gid)
    if not record:
        raise ValueError("没有这个群")
    if "members" in fields:
        picked = [safe_id(a) for a in (fields.get("members") or []) if get(a)]
        if len(picked) < 2:
            raise ValueError("群聊至少要两个助理")
        record["members"] = picked
    if fields.get("rule") in GROUP_RULES:
        record["rule"] = fields["rule"]
    if "host" in fields:
        host = safe_id(fields.get("host") or "")
        record["host"] = host if host in record["members"] else ""
    if record.get("rule") == "host" and not record.get("host"):
        record["host"] = record["members"][0]
    if "name" in fields:
        record["name"] = " ".join(str(fields.get("name") or "").split())[:32] or record["name"]
    if "topic" in fields:
        record["topic"] = str(fields.get("topic") or "").strip()[:400]
    _write(_group_path(gid), record)
    return record


def remove_group(gid: str) -> None:
    _group_path(gid).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 给会话层用
# ---------------------------------------------------------------------------

def system_note(record: dict) -> str:
    """把一个人设翻成追加在系统提示后面的那一段。

    刻意只加「你是谁」，不加「你能用什么工具」——工具是会话的事，
    人设不该有能力改变安全边界。
    """
    if not record:
        return ""
    name = record.get("name") or "助理"
    emoji = record.get("emoji") or ""
    persona = (record.get("persona") or "").strip()
    lines = [f"## 你现在是「{name}」{(' ' + emoji) if emoji else ''}"]
    if record.get("tagline"):
        lines.append(f"你的定位：{record['tagline']}")
    if persona:
        lines.append(persona)
    lines.append("以上是你的身份和说话方式。工具怎么用、权限多严，仍按系统规则来。")
    return "\n".join(lines)


def members_of(group: dict) -> list[dict]:
    out = []
    for aid in group.get("members") or []:
        record = get(aid)
        if record:
            out.append(record)
    return out


def pick_speakers(group: dict, text: str) -> list[dict]:
    """按群规则决定这一轮谁说话，顺序即发言顺序。

    mention 规则下如果谁都没被点名，返回空 —— 由调用方提示「点个名」。
    这里的「点名」认 @名字 和直接出现名字两种，因为用户不会记得要打 @。
    """
    members = members_of(group)
    rule = group.get("rule") or "round_robin"

    if rule == "mention":
        hit = [m for m in members if _mentioned(m, text)]
        return hit

    if rule == "host":
        host_id = group.get("host") or (members[0]["id"] if members else "")
        host = next((m for m in members if m["id"] == host_id), members[0] if members else None)
        others = [m for m in members if host and m["id"] != host["id"]]
        return ([host] if host else []) + others + ([host] if host else [])

    return members


def _mentioned(member: dict, text: str) -> bool:
    name = str(member.get("name") or "")
    if not name:
        return False
    if re.search(rf"@{re.escape(name)}", text or ""):
        return True
    return name in (text or "")


def stats(aid: str) -> dict:
    """这个助理名下有多少东西。给界面显示用，不落盘。"""
    from . import store

    layout = dirs(aid)
    traces = store.list_traces(assistant=safe_id(aid), include_archived=True)
    files = 0
    size = 0
    workspace = layout["workspace"]
    if workspace.is_dir():
        for item in workspace.rglob("*"):
            if item.is_file():
                files += 1
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
    return {
        "sessions": len(traces),
        "files": files,
        "bytes": size,
        "last": max([float(t.get("updated") or 0) for t in traces] or [0]),
        "workspace": str(workspace),
        "exports": str(layout["exports"]),
    }


def describe() -> dict:
    return {
        "ok": True,
        "items": list_all(),
        "groups": list_groups(),
        "starters": STARTERS,
        "rules": GROUP_RULES,
        "home": str(HOME),
    }
