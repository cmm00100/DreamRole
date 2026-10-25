"""工具注册表。

约定：
- 每个工具模块导出 `SCHEMAS` 和 `HANDLERS`，handler 签名统一 `(ctx, **kwargs)`；
- 没登记在 `HANDLERS` 里的 schema 必须在 `VIRTUAL` 里 —— 它们由会话层直接处理
  （`set_stage` 要改会话状态，不是文件系统）。自检脚本会校验这条纪律。
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import ToolContext
from . import clean, exec_cmd, extra, files, memory, pc, power, web

MODULES = (files, extra, power, exec_cmd, clean, web, pc, memory)

# 工具分组。只影响界面怎么摆（一组一折），不影响模型看到什么 ——
# 元数据一旦开始决定「模型能看到哪些工具」，排查问题就会变成猜谜。
GROUPS: dict[str, str] = {
    "list_dir": "文件", "read_file": "文件", "write_file": "文件",
    "edit_file": "文件", "apply_patch": "文件", "copy_file": "文件",
    "move_file": "文件", "make_dir": "文件", "delete_file": "文件",
    "grep": "文件", "glob": "文件", "archive": "文件", "image_info": "文件",
    "read_many": "文件", "file_stat": "文件", "tail_file": "文件",
    "diff_text": "文件", "search_replace": "文件", "find_duplicates": "文件",
    "run_command": "执行", "list_processes": "执行", "which_command": "执行",
    "scan_junk": "清理", "remove_junk": "清理",
    "web_search": "联网", "web_fetch": "联网", "http_request": "联网",
    "download": "联网",
    "open_target": "桌面", "list_windows": "桌面", "focus_window": "桌面",
    "screenshot": "桌面", "type_text": "桌面", "press_keys": "桌面",
    "mouse_click": "桌面", "clipboard": "桌面",
    "list_software": "软件", "open_software": "软件",
    "memory_read": "记忆", "memory_write": "记忆",
    "sqlite_query": "数据", "calculate": "数据", "csv_query": "数据",
    "json_query": "数据", "text_stats": "数据",
    "system_info": "系统", "port_check": "系统", "env_var": "系统",
    "now": "时间", "date_math": "时间",
    "todo_write": "协作", "set_stage": "协作", "skill_load": "协作",
    "skill_create": "协作",
    "subagent": "协作", "schedule_create": "协作",
}

# 由会话层自己处理的工具：有 schema，没有 handler
VIRTUAL = {"set_stage", "subagent", "schedule_create", "skill_load", "skill_create",
           "todo_write"}

_EXTRA_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "写下/更新这一轮的待办清单，用户可以实时看到进度。"
                "任务超过三步就先列一遍，之后每完成一步就把对应项改成 done。"
                "每次调用都要传**完整**清单，不是只传变化的那几条。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": 20,
                        "description": "完整清单，按执行顺序",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "这件事是什么，一句话"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "doing", "done"],
                                    "description": "pending=还没开始，doing=正在做，done=已完成",
                                },
                            },
                            "required": ["text"],
                        },
                    }
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_stage",
            "description": (
                "进入一个新的工作阶段。阶段是过程图上的一列，同一阶段的步骤会纵向排列。"
                "第一次调用开始第一个阶段；每换一个新阶段都要先调用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "阶段名，短而具体，例如「搭骨架」「写移动逻辑」"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_load",
            "description": "按名字加载一个技能的完整说明（SKILL.md）。先看系统提示里列出的可用技能名再加载。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "技能名（技能目录名）"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_create",
            "description": (
                "新增或改写一个技能（SKILL.md）。用户说「把这件事变成技能」「以后都这么做」"
                "「加一个 xxx 技能」时就该用它 —— 不要用 write_file 写到工作区里，"
                "那样技能栏里不会出现，下次也用不上。\n"
                "写进的是你自己的技能目录（数据目录下的 skills/），升级和卸载都不会丢。"
                "同名技能已存在时要显式 overwrite=true 才会覆盖。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "技能名，同时是目录名。短、具体，例如 pdf-report"},
                    "description": {"type": "string",
                                    "description": "一句话说明「什么时候该用这个技能」，会进系统提示"},
                    "body": {"type": "string",
                             "description": "正文（Markdown）。写清步骤、命令、判据、坑，"
                                            "让人照做就能做完 —— 不要只写一句概述"},
                    "overwrite": {"type": "boolean",
                                  "description": "同名技能已存在时是否覆盖，默认 false"},
                },
                "required": ["name", "description", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subagent",
            "description": (
                "派只读子代理去做一次专门的调查（读代码、找调用点、核对、给方案），"
                "它有自己的上下文，不会污染你这一轮。适合「需要翻很多文件但只需要结论」的事。\n"
                "role：explore=摸清结构并回答；review=找问题和风险；summarize=把一堆文件压成摘要；"
                "plan=给一份能落地的改法；verify=核对说法和实现对不对得上。\n"
                "要同时查几件互不相干的事，用 tasks 一次派多个（最多 4 个），它们会并行跑。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {"type": "string",
                             "enum": ["explore", "review", "summarize", "plan", "verify"]},
                    "task": {"type": "string",
                             "description": "要给子代理的完整任务描述，它看不到我们的对话"},
                    "tasks": {
                        "type": "array",
                        "description": "要并行派多个时用这个，每项 {role, task}；和上面的 role/task 二选一",
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string",
                                         "enum": ["explore", "review", "summarize", "plan", "verify"]},
                                "task": {"type": "string"},
                            },
                            "required": ["role", "task"],
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_create",
            "description": (
                "创建一个定时任务，到点自动跑一次这句 prompt。"
                "freq 支持 daily（每天 HH:MM）/ weekly（每周几 HH:MM）/ interval（每 N 分钟）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务名"},
                    "prompt": {"type": "string", "description": "到点要执行的那句话"},
                    "freq": {"type": "string", "enum": ["daily", "weekly", "interval"]},
                    "time": {"type": "string", "description": "daily/weekly 用，24 小时制 HH:MM，例如 09:15"},
                    "weekday": {"type": "integer", "description": "weekly 用，1=周一 … 7=周日"},
                    "minutes": {"type": "integer", "description": "interval 用，间隔分钟数，最小 5"},
                },
                "required": ["title", "prompt", "freq"],
            },
        },
    },
]


def _collect() -> tuple[list[dict], dict]:
    schemas: list[dict] = []
    handlers: dict = {}
    for module in MODULES:
        schemas.extend(module.SCHEMAS)
        handlers.update(module.HANDLERS)
    schemas.extend(_EXTRA_SCHEMAS)
    return schemas, handlers


SCHEMAS, HANDLERS = _collect()
ALL_NAMES = {s["function"]["name"] for s in SCHEMAS}

# 新装/升级时不默认打开的工具：这些等于把键盘鼠标交出去，得用户自己点开
DEFAULT_OFF = {
    "open_target", "focus_window", "screenshot",
    "type_text", "press_keys", "mouse_click", "clipboard",
}
# 注：open_software 故意不在这里。它和 open_target 的分工不同 ——
# open_target 能打开**任意**路径和网址（用户没见过的文件也会被打开），
# open_software 只认清单里那 100 多个软件名。权限上两者都算 danger，
# 标准档会停下来问，所以默认开着也不会变成后门。

READ_ONLY = {
    "list_dir", "read_file", "grep", "glob", "scan_junk",
    "web_search", "web_fetch", "list_windows", "memory_read", "skill_load",
    "sqlite_query", "image_info", "calculate", "list_processes",
    # 第三批：全都不动盘。search_replace / download 能改文件，所以不在里面。
    "read_many", "file_stat", "tail_file", "diff_text", "find_duplicates",
    "which_command", "csv_query", "json_query", "text_stats",
    "system_info", "port_check", "env_var", "now", "date_math",
    # 只是读开始菜单和注册表，不启动任何东西
    "list_software",
}


def default_enabled() -> list[str]:
    return sorted(ALL_NAMES - DEFAULT_OFF)


def enabled_set() -> set[str]:
    """用户当前启用着哪些工具。

    顺带做一次「升级补新工具」：老版本没有 todo_write，用户如果手动勾选过工具
    清单，那份清单里就永远不会有后来新增的工具 —— 于是他升级完发现新功能不见了。
    这里记一个 `tools_seen`，只在第一次见到某个工具时把它补进启用清单，
    用户后来主动关掉的工具不会被重新打开（那才是真的烦人）。
    """
    from .. import config

    settings = config.get()
    enabled = settings.get("enabled_tools")
    seen = list(settings.get("tools_seen") or [])

    if not enabled:
        # 用户从没动过清单：全按默认来。但仍然要记下「见过哪些」，
        # 否则下次升级判断不出哪些是新增的。
        if set(seen) != set(ALL_NAMES):
            config.update({"tools_seen": sorted(ALL_NAMES)})
        return set(default_enabled())

    if not seen:
        config.update({"tools_seen": sorted(ALL_NAMES)})
        return set(enabled)

    fresh = sorted(set(ALL_NAMES) - set(seen) - DEFAULT_OFF)
    if fresh:
        config.update({"enabled_tools": sorted(set(enabled) | set(fresh)),
                       "tools_seen": sorted(ALL_NAMES)})
        return set(enabled) | set(fresh)
    return set(enabled)


def schemas_for(names: set[str] | None) -> list[dict]:
    if names is None:
        return list(SCHEMAS)
    return [s for s in SCHEMAS if s["function"]["name"] in names]


def call(ctx: ToolContext, name: str, arguments_json: str) -> str:
    """统一入口。任何失败都返回字符串，不抛异常——工具结果要回填给模型。"""
    handler = HANDLERS.get(name)
    if handler is None:
        if name in VIRTUAL:
            return f"（{name} 由会话层处理，不该走到这里）"
        return f"未知工具：{name}"

    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return f"参数不是合法 JSON：{exc}"
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象。"

    # 丢掉 schema 里没声明的字段，避免模型多塞参数导致 TypeError
    schema = next((s for s in SCHEMAS if s["function"]["name"] == name), None)
    if schema:
        allowed = set((schema["function"].get("parameters") or {}).get("properties") or {})
        if allowed:
            unknown = set(args) - allowed
            for key in unknown:
                args.pop(key, None)

    try:
        return handler(ctx, **args)
    except TypeError as exc:
        return f"参数不对：{exc}"
    except ValueError as exc:
        return f"拒绝：{exc}"
    except Exception as exc:  # noqa: BLE001 - 工具不能把主循环带崩
        return f"工具出错：{type(exc).__name__}: {exc}"


def describe() -> list[dict]:
    enabled = enabled_set()
    out = []
    for schema in SCHEMAS:
        fn = schema["function"]
        name = fn["name"]
        out.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "enabled": name in enabled,
                "default_off": name in DEFAULT_OFF,
                "read_only": name in READ_ONLY,
                "group": GROUPS.get(name, "其他"),
            }
        )
    return out


def grouped() -> list[dict]:
    """按组归好的清单，给界面画折叠用。"""
    buckets: dict[str, list[dict]] = {}
    for item in describe():
        buckets.setdefault(item["group"], []).append(item)
    return [
        {"group": group, "items": items,
         "on": sum(1 for i in items if i["enabled"]), "total": len(items)}
        for group, items in buckets.items()
    ]


def root_of(path: str) -> Path:
    return Path(path)
