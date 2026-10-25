"""运行时探测：这个机器上到底有哪些东西能跑。

为什么要有这一层：工具里写死 `python` 的后果是，用户机器上 `python` 指向
Microsoft Store 那个 0 字节的转发器，一调就弹商店；写死 `node` 则可能在
nvm 切过版本之后指向旧目录。与其在工具里到处猜，不如启动后探一次、
探到了就记住，探不到就老实告诉模型「这条命令现在不能跑」。

三条纪律：

1. **探测不执行任何有副作用的命令** —— 只跑 `<exe> --version` 这类；
2. **结果带 `usable` 而不是只有 `found`** —— 找到了但版本太低/是商店转发器，
   和没找到是两件事，模型需要知道区别；
3. **用户可以覆盖路径** —— 探测错了一定有救，不然就只能重装。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# name -> 探测方式。args 用 `-V`/`--version` 这种，不碰用户数据。
PROBES: dict[str, dict] = {
    "python": {"args": ["-c", "import sys;print(sys.version.split()[0])"], "why": "跑脚本、测代码"},
    "node": {"args": ["--version"], "why": "前端 / 构建工具"},
    "git": {"args": ["--version"], "why": "看改动、拉依赖"},
    "rg": {"args": ["--version"], "why": "全文搜索（比 grep 快）"},
    "ffmpeg": {"args": ["-version"], "why": "处理音视频"},
    "pwsh": {"args": ["-NoLogo", "-Command", "$PSVersionTable.PSVersion.ToString()"], "why": "Windows 上的脚本"},
    "sqlite3": {"args": ["--version"], "why": "查小数据库"},
    "curl": {"args": ["--version"], "why": "请求接口"},
}

# 和 PROBES 里名字不同、但值得去 PATH 里找的可执行名
ALIASES: dict[str, list[str]] = {
    "python": ["python", "python3", "py"],
    "node": ["node", "nodejs"],
    "rg": ["rg"],
    "pwsh": ["pwsh", "powershell"],
}

_CACHE: dict[str, dict] = {}
_TTL = 90.0          # 秒。装完东西回来点一下就该看到，不用重启
_TESTED: dict[str, float] = {}


def _run(args: list[str], timeout: float = 6.0) -> tuple[bool, str]:
    try:
        done = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            # Windows 上不弹黑框
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    text = (done.stdout or "").strip() or (done.stderr or "").strip()
    return done.returncode == 0, text.splitlines()[0] if text else ""


def _override(name: str) -> str:
    from . import config

    paths = config.get().get("runtime_paths") or {}
    return str(paths.get(name) or "").strip()


def _locate(name: str) -> str:
    """先看用户指定，再按别名在 PATH 里找。返回空串表示没找到。"""
    custom = _override(name)
    if custom:
        target = Path(custom).expanduser()
        if target.is_file():
            return str(target)
    for alias in ALIASES.get(name, [name]):
        found = shutil.which(alias)
        if found:
            return found
    return ""


def probe(name: str, force: bool = False) -> dict:
    meta = PROBES.get(name)
    if not meta:
        return {"name": name, "found": False, "usable": False, "path": "",
                "version": "", "why": "", "note": "不认识这个运行时"}

    now = time.time()
    if not force and name in _CACHE and now - _TESTED.get(name, 0) < _TTL:
        return _CACHE[name]

    path = _locate(name)
    record = {
        "name": name,
        "why": meta["why"],
        "found": bool(path),
        "usable": False,
        "path": path,
        "version": "",
        "note": "",
        "checked": now,
    }

    if not path:
        record["note"] = "PATH 里没找到"
    elif "WindowsApps" in path and name == "python":
        # 商店版 python 是个 0 字节转发器：`which` 能找着，一跑就弹商店。
        # 这条只对 python 成立 —— pwsh / winget 这些在 WindowsApps 里是真别名，
        # 一律当坏的会误伤。
        ok, out = _run([path] + meta["args"])
        record["usable"] = False
        record["version"] = out[:40]
        record["note"] = "像是微软商店的转发器，建议改成真实安装路径"
    else:
        ok, out = _run([path] + meta["args"])
        record["usable"] = ok
        record["version"] = out[:60] if ok else ""
        if not ok:
            record["note"] = out[:120] or "跑起来了但报了错"

    _CACHE[name] = record
    _TESTED[name] = now
    return record


def detect(force: bool = False) -> list[dict]:
    """并行探。

    串行探的代价是「8 个 × 最坏 6 秒 = 48 秒」—— 有一个卡住的 exe 就会把整个
    面板拖住。这些探测之间毫无依赖，纯粹在等 IO，没理由排队。
    """
    names = list(PROBES)
    results: dict[str, dict] = {}

    def worker(name: str) -> None:
        try:
            results[name] = probe(name, force=force)
        except Exception as exc:  # noqa: BLE001 - 单个探测炸了不该拖垮整批
            results[name] = {"name": name, "found": False, "usable": False, "path": "",
                             "version": "", "why": PROBES[name]["why"],
                             "note": f"探测出错：{type(exc).__name__}"}

    threads = [threading.Thread(target=worker, args=(name,), daemon=True) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=12)

    return [results.get(name) or {"name": name, "found": False, "usable": False,
                                  "path": "", "version": "", "why": PROBES[name]["why"],
                                  "note": "探测超时"}
            for name in names]


_WARM: dict[str, bool] = {}


def warm() -> None:
    """后台先把缓存填上。

    为什么需要：`/api/bootstrap` 是界面第一屏要等的接口，它不该替运行时探测买单。
    探测在这一侧慢慢做，bootstrap 只读缓存 —— 缓存还是空就先说「正在探测」。
    """
    if _WARM.get("started") and _CACHE:
        return
    _WARM["started"] = True

    def run() -> None:
        try:
            detect()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=run, name="runtime-warm", daemon=True).start()


def summary_cached() -> str:
    """给 bootstrap 用：绝不为探测阻塞。"""
    if not _CACHE:
        warm()
        return "正在探测…"
    items = list(_CACHE.values())
    ok = [i["name"] for i in items if i["usable"]]
    bad = [i["name"] for i in items if not i["usable"]]
    return f"{len(ok)} 个运行时都可用" if not bad else f"{len(ok)} 个可用；缺 {', '.join(bad)}"


def set_path(name: str, path: str) -> dict:
    """用户手动指定。空串表示「恢复自动探测」。"""
    from . import config

    current = dict(config.get().get("runtime_paths") or {})
    path = str(path or "").strip()
    if path:
        if not Path(path).expanduser().is_file():
            raise ValueError(f"这个路径上不是文件：{path}")
        current[name] = path
    else:
        current.pop(name, None)
    config.update({"runtime_paths": current})
    _CACHE.pop(name, None)
    return probe(name, force=True)


def which(name: str) -> str:
    """给别的模块用：拿到能直接跑的路径，没有就返回 name（让它自己报错）。"""
    record = probe(name)
    return record["path"] or name


def summary() -> str:
    """一句话给人看的：都齐了吗，缺什么。"""
    items = detect()
    ok = [i["name"] for i in items if i["usable"]]
    bad = [i["name"] for i in items if not i["usable"]]
    if not bad:
        return f"{len(ok)} 个运行时都可用"
    return f"{len(ok)} 个可用；缺 {', '.join(bad)}"


def self_check() -> dict:
    """给 /api/runtime 用：带上当前进程自己的信息。"""
    return {
        "ok": True,
        "items": detect(),
        "summary": summary(),
        "this": {
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "platform": sys.platform,
            "cwd": os.getcwd(),
        },
    }
