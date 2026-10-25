"""通道：同一套内核，从哪几个门进来。

DreamRole 的会话内核不关心请求是从桌面窗口、命令行、还是 HTTP 来的 —— 它只认
「谁在说话、说什么」。通道这一层就是那三个门，各自记着自己的开关和流量。

为什么把「通道」当成一等公民而不是写在 server.py 里：一条会话被桌面窗口创建、
被 CLI 接着说了两句、再被外部脚本收了尾，这是正常用法。如果流量只记在
server.py 里，用户就永远看不到「这条会话其实是从命令行动的」。
每个请求打进来先认门，再干活，账才对得上。

安全上唯一硬的一条：**非本机来源必须有 token**。本地回环地址不验 —— 否则
每次开窗口都要输 token，用户会直接把校验关掉，等于没有。
"""
from __future__ import annotations

import secrets
import time

from . import config

# 固定的三个门。id 不要改，前端和测试都按 id 认。
CHANNELS: dict[str, dict] = {
    "desktop": {
        "label": "桌面窗口",
        "why": "点开会话、看图、审批权限都在这里",
        "always": True,
    },
    "cli": {
        "label": "命令行",
        "why": "不打开窗口也能跑一轮，适合脚本和定时任务",
        "always": True,
    },
    "api": {
        "label": "本地接口",
        "why": "给别的程序用；从别的机器来必须带 token",
        "always": True,
    },
}

_HITS: dict[str, dict] = {}
_LAST: dict[str, float] = {}
_OPENED = time.time()


def _bucket(name: str) -> dict:
    return _HITS.setdefault(name, {"count": 0, "errors": 0})


def hit(name: str, ok: bool = True, weight: int = 1) -> None:
    """记一次流量。server 的中间件对每个请求调一次。"""
    bucket = _bucket(name)
    bucket["count"] += weight
    if not ok:
        bucket["errors"] += 1
    _LAST[name] = time.time()


def token() -> str:
    """取（必要时生成）外部接口令牌。只用标准库，不引第三方。"""
    current = str(config.get().get("api_token") or "")
    if len(current) >= 24:
        return current
    fresh = secrets.token_urlsafe(24)
    config.update({"api_token": fresh})
    return fresh


def rotate_token() -> str:
    fresh = secrets.token_urlsafe(24)
    config.update({"api_token": fresh})
    return fresh


def api_open() -> bool:
    return bool(config.get().get("api_open"))


def set_api(open_: bool | None = None) -> dict:
    if open_ is not None:
        config.update({"api_open": bool(open_)})
    return state()


def is_loopback(host: str | None) -> bool:
    return str(host or "").strip() in ("127.0.0.1", "::1", "localhost", "")


def listening_wide() -> bool:
    """服务是不是绑在了一个「外面能连进来」的地址上。

    这件事决定了要不要查令牌：只绑回环的时候，能连上来的按定义都是本机进程，
    再验一遍只是在给自己添堵。判断依据是**实际绑定的地址**（由启动方 note_listen 告知），
    不是配置文件 —— 配置里写的和真正绑的可能不是一回事。
    """
    host = (_bound_host or "").strip().lower()
    if not host:
        return False          # 没人说，就按默认的 127.0.0.1 对待
    return host not in ("127.0.0.1", "::1", "localhost")


_bound_host = ""


def note_listen(host: str) -> None:
    """由启动方告诉这里「我们绑在哪」。不读配置 —— 配置里写的和实际绑的可能不是一回事。"""
    global _bound_host
    _bound_host = str(host or "")


def allowed_origin(client_host: str | None, supplied: str | None) -> tuple[bool, str]:
    """这个来源能不能用接口。

    回环地址一律放行；其余来源只认 token。故意把 token 比对放在
    长度检查之后 —— `secrets.compare_digest` 对长度不等会直接 False，
    但先比长度能少算一次哈希。
    """
    if is_loopback(client_host):
        return True, ""
    expected = token()
    given = (supplied or "").strip()
    if len(given) != len(expected):
        return False, "不是本机来的请求，必须带 X-DreamRole-Token"
    if not secrets.compare_digest(given, expected):
        return False, "token 不对"
    return True, ""


def state() -> dict:
    now = time.time()
    items = []
    for name, meta in CHANNELS.items():
        bucket = _bucket(name)
        last = _LAST.get(name, 0)
        items.append(
            {
                "id": name,
                "label": meta["label"],
                "why": meta["why"],
                "enabled": True if name != "api" else api_open(),
                "count": bucket["count"],
                "errors": bucket["errors"],
                "last": last,
                "last_ago": round(now - last, 1) if last else None,
            }
        )
    return {
        "ok": True,
        "items": items,
        "uptime": round(now - _OPENED, 1),
        "token": token(),
        "api_open": api_open(),
        "listen": str(config.get().get("host") or "127.0.0.1"),
        "port": int(config.get().get("port") or 0),
    }
