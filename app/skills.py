"""Skills：扫 skills/*/SKILL.md，按需加载。

和 WorkBuddy 的做法保持一致：技能列表只把 `name` + `description` 放进系统提示，
**正文按需加载**。这样技能再多也不吃上下文。

SKILL.md 用最小的 frontmatter 约定：

    ---
    name: pdf-report
    description: 生成 PDF 报表
    ---
    # 正文（真正的做法说明）

## 两个目录，用户目录优先

| 目录 | 是什么 | 能不能写 |
|---|---|---|
| `DATA/skills` | **你自己的技能** | 能。升级、卸载都不动它 |
| `RESOURCE/skills` | 内置技能，跟着程序走 | 打包后不能 |

「不能」不是权限问题，是**结构问题**：打包成 exe 之后 `RESOURCE` 指向
`%TEMP%\\_MEIxxxx`，那是 onefile 每次启动现解出来的临时目录，**退出就删**。
往那里写技能，当次运行能看到，关掉以后就没了 —— 而且下次启动又换了个目录名。

这就是「明明让 agent 加了技能，技能栏里却什么都没有」的真正原因：
从前只有一个目录，agent 写进去的是那个每次都会消失的地方。
（当时它还自己写了个 `sync_skills.py`，想把 `%LOCALAPPDATA%\\DreamRole\\skills`
同步进 `_MEI` —— 那是在绕一个本该由程序解决的坑。）

所以现在：**读两个目录，写只写第一个**。
"""
from __future__ import annotations

from pathlib import Path

from . import config, paths, trash

# 名字里不能出现的字符：Windows 文件名限制 + 路径分隔符。
# 技能名同时是目录名，放开了就会出现「技能写到别人目录里」这种事。
BAD_CHARS = set('/\\:*?"<>|')
# Windows 保留设备名。技能目录叫 con 会创建失败，而且报错莫名其妙。
RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)}

BODY_LIMIT = 20_000  # 单个技能正文上限（读的时候截断）


def builtin_dir() -> Path:
    return Path(paths.RESOURCE) / "skills"


def user_dir() -> Path:
    """自己的技能放数据目录里 —— 那是唯一「写下去还在」的地方。"""
    folder = Path(paths.USER_SKILLS)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return folder


def dirs() -> list[tuple[Path, str]]:
    """(目录, 来源)，**用户目录在前** —— 同名的以后面的为准，用户就能覆盖内置技能。"""
    return [(user_dir(), "user"), (builtin_dir(), "builtin")]


def _parse(text: str) -> tuple[str, str]:
    name = desc = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            head, body = parts[1], parts[2]
            for line in head.splitlines():
                line = line.strip()
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
    if not desc:
        # 没有 frontmatter 就取第一行非空正文当描述
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                desc = line[:120]
                break
    return name, desc


def strip_front(text: str) -> str:
    """去掉 frontmatter，只留正文。"""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return text


def catalog() -> list[dict]:
    off = disabled()
    items: list[dict] = []
    seen: set[str] = set()
    for folder, source in dirs():
        if not folder.is_dir():
            continue
        try:
            children = sorted(folder.iterdir())
        except OSError:
            continue
        for child in children:
            skill_file = child / "SKILL.md"
            if not (child.is_dir() and skill_file.is_file()):
                continue
            try:
                text = skill_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, desc = _parse(text)
            name = name or child.name
            if name in seen:
                continue          # 用户目录先扫，所以同名的内置技能会被盖掉
            seen.add(name)
            items.append(
                {
                    "name": name,
                    "dir": child.name,
                    "description": desc,
                    "path": str(skill_file),
                    "chars": len(text),
                    "source": source,
                    # 只有自己的技能能改能删 —— 内置的在临时目录里，
                    # 改了下次启动就没了，删了下次启动又回来，都不该让用户以为生效了
                    "editable": source == "user",
                    # 开关。关掉 = 不进系统提示、不进 @ 候选、skill_load 也读不出来。
                    # 内置的也能关：不能改它的正文，但「这次不想让它掺和」是很正常的需求。
                    "enabled": name not in off,
                }
            )
    return items


