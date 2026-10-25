"""额外授权目录（通行证）。

工作区是「主目录」，不是「唯一能碰的地方」。用户在选工作区的弹窗里
点系统对话框加进来的每个目录，就是一张通行证，带一个档：

- ``ro`` 只读：能看、能 grep、能读文件，不能改；
- ``rw`` 可写：和工作区一样。

三条规矩写在这一处，而不是散进每个工具：

1. **最长前缀优先。** 给 ``D:\\work`` 开了 rw，又给 ``D:\\work\\secret`` 开 ro，
   那 secret 里就是只读 —— 更具体的那张说了算。工作区本身也当成一张隐式 rw
   通行证参与比较，所以没有「工作区是例外」这种分支。
2. **授权不绕过安全规则。** ``rules.BUILTIN`` 里「不许写系统目录」「不许读私钥」
   是 deny，在权限档位之前就拦。把 ``C:\\Windows`` 加进来也照样写不进去。
3. **目录没了不等于授权没了。** 盘没插、目录被删时，记录留着并标 ``missing``，
   界面上说清楚。静默丢掉用户点过的授权，比留着更让人糊涂。

为什么每个会话一份、不是全局一份：全局授权等于「每条会话都带着，而你不知道」——
和以前 ``settings.json`` 里那个静默默认的 ``data/projects`` 是同一个毛病。

## 但「全局默认」是可以有的，只要它不静默

上面那条说的是**静默**的全局默认，不是「全局」本身。用户一个一个目录地给
每条新会话补通行证，补到第十次就会开始骂人 —— 这不是安全，这是折磨。

所以现在有两层：

- ``global_grants``（设置里显式勾的）—— 建会话那一刻**抄一份**进会话；
- 会话自己的 ``grants`` —— 抄完就归它自己，之后各走各的。

三件事保证了它不回到老毛病上：抄的动作只发生在 ``sessions.create``；
抄的是用户自己点过的目录（不是程序编的）；界面上会话里列着哪几张、几张是全
局带来的，都写在明面上。改全局**不会**回头动已经在的会话：
「这条会话能去哪」不该在它跑到一半时自己变。
"""
from __future__ import annotations

from pathlib import Path

from . import config

MODE_RO = "ro"
MODE_RW = "rw"
MODE_LABELS = {MODE_RO: "只读", MODE_RW: "可写"}

# 一个会话最多挂几张。不是怕性能，是怕「授权了二十个目录」之后
# 用户自己都说不清这个 agent 到底能去哪 —— 那种状态下边界等于不存在。
MAX_GRANTS = 12


def normalize_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return MODE_RW if text in ("rw", "w", "write", "readwrite", "可写") else MODE_RO


def _norm(raw: object) -> str:
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, ValueError):
        return text


