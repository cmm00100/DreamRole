"""工具的中文名。

为什么单独一个文件：这些名字会同时出现在三个地方——事件流（图上节点的标题）、
设置页的工具清单、以及等待确认时的提示语。写在一处，改一处就全改。

原则：动词 + 对象，四个字以内。「读取文件」而不是「read_file」，
因为用户看的是过程，不是 API。
"""
from __future__ import annotations

TOOL_LABELS: dict[str, str] = {
    # 文件
    "list_dir": "看目录",
    "read_file": "读文件",
    "write_file": "写文件",
    "edit_file": "改文件",
    "grep": "搜内容",
    "glob": "找文件",
    "apply_patch": "批量改",
    "copy_file": "复制文件",
    "move_file": "移动文件",
    "make_dir": "建目录",
    "archive": "压缩包",
    "image_info": "看图片",
    # 文件（第三批）
    "read_many": "读一批",
    "file_stat": "看详情",
    "tail_file": "看结尾",
    "diff_text": "比差异",
    "search_replace": "批量换",
    "find_duplicates": "找重复",
    # 命令
    "run_command": "跑命令",
    "list_processes": "看进程",
    "which_command": "查命令",
    # 清理
    "scan_junk": "扫垃圾",
    "remove_junk": "清垃圾",
    "delete_file": "删文件",
    # 联网
    "web_search": "搜网页",
    "web_fetch": "读网页",
    "http_request": "调接口",
    "download": "下文件",
    # 数据
    "sqlite_query": "查库",
    "calculate": "算一下",
    "csv_query": "查表格",
    "json_query": "取字段",
    "text_stats": "数字数",
    # 系统
    "system_info": "看机器",
    "port_check": "查端口",
    "env_var": "看环境",
    # 时间
    "now": "看时间",
    "date_math": "算日期",
    # 电脑
    "open_target": "开程序",
    "list_windows": "看窗口",
    "focus_window": "切窗口",
    "screenshot": "截屏",
    "type_text": "打字",
    "press_keys": "按键",
    "mouse_click": "点鼠标",
    "clipboard": "剪贴板",
    # 本机软件
    "list_software": "看软件",
    "open_software": "开软件",
    # 记忆
    "memory_read": "读记忆",
    "memory_write": "记一笔",
    # 会话层虚拟工具
    "todo_write": "列待办",
    "set_stage": "开阶段",
    "skill_load": "载技能",
    "subagent": "派子代理",
    "schedule_create": "设定时",
}

EVENT_LABELS: dict[str, str] = {
    "user": "你",
    "reasoning": "思",
    "think": "想",
    "action": "做",
    "result": "果",
    "answer": "答",
    "notice": "提示",
}


def label(tool: str) -> str:
    return TOOL_LABELS.get(tool, tool)