# --------------------------------------------------------------- 开关
#
# 记「关掉的」而不是「开着的」：新建的技能默认就该是能用的，
# 记开着的那份会导致「刚写完一个技能，系统提示里没有它」——
# 而那正是当初技能栏最大的那个 bug 的长相。


def disabled() -> set[str]:
    raw = config.get().get("skills_off") or []
    return {str(item).strip() for item in raw if str(item).strip()}


def is_enabled(name: str) -> bool:
    return str(name or "").strip() not in disabled()


def set_enabled(name: str, enabled: bool) -> dict:
    """开 / 关一个技能。落盘，重启之后还是这个样子。"""
    item = find(name)
    if not item:
        raise ValueError(f"没有叫「{name}」的技能")
    off = disabled()
    if enabled:
        off.discard(item["name"])
    else:
        off.add(item["name"])
    config.update({"skills_off": sorted(off)})
    item["enabled"] = bool(enabled)
    return item


def enable_all() -> int:
    """全部打开。返回打开了几个（界面用它提示「刚开了 N 个」）。"""
    count = len(disabled())
    config.update({"skills_off": []})
    return count



def find(name: str) -> dict | None:
    target = (name or "").strip()
    for item in catalog():
        if target in (item["name"], item["dir"]):
            return item
    return None


def load(name: str) -> str:
    item = find(name)
    if not item:
        names = ", ".join(i["name"] for i in catalog()) or "（一个都没有）"
        return f"没有叫「{name}」的技能。可用：{names}"
    if not item.get("enabled", True):
        return (f"「{item['name']}」现在是**关闭**状态（用户在技能栏里把它关掉了），"
                "这一轮不加载。要用就请用户先把它的开关打开。"
                "别换个说法绕过去 —— 关掉是明确的意思。")
    try:
        return Path(item["path"]).read_text(encoding="utf-8", errors="replace")[:BODY_LIMIT]
    except OSError as exc:
        return f"读不到 {item['path']}：{exc}"


# --------------------------------------------------------------- 增 / 删


def clean_name(raw: str) -> str:
    """校验并返回能当目录名的技能名。不合法就抛 ValueError，说清哪里不对。"""
    name = " ".join(str(raw or "").split())     # 折叠空白，去掉首尾
    if not name:
        raise ValueError("技能名不能是空的")
    if len(name) > 40:
        raise ValueError("技能名太长了，40 个字以内")
    for ch in name:
        if ch in BAD_CHARS:
            raise ValueError(f"技能名里不能有 {ch}（它同时是文件夹名）")
        if ord(ch) < 32:
            raise ValueError("技能名里有控制字符")
    if name.startswith("."):
        raise ValueError("技能名不能以点开头（会被当成隐藏目录）")
    if name.lower() in RESERVED:
        raise ValueError(f"「{name}」是 Windows 的保留设备名，换个名字")
    return name


def compose(name: str, description: str, body: str) -> str:
    """拼出完整的 SKILL.md。frontmatter 由我们统一写，免得模型写出花样来。"""
    text = strip_front(str(body or "")).strip()
    lines = ["---", f"name: {name}", f"description: {' '.join(str(description or '').split())}",
             "---", "", text]
    return "\n".join(lines).rstrip() + "\n"


