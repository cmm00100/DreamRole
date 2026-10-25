"""自动记忆：一轮跑完之后，把值得留下的东西落下来。

**为什么需要它。** 在 1.5.6 之前，记忆只有一条入口 —— 模型主动调 `memory_write`。
而 `memory_write` 归在 danger 类，标准权限下还要用户点一次确认。结果是
记忆目录一直是空的：模型在正常对话里几乎不会主动去记，就算想记，也被那一次确认劝退了。
用户看到的现象是「聊了半天，它什么都没记住」。

**所以分两层，开关也分开。**

1. **每日记录**（确定性，不花钱）—— 从这一轮的事件流里挑事实：用户要了什么、
   动了哪些文件、跑了哪些命令、最后怎么答的。用规则拼，不调模型。
   它回答的是「那天到底发生了什么」。
2. **项目记忆**（模型抽，花一次小调用）—— 只有这一轮**真的动了东西**才跑，
   让模型抽 1~2 条「以后还用得上的约定」。它回答的是「这个项目有什么规矩」。

**两层都不走工具审批。** 这是用户开的功能按规则跑，不是模型临时起意想写。
区分这两件事很重要：前者不该每轮让你点一次确认，后者才该。

**失败一律静默。** 记忆是副产品，不能因为它把一轮对话搞崩，也不该让用户看见报错 ——
用户根本没在做「记忆」这件事，是他正在干活的时候后台顺手记了一笔。
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import config, memory
from .events import EventType

# 档位：off / daily / smart
LEVELS = ("off", "daily", "smart")
DEFAULT_LEVEL = "smart"

# 哪些工具算「动了东西」。只读的不算 —— 翻十个文件不值得记一笔。
TOUCH_TOOLS = {
    "write_file", "edit_file", "delete_file", "copy_file", "move_file",
    "make_dir", "apply_patch", "archive", "search_replace", "remove_junk",
    "download",
}
TOUCH_KEYS = ("path", "dest", "target", "source")

# 「只是看一眼」的命令不记。记满一屏 `dir` `git status` 的流水没人会回头看。
BORING_COMMANDS = (
    "dir", "ls", "cd", "pwd", "type", "cat", "head", "tail", "echo",
    "git status", "git diff", "git log", "where", "which",
)

MAX_FILES = 8
MAX_COMMANDS = 4
MAX_ENTRIES = 2


def level() -> str:
    value = str(config.get().get("auto_memory") or DEFAULT_LEVEL).strip().lower()
    return value if value in LEVELS else DEFAULT_LEVEL


def after_turn(sess, wait: bool = False) -> None:
    """一轮结束后调。默认在后台线程里跑 —— 别让用户多等那两三秒。

    `wait=True` 给测试用，否则断言会跑在写入之前，测试就成了「有时绿有时红」。
    """
    if level() == "off":
        return
    # 没有工作区就没有「项目记忆」可言，每日记录也不该凭空攒
    if not getattr(sess, "has_workspace", False):
        return
    step = sess.step_id
    if wait:
        _run(sess, step)
        return
    threading.Thread(target=_run, args=(sess, step), daemon=True).start()


def _run(sess, step: int) -> None:
    """真正干活的地方。整段包在 try 里 —— 这个函数没有「让对话失败」这个选项。"""
    try:
        facts = gather(sess, step)
    except Exception:  # noqa: BLE001
        return
    if not (facts["user"] or facts["files"] or facts["commands"]):
        return

    line = daily_line(facts)
    if line:
        try:
            memory.append("daily", line, sess.project)
        except Exception:  # noqa: BLE001 - 写不进去就算了，磁盘满了不该影响对话
            pass

    if level() != "smart":
        return
    # 纯聊天不值得花一次模型调用。只在这轮真的动了东西时才抽。
    if not facts["files"] and not facts["commands"]:
        return
    try:
        entries = extract(sess.project, facts)
    except Exception:  # noqa: BLE001 - 没 key、超时、模型抽风，都只是「这次没记成」
        return
    for entry in entries:
        if already_known(sess.project, entry):
            continue
        try:
            memory.append("project", entry, sess.project)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 从事件流里挑事实
# ---------------------------------------------------------------------------

def _relative(project: str, raw: str) -> str:
    """尽量给相对路径。绝对路径一多，每日记录就没法读了。"""
    try:
        target = Path(raw).expanduser()
        base = Path(project).expanduser()
        if target.is_absolute() and base:
            resolved = target.resolve()
            if resolved == base.resolve() or resolved.is_relative_to(base.resolve()):
                return str(resolved.relative_to(base.resolve())).replace("\\", "/") or "."
        return target.name if target.is_absolute() else str(target).replace("\\", "/")
    except (OSError, ValueError):
        return str(raw)


def _boring(command: str) -> bool:
    head = " ".join(command.strip().lower().split()[:2])
    return any(head == item or head.startswith(item + " ") for item in BORING_COMMANDS)


def gather(sess, step: int) -> dict:
    """把这一轮的事件流压成几个字段。做成纯函数，好在测试里直接喂事件。"""
    with sess._lock:                       # noqa: SLF001 - 同一个包里的模块，读快照
        events = list(sess.events)
    turn = [event for event in events if event.step_id == step]

    user = ""
    answer = ""
    files: list[str] = []
    commands: list[str] = []

    for event in turn:
        if event.type == EventType.USER and not user:
            user = " ".join(str(event.content or "").split())[:90]
        elif event.type == EventType.ANSWER:
            answer = " ".join(str(event.content or "").split())[:80]
        elif event.type == EventType.ACTION:
            name = event.tool or ""
            args = (event.payload or {}).get("arguments") or {}
            if name in TOUCH_TOOLS:
                for key in TOUCH_KEYS:
                    value = args.get(key)
                    if isinstance(value, str) and value.strip():
                        files.append(_relative(sess.project, value.strip()))
            elif name == "run_command":
                command = str(args.get("command") or "").strip()
                if command and not _boring(command):
                    commands.append(" ".join(command.split())[:70])

    seen: set[str] = set()
    unique: list[str] = []
    for path in files:
        if path not in seen:
            seen.add(path)
            unique.append(path)

    return {
        "user": user,
        "answer": answer,
        "files": unique[:MAX_FILES],
        "more": max(0, len(unique) - MAX_FILES),
        "total_files": len(unique),
        "commands": commands[:MAX_COMMANDS],
    }


def daily_line(facts: dict) -> str:
    """拼一行。写成「用户要什么 → 动了什么 → 结果」的顺序，因为人回忆时就是这个顺序。"""
    parts: list[str] = []
    if facts["user"]:
        parts.append(f"用户：{facts['user']}")
    if facts["files"]:
        listed = "、".join(facts["files"])
        if facts["more"]:
            listed += f" 等 {facts['total_files']} 个文件"
        parts.append("动了：" + listed)
    if facts["commands"]:
        parts.append("跑了：" + " ; ".join(facts["commands"]))
    if facts["answer"]:
        parts.append(f"结果：{facts['answer']}")
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 让模型抽结论
# ---------------------------------------------------------------------------

_PROMPT = (
    "你在给一个编程助手写「项目记忆」。只记**以后还用得上**的东西：\n"
    "- 这个项目/这批文件的约定（命名、目录结构、构建方式）\n"
    "- 用户明确说过的偏好（「别动那个目录」「用 pnpm 不用 npm」）\n"
    "- 踩过的坑（哪个命令在这个项目里会挂、某个字段是干什么的）\n"
    "\n"
    "**不要记「这次做了什么」** —— 那是日志，不是记忆。\n"
    "不要复述文件名清单，不要写「用户要求修改 X」这种话。\n"
    "\n"
    f"输出格式：每行一条，以「- 」开头，最多 {MAX_ENTRIES} 条。"
    "没有值得记的就输出一个空行，别硬凑。"
)


def extract(project: str, facts: dict) -> list[str]:
    """让模型抽 1~2 条。抽不出来就返回空列表，不是错误。"""
    from .llm import ChatClient

    lines = [f"这一轮发生在：`{project}`"]
    if facts["user"]:
        lines.append(f"用户说：{facts['user']}")
    if facts["files"]:
        lines.append("动过的文件：" + "、".join(facts["files"]))
    if facts["commands"]:
        lines.append("跑过的命令：" + " / ".join(facts["commands"]))
    if facts["answer"]:
        lines.append(f"最后的答复：{facts['answer']}")

    client = ChatClient()
    result = client.chat(
        [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ],
        temperature=0.1,
        max_tokens=220,
    )
    return parse_entries(str(result.get("content") or ""))


def parse_entries(text: str) -> list[str]:
    """只认 `- ` 开头的行。模型爱加前言后语，全丢掉 —— 那些进了记忆就是噪音。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith(("-", "•", "*")):
            continue
        body = line.lstrip("-•*").strip()
        body = body.strip("「」\"'` ")
        if len(body) < 4 or len(body) > 120:
            continue
        if body in out:
            continue
        out.append(body)
        if len(out) >= MAX_ENTRIES:
            break
    return out


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------

