"""会话主循环。

一句话概括这个文件在干什么：**把「模型说要做什么」变成「事件流上真的发生了什么」**。

几个刻意的设计：

1. `emit()` 一定遍历 `self.listeners` 广播给所有订阅者。
   「我的agent」那个项目在这里出了一个很贵的 bug：`emit_control()` 只在
   `on_event` 非空时才发断点信号，而 `server.py` 建 Session 时从没传过 `on_event`，
   于是所有断点信号静默消失，前端永远等不到确认框。这里不设「可选回调」——
   订阅是唯一的出路，没人订阅就是没人看，但代码路径不会分叉。

2. 权限判定在「执行前」，不在「计划后」。模型说要删文件，删之前拦下来；
   而不是等它删完了再告诉用户。

3. 断点（阶段交接）超时一律**停下**，绝不替用户往下走。
   「自动继续」看起来更顺滑，但那会让用户在一次走神后发现 agent 已经干完了
   他没同意的事——这是不可接受的。
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from . import (
    audit,
    automemory,
    config,
    context,
    dsh_engine,
    engines,
    grants as grants_mod,
    labels,
    modes,
    permissions,
    rules,
    skills,
    store,
    textio,
    thinking,
    tools,
)
from .events import Event, EventType
from .llm import Cancelled, ChatClient, LLMError
from .tools.base import ToolContext

MAX_TOOL_CALLS_PER_ROUND = 4      # 一轮模型回答里最多执行几个工具（防止它一口气干 20 件事）
MAX_TOOL_OUTPUT = 20_000          # 回填给模型的单个工具输出上限
GATE_TIMEOUT = 180.0              # 等用户确认的最长时间（秒）
SAVE_EVERY = 8                    # 每积累这么多事件落一次盘，防止崩溃丢过程

# 并行子代理会从多个线程写同一份轨迹文件，落盘要排队
_SAVE_LOCK = threading.Lock()


class _Pause(Exception):
    """用户选择「我接手」，从工具执行的最深处一路抛到 turn 层。"""


REF_PATTERN = re.compile(r"@([^\s@，。；、：:（）()\[\]【】\"'`]+)")
MAX_REFS = 8
MAX_REF_CHARS = 6000

# 教学模式里「开阶段之前也可以做」的调用：它们只是整理信息，不动项目。
# 拦住它们没有意义，反而会让模型一开头就撞墙。
_STAGE_FREE = {"todo_write", "skill_load", "memory_read"}


def _audit_target(args: dict) -> str:
    """审计里记「它到底要动什么」。一个工具可能用好几个键指向目标，按重要性取第一个。"""
    for key in ("path", "command", "url", "target", "source", "name", "role"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ", ".join(sorted(args)) or "（无参数）"


# ---------------------------------------------------------------------------
# 活着的会话
# ---------------------------------------------------------------------------

SESSIONS: dict[str, "Session"] = {}
_SESSIONS_LOCK = threading.Lock()


def new_cid() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def create(project: str, mode: str | None = None, permission: str | None = None,
           cid: str | None = None, expert: str | None = None,
           assistant: str | None = None, members: list[str] | None = None,
           group: str | None = None, grants: list | None = None,
           engine: str | None = None, inherit: bool = True) -> "Session":
    """开一条会话。

    `inherit=True`（默认）时把**全局通行证**抄进来 —— 这是「新会话起手就有」
    的唯一入口。从磁盘恢复老会话时传 False：那份列表是它当时的样子，
    重启或者重开一次不该悄悄多出几个目录（见 grants.py 顶部）。
    """
    cid = cid or new_cid()
    session = Session(cid=cid, project=project, mode=mode, permission=permission,
                      expert=expert, assistant=assistant, members=members, group=group,
                      grants=grants_mod.inherit(grants) if inherit else grants,
                      engine=engine)
    with _SESSIONS_LOCK:
        SESSIONS[cid] = session
    return session


def get(cid: str) -> "Session | None":
    with _SESSIONS_LOCK:
        return SESSIONS.get(cid)


def drop(cid: str) -> None:
    with _SESSIONS_LOCK:
        SESSIONS.pop(cid, None)


def running_ids() -> list[str]:
    with _SESSIONS_LOCK:
        return [cid for cid, s in SESSIONS.items() if s.running]


# ---------------------------------------------------------------------------
# 小工具函数
# ---------------------------------------------------------------------------

def _short_args(raw: str, limit: int = 140) -> str:
    """把 {"path": "a.py", "content": "很长很长…"} 压成 path=a.py, content=很长…"""
    try:
        obj = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return (raw or "")[:limit]
    if not isinstance(obj, dict):
        return str(obj)[:limit]
    parts: list[str] = []
    for key, value in obj.items():
        text = str(value).replace("\n", "\\n").replace("\r", "")
        if len(text) > 44:
            text = text[:44] + "…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)[:limit]


def _parse_args(raw: str) -> dict:
    try:
        obj = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


# 权限严格程度排序，只用于展示与排序，判定见 modes.MODE_PERMISSION
_STRICTNESS = {"readonly": 0, "standard": 1, "staged": 2, "full": 3}


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, cid: str, project: str,
                 mode: str | None = None, permission: str | None = None,
                 expert: str | None = None, assistant: str | None = None,
                 members: list[str] | None = None, group: str | None = None,
                 grants: list | None = None, engine: str | None = None) -> None:
        self.cid = cid
        # 底层用哪一套：role1 = 本文件这套内核；role2 = 本机的 DeepSeek Harness。
        # **建好就绑死**，不给中途换 —— 两边的历史格式不一样，硬换等于让模型读天书。
        self.engine = engines.normalize(engine if engine is not None else config.get().get("engine"))
        # role2 的会话 id 存在 DSH 那边（~/.dsh/sessions）。多轮靠它续：
        # 下一轮用 --session-id 接上，不然每句都是第一句。
        self.dsh_session = ""
        self._proc = None       # role2 的子进程句柄，中断时要杀它
        # 工作区是**每个会话自己的**，而且必须由用户显式选。
        # 空串 = 还没选。以前这里会静默退到 cwd，于是「没设工作区也能跑」——
        # 模型就会在一个用户根本不知道的目录里读写文件。
        self.project = str(Path(project).expanduser().resolve()) if project else ""
        self.root = Path(self.project) if self.project else Path.cwd()
        self.mode = modes.normalize(mode or config.get().get("mode"))
        self.permission = permission if permission in permissions.MATRIX else permissions.current_level()
        # 额外授权目录（通行证）。工作区是主目录，这些是「另外也允许去一趟」的地方。
        # 跟着会话走：全局授权等于每条会话都带着而用户不知道，见 grants.py 顶部。
        self.grants = grants_mod.clean(grants)
        # 「要不要停」这个标志必须在 ctx 之前建好 —— ctx 要把 is_cancelled 交给
        # run_command，否则那条命令跑到一半没人能叫停它（见 shell.py 顶部）。
        self.cancelled = threading.Event()
        # 当前正在跑的工具。心跳只知道「跑了多少秒」，
        # 得靠它说出「是哪件事跑了多少秒」。
        self._beat: dict | None = None
        self.ctx = self._make_ctx()

        # 专家 = 常驻身份；技能 = 按需加载的能力。两者互不替代，见 experts.py 顶部
        self.expert = str(expert or "")
        # 助理 = 这条会话属于谁。群聊时 members 有两个以上，assistant 留空。
        self.assistant = str(assistant or "")
        self.members = [str(a) for a in (members or []) if str(a)]
        self.group = str(group or "")
        # 深度思考：off / low / medium / high。翻译成每家参数的事在 thinking.py
        self.think = thinking.normalize(str(config.get().get("think") or thinking.DEFAULT))
        # 轮次：检查点按 (cid, turn) 聚合，比时间窗口可靠
        self.turn = 0

        # 事件流与订阅
        self.events: list[Event] = []
        self.listeners: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._unsaved = 0

        # 对话状态
        self.messages: list[dict] = [{"role": "system", "content": ""}]
        self.running = False
        self.step_id = 0
        self.stage = 0
        self.stage_title = ""
        self.last_parent: str | None = None
        self.compactions = 0
        self.usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "seconds": 0.0}
        # 上一次模型调用花了多少 token。它要挂到「这一次调用产生的那条事件」上，
        # 否则过程图里每个节点都只能写「不知道花了多少」—— 整轮一个总数，
        # 分摊到十几个步骤上全是编的。谁先来谁领走（工具调用或最终答复）。
        self._pending_usage: dict = {}
        self.todos: list[dict] = []      # 模型自己维护的待办清单，长任务时给用户看进度
        self._stage_at_turn_start = 0    # 这一轮开始时在第几阶段（用来判断「是不是真的在交接」）
        self._stage_nudges = 0           # 教学模式里提醒过几次「先开阶段」
        self._final_nudges = 0           # 提醒过几次「别一个阶段就收尾」
        self._actions_this_turn = 0      # 这一轮真做了几件事（用来判断该不该催）
        self._dlp_noted = False          # 这一轮是否已经提示过「内容里有凭据，已打码」
        # 用户在这一轮点名要用的技能。只在当前这一轮进系统提示，
        # 下一轮不带上 —— 否则用户在第一句选了「写周报」，之后每句都背着它。
        self._turn_skills: list[str] = []

        # 等待用户决策
        self._cond = threading.Condition()
        self._pending: dict[str, dict] = {}
        self._answers: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        # 「已经有人认领了这一轮，但线程还没把 running 置上」的那一小段。
        # 只靠 running 判断会漏掉它：两次几乎同时的 start() 都能看到 running=False，
        # 于是同一轮被跑两遍，消息交错、事件流出现两条并行的链。
        self._starting = False
        self._turn_gate = threading.Lock()

    # -- 权限 ---------------------------------------------------------------

    @property
    def has_workspace(self) -> bool:
        return bool(self.project)

    def wait_idle(self, timeout: float = 30) -> bool:
        """等这一轮跑完。返回 False 表示等到超时它还在跑。

        外部脚本和测试用得上：`run_turn` 是在线程里跑的，直接 `run_turn` 下一轮
        会撞上「上一轮还没结束」而抛异常 —— 在守护线程里这异常会被吞掉，
        表现就是「什么都没发生」，很难查。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                return True
            time.sleep(0.05)
        return not self.running

    def set_workspace(self, path: str) -> str:
        """换这个会话的工作区。返回规范化后的路径。

        换之前已经读过的文件引用、已经压在上下文里的旧目录内容都不清 ——
        它们本来就是那一轮的事实。但要在事件流里留一句，
        否则用户会以为整轮对话都发生在新目录里。
        """
        text = str(path or "").strip()
        if not text:
            raise ValueError("工作区不能为空")
        target = Path(text).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        resolved = str(target.resolve())
        previous = self.project
        self.project = resolved
        self.root = Path(resolved)
        self.ctx = ToolContext(root=self.root, cid=self.cid, project=resolved,
                               grants=self.grants)
        if previous != resolved:
            self.emit(
                EventType.NOTICE,
                f"工作区改为 `{resolved}`。" + (f"（原来是 `{previous}`）" if previous else ""),
                payload={"workspace": resolved},
            )
            self._save()
        return resolved

    # -- 额外授权目录（通行证） ---------------------------------------------

    def _sync_ctx(self) -> None:
        """grants 是可变列表，换工作区/改授权之后要重新交给 ctx ——
        不然工具层还用着上一份。"""
        self.ctx = self._make_ctx()

    def _make_ctx(self) -> ToolContext:
        """造一份工具上下文。

        `extra` 里那两个回调是给**长命令**用的（`run_command`）：
        `is_cancelled` 让它在用户点「停下」时立刻 taskkill，
        `on_beat` 让它每两秒报一声「还在跑，跑了多少秒」。
        不给的话这条命令会把整条会话锁到超时为止 —— 那正是用户报的
        「显示已停下，可对话框还用不了」。
        """
        return ToolContext(
            root=self.root, cid=self.cid, project=self.project, grants=self.grants,
            extra={
                "is_cancelled": self.cancelled.is_set,
                "on_beat": self._tool_beat,
            },
        )

    def _tool_beat(self, seconds: int, tail: str) -> None:
        """长命令的心跳。**只推流，不落盘。**

        它每两秒变一次，进了事件流就变成噪音（一次安装能刷出上百条），
        而它的价值只在「此刻这一屏」——历史里没人会去翻「第 37 秒它在干什么」。
        """
        current = self._beat or {}
        self._push({
            "kind": "toolbeat",
            "cid": self.cid,
            "tool": current.get("tool") or "",
            "label": current.get("label") or "",
            "seconds": int(seconds),
            "tail": tail or "",
        })

    def update_grants(self, op: str, path: str = "", mode: str = "") -> tuple[list[dict], str]:
        """加 / 删 / 改档 / 清空。返回 (当前清单, 一句说明)。

        每次都往事件流里记一句：用户改了「这个 agent 能去哪」，这件事本身
        得看得见 —— 过程图里能看到「什么时候开了哪道门」，比事后翻设置文件强。
        """
        if op == "add":
            self.grants, note = grants_mod.add(self.grants, path, mode or grants_mod.MODE_RO)
        elif op == "remove":
            self.grants, note = grants_mod.remove(self.grants, path)
        elif op == "mode":
            self.grants, note = grants_mod.set_mode(self.grants, path, mode)
        elif op == "clear":
            note = f"已撤掉全部 {len(self.grants)} 个授权目录。" if self.grants else "本来就没有授权目录。"
            self.grants = []
        else:
            return grants_mod.describe(self.grants), f"不认识的操作：{op}"
        self._sync_ctx()
        self.emit(EventType.NOTICE, note,
                  payload={"grants": grants_mod.describe(self.grants)})
        self._save()
        return grants_mod.describe(self.grants), note

    def grant_paths(self) -> list[dict]:
        return grants_mod.describe(self.grants)

    def effective_permission(self) -> str:
        """这一轮实际按哪一档权限跑（模式优先于用户设置，规则见 modes.py）。"""
        return modes.effective_permission(self.mode, self.permission)

    # -- 事件 ---------------------------------------------------------------

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        """注册订阅者，返回取消函数。

        注册和「补发已有事件」在同一把锁里完成 —— 否则刚补完历史、还没注册好，
        中间发生的事件就永久丢了。SSE 断线重连时这个竞态是真实存在的。
        """
        with self._lock:
            backlog = [e.to_dict() for e in self.events]
            self.listeners.append(listener)
        try:
            for data in backlog:
                listener({"kind": "event", "event": data})
        except Exception:  # noqa: BLE001 - 订阅者自己的问题不该影响会话
            pass

        def unsubscribe() -> None:
            with self._lock:
                if listener in self.listeners:
                    self.listeners.remove(listener)

        return unsubscribe

    def _push(self, message: dict) -> None:
        """广播一条非事件消息（流式片段、状态变化、待确认请求）。"""
        with self._lock:
            listeners = list(self.listeners)
        for listener in listeners:
            try:
                listener(message)
            except Exception:  # noqa: BLE001
                continue

    def _merge_pending_usage(self, usage: dict) -> None:
        """累加「还没被领走」的用量。

        为什么要累加而不是覆盖：一次模型调用可能只产出一段思考、既没有工具调用
        也没有最终答复（下一句还会接着来）。直接覆盖的话，前一次的账就被后一次
        冲掉了 —— 加起来的数会比会话总数少，而少掉的正是那些「光想了想」的调用。
        """
        if not usage:
            return
        cur = self._pending_usage
        merged = dict(cur)
        for key in ("prompt", "completion", "total"):
            merged[key] = int(cur.get(key) or 0) + int(usage.get(key) or 0)
        merged["seconds"] = round(float(cur.get("seconds") or 0)
                                  + float(usage.get("seconds") or 0), 3)
        merged["calls"] = int(cur.get("calls") or 0) + int(usage.get("calls") or 1)
        self._pending_usage = merged

    def _flush_pending_usage(self) -> None:
        """一轮结束时把还没归账的用量贴到最后一条动作 / 答复上。

        什么情况下会有剩的：一次模型调用只回了段内容、既没调工具也没收尾
        （提示词催了一下，它又想了想），那一笔账就悬在半空。下一轮开跑会清空它，
        于是「会话总账 60、节点加起来 30」。贴到同轮最后一步是最不歪的一种记法：
        那次调用确实是为这一步收尾花的。
        """
        if not self._pending_usage:
            return
        with self._lock:
            last = next((event for event in reversed(self.events)
                         if event.type in (EventType.ANSWER, EventType.ACTION)), None)
        if last is not None:
            old = dict(last.payload.get("usage") or {})
            merged = dict(old)
            for key in ("prompt", "completion", "total"):
                merged[key] = int(old.get(key) or 0) + int(self._pending_usage.get(key) or 0)
            merged["seconds"] = round(float(old.get("seconds") or 0)
                                      + float(self._pending_usage.get("seconds") or 0), 3)
            merged["calls"] = int(old.get("calls") or 0) + int(self._pending_usage.get("calls") or 0)
            last.payload = {**last.payload, "usage": merged}
        self._pending_usage = {}

    def emit(self, etype: EventType | str, content: str = "", *,
             tool: str | None = None,
             payload: dict | None = None,
             parent_id: str | None = None,
             stage: int | None = None,
             stage_title: str | None = None) -> Event:
        if not isinstance(etype, EventType):
            etype = EventType(etype)
        # token 记账：一次模型调用产生的第一条「动作 / 答复」领走这次用量。
        # 只认这两种 —— 思维链和系统提示会把同一笔账重复记两遍。
        if self._pending_usage and etype in (EventType.ACTION, EventType.ANSWER):
            payload = {**(payload or {}), "usage": self._pending_usage}
            self._pending_usage = {}

        event = Event(
            type=etype,
            content=content,
            parent_id=parent_id if parent_id is not None else self.last_parent,
            tool=tool,
            step_id=self.step_id,
            stage=self.stage if stage is None else stage,
            stage_title=self.stage_title if stage_title is None else stage_title,
            payload=payload or {},
        )
        with self._lock:
            self.events.append(event)
            self.last_parent = event.id
            listeners = list(self.listeners)
            self._unsaved += 1
            need_save = self._unsaved >= SAVE_EVERY
            if need_save:
                self._unsaved = 0
        data = event.to_dict()
        for listener in listeners:
            try:
                listener({"kind": "event", "event": data})
            except Exception:  # noqa: BLE001
                continue
        if need_save:
            self._save()
        return event

    # -- 持久化 -------------------------------------------------------------

    def _save(self) -> None:
        with self._lock:
            snapshot = list(self.events)
        first_user = next((e.content for e in snapshot if e.type == EventType.USER), "")
        title = " ".join(str(first_user).split())[:60] or "新会话"
        try:
            # 并行子代理会从多个线程同时触发落盘；同一份轨迹写两次
            # 会撞上同一个 .tmp 文件名，所以这里自己排个队。
            with _SAVE_LOCK:
                store.save_trace(
                    self.cid,
                    snapshot,
                    {
                        "cid": self.cid,
                        "title": title,
                        "project": self.project,
                        # 授权目录跟着会话落盘：重启之后「这个 agent 能去哪」还得是原样
                        "grants": grants_mod.describe(self.grants),
                        "mode": self.mode,
                        "permission": self.permission,
                        "expert": self.expert,
                        "assistant": self.assistant,
                        "members": list(self.members),
                        "group": self.group,
                        "think": self.think,
                        "stage": self.stage,
                        "usage": dict(self.usage),
                        "steps": self.step_id,
                        # 引擎跟着会话落盘：重启之后点开同一句话，不能变成另一套内核在跑
                        "engine": self.engine,
                        "dsh_session": self.dsh_session,
                    },
                )
        except OSError:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            events = [e.to_dict() for e in self.events]
            pending = list(self._pending.values())
        return {
            "cid": self.cid,
            "project": self.project,
            "engine": self.engine,
            "engine_label": engines.label(self.engine),
            "dsh_session": self.dsh_session,
            "grants": grants_mod.describe(self.grants),
            "mode": self.mode,
            "permission": self.permission,
            "expert": self.expert,
            "assistant": self.assistant,
            "members": list(self.members),
            "group": self.group,
            "think": self.think,
            "turn": self.turn,
            "effective": self.effective_permission(),
            "running": self.running,
            "stage": self.stage,
            "stage_title": self.stage_title,
            "step_id": self.step_id,
            "usage": dict(self.usage),
            "compactions": self.compactions,
            "todos": list(self.todos),
            "events": events,
            "pending": pending,
        }

    # -- 用户决策 -----------------------------------------------------------

    def _ask(self, kind: str, payload: dict, timeout: float = GATE_TIMEOUT) -> str:
        """挂一个待确认请求，阻塞等前端回话。返回决策字符串；空串表示超时/取消。"""
        req_id = uuid.uuid4().hex[:8]
        request = {"id": req_id, "kind": kind, "cid": self.cid,
                   "created": time.time(), **payload}
        with self._cond:
            self._pending[req_id] = request
        self._push({"kind": "pending", "request": request})

        deadline = time.monotonic() + timeout
        decided = ""
        with self._cond:
            while req_id not in self._answers:
                left = deadline - time.monotonic()
                if left <= 0 or self.cancelled.is_set():
                    break
                self._cond.wait(min(left, 0.4))
            decided = self._answers.pop(req_id, "")
            self._pending.pop(req_id, None)
        self._push({"kind": "pending_gone", "id": req_id})
        return decided

    def answer(self, req_id: str, decision: str) -> bool:
        """前端回话。返回 False 表示这个请求已经过期/已被处理。"""
        with self._cond:
            if req_id not in self._pending:
                return False
            self._answers[req_id] = decision
            self._pending.pop(req_id, None)
            self._cond.notify_all()
        return True

    @property
    def is_starting(self) -> bool:
        """已经认领了这一轮，但线程还没跑到 `running = True`。

        这两件事之间有个空窗，`send` 的 409、`cancel` 的返回值都得以它为准，
        只看 `running` 会在那个空窗里给出相反的答案。
        """
        return self._starting

    def cancel(self) -> bool:
        """请它停下。返回「本来到底有没有东西可停」。

        返回值的用处很具体：界面上原来不管什么情况都弹一句「已让它停下」，
        可这一轮其实还活着（命令还在跑、`_starting` 还占着），
        于是用户看到的是「说了停下了，但对话框还是不能用」。
        如实回答「本来就在跑」，前端才有本钱说「正在停」而不是「已停」。

        注意 `cancel()` 是**请求**不是**完成**：真正收尾在 worker 线程的
        `finally` 里（命令被杀、`running` 归 False、`_starting` 放开）。
        从前那条命令要等 `communicate(timeout=300)` 返回，这个「收尾」
        最长要三百秒 —— 现在 shell.run 每 80ms 看一眼标志位，是一秒级的事。
        """
        was_alive = self.running or self._starting
        self.cancelled.set()
        # role2 是个独立进程：只设标志位它不会自己停。这里顺手杀一下，
        # 不用等 watchdog 那 0.25 秒的轮询 —— 用户按了中断就该马上停。
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        with self._cond:
            self._cond.notify_all()
            # 正在等确认的请求也要解开，否则线程卡死在 wait() 上
            waiting = [item["id"] for item in self._pending.values()]
        for req_id in waiting:
            self.answer(req_id, "cancel")
        return was_alive

    # -- 权限闸门 -----------------------------------------------------------

    def _gate(self, tool: str, args: dict, reason: str) -> tuple[bool, str]:
        detail = _short_args(json.dumps(args, ensure_ascii=False)) if args else ""
        self.emit(
            EventType.NOTICE,
            f"要执行「{labels.label(tool)}」，等你确认。",
            payload={
                "gate": True,
                "tool": tool,
                "label": labels.label(tool),
                "reason": reason,
                "arguments": args,
                "detail": detail,
            },
        )
        decision = self._ask(
            "gate",
            {
                "tool": tool,
                "label": labels.label(tool),
                "reason": reason,
                "arguments": args,
                "detail": detail,
                "options": [
                    {"value": "allow", "label": "允许这一次"},
                    {"value": "always", "label": "以后都允许"},
                    {"value": "deny", "label": "拒绝"},
                ],
            },
        )
        if decision in ("allow", "always"):
            if decision == "always":
                permissions.remember(tool)
            return True, decision
        if decision == "cancel" or self.cancelled.is_set():
            raise Cancelled()
        return False, decision or "timeout"

    def _stage_gate(self, next_title: str) -> bool:
        """阶段交接处的断点。只问「往下走还是我接手」，超时按停下处理。"""
        self.emit(
            EventType.NOTICE,
            f"阶段 {self.stage} 做完了。下一步要进「{next_title}」，等你点头。",
            payload={"breakpoint": True, "next_stage": self.stage + 1, "next_title": next_title},
        )
        decision = self._ask(
            "stage",
            {
                "stage": self.stage,
                "next_stage": self.stage + 1,
                "next_title": next_title,
                "options": [
                    {"value": "continue", "label": "继续，我看着"},
                    {"value": "stop", "label": "停一下，我来接手"},
                ],
            },
        )
        if decision == "cancel" or self.cancelled.is_set():
            raise Cancelled()
        return decision == "continue"

    # -- 系统提示 -----------------------------------------------------------

    def _system_message(self) -> str:
        parts = [modes.system_prompt(self.mode, self.project, self.effective_permission(),
                                  self.grants)]
        # 助理 = 常驻身份，排在专家前面：专家是「这次换谁的角度」，
        # 助理是「我是谁」。后者更底层，不该被前者盖掉。
        if self.assistant:
            try:
                from . import assistants

                note = assistants.system_note(assistants.get(self.assistant) or {})
                if note:
                    parts.append(note)
            except Exception:  # noqa: BLE001 - 人设坏了不该让对话起不来
                pass
        try:
            from . import experts

            block = experts.prompt_block(self.expert)
            if block:
                parts.append(block)
        except Exception:  # noqa: BLE001 - 专家配置坏了不该让对话起不来
            pass
        block = skills.prompt_block()
        if block:
            parts.append(block)
        # 用户这一轮点名要用的技能：正文直接进来。
        # 和上面那段的分工是「让模型自己判断」vs「用户已经决定了」。
        picked = skills.turn_block(self._turn_skills)
        if picked:
            parts.append(picked)
        try:
            from . import mcp

            extra = mcp.prompt_block()
            if extra:
                parts.append(extra)
        except Exception:  # noqa: BLE001 - MCP 没配/没连上不该影响主流程
            pass
        return "\n\n".join(p for p in parts if p)

    def _tool_payload(self) -> list[dict] | None:
        enabled = tools.enabled_set()
        names = modes.allowed_tools(self.mode, tools.ALL_NAMES) & enabled
        # 专家可以再收一层：模式说「你能用这些」，专家说「这个身份只用这些」
        if self.expert:
            try:
                from . import experts

                names = experts.allowed_tools(self.expert, names)
            except Exception:  # noqa: BLE001
                pass
        schemas = tools.schemas_for(names)
        # 只读档不给外部工具：MCP 工具做什事我们管不了，保守起见整类不开放
        if self.effective_permission() != "readonly":
            try:
                from . import mcp

                extra = mcp.schemas()
                if extra:
                    schemas = schemas + extra
                    names = names | {s["function"]["name"] for s in extra}
            except Exception:  # noqa: BLE001
                pass
        self._tool_names = names
        return schemas or None

    # -- 上下文治理 ---------------------------------------------------------

    def _maybe_compact(self, client: ChatClient) -> None:
        if not context.should_compact(self.messages, client.model):
            return
        before = len(self.messages)
        try:
            self.messages, summary = context.compact(self.messages, client.model, client)
        except Exception:  # noqa: BLE001 - 压缩失败就带着大上下文继续，别崩
            return
        if summary:
            self.compactions += 1
            self.emit(
                EventType.NOTICE,
                f"上下文用了不少，把较早的 {before - len(self.messages) + 1} 条记录压成了一段摘要。",
                payload={"compact": True, "summary": summary[:4000]},
            )

    def _bump_usage(self, usage: dict) -> None:
        if not usage:
            return
        self.usage["prompt"] += int(usage.get("prompt") or 0)
        self.usage["completion"] += int(usage.get("completion") or 0)
        self.usage["total"] += int(usage.get("total") or 0)
        self.usage["seconds"] = round(self.usage["seconds"] + float(usage.get("seconds") or 0), 2)
        self.usage["calls"] += 1

    # -- 工具执行 -----------------------------------------------------------

    def _enter_stage(self, call: dict) -> None:
        """阶段切换。

        这件事不做成普通工具调用，因为它改的是会话结构，不是文件。
        走工具那条路的话，动作事件会落在旧阶段里、结果事件落在新阶段里，
        图上就出现两个「开阶段」夹着一列 —— 看着像 bug。

        这里只产出一个「阶段标记」事件：它不占节点，只给那一列起个名字。
        """
        args = _parse_args(call.get("arguments") or "{}")
        title = str(args.get("title") or "").strip() or f"阶段 {self.stage + 1}"
        # 只在「这一轮中途换阶段」时断点。跨轮的第一阶段不拦 ——
        # 用户刚说完要干什么，再问一次「要不要开始」就是纯骚扰。
        if self.stage > self._stage_at_turn_start and self.effective_permission() == "staged":
            if not self._stage_gate(title):
                raise _Pause()
        self.stage += 1
        self.stage_title = title
        self.emit(EventType.NOTICE, f"阶段 {self.stage} · {title}",
                  payload={"stage_marker": True})
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id") or "",
                "content": f"已进入阶段 {self.stage}：{title}。继续这一步。",
            }
        )

    def _run_virtual(self, name: str, args: dict) -> str:
        if name == "set_stage":
            return "（阶段切换由会话层处理）"

        if name == "skill_load":
            target = str(args.get("name") or "").strip()
            text = skills.load(target)
            self.emit(EventType.NOTICE, f"载入技能「{target}」",
                      payload={"skill": target, "chars": len(text)})
            return text

        if name == "skill_create":
            # 虚拟工具不走权限闸门（它们改的是会话状态，不是文件），
            # 但技能是实打实往磁盘写东西的，只读档必须挡住。
            if self.effective_permission() == "readonly":
                return "现在是只读档，什么文件都不写 —— 技能也是一个文件。让用户切一下档位。"
            try:
                item = skills.create(
                    str(args.get("name") or ""),
                    str(args.get("description") or ""),
                    str(args.get("body") or ""),
                    overwrite=bool(args.get("overwrite")),
                )
            except ValueError as exc:
                return str(exc)
            except OSError as exc:
                return f"写不进去：{exc}"
            self.emit(EventType.NOTICE, f"写好了一个技能「{item.get('name')}」",
                      payload={"skill": item.get("name"), "created": True,
                               "path": item.get("path")})
            return (f"已创建技能「{item.get('name')}」：{item.get('path')}\n"
                    f"它现在就在技能栏里了，下一轮的系统提示也会带上它的名字和说明。")

        if name == "subagent":
            from . import subagents

            batch = args.get("tasks")
            if isinstance(batch, list) and batch:
                self.emit(EventType.NOTICE,
                          f"并行派 {min(len(batch), 4)} 个子代理",
                          payload={"subagent": "batch", "count": len(batch)})
                return subagents.run_many(batch, self)

            role = str(args.get("role") or "explore")
            task = str(args.get("task") or "").strip()
            if not task:
                return "任务描述是空的，子代理不知道要做什么。"
            self.emit(EventType.NOTICE, f"派子代理（{role}）去查：{task[:80]}",
                      payload={"subagent": role, "task": task})
            return subagents.run(role, task, self)

        if name == "schedule_create":
            from . import scheduler

            return scheduler.create_from_tool(args, self.project)

        if name == "todo_write":
            return self._update_todos(args)

        return f"（{name} 没有对应的处理逻辑）"

    def _update_todos(self, args: dict) -> str:
        """模型自己维护的待办清单。

        为什么值得单独做：一轮超过十步的任务，用户唯一想知道的是「还剩几件事」。
        让他自己去数消息条数是很糟的体验。清单不落文件、不进 memory ——
        它的生命周期就是这一轮对话。
        """
        raw = args.get("items") or []
        cleaned: list[dict] = []
        if isinstance(raw, list):
            for item in raw[:20]:
                if isinstance(item, str):
                    text = item.strip()
                    status = "pending"
                elif isinstance(item, dict):
                    text = str(item.get("text") or "").strip()
                    status = str(item.get("status") or "pending")
                else:
                    continue
                if not text:
                    continue
                if status not in ("pending", "doing", "done"):
                    status = "pending"
                cleaned.append({"text": text[:200], "status": status})
        self.todos = cleaned
        self.emit(EventType.NOTICE, "待办清单已更新",
                  payload={"todos": cleaned, "todos_update": True})
        if not cleaned:
            return "待办清单已清空。"
        marks = {"pending": "[ ]", "doing": "[~]", "done": "[x]"}
        body = "\n".join(f"{marks[item['status']]} {item['text']}" for item in cleaned)
        done = sum(1 for item in cleaned if item["status"] == "done")
        return f"已更新待办（{done}/{len(cleaned)} 完成）：\n{body}"

    def _expand_refs(self, text: str) -> tuple[str, list[str]]:
        """把 `@某个/文件.py` 展开成文件内容，塞在用户话后面。

        为什么不让模型自己去读：用户既然手点了这个文件，意思就是「这个我必须让你看」。
        如果只给个路径，模型有一半概率不去读它，或者读错别的同名文件。
        这里直接把内容摆到眼前，省一次往返，也省一次读错。

        展开结果只进模型上下文，不改用户看到的原话 —— 免得界面里出现几屏文件内容。
        """
        if "@" not in text:
            return text, []
        names = []
        for hit in REF_PATTERN.finditer(text):
            name = hit.group(1).strip()
            if name and name not in names:
                names.append(name)
        if not names:
            return text, []

        blocks: list[str] = []
        used: list[str] = []
        budget = MAX_REF_CHARS * MAX_REFS
        for name in names[:MAX_REFS]:
            target = (self.root / name).resolve()
            try:
                target.relative_to(self.root)
            except ValueError:
                blocks.append(f"【{name}】不在项目目录里，没有展开。")
                continue
            if not target.is_file():
                continue
            try:
                raw = textio.read_text(target)
            except OSError as exc:
                blocks.append(f"【{name}】读不出来：{exc}")
                continue
            body = raw[: min(MAX_REF_CHARS, max(budget, 0))]
            budget -= len(body)
            used.append(name)
            blocks.append(f"【{name}】\n```\n{body}\n```")
            if budget <= 0:
                break

        if not blocks:
            return text, []
        head = "\n\n（用户引用的文件内容如下，已经帮你读好了，不用再调工具读一遍）\n"
        return text + head + "\n\n".join(blocks), used

    def _run_tool(self, name: str, raw_args: str) -> str:
        args = _parse_args(raw_args)

        if name in tools.VIRTUAL:
            try:
                return self._run_virtual(name, args)
            except _Pause:
                raise
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                return f"处理失败：{type(exc).__name__}: {exc}"

        if getattr(self, "_tool_names", None) is not None and name not in self._tool_names:
            return f"工具 {name} 在当前模式下不可用。"

        # 先过安全规则，再过权限档位。
        # 顺序不能反：档位是用户可以放松的偏好，「往系统目录写」「读出私钥」这种
        # 底线不该因为他点了「全权」就变成允许。
        rule_action, rule_why, rule_id = rules.check(name, args, self.project)
        if rule_action == "deny":
            audit.append("agent", name, _audit_target(args), "deny", rule_why, self.cid)
            self.emit(EventType.NOTICE, f"没有执行「{labels.label(name)}」：{rule_why}",
                      payload={"denied": True, "rule": rule_id, "tool": name})
            return f"（被安全规则拦下：{rule_why}）"

        verdict, reason = permissions.decide(name, self.effective_permission())
        if rule_action == "gate" and verdict is permissions.Verdict.ALLOW:
            verdict, reason = permissions.Verdict.GATE, rule_why
        if verdict is permissions.Verdict.DENY:
            audit.append("agent", name, _audit_target(args), "deny", reason, self.cid)
            self.emit(EventType.NOTICE, f"没有执行「{labels.label(name)}」：{reason}",
                      payload={"denied": True, "tool": name, "reason": reason})
            return f"（被权限拦下：{reason}）"
        if verdict is permissions.Verdict.GATE:
            audit.append("agent", name, _audit_target(args), "gate", reason, self.cid)
            allowed, decision = self._gate(name, args, reason)
            if not allowed:
                audit.append("user", name, _audit_target(args), "skip", "用户没有同意", self.cid)
                return "（用户没有同意这一步，已跳过。换个更保守的做法，或者问他为什么。）"
            audit.append("user", name, _audit_target(args), "allow", "用户同意", self.cid)
            if decision == "always":
                self.emit(EventType.NOTICE, f"以后「{labels.label(name)}」不再询问。",
                          payload={"remembered": name})
        else:
            audit.append("agent", name, _audit_target(args), "allow", "", self.cid)

        # 改文件的工具要留快照，供 diff 与回滚
        before = after = ""
        target_path = ""
        existed = True
        if name in ("write_file", "edit_file"):
            try:
                target = self.ctx.resolve(str(args.get("path") or ""))
                target_path = str(target)
                existed = target.is_file()
                if existed:
                    before = textio.read_text(target)
            except (ValueError, OSError):
                before = ""

        output = (
            self._call_mcp(name, args)
            if name.startswith("mcp__")
            else tools.call(self.ctx, name, json.dumps(args, ensure_ascii=False))
        )

        if target_path:
            try:
                path = Path(target_path)
                after = textio.read_text(path) if path.is_file() else ""
                store.record_change(target_path, before, after, self.cid, self.project,
                                    turn=self.turn, existed=existed)
            except OSError:
                pass

        return output

    def _call_mcp(self, name: str, args: dict) -> str:
        try:
            from . import mcp

            return mcp.call(name, args)
        except Exception as exc:  # noqa: BLE001
            return f"MCP 调用出错：{type(exc).__name__}: {exc}"

    def _needs_stage_first(self) -> bool:
        """教学模式：这一轮还没开过阶段，就不许先干活。

        为什么要在代码里拦，而不是只写进提示词：实测（DeepSeek）会把 set_stage
        放到最后当「总结」用，于是整轮只有一个阶段，断点永远不触发，
        教学模式就退化成普通模式了。提示词管不住这件事，得让那次工具调用真的失败一次。

        只拦前两次 —— 万一模型就是不肯配合，也要让它把话说完，不能卡死。
        """
        return (
            self.mode == "teach"
            and self._stage_nudges < 2
            and self.stage == self._stage_at_turn_start
        )

    def _refuse_for_stage(self, call: dict) -> None:
        self._stage_nudges += 1
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id") or "",
                "content": (
                    "还没有开阶段，这一步没有执行。教学模式里动手之前必须先调用 "
                    "set_stage(title) 说明「这一阶段要做什么」，然后再做具体的事。"
                ),
            }
        )

    def _nudge_for_next_stage(self) -> bool:
        """教学模式：一个阶段就打算收尾，推它一把。

        为什么又要在代码里管：实测（DeepSeek）会把「分两步」理解成「一个阶段里
        做两件事」，然后一口气总结完。用户在带练模式里要的是**每步都停一下**，
        一次讲完等于没带。提示词里写得很清楚（每个阶段结束前提一个问题等用户回答），
        但模型不照做，所以这里让它的「总结」被打回一次。

        只推两次、只在这一轮真动过手的时候推 —— 用户问一句「这是什么」不该被逼着
        开阶段。已经开了两个以上阶段的，说明它在按节奏走，放它说完。
        """
        if self.mode != "teach" or self.effective_permission() != "staged":
            return False
        if self._final_nudges >= 2 or self._actions_this_turn == 0:
            return False
        opened = self.stage - self._stage_at_turn_start
        if opened > 1:
            return False

        self._final_nudges += 1
        self.emit(
            EventType.NOTICE,
            "只走了一个阶段就打算收尾，让它接着往下带。",
            payload={"stage_nudge": True},
        )
        self.messages.append(
            {
                "role": "user",
                "content": (
                    "（这是系统提醒，不是用户说的）这一轮你只开了 "
                    f"{max(opened, 0)} 个阶段就准备总结了。带练模式要求一步一步来："
                    "一个阶段只解决一个小问题，做完停下来等用户回答。"
                    "现在请调用 set_stage 开下一个阶段，只做那一步，然后把话交回给用户。"
                ),
            }
        )
        return True

    def _exec(self, call: dict) -> None:
        name = call["name"]
        raw = call.get("arguments") or "{}"
        args = _parse_args(raw)
        action = self.emit(
            EventType.ACTION,
            f"{labels.label(name)} · {_short_args(json.dumps(args, ensure_ascii=False))}",
            tool=name,
            payload={"tool": name, "label": labels.label(name), "arguments": args,
                     "detail": _short_args(raw)},
        )
        try:
            self._beat = {"tool": name, "label": labels.label(name)}
            output = self._run_tool(name, raw)
        except (_Pause, Cancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            output = f"工具出错：{type(exc).__name__}: {exc}"
        finally:
            self._beat = None

        ok = not output.startswith(("工具出错", "（被权限拦下", "参数不对", "拒绝："))
        self._actions_this_turn += 1
        self.emit(
            EventType.RESULT,
            output[:MAX_TOOL_OUTPUT],
            tool=name,
            parent_id=action.id,
            payload={"ok": ok, "tool": name, "chars": len(output)},
        )
        self.messages.append(
            {"role": "tool", "tool_call_id": call.get("id") or "",
             "content": output[:MAX_TOOL_OUTPUT]}
        )

    # -- 主循环 -------------------------------------------------------------

    def _on_delta(self, piece: str) -> None:
        self._push({"kind": "delta", "cid": self.cid, "text": piece})

    def _loop(self) -> None:
        max_steps = int(config.get().get("max_steps") or 24)
        client = ChatClient()
        self._tool_names = None
        # 思考等级要翻译成这一家的参数。翻译不了的（比如这家根本没这个能力），
        # 会拿到一句说明 —— 只提示一次，不然每一轮都刷一遍「这家不支持」。
        actual_model, extra, think_note = thinking.apply_level(
            client.conn.get("provider") or "", client.model, self.think,
            has_tools=bool(self._tool_payload()),
        )
        if think_note and self.think != "off":
            self.emit(EventType.NOTICE, think_note, payload={"thinking": self.think,
                                                             "degraded": True})
        rounds = 0
        while rounds < max_steps:
            if self.cancelled.is_set():
                raise Cancelled()
            rounds += 1
            self._maybe_compact(client)

            def on_reason(piece: str) -> None:
                # 只往界面推，不在这里攒：正式的那条事件用模型返回的完整思维链，
                # 攒两遍迟早会出现「实时那段和落盘那段对不上」。
                self._push({"kind": "reason", "cid": self.cid, "text": piece})

            # 只在真的需要时才多传参数：调用形状保持「最朴素的那种」，
            # 换一个实现了 ChatClient 接口的东西过来（测试用的假模型就是），
            # 不至于因为多了个它不认识的 kwarg 就整体崩掉。
            call_kwargs: dict = {}
            if self.think != "off":
                call_kwargs["on_reason"] = on_reason
            if actual_model and actual_model != client.model:
                call_kwargs["model"] = actual_model
            if extra:
                call_kwargs["extra"] = extra

            result = client.chat(
                self.messages,
                tools=self._tool_payload(),
                on_delta=self._on_delta,
                is_cancelled=self.cancelled.is_set,
                **call_kwargs,
            )
            self._bump_usage(result.get("usage") or {})
            self._merge_pending_usage(result.get("usage") or {})
            if result.get("redactions") and not self._dlp_noted:
                # 一轮只提一次：这是「我替你把钥匙遮了」，说多了就成了噪音
                self._dlp_noted = True
                self.emit(EventType.NOTICE,
                          f"这段内容里有 {result['redactions']} 处看起来像密钥/令牌，"
                          "已经打码之后才发出去（可在「安全」栏关掉）。",
                          payload={"redactions": result["redactions"]})
            for note in result.get("degraded") or []:
                self.emit(EventType.NOTICE, f"思考等级没能生效：{note}",
                          payload={"degraded": True})

            # 思维链单独成一条事件，而不是混进回答里。
            # 混进去的后果是：用户看到的「答案」前半段其实是它在自言自语。
            thought = (result.get("reasoning") or "").strip()
            if thought:
                self.emit(EventType.REASONING, thought,
                          payload={"level": self.think, "chars": len(thought)})

            content = (result.get("content") or "").strip()
            calls = result.get("tool_calls") or []
            if len(calls) > MAX_TOOL_CALLS_PER_ROUND:
                self.emit(
                    EventType.NOTICE,
                    f"模型一口气要做 {len(calls)} 件事，这轮先做前 {MAX_TOOL_CALLS_PER_ROUND} 件。",
                    payload={"truncated_calls": len(calls)},
                )
                calls = calls[:MAX_TOOL_CALLS_PER_ROUND]

            assistant: dict[str, Any] = {"role": "assistant", "content": result.get("content") or ""}
            if calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.get("id") or f"call_{i}",
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call.get("arguments") or "{}"},
                    }
                    for i, call in enumerate(calls)
                ]
            self.messages.append(assistant)

            if content:
                self.emit(EventType.THINK if calls else EventType.ANSWER, content)

            if not calls:
                if not content:
                    self.emit(EventType.ANSWER, "（模型这轮没说话，也没调工具。再说一次你的要求？）")
                    return
                if self._nudge_for_next_stage():
                    continue
                return

            for call in calls:
                if self.cancelled.is_set():
                    raise Cancelled()
                if call.get("name") == "set_stage":
                    self._enter_stage(call)
                    continue
                if self._needs_stage_first() and call.get("name") not in _STAGE_FREE:
                    self._refuse_for_stage(call)
                    continue
                self._exec(call)

        self.emit(
            EventType.NOTICE,
            f"已经跑了 {max_steps} 步还没收尾，先停在这里。把任务拆小一点会更快。",
            payload={"max_steps": max_steps},
        )

    def _group_loop(self) -> None:
        """群聊：几个助理对同一段对话依次发言。

        刻意**不给群成员工具**。理由是成本与责任：一轮讨论里四个人各调三次工具，
        谁写了哪个文件、写坏了算谁的，用户根本追不回来。群聊的产出是「看法」，
        真要动手，界面上每位助理旁边都有「单独聊」—— 那才是能干活的入口。
        """
        from . import assistants as A

        group = A.get_group(self.group) or {}
        members = A.members_of(group)
        if not members:
            members = [m for m in (A.get(a) for a in self.members) if m]
        if not members:
            self.emit(EventType.NOTICE, "这个群里的助理都不在了，先回去把成员补上。",
                      payload={"error": True})
            return

        said = self.messages[-1].get("content", "") if self.messages else ""
        speakers = A.pick_speakers(group, said)[:4]
        if not speakers:
            names = "、".join(m.get("name", "?") for m in members)
            self.emit(
                EventType.ANSWER,
                f"这个群是「只让被点名的说」。在话里带上名字（{names}）再发一次。",
                payload={"group_rule": group.get("rule"), "need_mention": True},
            )
            return

        self.emit(
            EventType.NOTICE,
            "这一轮轮到：" + "、".join(m.get("name", "?") for m in speakers),
            payload={"group": self.group, "speakers": [m["id"] for m in speakers]},
        )

        try:
            client = ChatClient()
        except LLMError as exc:
            self.emit(EventType.NOTICE, f"模型没起来：{exc}", payload={"error": True})
            return

        base = modes.system_prompt(self.mode, self.project, self.effective_permission(),
                                   self.grants)
        rule = group.get("rule") or "round_robin"
        host = group.get("host") or ""

        for order, member in enumerate(speakers):
            if self.cancelled.is_set():
                raise Cancelled()
            persona = A.system_note(member)
            role_hint = "你是这个群里的成员。"
            if rule == "host":
                if member["id"] == host and order == 0:
                    role_hint = "你是主持人。先开个头，把问题拆开，然后请其他人说。"
                elif member["id"] == host:
                    role_hint = "你是主持人。把大家说的收一收，给出这个群的结论。"
            system = "\n\n".join(
                p for p in (
                    base,
                    persona,
                    role_hint,
                    "这是群聊：每个人说的话前面都标了是谁说的。只说你要补充的、要反对的、"
                    "或者要改的地方 —— 不要复述别人刚说过的话。三句话以内。",
                ) if p
            )
            payload_messages = [{"role": "system", "content": system}] + self.messages[1:]

            extra: dict = {}
            if member.get("model"):
                extra["model"] = member["model"]
            try:
                result = client.chat(
                    payload_messages,
                    is_cancelled=self.cancelled.is_set,
                    temperature=0.6,
                    **extra,
                )
            except LLMError as exc:
                self.emit(EventType.NOTICE, f"{member.get('name')} 没能说话：{exc}",
                          payload={"error": True, "member": member["id"]})
                continue
            except Cancelled:
                raise

            reply = (result.get("content") or "").strip()
            self._bump_usage(result.get("usage") or {})
            self._merge_pending_usage(result.get("usage") or {})
            if not reply:
                continue
            self.emit(
                EventType.ANSWER,
                reply,
                payload={
                    "member": {
                        "id": member["id"],
                        "name": member.get("name") or member["id"],
                        "emoji": member.get("emoji") or "🙂",
                    },
                    "group": self.group,
                },
            )
            # 后面的人要看得见前面说了什么，所以带上前缀存进上下文
            self.messages.append(
                {"role": "assistant", "content": f"[{member.get('name')}] {reply}"}
            )

        if self.assistant:
            A.touch(self.assistant, 1)

    def run_turn(self, text: str) -> None:
        """同步跑完一轮。HTTP 层不需要直接调它，调 `start()`。"""
        if self.running:
            raise RuntimeError("上一轮还没结束")
        # 兜底：HTTP 层已经拦了一次，这里再拦一次。
        # 「没工作区就不许跑」是一条不变量，靠调用方自觉迟早会被绕过去。
        if not self.has_workspace:
            self.emit(EventType.NOTICE, "这个会话还没有工作区，先选一个目录再开始。",
                      payload={"need_workspace": True})
            return
        self.running = True
        # 直接调 run_turn 的（脚本、selftest、live_test）没经过 `start()`，
        # 得在这儿把上一轮留下的停止标志清掉。经过 `start()` 的那条路已经
        # 在门里清过了 —— 两处都清不是重复，是各管一段：
        # 在门里清是必须的，因为「认领之后、线程被调度到之前」那几百微秒里
        # 按下的「停下」如果留到这儿才清，就会被**吞掉**（用户按了，没生效）。
        if not self._starting:
            self.cancelled.clear()
        self.step_id += 1
        self.turn += 1
        # 每轮重置「阶段基线」：断点只在同一轮内部换阶段时触发，
        # 跨轮的第一阶段不拦（用户刚说要干什么，再问一次就是骚扰）。
        self._stage_at_turn_start = self.stage
        self._stage_nudges = 0
        self._final_nudges = 0
        self._actions_this_turn = 0
        self._dlp_noted = False
        self._pending_usage = {}
        self._push({"kind": "state", "cid": self.cid, "running": True, "step_id": self.step_id})
        try:
            if self.engine != "role2":
                # role2 不吃我们这份系统提示（它有自己的），省下来也算省一次拼装
                self.messages[0] = {"role": "system", "content": self._system_message()}
            expanded, refs = self._expand_refs(text)
            self.emit(EventType.USER, text, payload={"refs": refs} if refs else None)
            # 让用户在对话里看得见「这一轮挂了哪些技能」。
            # 不说的话，模型行为变了而界面上毫无线索，很像「它今天怎么怪怪的」。
            if self._turn_skills:
                live = [name for name in self._turn_skills if skills.find(name)]
                if live and self.engine == "role2":
                    # 说清楚，别让用户以为「我挂了技能它怎么不用」是 bug
                    self.emit(EventType.NOTICE,
                              "role2 用的是 DSH 自己的技能，" + "、".join(live) + " 这一轮不会生效。",
                              payload={"skills": live, "engine": "role2"})
                elif live:
                    self.emit(EventType.NOTICE, "本轮启用技能：" + "、".join(live),
                              payload={"skills": live})
            if refs:
                self.emit(EventType.NOTICE, f"已带上 {len(refs)} 个引用文件：{'、'.join(refs)}",
                          payload={"refs": refs})
            self.messages.append({"role": "user", "content": expanded})
            if self.engine == "role2":
                # role2 有自己的系统提示、工具和权限模型，DreamRole 这边不掺和 ——
                # 只把这句话（含 @ 引用展开后的文本）原样交给它。
                dsh_engine.run(self, expanded)
            elif self.members:
                self._group_loop()
            else:
                self._loop()
                if self.assistant:
                    from . import assistants as _assistants

                    _assistants.touch(self.assistant, 1)
            # 这一轮真的跑完了：把值得留下的东西落到记忆里。
            # 放在 try 里而不是 finally —— 中断、报错、暂停的那一轮不该被当成「经验」记下来。
            # 它自己起线程、自己吞异常，所以这里不用再包一层 try。
            automemory.after_turn(self)
        except _Pause:
            self.emit(EventType.ANSWER, "按你的意思停在这里。接下来怎么做，等你安排。",
                      payload={"paused": True})
        except Cancelled:
            self.emit(EventType.NOTICE, "已经中止这一轮。", payload={"cancelled": True})
        except LLMError as exc:
            self.emit(EventType.NOTICE, str(exc), payload={"error": True})
        except Exception as exc:  # noqa: BLE001 - 主循环不能把整个进程带崩
            self.emit(
                EventType.NOTICE,
                f"内部错误：{type(exc).__name__}: {exc}",
                payload={"error": True, "trace": traceback.format_exc()[-3000:]},
            )
        finally:
            self.running = False
            self._flush_pending_usage()
            # 技能只对这一轮有效。留在身上，下一句就会莫名其妙地还按老技能做。
            self._turn_skills = []
            self._save()
            self._push({"kind": "state", "cid": self.cid, "running": False,
                        "step_id": self.step_id, "usage": dict(self.usage)})

    def start(self, text: str, skill_names: list[str] | None = None) -> None:
        """起一个后台线程跑这一轮，立刻返回。SSE 会把过程推给前端。

        认领这一轮必须发生在**开线程之前**、并且在锁里：`run_turn` 里的
        `running = True` 要等线程被调度到才执行，中间那段空窗足够让第二次调用
        也通过检查（双击发送、定时任务和手动消息撞在一起都会走到这里）。

        `skill_names`：用户在输入栏点名要用的技能。放在门里设置，
        和 `_starting` 是一回事 —— 它得在**这一轮开跑之前**就位。
        """
        with self._turn_gate:
            if self.running or self._starting:
                # 这两句话得分开。「停下之后仍不可用」那件事，本质就是
                # 用户按了停下、界面也说停了，但这里还在占着门。
                # 说清楚「正在停」和「还在跑」，用户才知道是等一秒还是等一分钟。
                if self.cancelled.is_set():
                    raise RuntimeError("上一轮正在停下，稍等一下再发")
                raise RuntimeError("上一轮还没结束，等它跑完或者先按停下")
            self._starting = True
            # 新一轮从「没被要求停下」开始。清在这儿（而不是等 run_turn 里清）
            # 是因为认领之后线程还要等调度，那段空窗里按的停下必须算数。
            self.cancelled.clear()
            self._turn_skills = [str(n).strip() for n in (skill_names or [])
                                 if str(n).strip()]

        def worker() -> None:
            try:
                self.run_turn(text)
            except RuntimeError:
                pass
            finally:
                self._starting = False

        thread = threading.Thread(target=worker, name=f"turn-{self.cid}", daemon=True)
        self._thread = thread
        thread.start()

    # -- 重来 ---------------------------------------------------------------

    def _last_user_index(self) -> int:
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                return i
        return -1

    def retry(self) -> str:
        """把上一次尝试从轨迹里删掉，用同一句用户原话重跑。

        为什么是「删掉」而不是「在下面开个分支」：用户点重试的意思是
        「这次不算，重来」。留着一个失败分支，模型下一轮会把它当成上下文继续推理，
        于是在错误的方向上越走越远。真要保留对比，那是「分支」功能该干的事。

        返回重跑用的原话；空串表示没有可重试的内容。
        """
        if self.running:
            raise RuntimeError("上一轮还没结束")
        idx = self._last_user_index()
        if idx < 0:
            return ""
        original = self.messages[idx].get("content") or ""

        # 事件流里把这一条 user 之后的全删掉，界面上也就干净了
        with self._lock:
            cut = len(self.events)
            for i, event in enumerate(self.events):
                if event.type == EventType.USER:
                    cut = i
            if cut < len(self.events):
                self.events = self.events[:cut]
        del self.messages[idx:]
        self.compactions = 0
        self.todos = []
        self._rewind_tail()
        self._push({"kind": "reset", "cid": self.cid, "step_id": self.step_id + 1})
        return original

    def _rewind_tail(self) -> None:
        """截断事件流之后，把「最后一个事件」指针退回去。

        不退的话，新产生的事件会挂在一条已经不存在的父节点上，
        过程图里就会出现一根指向虚空的连线。
        """
        with self._lock:
            self.last_parent = self.events[-1].id if self.events else None
            self._unsaved = 0
            # 阶段也跟着事件流回退，否则图上的列号会对不上
            stage = 0
            title = ""
            for event in self.events:
                if event.payload.get("stage_marker"):
                    stage = event.stage
                    title = event.stage_title
            self.stage = stage
            self.stage_title = title

    # -- 分支 ---------------------------------------------------------------

    def branch(self, index: int) -> "Session":
        """从第 index 条用户消息处分叉，开一个新会话。

        和「改一句再发」的区别：那个是**改**原来那条路，这个是**留着**原来的，
        另外起一条。用户想试两种走法的时候，删掉旧的就再也回不去了。

        新会话的模型上下文只保留「用户说了什么 + 最终答了什么」，
        工具调用细节不搬 —— 那些 tool 消息带着原来的 call_id，
        搬到新会话里既对不上号，也只会白烧 token。
        """
        users = [i for i, event in enumerate(self.events) if event.type == EventType.USER]
        if index < 0 or index >= len(users):
            return None  # type: ignore[return-value]
        cut = users[index]

        # 分叉出去的那条接着干同一件事，授权也得跟着 ——
        # 不跟的话，用户分叉之后会发现「刚才还能读的目录现在读不了了」。
        child = create(self.project, mode=self.mode, permission=self.permission,
                       grants=list(self.grants))
        child.stage = 0
        child.stage_title = ""

        # 事件流照抄到分叉点（时间戳和 id 重新生成，否则两张图会共用节点 id）
        for event in self.events[:cut + 1]:
            copied = Event(
                type=event.type,
                content=event.content,
                tool=event.tool,
                step_id=event.step_id,
                stage=event.stage,
                stage_title=event.stage_title,
                payload=dict(event.payload or {}),
            )
            with child._lock:
                child.events.append(copied)
                child.last_parent = copied.id

        # 模型上下文只留对话本身
        turns: list[dict] = []
        for event in self.events[:cut + 1]:
            if event.type == EventType.USER:
                turns.append({"role": "user", "content": event.content})
            elif event.type == EventType.ANSWER:
                turns.append({"role": "assistant", "content": event.content})
        child.messages = [{"role": "system", "content": ""}] + turns
        child.step_id = max((event.step_id for event in self.events[:cut + 1]), default=0)
        child._save()
        return child

    # -- 编辑单条消息 -------------------------------------------------------

    def edit_user(self, index: int, text: str) -> str:
        """改掉第 N 条用户消息，把它之后的内容全丢，然后用新话重跑。

        和 retry 是一套东西：点「重试」保留原话，点「改了再发」换新话。
        两者都必须把后面清干净，否则对话里会出现两条互相矛盾的指令。
        """
        if self.running:
            raise RuntimeError("上一轮还没结束")
        users = [i for i, msg in enumerate(self.messages) if msg.get("role") == "user"]
        if index < 0 or index >= len(users):
            return ""
        at = users[index]
        del self.messages[at:]
        # 事件流：从第 index+1 条 USER 事件开始截断
        seen = 0
        cut = len(self.events)
        for i, event in enumerate(self.events):
            if event.type == EventType.USER:
                if seen == index:
                    cut = i
                    break
                seen += 1
        with self._lock:
            self.events = self.events[:cut]
        self.compactions = 0
        self.todos = []
        self._rewind_tail()
        self._push({"kind": "reset", "cid": self.cid, "step_id": self.step_id + 1})
        return text

