"""命令行通道：不开窗口也能真的跑一轮。

为什么值得做（而不是「反正有界面」）：

- 定时任务、CI、脚本里要它干活的时候，没人坐在窗口前点确认；
- 排查「到底是模型的问题还是界面的问题」时，命令行是唯一没有中间层的地方；
- 关掉窗口进程就没了，但一条会话还在。第二天用 `chat --resume <cid>` 接着说是正常需求。

实现上刻意走 HTTP 而不是直接调 Session：这样「通道」是真的三条，不是三条
共用同一段绕过服务层的捷径。走了捷径，命令行看到的行为就会和界面悄悄不一样，
而那种差异最费时间。

权限闸门在命令行里也要问。**「没人可以问」不等于「可以替用户同意」** ——
所以非交互模式下遇到闸门默认跳过，并把这一步明确说出来。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
POLL = 0.25
CHANNEL_HEADER = {"X-DreamRole-Channel": "cli"}

HELP = """DreamRole 命令行

  dreamrole chat [选项] [一句话]

选项：
  --project PATH   工作区（必须；也可以用 --resume 从旧会话带过来）
  --mode MODE      craft / plan / ask / teach
  --permission P   readonly / standard / staged / full
  --think LEVEL    off / low / medium / high
  --assistant ID   用某个助理的身份和它的屋子
  --group ID       进某个群聊
  --resume CID     接着一条已有会话说
  --json           把事件按 JSON 一行一条打出来（给脚本用）
  --yes            遇到要确认的一律同意（脚本里明确表过态才用）

不给「一句话」就进交互模式，一行一句，exit 退出。
"""


def _call(port: int, method: str, path: str, payload=None):
    data = None
    headers = dict(CHANNEL_HEADER)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"error": exc.reason}
        return exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": f"连不上本地服务：{exc}"}


def _parse(argv: list[str]) -> dict:
    options = {"project": "", "mode": "", "permission": "", "think": "",
               "assistant": "", "group": "", "resume": "", "json": False,
               "yes": False, "text": ""}
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("-h", "--help"):
            options["help"] = True
            index += 1
            continue
        if item in ("--json", "--yes"):
            options[item.lstrip("-")] = True
            index += 1
            continue
        if item.startswith("--") and item[2:] in options:
            key = item[2:]
            index += 1
            if index < len(argv):
                options[key] = argv[index]
                index += 1
            continue
        rest.append(item)
        index += 1
    options["text"] = " ".join(rest).strip()
    return options


def _print_event(event: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(event, ensure_ascii=False))
        return
    kind = event.get("type")
    content = event.get("content") or ""
    if kind == "user":
        print("\n> " + content)
    elif kind == "reasoning":
        print("  [想] " + content.replace("\n", "\n       "))
    elif kind == "action":
        print("  [做] " + content)
    elif kind == "result":
        head = content.strip().splitlines()[:6]
        print("  [果] " + ("\n       ".join(head) if head else "（空）")
              + ("\n       …" if content.count("\n") > 5 else ""))
    elif kind == "answer":
        member = (event.get("payload") or {}).get("member")
        who = f"{member.get('emoji', '')}{member.get('name')}" if member else ""
        print((f"\n{who}：" if who else "\n") + content)
    elif kind == "notice":
        print("  [提示] " + content)
    elif kind == "think":
        print("  [讲] " + content)


def _drain(port: int, cid: str, seen: set[str], as_json: bool) -> dict:
    status, data = _call(port, "GET", f"/api/session/{urllib.parse.quote(cid)}")
    if status != 200:
        print("读不到会话状态：" + str(data.get("error") or status), file=sys.stderr)
        return {}
    for event in data.get("events") or []:
        key = event.get("id") or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        _print_event(event, as_json)
    return data


def _answer_gates(port: int, cid: str, state: dict, auto_yes: bool) -> None:
    """把挂着的问题处理掉。非交互模式下一律跳过，并且说清跳过了什么。"""
    for request in state.get("pending") or []:
        kind = request.get("kind")
        if kind == "gate":
            decision = "always" if auto_yes else "skip"
            if not auto_yes:
                print(f"  [跳过] {request.get('tool')} —— 命令行里没人能确认，"
                      f"加 --yes 可以让它自己同意", file=sys.stderr)
            _call(port, "POST", f"/api/session/{urllib.parse.quote(cid)}/answer",
                  {"id": request.get("id"), "decision": decision})
        elif kind in ("stage", "ask"):
            _call(port, "POST", f"/api/session/{urllib.parse.quote(cid)}/answer",
                  {"id": request.get("id"), "decision": "skip"})


def drive(port: int, argv: list[str]) -> int:
    options = _parse(argv)
    if options.get("help"):
        print(HELP)
        return 0

    cid = options["resume"]
    payload: dict = {}
    if options["mode"]:
        payload["mode"] = options["mode"]
    if options["permission"]:
        payload["permission"] = options["permission"]
    if options["think"]:
        payload["think"] = options["think"]
    if options["assistant"]:
        payload["assistant"] = options["assistant"]
    if options["group"]:
        payload["group"] = options["group"]

    if not cid:
        # 没给 resume 就必须有落脚的目录。助理和群有自己的屋子，不用给。
        if not (options["assistant"] or options["group"]) and not options["project"]:
            print("先给一个工作区：--project D:/某个目录（或者用 --resume 接着旧会话）",
                  file=sys.stderr)
            return 2
        if options["project"]:
            payload["project"] = options["project"]
        status, data = _call(port, "POST", "/api/session", payload)
        if status != 200:
            print("开不了会话：" + str(data.get("error") or status), file=sys.stderr)
            return 1
        cid = data.get("cid", "")
        if not options["json"]:
            print(f"会话 {cid} · {data.get('project')}")
    else:
        status, data = _call(port, "GET", f"/api/session/{urllib.parse.quote(cid)}")
        if status != 200:
            print("没有这条会话：" + str(data.get("error") or status), file=sys.stderr)
            return 1
        if not options["json"]:
            print(f"接着 {cid} 说 · {data.get('project')}")

    if options["think"]:
        _call(port, "POST", f"/api/session/{urllib.parse.quote(cid)}/think",
              {"level": options["think"]})

    seen: set[str] = set()
    _drain(port, cid, seen, options["json"])

    def one_turn(text: str) -> bool:
        status, data = _call(port, "POST",
                             f"/api/session/{urllib.parse.quote(cid)}/send", {"text": text})
        if status != 200:
            print("发不出去：" + str(data.get("error") or status), file=sys.stderr)
            return False
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            state = _drain(port, cid, seen, options["json"])
            if state.get("pending"):
                _answer_gates(port, cid, state, bool(options["yes"]))
            if not state.get("running"):
                return True
            time.sleep(POLL)
        print("等太久了，先撤。", file=sys.stderr)
        return False

    if options["text"]:
        return 0 if one_turn(options["text"]) else 1

    # 交互模式：一行一句。Ctrl+D / exit 退出。
    if not options["json"]:
        print("一行一句，exit 退出。")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in ("exit", "quit", ":q"):
            return 0
        one_turn(line)


def main(port: int, argv: list[str]) -> int:
    try:
        return drive(port, argv)
    except KeyboardInterrupt:
        return 130
