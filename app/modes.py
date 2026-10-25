"""交互模式：一份模式 = 一段提示词 + 一组工具白名单 + 一个默认权限档。

提示词放在 `app/prompts/*.md`，不写在代码里。改文案不用碰 Python，
这条纪律是 PromptOptimizer 那个项目最值得抄的一点。
"""
from __future__ import annotations

from pathlib import Path

from . import config, context, grants as grants_mod
from .paths import RESOURCE

PROMPT_DIR = RESOURCE / "app" / "prompts"

MODES: dict[str, dict] = {
    "craft": {
        "label": "执行",
        "hint": "把事做完",
        "permission": "standard",
        "allow": None,  # 全部工具
    },
    "plan": {
        "label": "规划",
        "hint": "只出方案",
        "permission": "standard",
        "allow": {"list_dir", "read_file", "grep", "glob", "web_search", "web_fetch", "set_stage", "memory_read", "skill_load", "todo_write"},
    },
    "ask": {
        "label": "问答",
        "hint": "不写代码",
        "permission": "readonly",
        "allow": {"list_dir", "read_file", "grep", "glob", "web_search", "web_fetch", "memory_read", "skill_load", "todo_write"},
    },
    "teach": {
        "label": "带练",
        "hint": "一步一步教",
        "permission": "staged",
        "allow": None,
    },
}

DEFAULT_MODE = "craft"
_fragment_cache: dict[str, str] = {}

# 每个模式实际按哪一档权限跑。
#
# 这里不能写成「取更严的那个」：教学模式声明的 staged 比用户默认的 standard 更宽，
# 取严的话断点永远不触发，教学模式就退化成普通模式了。所以逐个模式显式写清楚：
#   ask / plan —— 一律只读，跟用户选什么无关（这两个模式本来就不该动文件）
#   craft      —— 完全听用户的
#   teach      —— 强制分阶段；只有用户明确选了「只读」或「全权」才尊重用户
MODE_PERMISSION = {
    "craft": lambda user: user,
    "plan": lambda user: "readonly",
    "ask": lambda user: "readonly",
    "teach": lambda user: user if user in ("readonly", "full") else "staged",
}


def effective_permission(mode: str, user_level: str) -> str:
    rule = MODE_PERMISSION.get(normalize(mode))
    level = rule(user_level) if rule else user_level
    return level if level in ("readonly", "standard", "staged", "full") else "standard"


def _fragment(name: str) -> str:
    if name in _fragment_cache:
        return _fragment_cache[name]
    path = PROMPT_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    _fragment_cache[name] = text
    return text


def reload_prompts() -> None:
    _fragment_cache.clear()


def normalize(mode: str | None) -> str:
    return mode if mode in MODES else DEFAULT_MODE


def allowed_tools(mode: str, all_names: set[str]) -> set[str]:
    allow = MODES.get(normalize(mode), {}).get("allow")
    return set(all_names) if allow is None else (set(all_names) & allow)


def system_prompt(mode: str, project: str = "", permission: str = "",
                  grants: list | None = None) -> str:
    mode = normalize(mode)
    parts = [
        f"你在 DreamRole 里工作。当前工作目录（项目根）：`{project or '未指定'}`。",
        _fragment("base"),
        "-" * 40,
        _fragment(mode),
    ]

    # 额外授权目录。没有就不写这一段 —— 空着比写一句「你没有任何额外授权」好，
    # 后者会变成模型眼里的一条待办（「我是不是该申请点什么」）。
    extra = grants_mod.summary(grants or [])
    if extra:
        parts.append("-" * 40)
        parts.append(extra)

    brief = context.memory_brief(project) if project else ""
    if brief:
        parts.append("-" * 40)
        parts.append(brief)

    perm = permission or config.get().get("permission")
    label = {
        "readonly": "只读（写操作会被拒绝）",
        "standard": "标准（写文件/执行命令前会请用户确认）",
        "staged": "分阶段（只在阶段交接和危险动作前确认）",
        "full": "全权（不再打断）",
    }.get(str(perm), str(perm))
    parts.append("-" * 40)
    parts.append(f"当前权限：{label}。")

    # 个性化：用户自己写的那几句话。放在最后，优先级高于上面的通用规矩 ——
    # 通用规矩是「一般不这样」，用户写的是「我就要这样」。
    note = str(config.get().get("personal_note") or "").strip()
    if note:
        parts.append("-" * 40)
        parts.append("用户对这个助手的要求（优先级最高）：\n" + note[:1200])

    return "\n\n".join(p for p in parts if p)
