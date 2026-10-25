"""工具执行上下文。

统一签名 `handler(ctx, **kwargs)` —— 不再用 `handler(root, **kwargs)`。
旧写法（我的agent）的坑是：少写第一个参数不报语法错，只在模型真调用时变成
「got multiple values for argument」。用 ctx 一个对象承载所有环境，加字段不用改签名。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import grants as grants_mod


@dataclass
class ToolContext:
    root: Path
    cid: str = ""
    project: str = ""
    # 这个会话额外授权的目录（见 app/grants.py）。工作区是主目录，
    # 这些是「另外也允许去一趟」的地方，每张都带只读/可写档。
    grants: list = field(default_factory=list)
    # 由 session 注入的可选能力（避免工具层反向 import session）
    extra: dict = field(default_factory=dict)

    def resolve(self, path: str, need: str = "read") -> Path:
        """把用户/模型给的相对路径收敛成绝对路径，越界或档位不够抛 ValueError。

        `need` 只有两个值：
        - `read`（默认）：工作区和授权目录都行，只读的也行；
        - `write`：落在只读授权目录里就会被拒。

        **写操作必须显式传 `write`。** 默认 read 是故意的：漏写一处 `need="write"`
        的后果是「本该拒绝的写被放行」，比反过来危险得多 —— 所以
        `selftest.py` 里对每种写工具都有「往只读授权目录写会被拒」的用例兜着。
        """
        base = self.root.resolve()
        if not path or path in (".", "./"):
            return base
        candidate = Path(path).expanduser()
        target = (candidate if candidate.is_absolute() else base / candidate).resolve()
        why = grants_mod.check(base, self.grants, target, need)
        if why:
            raise ValueError(why)
        return target

    def relative(self, target: Path) -> str:
        try:
            return str(target.resolve().relative_to(self.root.resolve())).replace("\\", "/")
        except ValueError:
            return str(target).replace("\\", "/")