def _fingerprint(text: str) -> str:
    """把一句话压成指纹：去时间戳、去标点、去空白、只留字母数字和汉字。"""
    body = text
    if "]" in body and body.lstrip().startswith("- ["):
        body = body.split("]", 1)[-1]
    keep = [ch for ch in body if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"]
    return "".join(keep).lower()


def already_known(project: str, entry: str) -> bool:
    """这条记忆是不是已经有了。

    只做「一样」和「一边包含另一边」两种判断 —— 做语义相似度要再花一次模型调用，
    而重复记一条的代价不过是提示词里多一行。这个不对称决定了这里该懒。
    """
    new = _fingerprint(entry)
    if not new:
        return True
    try:
        item = memory.item_id("project", project)
        text = memory.read(item, project).get("text") or ""
    except Exception:  # noqa: BLE001
        return False
    for raw in text.splitlines():
        old = _fingerprint(raw)
        if not old:
            continue
        if old == new or new in old or old in new:
            return True
    return False


def describe() -> dict:
    """给面板和测试看的一眼状态。"""
    return {
        "level": level(),
        "label": {
            "off": "关闭 · 只有模型自己想起来才记",
            "daily": "每日记录 · 每轮记一笔做过什么",
            "smart": "每日记录 + 项目记忆 · 动了文件还会让模型抽结论",
        }.get(level(), level()),
    }
