r"""role2：把这一轮的活儿交给本机的 DeepSeek Harness。

DSH 是「Electron 壳 + 一套 cordis 插件运行时」，它没有「给我一个 HTTP 接口让我调」
这回事 —— 但它带了一个 headless profile：

    dsh --profile headless --json "任务"

stdout 一行一个 JSON 事件，答完就退出。这就是 role2 的全部接口，也是为什么
role2 不用改 DSH 一行代码、DSH 升级也不怕：**我们只吃它公开的那份事件流**。

三个踩过/绕开的坑：

1. **不走 `dsh.cmd`，直接调 exe + cli.js。**
   `cli/bin/dsh.cmd` 里那句 `"%~dp0..\..\..\..\DeepSeek Harness.exe"` 在某些 cwd 下
   会被 cmd 的引号规则吃掉，报 `'D:\DeepSeek' 不是内部或外部命令` —— 实测同样一条
   命令在 `C:\Users\you` 能跑、在其子目录里就炸。subprocess 传参数列表没有这个问题。

2. **一次一轮一个进程。** headless 本来就是「答完就走」。多轮靠 `--session-id` 续，
   所以第一轮拿到的 sessionId 必须存进我们自己的 meta 里（`Session.dsh_session`）。

3. **事件要翻译成 DreamRole 那 6 种。** 界面只有一套，两种内核必须喂同样的形状 ——
   否则「换个引擎」就变成「换个软件」。

事件对照（实测，2026-10-24，DSH 0.2.1-rc.3）：

    {"type":"session","sessionId":"session-…","cwd":"…"}       → 记下 sessionId
    {"type":"status","phase":"turn_start"|"step_start"|…}      → 计时/用量
    {"type":"thinking","text":"…"}                             → REASONING
    {"type":"text","text":"…"}                                 → ANSWER（先推 delta 再落事件）
    {"type":"tool_call","callId","tool","input"}               → ACTION
    {"type":"tool_result","callId","status","result"}          → RESULT
    {"type":"final","text":"…"}                                → 兜底：本轮没答过话就把它当答案
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import config
from .events import EventType

# 安装包默认落点 + 常见的自定义安装位置。用户也可以在设置里手填。
_SEARCH = (
    r"D:\DeepSeek Harness",
    r"C:\Program Files\DeepSeek Harness",
    r"C:\Program Files (x86)\DeepSeek Harness",
)
_PROFILE = "headless"
_TURN_TIMEOUT = 30 * 60          # 一轮最多半小时。卡死的进程比失败的进程更让人难受
_CLI_IN_ASAR = "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"


def _candidates() -> list[Path]:
    out: list[Path] = []
    configured = str(config.get().get("dsh_path") or "").strip()
    if configured:
        out.append(Path(configured).expanduser())
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Programs" / "DeepSeek Harness")
    out.extend(Path(p) for p in _SEARCH)
    return out


def asar_has(asar: Path, needle: str) -> bool | None:
    """asar 里有没有这个文件。

    为什么不能直接 `Path.exists()`：`app.asar` 在操作系统眼里是**一个 182MB 的文件**，
    只有 Electron 自己那层 fs 补丁才把 `xxx.asar/里面/的路径` 当目录看。
    所以 Python 侧怎么 exists 都是 False —— 但拿这个路径去喂 `DeepSeek Harness.exe`
    又是能跑的（它就是靠这个活着的）。这里读 asar 自己的 JSON 头来判断，
    读不出来就返回 None（不知道），别拿「不知道」当「不存在」。
    """
    try:
        with asar.open("rb") as fh:
            head = fh.read(16)
            if len(head) < 16:
                return None
            json_len = struct.unpack("<I", head[12:16])[0]
            if not 0 < json_len < 8 * 1024 * 1024:
                return None
            raw = fh.read(json_len)
        import json as _json
        _json.loads(raw.decode("utf-8", "replace"))
        return needle.encode("utf-8") in raw
    except (OSError, ValueError, struct.error, UnicodeDecodeError):
        return None


def locate() -> dict[str, Any]:
    """找 DSH。返回 {ok, exe, cli, home, note}，找不到也不抛 —— 界面要能说话。

    结果缓存 15 秒：`/api/engines` 会被反复调（切栏、打开选择器），
    而每探一次都要读 asar 头 —— 那是 182MB 的文件，不该为了一句「装没装」
    每次都去翻。15 秒足够「刚装完回来点一下」看到变化，又不会把磁盘读穿。
    """
    global _LOCATE_AT
    now = time.time()
    if _LOCATE and now - _LOCATE_AT < 15:
        return dict(_LOCATE)

    found = _locate_uncached()
    _LOCATE.clear()
    _LOCATE.update(found)
    _LOCATE_AT = now
    return dict(found)


_LOCATE: dict[str, Any] = {}
_LOCATE_AT = 0.0


def forget() -> None:
    """把缓存的探测结果丢掉（改了 dsh_path、或者用户刚装完 DSH 时用）。"""
    global _LOCATE_AT
    _LOCATE.clear()
    _LOCATE_AT = 0.0


def _locate_uncached() -> dict[str, Any]:
    for base in _candidates():
        # 用户可能填的是 exe 本身，也可能填的是目录
        exe = base if base.suffix.lower() == ".exe" else base / "DeepSeek Harness.exe"
        if not exe.exists():
            continue
        asar = exe.parent / "resources" / "app.asar"
        if not asar.exists():
            return {"ok": False, "exe": str(exe), "cli": "",
                    "note": "装是装了，但缺 resources/app.asar（安装不完整？）"}
        present = asar_has(asar, "dsh-desktop-host")
        if present is False:
            return {"ok": False, "exe": str(exe), "cli": "",
                    "note": "asar 里没有 dsh-desktop-host，这个版本的命令行入口变了"}
        # 注意：这个路径在 OS 里 exists() 为 False，但喂给 exe 是对的（见 asar_has）
        cli = str(asar / _CLI_IN_ASAR.replace("/", os.sep))
        return {"ok": True, "exe": str(exe), "cli": cli,
                "home": str(Path.home() / ".dsh"), "note": ""}

    # 有些人装的是纯命令行版（`dsh` 在 PATH 里）
    onpath = shutil.which("dsh")
    if onpath:
        return {"ok": True, "exe": onpath, "cli": "", "home": str(Path.home() / ".dsh"),
                "note": "用的是 PATH 里的 dsh"}
    return {"ok": False, "exe": "", "cli": "", "note": "这台机器上没找到 DeepSeek Harness"}


def _api_key() -> str:
    """role2 的凭据从 DreamRole 这边借。

    DSH 自己有一套凭据服务（桌面版「模型」页写进去的那份），但它躺在一个我们读不到
    的私有存储里；而 headless 认的是环境变量 DEEPSEEK_API_KEY。所以把用户在
    DreamRole 里填过的那个 key 传过去 —— 一个 key 配两次才是真的别扭。
    """
    env = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env:
        return env
    providers = config.get().get("providers") or {}
    for pid in ("deepseek", "deepseek-official", "dsh"):
        conf = providers.get(pid) or {}
        key = str(conf.get("api_key") or "").strip()
        if key:
            return key
    for conf in providers.values():
        if not isinstance(conf, dict):
            continue
        key = str(conf.get("api_key") or "").strip()
        if key:
            return key
    return ""


def _command(found: dict, text: str, session_id: str) -> list[str]:
    if found.get("cli"):
        cmd = [found["exe"], "--expose-internals", found["cli"]]
    else:
        cmd = [found["exe"]]
    cmd += ["--profile", _PROFILE, "--json"]
    if session_id:
        cmd += ["--session-id", session_id]
    cmd.append(text)
    return cmd


def _arg_summary(args: Any) -> str:
    """把工具参数压成一行摘要 —— 和 role1 的工具卡长得一致。

    路径类的值**从前面截**（留 `…/父目录/文件名`），不是从后面截。
    从后面截会把文件名切掉，而「它在动哪个文件」正是这一行要回答的事 ——
    截了头你还能认出是哪个文件，截了尾就只剩一串谁也不是的目录名。
    """
    if not isinstance(args, dict):
        return str(args)[:120]
    parts: list[str] = []
    for key, value in args.items():
        text = str(value).replace("\n", " ").replace("\r", "")
        if len(text) > 60:
            head = text.split(":", 1)[0].lower()
            looks_path = ("\\" in text or "/" in text) and len(head) <= 2
            if looks_path:
                bits = [one for one in text.replace("\\", "/").split("/") if one]
                text = "…/" + "/".join(bits[-2:]) if len(bits) > 2 else text
                if len(text) > 60:
                    text = text[:60] + "…"
            else:
                text = text[:60] + "…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)[:160]


def run(sess, text: str) -> None:
    """跑 role2 的一轮。异常由调用方（`Session.run_turn`）统一翻译成 notice。"""
    found = locate()
    if not found["ok"]:
        sess.emit(EventType.NOTICE,
                  f"role2 用不了：{found['note']}。装好 DeepSeek Harness，或到设置里填路径。",
                  payload={"engine": "role2", "error": True})
        return

    key = _api_key()
    if not key:
        # 没有 key 时 dsh 会回 MISSING_CREDENTIAL。提前说清楚，省一轮等待。
        sess.emit(EventType.NOTICE,
                  "role2 缺 DeepSeek 的 API key：到设置里填一个（role2 会借用它）。",
                  payload={"engine": "role2", "error": True})
        return

    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = key
    if found.get("cli"):
        env["ELECTRON_RUN_AS_NODE"] = "1"

    cmd = _command(found, text, sess.dsh_session)
    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=sess.project or None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        sess.emit(EventType.NOTICE, f"role2 起不来：{exc}", payload={"engine": "role2", "error": True})
        return

    sess._proc = proc
    stderr_tail: list[str] = []

    def drain_err() -> None:
        try:
            for line in proc.stderr:            # type: ignore[union-attr]
                stderr_tail.append(line.rstrip())
                if len(stderr_tail) > 40:
                    del stderr_tail[:10]
        except Exception:                       # noqa: BLE001
            pass

    def watchdog() -> None:
        """用户点「中断」时，光设个标志位是没用的 —— 对面是个独立进程。"""
        while proc.poll() is None:
            if sess.cancelled.is_set() or time.time() - started > _TURN_TIMEOUT:
                try:
                    proc.kill()
                except OSError:
                    pass
                return
            time.sleep(0.25)

    threading.Thread(target=drain_err, name=f"dsh-err-{sess.cid}", daemon=True).start()
    threading.Thread(target=watchdog, name=f"dsh-watch-{sess.cid}", daemon=True).start()

    call_ids: dict[str, str] = {}       # DSH 的 callId → 我们那条 ACTION 事件的 id
    answered = False
    final_text = ""
    err_reason = ""

    try:
        for line in proc.stdout:        # type: ignore[union-attr]
            if sess.cancelled.is_set():
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")

            if kind == "session":
                sid = str(event.get("sessionId") or "")
                if sid:
                    sess.dsh_session = sid
                continue

            if kind == "status":
                phase = event.get("phase")
                if phase == "step_end":
                    usage = event.get("usage") or {}
                    _bump(sess, usage)
                elif phase == "turn_end":
                    reason = event.get("reason") or {}
                    if reason.get("kind") == "error":
                        err_reason = str((reason.get("error") or {}).get("message") or "未知错误")
                continue

            if kind == "thinking":
                thought = str(event.get("text") or "").strip()
                if thought:
                    sess._push({"kind": "reason", "cid": sess.cid, "text": thought})
                    sess.emit(EventType.REASONING, thought,
                              payload={"engine": "role2", "chars": len(thought)})
                continue

            if kind == "text":
                piece = str(event.get("text") or "")
                if not piece.strip():
                    continue
                # 先推增量（界面有一条跟着长出来的气泡），再落正式事件 ——
                # 这条路径和 role1 的流式完全一致，界面那边不用为 role2 写分支。
                for chunk in _chunks(piece):
                    sess._push({"kind": "delta", "cid": sess.cid, "text": chunk})
                sess.emit(EventType.ANSWER, piece, payload={"engine": "role2"})
                answered = True
                continue

            if kind == "tool_call":
                name = str(event.get("tool") or "tool")
                args = event.get("input") or {}
                action = sess.emit(
                    EventType.ACTION,
                    _arg_summary(args),
                    tool=name,
                    payload={"engine": "role2", "args": args,
                             "call_id": str(event.get("callId") or "")},
                )
                cid = str(event.get("callId") or "")
                if cid:
                    call_ids[cid] = action.id
                continue

            if kind == "tool_result":
                cid = str(event.get("callId") or "")
                result = event.get("result")
                text_result = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                status = str(event.get("status") or "")
                sess.emit(
                    EventType.RESULT,
                    (text_result or "")[:20000],
                    parent_id=call_ids.get(cid),
                    payload={"engine": "role2", "status": status, "ok": status == "completed"},
                )
                continue

            if kind == "final":
                final_text = str(event.get("text") or "")
                continue
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        sess._proc = None

    if sess.cancelled.is_set():
        return

    code = proc.returncode
    if err_reason:
        sess.emit(EventType.NOTICE, f"role2 报错：{err_reason}",
                  payload={"engine": "role2", "error": True})
    elif code not in (0, None):
        tail = "\n".join(stderr_tail[-6:])[:1200]
        sess.emit(EventType.NOTICE,
                  f"role2 异常退出（code={code}）。{tail or 'DSH 没留下原因。'}",
                  payload={"engine": "role2", "error": True, "exit": code})
    elif not answered and final_text.strip():
        sess.emit(EventType.ANSWER, final_text, payload={"engine": "role2"})
    elif not answered and not err_reason:
        sess.emit(EventType.ANSWER, "（role2 这一轮没输出任何内容。再说一次？）",
                  payload={"engine": "role2"})


def _chunks(text: str, size: int = 60) -> list[str]:
    """把一整段切小一点再推增量。

    DSH 的 `text` 是「一个 step 的完整正文」，不是 token 流。一口气推给界面
    会变成「啪」地出现一大块；切 60 字一节，观感才和 role1 的真流式接近。
    """
    if len(text) <= size:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)]


def _bump(sess, usage: dict) -> None:
    try:
        prompt = int(usage.get("inputTokens") or 0)
        completion = int(usage.get("outputTokens") or 0)
    except (TypeError, ValueError):
        return
    sess.usage["prompt"] = int(sess.usage.get("prompt") or 0) + prompt
    sess.usage["completion"] = int(sess.usage.get("completion") or 0) + completion
    sess.usage["total"] = int(sess.usage.get("total") or 0) + prompt + completion
    sess.usage["calls"] = int(sess.usage.get("calls") or 0) + 1


def describe() -> dict[str, Any]:
    found = locate()
    has_key = bool(_api_key())
    return {
        "id": "role2",
        "label": "Role 2 · DSH 内核",
        "short": "role2",
        "ready": bool(found["ok"] and has_key),
        "ok": found["ok"],
        "has_key": has_key,
        "path": found.get("exe") or "",
        "home": found.get("home") or "",
        "note": found.get("note") or ("" if has_key else "还没有可用的 DeepSeek key"),
        "tools": "DSH 自己的 56 个工具",
        "approval": "无（headless 没有审批通道）",
    }
