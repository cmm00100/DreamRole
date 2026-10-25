"""两套底层：role1 是 DreamRole 自己的内核，role2 是本机的 DeepSeek Harness。

为什么要两个名字，而不是一个「切换」开关：

- 它们的**能力边界不一样**。role1 有 DreamRole 的四档权限、审批卡、三层记忆、
  技能与专家；role2 的工具、技能、子代理全是 DSH 那套，DreamRole 只能转发和记录。
  同一个会话中途换内核，模型会看到一堆它不认识的历史 —— 所以**会话建好就绑死**，
  想换就开新会话。这也是 `role1`/`role2` 这两个名字存在的意义：它们不是同一件东西
  的两个模式，是两件东西。
- 它们在界面上必须**看得出区别**，但不能是两个软件。所以事件流共用一套形状，
  只有「谁在干活」不同。

这个模块只做登记与归一，不掺业务：真正的 runner 在 `dsh_engine.run()` 和
`Session._loop()` 里。
"""
from __future__ import annotations

from typing import Any

from . import config, dsh_engine

DEFAULT = "role1"
ROLES = ("role1", "role2")

# 界面上显示的名字。**不走 probe**：会话快照每次都会带上它，
# 而 probe 要去看 DSH 装没装（要读 asar 头）。为了一个标签去读 182MB 的文件，
# 在每推一次状态都跑一遍，是不能接受的。真正的可用性由 list_roles() 现算。
LABELS = {
    "role1": "Role 1 · DreamRole 内核",
    "role2": "Role 2 · DSH 内核",
}


def label(name: Any) -> str:
    key = normalize(name)
    return LABELS.get(key, key)


def normalize(name: Any) -> str:
    """认不出的值一律退回 role1。

    为什么不退回「报错」：引擎名是从前端、meta、命令行三处来的，其中任何一处写错
    都不该让会话打不开。而 role1 是自带的、一定能跑的 —— 退回它至少还能用。
    """
    text = str(name or "").strip().lower()
    return text if text in ROLES else DEFAULT


def _role1() -> dict[str, Any]:
    provider = str(config.get().get("provider") or "")
    providers = config.get().get("providers") or {}
    conf = providers.get(provider) or {}
    has_key = bool(str(conf.get("api_key") or "").strip())
    return {
        "id": "role1",
        "label": "Role 1 · DreamRole 内核",
        "short": "role1",
        "ready": has_key,
        "ok": True,
        "has_key": has_key,
        "path": "",
        "home": "",
        "note": "" if has_key else "还没配模型 key（设置 → 模型）",
        "tools": "DreamRole 自己的 56 个工具",
        "approval": "四档权限 + 审批卡",
        "model": str(conf.get("model") or config.get().get("model") or ""),
    }


def list_roles() -> list[dict[str, Any]]:
    rows = [_role1(), dsh_engine.describe()]
    for row in rows:
        row["selected"] = row["id"] == normalize(config.get().get("engine"))
    return rows


def describe(name: str) -> dict[str, Any]:
    want = normalize(name)
    return next((row for row in list_roles() if row["id"] == want), list_roles()[0])
