"""执行命令。边界逻辑在 app/shell.py，这里只做工具层包装。"""
from __future__ import annotations

from .. import shell
from .base import ToolContext


def run_command(ctx: ToolContext, command: str, cwd: str = "", timeout: int = shell.DEFAULT_TIMEOUT) -> str:
    # 会话把「要不要停」和「报个心跳」挂在 extra 上 —— 工具层不认识 Session，
    # 只认这两个回调。没有它们（比如测试里直接调工具）就是老行为，照样跑。
    return shell.run(
        ctx.root, command, cwd=cwd, timeout=timeout, grants=ctx.grants,
        is_cancelled=ctx.extra.get("is_cancelled"),
        on_beat=ctx.extra.get("on_beat"),
    )


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在项目目录里跑一条命令，拿到 stdout/stderr 和退出码。用于跑测试、跑脚本、装依赖、看 git 状态。"
                "命令由系统默认 shell 执行（Windows 是 cmd.exe：串联用 && 而不是分号，别用 ls -la）。"
                "会关机/格盘/改注册表这类毁系统的命令一律被拒。"
                "别用 cat/type 读文件（用 read_file），也别用 dir/ls 看目录（用 list_dir）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令，例如 python -m pytest -q"},
                    "cwd": {"type": "string", "description": "相对项目根的工作目录，默认根目录"},
                    "timeout": {
                        "type": "integer",
                        "description": f"超时秒数，默认 {shell.DEFAULT_TIMEOUT}，最多 {shell.MAX_TIMEOUT}",
                    },
                },
                "required": ["command"],
            },
        },
    },
]

HANDLERS = {"run_command": run_command}