def clean(raw: object) -> list[dict]:
    """把读回来的东西收拾成规范形状。

    老数据可能是一串字符串（没有档位），也可能是手改坏的字典。
    坏掉的单条直接跳过，而不是让整个会话起不来。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            path, mode = item, MODE_RO
        elif isinstance(item, dict):
            path, mode = item.get("path"), item.get("mode")
        else:
            continue
        resolved = _norm(path)
        if not resolved or resolved.lower() in seen:
            continue
        seen.add(resolved.lower())
        out.append({
            "path": resolved,
            "mode": normalize_mode(mode),
            "missing": not Path(resolved).is_dir(),
        })
    return out[:MAX_GRANTS]


def covers(root: Path | str, grants: list[dict], target: Path) -> tuple[str, str] | None:
    """谁管这个路径？返回 (模式, 命中的那个目录)，没人管返回 None。

    比较的是「目录层级深度」，不是字符串长度 —— `D:\\work` 和 `D:\\work-x`
    前缀一样，但后者不是前者的子目录。
    """
    try:
        goal = Path(target).resolve()
    except (OSError, ValueError):
        return None

    best: tuple[int, str, str] | None = None
    candidates: list[tuple[str, str]] = []
    if root:
        candidates.append((str(root), MODE_RW))
    for grant in grants or []:
        candidates.append((str(grant.get("path") or ""), normalize_mode(grant.get("mode"))))

    for base, mode in candidates:
        if not base:
            continue
        try:
            node = Path(base).resolve()
        except (OSError, ValueError):
            continue
        if goal != node and not goal.is_relative_to(node):
            continue
        depth = len(node.parts)
        # >= 让「同一路径后来改成别的档」能覆盖先前的判定
        if best is None or depth >= best[0]:
            best = (depth, mode, str(node))
    return (best[1], best[2]) if best else None


def check(root: Path | str, grants: list[dict], target: Path, need: str = "read") -> str:
    """空串 = 放行；否则返回一句给模型看的话（会原样进工具结果）。"""
    hit = covers(root, grants, target)
    if hit is None:
        count = len(grants or [])
        if count:
            scope = f"不在工作区 `{root}` 里，也不在那 {count} 个授权目录里。"
        else:
            scope = f"不在工作区 `{root}` 里。"
        return (
            f"越界了：`{target}` {scope}\n"
            f"要用这个目录，得请用户在工作区弹窗里把它加进来（我可以先用 list_dir 确认一下路径拼对了没有）。"
        )
    mode, base = hit
    if need == "write" and mode == MODE_RO:
        return (
            f"`{target}` 在只读目录 `{base}` 里，不能改。\n"
            f"看是允许的；要写的话得请用户把这张通行证改成「可写」。"
        )
    return ""


def summary(grants: list[dict]) -> str:
    """塞进系统提示的那几行。没有授权时返回空串，别给提示词添噪音。"""
    items = clean(grants)
    if not items:
        return ""
    lines = []
    for grant in items:
        tags = [MODE_LABELS[grant["mode"]]]
        if grant.get("missing"):
            tags.append("目录现在不在了")
        lines.append(f"- `{grant['path']}`（{'，'.join(tags)}）")
    return (
        "用户给这个会话额外开了这几个目录的通行证：\n"
        + "\n".join(lines)
        + "\n这些地方也能去，但**默认还是待在工作区**：只有任务真的需要时才过去，"
          "过去了顺口说一句「我去看一眼 xxx」。标「只读」的只能看，别往里写。"
    )


def describe(grants: list[dict]) -> list[dict]:
    """给界面看的形状。`global` 标「这张是全局带来的」，
    用户在会话里能一眼分清「我自己加的」和「默认就有的」。"""
    names = {grant["path"].lower() for grant in global_defaults()}
    return [
        {
            "path": grant["path"],
            "mode": grant["mode"],
            "label": MODE_LABELS[grant["mode"]],
            "missing": bool(grant.get("missing")),
            "global": grant["path"].lower() in names,
        }
        for grant in clean(grants)
    ]


def add(grants: list[dict], path: str, mode: str = MODE_RO) -> tuple[list[dict], str]:
    """加一张。返回 (新列表, 一句说明)。已经有的就改档，不重复加。"""
    resolved = _norm(path)
    if not resolved:
        return clean(grants), "没给路径。"
    items = clean(grants)
    for grant in items:
        if grant["path"].lower() == resolved.lower():
            grant["mode"] = normalize_mode(mode)
            return items, f"`{resolved}` 已经在列表里，档位改成「{MODE_LABELS[grant['mode']]}」。"
    if len(items) >= MAX_GRANTS:
        return items, f"最多 {MAX_GRANTS} 个授权目录。先撤掉一个再加。"
    items.append({
        "path": resolved,
        "mode": normalize_mode(mode),
        "missing": not Path(resolved).is_dir(),
    })
    return items, f"已把 `{resolved}` 加进来（{MODE_LABELS[normalize_mode(mode)]}）。"


def remove(grants: list[dict], path: str) -> tuple[list[dict], str]:
    resolved = _norm(path)
    items = clean(grants)
    kept = [g for g in items if g["path"].lower() != resolved.lower()]
    if len(kept) == len(items):
        return items, f"列表里没有 `{resolved}`。"
    return kept, f"已撤掉 `{resolved}` 的通行证。"


def set_mode(grants: list[dict], path: str, mode: str) -> tuple[list[dict], str]:
    resolved = _norm(path)
    items = clean(grants)
    for grant in items:
        if grant["path"].lower() == resolved.lower():
            grant["mode"] = normalize_mode(mode)
            return items, f"`{resolved}` 改成「{MODE_LABELS[grant['mode']]}」。"
    return items, f"列表里没有 `{resolved}`。"


# --------------------------------------------------------------- 全局默认
#
# 存在 settings.json 的 global_grants 里。它是「新会话起手就有哪些通行证」，
# 不是「所有会话共用一份」—— 见文件顶部那段。


def global_defaults() -> list[dict]:
    return clean(config.get().get("global_grants") or [])


def set_global_defaults(items: list[dict]) -> list[dict]:
    cleaned = clean(items)
    config.update({"global_grants": cleaned})
    return cleaned


def update_global(op: str, path: str = "", mode: str = "") -> tuple[list[dict], str]:
    """改全局那张单子。op 与「会话级」完全一致，界面上一套写法两处能用。"""
    items = global_defaults()
    if op == "add":
        items, note = add(items, path, mode or MODE_RO)
        note = f"全局：{note}"
    elif op == "remove":
        items, note = remove(items, path)
        note = f"全局：{note}"
    elif op == "mode":
        items, note = set_mode(items, path, mode)
        note = f"全局：{note}"
    elif op == "clear":
        note = f"已撤掉全部 {len(items)} 张全局通行证。" if items else "本来就没有全局通行证。"
        items = []
    else:
        return describe(items), f"不认识的操作：{op}"
    return set_global_defaults(items), note


def inherit(session_grants: list[dict]) -> list[dict]:
    """建会话时把全局抄进来。同名以**会话自己的**为准（用户当场改过就该算数）。"""
    items = global_defaults()
    own = clean(session_grants)
    by_path = {grant["path"].lower(): grant for grant in items}
    for grant in own:
        by_path[grant["path"].lower()] = grant
    return clean(list(by_path.values()))
