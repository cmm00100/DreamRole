"""权限与审批。

四档权限不是四套代码，而是**一张判定表**。每个工具声明自己属于哪一类
（read / write / danger），权限决定这一类在当前档位下是放行、要问、还是直接拒绝。

这样加一个工具只需要在 `app/tools/registry.py` 里标注类别，不用回来改权限逻辑。
"""
from __future__ import annotations

from enum import Enum

from . import config

# 工具分类。没登记的一律按 danger 处理（保守：新工具默认需要确认）。
READ_TOOLS = {
    "list_dir", "read_file", "grep", "glob", "scan_junk",
    "web_search", "web_fetch", "list_windows",
    "memory_read", "skill_load", "todo_write",
    "sqlite_query", "image_info", "calculate", "list_processes",
    # 第三批：查一件事不该触发审批。「看一眼」和「改一下」必须分开算。
    "read_many", "file_stat", "tail_file", "diff_text", "find_duplicates",
    "which_command", "csv_query", "json_query", "text_stats",
    "system_info", "port_check", "env_var", "now", "date_math",
    # 读开始菜单和注册表建清单，不启动东西
    "list_software",
}
WRITE_TOOLS = {
    "write_file", "edit_file", "run_command",
    "copy_file", "move_file", "make_dir", "apply_patch", "archive",
    "search_replace", "download",
    # 出站请求单独算一类：它不动本地文件，但会把数据发出去
    "http_request",
}
DANGER_TOOLS = {
    "delete_file", "remove_junk",
    "open_target", "focus_window", "screenshot",
    "type_text", "press_keys", "mouse_click", "clipboard",
    # 启动本机软件：会往用户屏幕上弹东西，和 open_target 同一类
    "open_software",
    "memory_write", "subagent", "schedule_create",
}

# 权限档位 → 每一类的动作
#   allow = 直接执行；gate = 停下来问；deny = 拒绝并说明原因
MATRIX: dict[str, dict[str, str]] = {
    "readonly": {"read": "allow", "write": "deny", "danger": "deny"},
    "standard": {"read": "allow", "write": "gate", "danger": "gate"},
    "staged":   {"read": "allow", "write": "allow", "danger": "gate"},
    "full":     {"read": "allow", "write": "allow", "danger": "allow"},
}

PERMISSION_LABELS = {
    "readonly": "只读 · 只允许查看看",
    "standard": "标准 · 写文件和执行命令要确认",
    "staged": "分阶段 · 只在阶段交接和危险动作前确认",
    "full": "全权 · 不再打断（危险动作仍会记录）",
}


class Verdict(str, Enum):
    ALLOW = "allow"
    GATE = "gate"
    DENY = "deny"


def category(tool: str) -> str:
    if tool in READ_TOOLS:
        return "read"
    if tool in WRITE_TOOLS:
        return "write"
    return "danger"


def current_level() -> str:
    level = str(config.get().get("permission") or "standard")
    return level if level in MATRIX else "standard"


def is_remembered(tool: str) -> bool:
    """用户点过「总是允许」的工具，本机永久记住（存在 settings.approvals）。"""
    approvals = config.get().get("approvals") or {}
    return approvals.get(tool) == "always"


def remember(tool: str) -> None:
    config.merge_map("approvals", {tool: "always"})


def forget(tool: str) -> None:
    approvals = dict(config.get().get("approvals") or {})
    approvals.pop(tool, None)
    config.update({"approvals": approvals})


def decide(tool: str, level: str | None = None) -> tuple[Verdict, str]:
    """返回 (判定, 给用户/模型看的理由)。"""
    level = level or current_level()
    cat = category(tool)
    action = MATRIX.get(level, MATRIX["standard"]).get(cat, "gate")

    if action == "allow":
        return Verdict.ALLOW, ""
    if action == "deny":
        reason = {
            "write": "当前是「只读」权限，不能写文件或执行命令",
            "danger": "当前是「只读」权限，不能执行有副作用的操作",
        }.get(cat, "当前权限不允许这个操作")
        return Verdict.DENY, reason

    if is_remembered(tool):
        return Verdict.ALLOW, ""
    return Verdict.GATE, "这一步会改动文件或影响系统，需要你确认"


def describe() -> dict:
    level = current_level()
    return {
        "level": level,
        "label": PERMISSION_LABELS.get(level, level),
        "matrix": MATRIX,
        "approvals": config.get().get("approvals") or {},
    }
