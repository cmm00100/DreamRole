"""run_command 的边界。

四条线：
1. cwd 必须在工作区或授权目录内（授权目录另见 app/grants.py）；
2. 一小撮会毁机器的命令**硬拒绝**；
3. 有超时，超时连子进程一起杀（npm/pip 会 fork 一堆）；
4. 不接 stdin、输出截断（否则 `npm init`、裸 `python` 会挂在那儿等输入）。

第五条是后来补的，补的原因很具体：**用户点「停下」停不掉一条正在跑的命令**。

原来这里用 `proc.communicate(timeout=limit)` 一口气等到底。那是阻塞调用：
期间 `session.cancel()` 只是把 `cancelled` 标志置上，这条命令**根本不知道**，
于是最长要等 300 秒才轮到下一轮循环去检查标志。用户看到的是「已停下」，
但这一轮其实还活着 —— 再发一句就被「上一轮还没结束」挡回来。

现在改成：两个读线程把 stdout/stderr 抽出来，主线程每 80ms 看一眼
「进程死了没 / 要不要停 / 超时了没」。停是主动 `_kill_tree`，不是等它自己醒。

**这是软边界，得说清楚。** cwd 只是起点，命令里写 `cd /d D:\\x`
照样能跑出去。真要物理隔离只能靠沙箱账户或容器，这里没做。
所以第 1 条的作用是「让默认落点可预期 + 降低误伤」，不是「关住」。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import grants as grants_mod

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
MAX_OUTPUT = 8000
# 长命令多久报一次「还在跑」。装依赖那种一条命令跑五分钟的，
# 界面上如果一直一动不动，用户分不清是「在装」还是「卡死了」。
BEAT_EVERY = 2.0

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# (正则, 理由)。只收「一眼就能看出会毁系统」的，别把正常开发命令挡了。
BLOCKED: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bformat\s+[a-z]:", re.I), "格式化磁盘"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I), "格式化磁盘"),
    (re.compile(r"\bdiskpart\b", re.I), "动磁盘分区"),
    (re.compile(r"\b(shutdown|logoff)\b", re.I), "关机或注销"),
    (re.compile(r"\brestart-computer\b", re.I), "重启电脑"),
    (re.compile(r"\breg\s+(add|delete|import|restore)\b", re.I), "改注册表"),
    (re.compile(r"\bbcdedit\b", re.I), "改启动配置"),
    (re.compile(r"\bcipher\s+/w", re.I), "擦除磁盘空闲区"),
    (re.compile(r"\bicacls\b[^\n]*/reset", re.I), "重置整盘权限"),
    (re.compile(r"\bvssadmin\b", re.I), "删卷影副本"),
    (re.compile(r"\bnet\s+user\s+\S+\s+\S+\s+/add", re.I), "加系统账户"),
    (re.compile(r"\brd\s+/s\s+/q\s+[a-z]:\\?\s*$", re.I), "删整个盘"),
    (re.compile(r"\brmdir\s+/s\s+/q\s+[a-z]:\\?\s*$", re.I), "删整个盘"),
    (re.compile(r"\bdel\s+/[fsq]\b[^\n]*\b[a-z]:\\(\*|$)", re.I), "删整个盘"),
    (re.compile(r"\brm\s+-[rf]{1,2}\s+(/|/\*|~/?|\$home)\s*$", re.I), "删根目录或家目录"),
    (re.compile(r"\bremove-item\b[^\n]*-recurse[^\n]*\b[a-z]:\\?\s*$", re.I), "删整个盘"),
    (re.compile(r"\|\s*(ba)?sh\b", re.I), "把网上下载的脚本直接执行"),
    (re.compile(r"\|\s*(iex|invoke-expression)\b", re.I), "把网上下载的脚本直接执行"),
]

# 「会动文件 / 装东西」的命令：值不值得在执行前停下来问一句
RISKY = re.compile(
    r"\b(del|erase|rm|rmdir|rd|mv|move|ren|rename|truncate|tee)\b"
    r"|\bgit\s+(reset|clean|checkout|restore|rebase|push)\b"
    r"|\b(pip|pip3|npm|yarn|pnpm|poetry|cargo|go)\s+(install|uninstall|add|remove|get)\b"
    r"|\b(curl|wget)\b",
    re.I,
)


def is_risky(command: str) -> bool:
    return bool(RISKY.search(command or ""))


def blocked_reason(command: str) -> str | None:
    for pattern, why in BLOCKED:
        if pattern.search(command):
            return why
    return None


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clip(text: str) -> str:
    text = _ANSI.sub("", text).replace("\r\n", "\n").strip()
    if len(text) <= MAX_OUTPUT:
        return text
    head = text[: MAX_OUTPUT - 2000]
    tail = text[-1500:]
    return f"{head}\n…（中间省略 {len(text) - len(head) - len(tail)} 字符）…\n{tail}"


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _tail(chunks: list[bytes], limit: int = 120) -> str:
    """缓冲里最后一行有字的输出。给心跳用 —— 有它就不必再问「卡住了吗」。"""
    raw = b"".join(chunks[-40:])
    lines = [line.strip() for line in _decode(raw).replace("\r", "").splitlines() if line.strip()]
    return lines[-1][-limit:] if lines else ""


def _resolve_cwd(root: Path, cwd: str, grants: list | None = None) -> Path:
    base = root.resolve()
    if not cwd or cwd in (".", "./"):
        return base
    candidate = Path(cwd).expanduser()
    target = (candidate if candidate.is_absolute() else base / candidate).resolve()
    # 和工作区之外的同一套判定：授权目录也能进去，越界照样拒。
    why = grants_mod.check(base, grants or [], target)
    if why:
        raise ValueError(why)
    return target


def run(root: Path, command: str, cwd: str = "", timeout: int = DEFAULT_TIMEOUT,
        grants: list | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_beat: Callable[[int, str], None] | None = None) -> str:
    """跑一条命令。

    `is_cancelled`：用户在界面上点了「停下」。给了就得**真的**管用 ——
    这里每 80ms 问一次，一旦为真立刻 taskkill /T 连子进程一起杀。
    没有它的话，一条 300 秒的安装命令会把整条会话锁死五分钟，
    而界面上早已显示「已停下」。

    `on_beat`：每 2 秒报一次「跑了多少秒 + 最后一句话」。装依赖那种一条命令
    跑几分钟的场景，界面上一直一动不动和卡死是分不清的。
    """
    cmd = (command or "").strip()
    if not cmd:
        return "没给命令。"

    if is_cancelled is not None and is_cancelled():
        return "没执行：这一轮已经被你停下了。"

    why = blocked_reason(cmd)
    if why:
        return (
            f"拒绝执行：这条命令会{why}，超出「在项目目录里干活」的边界。\n"
            f"命令：{cmd}\n如果确实必要，请让用户自己在终端里执行。"
        )

    try:
        work = _resolve_cwd(root, cwd, grants)
    except ValueError as exc:
        return f"拒绝执行：\n{exc}"
    if not work.is_dir():
        return f"工作目录不存在：{cwd}"

    # 只读通行证里不许跑「会动文件」的命令。判定用的是命令文本的风险正则，
    # 是个近似 —— `cd /d D:\x && del a.txt` 这种仍然拦不住（cwd 只是起点）。
    # 所以下面这句话要说清楚：这是「降低误伤」，不是隔离。
    if grants and is_risky(cmd):
        hit = grants_mod.covers(root, grants, work)
        if hit and hit[0] == grants_mod.MODE_RO:
            return (
                f"拒绝执行：`{work}` 是只读目录，这条命令可能会改文件。\n"
                f"要在这里跑改文件的命令，得请用户把这张通行证改成「可写」；"
                f"只读的地方可以跑 `dir`、`git log` 这类只看的东西。"
            )

    try:
        limit = int(timeout or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        limit = DEFAULT_TIMEOUT
    limit = max(1, min(limit, MAX_TIMEOUT))

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(work),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=flags,
        )
    except OSError as exc:
        return f"没能启动这条命令：{exc}"

    # ---- 抽水：两个线程各读一路，主线程只负责「看着」----------------------
    # 不用 communicate()：它一进去就不回头，中途没法响应「停下」。
    # 抽水的线程必须分开，否则 stderr 写满管道缓冲时两边一起堵死。
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    def drain(stream, sink: list[bytes]) -> None:
        try:
            for line in iter(stream.readline, b""):
                sink.append(line)
        except Exception:  # noqa: BLE001 - 管道被我们关掉，是中止时的正常路径
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    readers = [
        threading.Thread(target=drain, args=(proc.stdout, out_chunks), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, err_chunks), daemon=True),
    ]
    for thread in readers:
        thread.start()

    timed_out = False
    stopped = False
    last_beat = started
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if is_cancelled is not None and is_cancelled():
            stopped = True
            _kill_tree(proc)
            break
        if now - started > limit:
            timed_out = True
            _kill_tree(proc)
            break
        if on_beat is not None and now - last_beat >= BEAT_EVERY:
            last_beat = now
            try:
                on_beat(int(now - started), _tail(out_chunks) or _tail(err_chunks))
            except Exception:  # noqa: BLE001 - 心跳坏了不该把命令搞停
                pass
        time.sleep(0.08)

    # 杀完之后进程可能还要几毫秒才真的躺下；等它一下，returncode 才读得准。
    # 不 wait 直接 join 抽水线程，偶尔会拿到 b"" 之外的空壳。
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for thread in readers:
        thread.join(3)
    spent = time.monotonic() - started

    out, err = b"".join(out_chunks), b"".join(err_chunks)
    stdout = _clip(_decode(out))
    stderr = _clip(_decode(err))

    # cwd 在授权目录里的时候，它不在工作区下面 —— relative_to 会抛 ValueError，
    # 而这一句本来只是为了让输出里那行 `[cwd]` 好看。给绝对路径就够。
    try:
        rel = "." if work == root.resolve() else str(work.relative_to(root.resolve()))
    except ValueError:
        rel = str(work)
    lines = [f"$ {cmd}", f"[cwd] {rel}"]
    if stopped:
        lines.append(f"[中止] 你点了停下，已连同子进程一起杀掉，用时 {spent:.1f}s")
    elif timed_out:
        lines.append(f"[超时] 超过 {limit}s，已连同子进程一起杀掉，用时 {spent:.1f}s")
    else:
        lines.append(f"[退出码 {proc.returncode}] 用时 {spent:.1f}s")
    lines.append("")
    lines.append(stdout if stdout else "(没有标准输出)")
    if stderr:
        lines.extend(["", "stderr:", stderr])
    if not timed_out and not stopped and proc.returncode not in (0, None):
        lines.extend(["", "这条命令失败了：先看 stderr 判断原因，再改代码，别原样重试。"])
    return "\n".join(lines)