def create(name: str, description: str, body: str, overwrite: bool = False) -> dict:
    """在用户目录里写一个技能。已存在且没让覆盖就报错。"""
    name = clean_name(name)
    if not str(body or "").strip():
        raise ValueError("正文是空的。技能正文就是「怎么做」，不能没有")

    existing = find(name)
    if existing and not overwrite:
        where = "内置技能" if existing.get("source") == "builtin" else "你自己的技能"
        raise ValueError(f"已经有叫「{name}」的{where}了。要改它就把覆盖打开（overwrite=true）")

    folder = user_dir() / name
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "SKILL.md"
    target.write_text(compose(name, description, body), encoding="utf-8", newline="\n")

    # 刚建出来的技能必须是开着的。名字被关过、删了又建的那种情况，
    # 如果不清掉这条记录，用户会得到「建好了，但哪儿都找不到它」——
    # 这正是当初技能栏最大的那个 bug 的长相，不能换个原因重演一次。
    off = disabled()
    if name in off:
        off.discard(name)
        config.update({"skills_off": sorted(off)})

    item = find(name) or {}
    item.update({"ok": True, "path": str(target)})
    return item


def remove(name: str) -> dict:
    """删掉一个**自己的**技能。内置的拒绝，并说清楚为什么。"""
    item = find(name)
    if not item:
        raise ValueError(f"没有叫「{name}」的技能")
    if item.get("source") != "user":
        raise ValueError(
            f"「{item['name']}」是内置技能，删不掉 —— 它在程序自己的目录里，"
            "下次启动会原样回来。想换掉它，就用同名建一个自己的（会覆盖显示）")
    folder = Path(item["path"]).parent
    # 走回收站：删错一个自己写了半天的技能，还能捡回来
    if not trash.to_trash(folder) and folder.exists():
        raise ValueError(f"删不掉：{folder} 还被占用着？")
    return {"ok": True, "name": item["name"]}


# --------------------------------------------------------------- 提示词


def prompt_block(limit: int = 24) -> str:
    """给系统提示用的那一段：只有名字和一句话，不带正文。"""
    # 关掉的技能不报名字 —— 报了名，模型就会去 skill_load 它，
    # 然后拿到一句「已关闭」，白花一次调用。开关的意义就是它压根不出现。
    items = [item for item in catalog() if item.get("enabled", True)]
    if not items:
        return ""
    lines = ["可用技能（需要时用 skill_load(name) 载入完整做法）："]
    for item in items[:limit]:
        lines.append(f"- {item['name']}：{item['description'] or '（无描述）'}")
    if len(items) > limit:
        lines.append(f"- …还有 {len(items) - limit} 个")
    lines.append("（用户要你把某件事「变成技能」时，用 skill_create 写一个。）")
    return "\n".join(lines)


def turn_block(names: list[str], limit: int = 12_000) -> str:
    """用户在输入栏点名要用的技能 —— 正文直接塞进这一轮的系统提示。

    和 `prompt_block` 的区别是：那个只报名字，让模型自己决定要不要读；
    这个是用户**已经决定**了，再让它自己判断一次就是多绕一圈。
    只在当前这一轮有效（`session.run_turn` 每轮重建系统提示），不会带到下一句。

    关掉的技能即使被点名也不带 —— 但要说一句，不然用户会以为指令丢了。
    """
    picked: list[str] = []
    skipped: list[str] = []
    used = 0
    for name in names or []:
        item = find(name)
        if not item:
            continue
        if not item.get("enabled", True):
            skipped.append(item["name"])
            continue
        try:
            text = Path(item["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body = strip_front(text).strip()
        room = limit - used
        if room <= 0:
            break
        if len(body) > room:
            body = body[:room] + "\n\n…（后面截掉了：技能正文太长，这一轮只带这么多）"
        picked.append(f"## 技能：{item['name']}\n{body}")
        used += len(body)
    if not picked and not skipped:
        return ""
    chunks: list[str] = []
    if picked:
        chunks.append("用户在这一轮点名要用下面的技能。这是明确指令，不要再去判断「要不要用」：\n\n"
                      + "\n\n".join(picked))
    if skipped:
        chunks.append(
            "这一轮点名里还有 "
            + "、".join(f"「{name}」" for name in skipped)
            + "，但它们被用户在技能栏里关掉了，所以没有带上。"
              "不要假装用过它们；如果这件事非它不可，就直说「这个技能现在是关的」。")
    return "\n\n".join(chunks)
