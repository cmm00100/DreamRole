"""HTTP 服务层。

只用两种传输：
- **普通 JSON 接口**给「动作」（发消息、点确认、改设置）；
- **SSE** 给「过程」（事件流、流式文字）。

不用 WebSocket，是因为这里的通信是单向的：服务端一直在说，客户端只在关键节点回一句。
SSE 断线会自动重连，而重连之后 `subscribe()` 会把已有事件补发一遍 —— 这正是我们要的
「刷新页面还能接上刚才的进度」。

线程模型：路由是同步函数，跑在 FastAPI 的线程池里；会话自己另起一个后台线程跑一轮。
两边只通过 `Session` 的锁、事件订阅和待确认队列交互，没有共享可变状态。
"""
from __future__ import annotations

import difflib
import json
import os
import platform
import queue
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (
    artifacts,
    assistants,
    audit,
    changes as change_book,
    channels,
    checkpoints,
    config,
    dlp,
    engines,
    experts,
    graph,
    grants,
    labels,
    mcp,
    memory,
    modes,
    permissions,
    promptopt,
    prompts as prompt_library,
    report,
    rules,
    runtime,
    scheduler,
    skills,
    software as software_book,
    store,
    subagents,
    thinking,
    tools,
    trash,
)
from . import session as sessions
from .events import Event, EventType
from .llm import ChatClient, LLMError
from .paths import DATA, RESOURCE

app = FastAPI(title="DreamRole", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _gate(request: Request, call_next):
    """认门 + 验来源。

    优先级：安全 > 记账。外来请求先被挡掉，就不会在统计里留下「用过一次」的假记录。

    判断的基准是**监听在哪**，不是「谁连上来了」：
    只绑 127.0.0.1 的时候，能连上来的按定义都是本机的进程，再验 token 只是
    给自己添麻烦 —— 每次开窗口都要输 token 的产品，用户第一件事是去关掉校验。
    绑到 0.0.0.0 之后才谈得上「外面有人敲门」，那时候才要开关和令牌。
    """
    name = (request.headers.get("X-DreamRole-Channel") or "desktop").lower()
    if name not in channels.CHANNELS:
        name = "api"

    client_host = request.client.host if request.client else ""
    is_api = request.url.path.startswith("/api/")

    if is_api and channels.listening_wide():
        # 外部接口关着的时候，非本机来源一律不认 —— 开关得真的管事，
        # 不能只是界面上画了个关掉的按钮。
        if not channels.is_loopback(client_host) and not channels.api_open():
            channels.hit(name, ok=False)
            return JSONResponse(content={"ok": False, "error": "外部接口没打开"},
                                status_code=403)
        ok, why = channels.allowed_origin(client_host, request.headers.get("X-DreamRole-Token"))
        if not ok:
            channels.hit(name, ok=False)
            return JSONResponse(content={"ok": False, "error": why}, status_code=403)

    response = await call_next(request)
    # 只记接口流量：静态资源每个页面几十个请求，记进去就把「谁在用」淹没了
    if is_api:
        channels.hit(name, ok=response.status_code < 500)
    return response

WEB_DIR = RESOURCE / "web"


@app.middleware("http")
async def _fresh_assets(request: Request, call_next):
    """静态资源不许浏览器自作聪明地缓存。

    不是洁癖，是踩到了。`StaticFiles` 只给 `ETag` / `Last-Modified`、不给
    `Cache-Control`，于是 Chromium 按「(Date - Last-Modified) 的 10%」猜一个
    保鲜期，期间**连问都不问**，直接用本地缓存。

    症状：改完 index.html / app.js，重启程序，窗口里还是旧界面，而且没有任何提示。
    「升级了但新功能没出现」很可能就是这个 —— 用户会以为是代码没生效。
    自检那边也一样：新加的按钮在页面里根本不存在，断言报「找不到元素」。

    这个应用是本机单用户、资源一共几百 KB，缓存省下的那点时间完全不值这个坑。
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".htm", ".js", ".css")):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(content=data, status_code=status)


def _fail(message: str, status: int = 400, **extra: Any) -> JSONResponse:
    return JSONResponse(content={"ok": False, "error": message, **extra}, status_code=status)


def _require_session(cid: str) -> sessions.Session:
    """拿会话。服务重启过也能救回来 —— 内存里没了就读磁盘上的轨迹。"""
    sess = sessions.get(cid)
    if sess is not None:
        return sess

    meta = store.load_meta(cid)
    if not meta:
        raise HTTPException(status_code=404, detail="没有这个会话")

    # 老会话如果记的是程序自造的 data/projects，当作「没选过」——
    # 它不是用户选的，不该成为默认值。
    saved = str(meta.get("project") or "")
    if config.is_placeholder_project(saved):
        saved = ""
    sess = sessions.create(
        project=saved,
        mode=meta.get("mode"),
        permission=meta.get("permission"),
        expert=meta.get("expert"),
        assistant=meta.get("assistant") or "",
        members=meta.get("members") or [],
        group=meta.get("group") or "",
        # 授权目录跟着会话存：重启之后不该变回「只能碰工作区」。
        # inherit=False：存档里那几张就是当时的答案，不要在重开时又抄一遍全局的，
        # 不然「在这条会话里撤掉某个全局目录」永远撤不掉。
        grants=meta.get("grants") or [],
        inherit=False,
        # 引擎也跟会话走。老会话没有这个字段 → normalize 会退回 role1，正好是对的。
        engine=meta.get("engine"),
        cid=cid,
    )
    # role2 的多轮靠 DSH 那边的 sessionId 续，重启后必须捞回来，
    # 否则用户说「继续」时，对面是个全新的会话，一问三不知。
    sess.dsh_session = str(meta.get("dsh_session") or "")
    # 思考等级是跟着会话走的：重启之后还得是用户上次调的那一档
    if meta.get("think"):
        sess.think = thinking.normalize(meta.get("think"))
    for row in store.load_trace(cid):
        try:
            sess.events.append(Event.from_dict(row))
        except (KeyError, ValueError):
            continue
    if sess.events:
        sess.last_parent = sess.events[-1].id
    return sess


# ---------------------------------------------------------------------------
# 启动数据
# ---------------------------------------------------------------------------

# 界面「活过来了」的回执。
#
# 桌面窗口只是一个壳：壳画出来了不等于里面的页面跑起来了。WebView2 在某些机器上
# 会连 JS 都不执行，用户看到的是一整块和背景色一样的空窗口 —— 所有「窗口开出来了」
# 的检查都会点头，用户却在对着空气发呆。
#
# 所以让页面自己回话：加载时带着启动令牌打一次这个接口。壳等不到回执就知道
# 该换个方式打开（见 desktop.py 的 open_fallback）。
_BOOT = {"token": "", "ui": "", "err": ""}


@app.get("/api/boot-ok")
def boot_ok(t: str = "", ui: int = 0, err: str = "") -> Any:
    """页面回执。`ui=1` 表示 app.js 已经跑完 bootstrap、界面画出来了。

    `err` 是界面启动失败时的现场：窗口程序没有控制台，用户也没法打开开发者工具，
    所以让页面把异常回传给这里，由外壳写进 logs/dreamrole.log。
    """
    if t:
        _BOOT["token"] = t
        if ui:
            _BOOT["ui"] = t
        if err:
            _BOOT["err"] = err
    return {"ok": True}


def boot_seen(token: str, ui_only: bool = False) -> bool:
    if not token:
        return False
    return _BOOT["ui"] == token if ui_only else _BOOT["token"] == token


def boot_error() -> str:
    return _BOOT.get("err") or ""



@app.get("/api/bootstrap")
def bootstrap() -> Any:
    return _json(
        {
            "version": config.APP_VERSION,
            "platform": platform.system(),
            "settings": config.public_settings(),
            # 两套底层：role1 自带内核 / role2 本机 DSH。界面拿它画「用哪个跑」
            "engines": engines.list_roles(),
            "modes": [
                {
                    "id": mid,
                    "label": meta["label"],
                    "hint": meta["hint"],
                    "permission": meta["permission"],
                }
                for mid, meta in modes.MODES.items()
            ],
            "permissions": {
                "levels": [
                    {"id": level, "label": permissions.PERMISSION_LABELS[level]}
                    for level in permissions.MATRIX
                ],
                "current": permissions.describe(),
            },
            "thinking": thinking.describe(),
            "tools": tools.describe(),
            "skills": skills.catalog(),
            "experts": experts.list_all(),
            "mcp": mcp.servers(),
            "schedules": scheduler.describe_all(),
            "templates": promptopt.catalog(),
            "traces": store.list_traces(),
            "assistants": assistants.list_all(),
            "groups": assistants.list_groups(),
            "runtime": runtime.summary_cached(),
            "channels": channels.state(),
            "subagents": subagents.catalog(),
            "labels": labels.TOOL_LABELS,
            "data_dir": str(DATA),
            # 全局通行证也带一份：还没建会话时，工作区弹窗里那排「还能去的地方」
            # 得能显示它们。不然用户看到的是空列表，发完第一句才发现多了三张。
            "global_grants": grants.describe(grants.global_defaults()),
        }
    )


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

@app.get("/api/engines")
def list_engines() -> Any:
    """两套底层的当前状态。

    单独开一个接口而不是只塞进 bootstrap：DSH 装没装、key 有没有，会随着
    用户的操作变（刚装完、刚填完 key），而 bootstrap 一辈子只拉一次 ——
    那样用户装完 DSH 回来，界面上还写着「没找到」。
    """
    return _json(engines.list_roles())


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

@app.post("/api/session")
def create_session(payload: dict = Body(default={})) -> Any:
    # 助理会话 / 群聊会话的工作区由系统分配（各助理自己那间屋子），
    # 普通会话仍然必须由用户显式选 —— 这两件事不能混成一条规则：
    # 「助理有自己固定的地方」是助理的定义，「每次会话都要选目录」是安全底线。
    assistant = str(payload.get("assistant") or "").strip()
    group_id = str(payload.get("group") or "").strip()
    members: list[str] = []

    if group_id:
        record = assistants.get_group(group_id)
        if not record:
            return _fail("没有这个群", status=404)
        members = list(record.get("members") or [])
        if len(members) < 2:
            return _fail("这个群里不足两个助理，先补上成员")
        project = str(assistants.group_workspace(group_id))
        assistant = ""
    elif assistant:
        record = assistants.get(assistant)
        if not record:
            return _fail("没有这个助理", status=404)
        assistants.ensure_dirs(assistant)
        project = str(assistants.dirs(assistant)["workspace"])
    else:
        project = str(payload.get("project") or "").strip()
        if not project:
            return _fail("先选一个工作区，再开始对话", status=412)
        path = Path(project).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(f"这个目录用不了：{exc}")
        if not path.is_dir():
            return _fail("工作区必须是一个目录")
        project = str(path.resolve())

    sess = sessions.create(
        project=project,
        mode=payload.get("mode") or (assistants.get(assistant) or {}).get("mode"),
        permission=payload.get("permission") or (assistants.get(assistant) or {}).get("permission"),
        expert=payload.get("expert"),
        assistant=assistant,
        members=members,
        group=group_id,
        # 底层在**建会话这一刻**定下来，之后不给换。前端可以传，不传就用设置里的默认。
        engine=payload.get("engine"),
        # 前端可能在会话建起来之前就加好了授权（工作区弹窗里那一步），
        # 那些先挂在前端，这里一次性带上来。
        grants=payload.get("grants") or [],
    )
    if payload.get("think"):
        sess.think = thinking.normalize(payload.get("think"))
    elif assistant:
        saved_think = (assistants.get(assistant) or {}).get("think")
        if saved_think:
            sess.think = thinking.normalize(saved_think)
    if assistant:
        # 助理的轨迹落在自己的子目录里（store.scope_of 按这个分目录）——
        # 不用在这里写 meta，会话第一次落盘时 _save 会把 assistant 带上去
        assistants.touch(assistant, 0)
    if not group_id:
        config.remember_project(project)
    return _json(sess.snapshot())


@app.post("/api/session/{cid}/workspace")
def set_workspace(cid: str, payload: dict = Body(default={})) -> Any:
    sess = _require_session(cid)
    project = str(payload.get("project") or "").strip()
    if not project:
        return _fail("工作区不能为空")
    if sess.running:
        return _fail("这一轮还在跑，等它跑完再换工作区", status=409)
    try:
        resolved = sess.set_workspace(project)
    except (OSError, ValueError) as exc:
        return _fail(f"这个目录用不了：{exc}")
    config.remember_project(resolved)
    return _json({"ok": True, "project": resolved, "cid": cid})


@app.post("/api/session/{cid}/grants")
def update_grants(cid: str, payload: dict = Body(default={})) -> Any:
    """加 / 删 / 改档 / 清空「额外授权目录」。

    为什么不做成全局设置：授权是「这条会话能干这件活」的一部分。做成全局的话，
    用户给 A 会话开了 D 盘，B 会话也悄悄带着，而界面上看不出来 —— 和以前那个
    静默默认的 data/projects 是同一个毛病。见 app/grants.py 顶部。

    允许在跑的时候改：这一条比换工作区松，因为授权只是「多开一道门」，
    不会让正在跑的那一轮读到半截不一样的世界。
    """
    sess = _require_session(cid)
    op = str(payload.get("op") or "").strip()
    path = str(payload.get("path") or "").strip()
    mode = str(payload.get("mode") or "").strip()
    items, note = sess.update_grants(op, path, mode)
    return _json({"ok": True, "grants": items, "note": note, "cid": cid})


@app.get("/api/grants/global")
def get_global_grants() -> Any:
    """全局通行证：新会话起手就带着的那几张。"""
    return _json({"ok": True, "grants": grants.describe(grants.global_defaults())})


@app.post("/api/grants/global")
def update_global_grants(payload: dict = Body(default={})) -> Any:
    """改全局通行证。**只影响之后新建的会话** —— 已经在跑的对话按它自己那份来，
    不然「这条会话能去哪」会在跑到一半时自己变，出了事没人说得清是哪个时点的规则。
    """
    op = str(payload.get("op") or "").strip()
    path = str(payload.get("path") or "").strip()
    mode = str(payload.get("mode") or "").strip()
    items, note = grants.update_global(op, path, mode)
    return _json({"ok": True, "grants": grants.describe(items), "note": note})


@app.get("/api/session/{cid}")
def get_session(cid: str) -> Any:
    sess = _require_session(cid)
    data = sess.snapshot()
    data["graph"] = graph.build(data["events"])
    data["stats"] = graph.stats(data["events"])
    return _json(data)


@app.get("/api/session/{cid}/stream")
def stream_session(cid: str, request: Request) -> Any:
    sess = _require_session(cid)
    channel: queue.Queue = queue.Queue(maxsize=4000)

    def listener(message: dict) -> None:
        try:
            channel.put_nowait(message)
        except queue.Full:
            pass  # 前端卡死了就丢，绝不能把会话线程堵在这里

    # 订阅时会把已有事件补发进队列，所以刷新页面不会丢历史
    unsubscribe = sess.subscribe(listener)

    def generate():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    message = channel.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"      # 心跳：避免代理把空闲连接掐掉
                    continue
                yield "data: " + json.dumps(message, ensure_ascii=False) + "\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/session/{cid}/send")
def send_message(cid: str, payload: dict = Body(default={})) -> Any:
    sess = _require_session(cid)
    text = str(payload.get("text") or "").strip()
    if not text:
        return _fail("说点什么")
    if sess.running:
        return _fail("上一轮还没结束，先停下或者等它跑完", status=409)
    if not sess.has_workspace:
        return _fail("这个会话还没有工作区。先选一个目录，它才有地方干活", status=412)
    if not config.can_chat():
        return _fail("还没有配置模型的 API key，先去「设置」填一下", status=412)
    # 输入栏里点名要用的技能。只认**存在且开着**的名字 —— 技能可能刚被删掉，
    # 也可能刚被关掉，这两种时候都不该让整句话发不出去，就当没选过。
    # 但「关掉的」不能静默丢：用户明明点了它，得有一句回音，否则会以为操作丢了。
    picked: list[str] = []
    was_off: list[str] = []
    for raw in payload.get("skills") or []:
        if not isinstance(raw, (str, int)):
            continue
        item = skills.find(str(raw))
        if not item:
            continue                      # 已经删掉了，当没选过
        if not item.get("enabled", True):
            was_off.append(item["name"])
            continue
        picked.append(str(raw))
    if was_off:
        sess.emit(
            EventType.NOTICE,
            "这几个技能现在是关的，这一轮没有带上：" + "、".join(f"「{n}」" for n in was_off),
            payload={"skills_off": was_off},
        )
    try:
        sess.start(text, picked)
    except RuntimeError as exc:
        return _fail(str(exc), status=409)
    return _json({"ok": True, "cid": sess.cid, "skills": picked})


@app.post("/api/session/{cid}/answer")
def answer_session(cid: str, payload: dict = Body(default={})) -> Any:
    sess = _require_session(cid)
    req_id = str(payload.get("id") or "")
    decision = str(payload.get("decision") or "")
    if not req_id or not decision:
        return _fail("缺 id 或 decision")
    return _json({"ok": sess.answer(req_id, decision)})


@app.post("/api/session/{cid}/cancel")
def cancel_session(cid: str) -> Any:
    """请它停下。**这是请，不是已经停下。**

    真正收尾在 worker 线程的 `finally` 里：命令被杀、`running` 归 False、
    门（`_starting`）放开。所以这里回的是两个状态，不是一句「好了」——
    前端拿它决定说「正在停」还是「本来就没在跑」，而不是一律骗用户「已停下」。
    """
    sess = _require_session(cid)
    was_alive = sess.cancel()
    return _json({
        "ok": True,
        "stopping": bool(was_alive),
        "running": bool(sess.running or sess.is_starting),
    })


@app.post("/api/session/{cid}/mode")
def set_mode(cid: str, payload: dict = Body(default={})) -> Any:
    sess = _require_session(cid)
    if sess.running:
        return _fail("跑着的时候不能换模式", status=409)
    sess.mode = modes.normalize(str(payload.get("mode") or ""))
    config.update({"mode": sess.mode})
    return _json({"ok": True, "mode": sess.mode, "effective": sess.effective_permission()})


@app.post("/api/session/{cid}/permission")
def set_permission(cid: str, payload: dict = Body(default={})) -> Any:
    sess = _require_session(cid)
    level = str(payload.get("permission") or "")
    if level not in permissions.MATRIX:
        return _fail("没有这个权限档位")
    sess.permission = level
    config.update({"permission": level})
    return _json({"ok": True, "permission": level, "effective": sess.effective_permission()})


@app.post("/api/session/{cid}/think")
def set_think(cid: str, payload: dict = Body(default={})) -> Any:
    """调思考等级。跑着的时候不让改 —— 半路换档会让这一轮的思维链前后矛盾。"""
    sess = _require_session(cid)
    if sess.running:
        return _fail("这一轮还在跑，等它跑完再调", status=409)
    level = thinking.normalize(str(payload.get("level") or ""))
    sess.think = level
    config.update({"think": level})
    provider = config.provider_conn().get("provider") or ""
    model = config.provider_conn().get("model") or ""
    return _json({
        "ok": True,
        "level": level,
        "label": thinking.LEVELS[level]["label"],
        "supported": thinking.supported(provider, model),
        "note": "" if thinking.supported(provider, model)
        else f"{provider} 这一家没提供可调的思考深度，设了也不会生效。",
    })


@app.get("/api/session/{cid}/mermaid", response_class=PlainTextResponse)
def session_mermaid(cid: str) -> Any:
    sess = _require_session(cid)
    rows = [e.to_dict() for e in sess.events]
    title = store.load_meta(cid).get("title") or ""
    return PlainTextResponse(graph.to_mermaid(rows, title))


# ---------------------------------------------------------------------------
# 重来 / 改一句再发
# ---------------------------------------------------------------------------

def _guard_turn(sess) -> Any:
    """起新一轮之前的公共检查。返回 None 表示可以走。"""
    if sess.running:
        return _fail("上一轮还没结束，先停下或者等它跑完", status=409)
    if not config.can_chat():
        return _fail("还没有配置模型的 API key，先去「设置」填一下", status=412)
    return None


@app.post("/api/session/{cid}/retry")
def retry_session(cid: str) -> Any:
    sess = _require_session(cid)
    blocked = _guard_turn(sess)
    if blocked is not None:
        return blocked
    text = sess.retry()
    if not text:
        return _fail("没有可重试的内容", status=400)
    sess._save()
    sess.start(text)
    return _json({"ok": True, "text": text})


@app.post("/api/session/{cid}/edit")
def edit_turn(cid: str, payload: dict = Body(default={})) -> Any:
    """把第 index 条用户消息改成新内容，然后从那里重跑。"""
    sess = _require_session(cid)
    blocked = _guard_turn(sess)
    if blocked is not None:
        return blocked
    text = str(payload.get("text") or "").strip()
    if not text:
        return _fail("说点什么")
    index = int(payload.get("index") or 0)
    if not sess.edit_user(index, text):
        return _fail("找不到这条消息", status=404)
    sess._save()
    sess.start(text)
    return _json({"ok": True, "text": text})


@app.post("/api/session/{cid}/branch")
def branch_session(cid: str, payload: dict = Body(default={})) -> Any:
    """从第 index 条用户消息分叉出一个新会话，原来那条留着不动。"""
    sess = _require_session(cid)
    if sess.running:
        return _fail("这一轮还在跑，等它跑完再分叉", status=409)
    index = int(payload.get("index") or 0)
    child = sess.branch(index)
    if child is None:
        return _fail("找不到这条消息", status=404)
    return _json({"ok": True, "cid": child.cid})


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

@app.get("/api/session/{cid}/export")
def export_session(cid: str, fmt: str = "md", download: int = 1) -> Any:
    meta = store.load_meta(cid)
    sess = sessions.get(cid)
    events = [e.to_dict() for e in sess.events] if sess else store.load_trace(cid)
    if not events and not meta:
        return _fail("找不到这个会话", status=404)

    fmt = (fmt or "md").lower()
    if fmt == "mermaid":
        body, media, ext = graph.to_mermaid(events, meta.get("title") or ""), "text/plain", "mmd"
    elif fmt == "html":
        body, media, ext = report.to_html(events, meta), "text/html", "html"
    elif fmt == "json":
        body, media, ext = json.dumps(
            {"meta": meta, "events": events}, ensure_ascii=False, indent=2
        ), "application/json", "json"
    else:
        body, media, ext = report.to_markdown(events, meta), "text/markdown", "md"

    headers = {}
    if download:
        name = store.safe_name(meta.get("title") or cid)[:40] or cid
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}.{ext}"
    return Response(content=body.encode("utf-8"), media_type=f"{media}; charset=utf-8",
                    headers=headers)


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

@app.get("/api/search")
def search_traces(q: str = "", project: str = "") -> Any:
    """按标题和正文搜会话。标题命中排前面，正文命中给一句上下文。"""
    needle = (q or "").strip().lower()
    if len(needle) < 1:
        return _json([])

    hits: list[dict] = []
    for meta in store.list_traces(project or None, include_archived=True):
        cid = meta.get("cid") or ""
        if not cid:
            continue
        title = str(meta.get("title") or "")
        where = "title" if needle in title.lower() else ""
        snippet = ""
        if not where:
            for event in store.load_trace(cid):
                text = str(event.get("content") or "")
                if needle in text.lower():
                    at = text.lower().index(needle)
                    snippet = " ".join(text[max(0, at - 40): at + 80].split())
                    where = "body"
                    break
        if not where:
            continue
        hits.append({**meta, "where": where, "snippet": snippet})

    hits.sort(key=lambda m: (m["where"] != "title", -float(m.get("created") or 0)))
    return _json(hits[:80])


# ---------------------------------------------------------------------------
# 用量统计
# ---------------------------------------------------------------------------

@app.get("/api/usage")
def usage_stats() -> Any:
    """按天汇总 token。数据来自每个会话的 meta，不另存一份账本：

    另存账本就要保证两边一致，而历史上删掉的会话到底算不算，永远扯不清。
    """
    by_day: dict[str, dict] = {}
    by_mode: dict[str, int] = {}
    total = {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "seconds": 0.0}
    sessions_count = 0

    for meta in store.list_traces(include_archived=True):
        usage = meta.get("usage") or {}
        if not int(usage.get("total") or 0):
            continue
        sessions_count += 1
        day = time.strftime("%Y-%m-%d", time.localtime(float(meta.get("created") or 0)))
        slot = by_day.setdefault(day, {"day": day, "total": 0, "prompt": 0, "completion": 0, "calls": 0})
        for key in ("total", "prompt", "completion", "calls"):
            slot[key] += int(usage.get(key) or 0)
        mode = str(meta.get("mode") or "craft")
        by_mode[mode] = by_mode.get(mode, 0) + int(usage.get("total") or 0)
        for key in ("prompt", "completion", "total", "calls"):
            total[key] += int(usage.get(key) or 0)
        total["seconds"] = round(total["seconds"] + float(usage.get("seconds") or 0), 2)

    return _json({
        "total": total,
        "sessions": sessions_count,
        "days": sorted(by_day.values(), key=lambda d: d["day"]),
        "modes": by_mode,
    })



# ---------------------------------------------------------------------------
# 会话列表
# ---------------------------------------------------------------------------

@app.get("/api/traces")
def list_traces(project: str = "", archived: int = 0) -> Any:
    """会话列表。

    顺带把 `running` 打进去：同时开几条会话之后，用户需要知道「哪条还在跑」——
    否则他在 B 里聊天，A 跑完了却没人告诉他，只能一条条点开看。
    """
    items = store.list_traces(project or None, include_archived=bool(archived))
    for item in items:
        # 内存里活着的那条会话，状态和引擎都以它为准：刚建的会话可能还没落盘，
        # 而「这条是 role2 在跑」这种事，meta 里那份是上一次写盘时的。
        live = sessions.get(str(item.get("cid") or ""))
        item["running"] = bool(live and live.running)
        item["engine"] = live.engine if live else (item.get("engine") or "role1")
    return _json(items)


@app.patch("/api/traces/{cid}")
def patch_trace(cid: str, payload: dict = Body(default={})) -> Any:
    """改名 / 置顶 / 归档。

    会话不存在时直接 404 —— 不然会凭空造出一个只有 meta、没有轨迹的幽灵会话，
    它还会出现在列表里，点进去是一片空白。
    """
    if not store.session_exists(cid):
        return _fail("找不到这个会话", status=404)
    allowed = {k: v for k, v in payload.items() if k in ("title", "pinned", "archived")}
    if not allowed:
        return _fail("没有可改的字段")
    if "title" in allowed:
        title = " ".join(str(allowed["title"] or "").split())[:80]
        if not title:
            return _fail("标题不能改成空的")
        allowed["title"] = title
    for flag in ("pinned", "archived"):
        if flag in allowed:
            allowed[flag] = bool(allowed[flag])
    return _json(store.patch_meta(cid, **allowed))


@app.get("/api/traces/{cid}/impact")
def trace_impact(cid: str) -> Any:
    """删之前先问清楚：删掉它，哪些改动就再也退不回来了。

    这个接口单独存在，是因为「删除」是不可逆动作，而用户点下去的时候
    手上没有任何信息能判断代价。把代价算出来摆在他面前，是删除框唯一的价值。
    """
    if not store.session_exists(cid):
        return _fail("找不到这个会话", status=404)
    meta = store.load_meta(cid)
    data = change_book.impact(cid)
    data.update({
        "title": meta.get("title") or "未命名",
        "archived": bool(meta.get("archived")),
        "pinned": bool(meta.get("pinned")),
        "events": int(meta.get("events") or 0),
    })
    return _json(data)


@app.post("/api/traces/{cid}/delete")
def delete_trace(cid: str) -> Any:
    """删除一条会话，连带删掉它名下的改动与轮次记录。

    级联的顺序是有讲究的：先算影响面（前端会先调 /impact 给用户看），
    再动文件，最后清账本。反过来（先清账本）如果中途失败，
    会出现「会话还在、改动记录没了」—— 那是真正的数据丢失，而且没有任何提示。

    改动记录现在是**真删**，不再搬去看不见的墓园：用户点删除时想的就是「都别留了」。
    """
    sess = sessions.get(cid)
    if sess and sess.running:
        return _fail("这一轮还在跑，先停下再删", status=409)
    paths = store.delete_trace(cid)
    if not paths:
        return _fail("找不到这个会话", status=404)
    moved = [{"path": str(path), "trashed": trash.to_trash(path)} for path in paths]
    # 文件没搬走，会话就还在。这时候绝不能清账本 —— 会出现「会话在、改动没了」，
    # 那是最糟的一种不一致：用户看得见会话，却再也退不回它改过的东西。
    if store.session_exists(cid):
        return _fail("轨迹文件没能移入回收站（可能正被占用），会话还在，改动记录也没动",
                     status=409, files=moved)
    # 会话已经真的不在了，它名下的改动和轮次记录要一起删 ——
    # 只删轨迹不删账本，改动栏里就会留一排「点得动、点了报错」的按钮。
    swept = store.forget_cid(cid)
    sessions.drop(cid)
    return _json({"ok": True, "files": moved, "swept": swept})


# ---------------------------------------------------------------------------
# 提示词优化
# ---------------------------------------------------------------------------

@app.get("/api/promptopt/templates")
def promptopt_templates() -> Any:
    return _json(promptopt.catalog())


@app.post("/api/promptopt/optimize")
def promptopt_optimize(payload: dict = Body(default={})) -> Any:
    text = str(payload.get("text") or "")
    if not config.can_chat():
        return _fail("优化提示词也要用模型，先去「设置」填一下 API key", status=412)
    goals = payload.get("goals")
    result = promptopt.optimize(
        text,
        str(payload.get("template") or ""),
        str(payload.get("target") or ""),
        [str(one) for one in goals] if isinstance(goals, list) else None,
    )
    if not result.get("ok") and result.get("error"):
        return _json(result, status=400)
    result["suggested"] = promptopt.suggest_template(text)
    return _json(result)


@app.post("/api/promptopt/compare")
def promptopt_compare(payload: dict = Body(default={})) -> Any:
    return _json(
        promptopt.compare(
            str(payload.get("original") or ""), str(payload.get("optimized") or "")
        )
    )


# ---------------------------------------------------------------------------
# 提示词库（收藏）
# ---------------------------------------------------------------------------

@app.get("/api/prompts")
def list_prompts() -> Any:
    return _json({"ok": True, "items": prompt_library.list_all()})


@app.post("/api/prompts")
def add_prompt(payload: dict = Body(default={})) -> Any:
    try:
        row = prompt_library.add(
            str(payload.get("title") or ""),
            str(payload.get("text") or ""),
            payload.get("tags"),
            str(payload.get("source") or ""),
        )
    except ValueError as exc:
        return _fail(str(exc), status=400)
    audit.append("user", "prompt.save", row["id"], "allow", row["title"], "")
    return _json({"ok": True, "item": row})


@app.patch("/api/prompts/{pid}")
def patch_prompt(pid: str, payload: dict = Body(default={})) -> Any:
    row = prompt_library.update(pid, payload)
    if not row:
        return _fail("找不到这条", status=404)
    return _json({"ok": True, "item": row})


@app.delete("/api/prompts/{pid}")
def delete_prompt(pid: str) -> Any:
    return _json({"ok": prompt_library.remove(pid)})


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> Any:
    data = config.public_settings()
    raw = config.get()
    data.update(
        {
            "optimize_model": raw.get("optimize_model") or "",
            "enabled_tools": raw.get("enabled_tools") or tools.default_enabled(),
            "approvals": raw.get("approvals") or {},
            "skills_enabled": raw.get("skills_enabled") or [],
        }
    )
    return _json(data)


@app.post("/api/settings")
def save_settings(payload: dict = Body(default={})) -> Any:
    allowed = {
        "provider", "model", "optimize_model", "mode", "permission", "project",
        "enabled_tools", "approvals", "skills_enabled", "compact_ratio",
        "max_steps", "auto_optimize", "providers", "mcp_servers", "personal_note",
        "context_window", "auto_memory", "engine", "dsh_path",
    }
    patch: dict[str, Any] = {}

    for key, value in payload.items():
        if key not in allowed:
            continue
        if key == "providers" and isinstance(value, dict):
            # 前端拿到的 key 是掩码。空字符串代表「这一栏没改」，不能覆盖掉真 key
            current = config.get().get("providers") or {}
            merged = {pid: dict(conf) for pid, conf in current.items() if isinstance(conf, dict)}
            for pid, conf in value.items():
                if not isinstance(conf, dict):
                    continue
                entry = merged.setdefault(pid, {})
                for field, field_value in conf.items():
                    if field == "api_key" and not str(field_value or "").strip():
                        continue
                    entry[field] = field_value
            patch[key] = merged
            continue
        patch[key] = value

    if not patch:
        return _fail("没有可保存的字段")
    config.update(patch)
    if "dsh_path" in patch:
        # 改完路径要立刻按新路径重探，不然要等 15 秒缓存过期才反应过来
        from . import dsh_engine

        dsh_engine.forget()
    return _json({"ok": True, "settings": config.public_settings()})


@app.post("/api/settings/test")
def test_model() -> Any:
    """花最少的 token 验一次连通性。"""
    try:
        client = ChatClient()
        reply = client.complete(
            [{"role": "user", "content": "只回两个字：通了"}], temperature=0.0, max_tokens=16
        )
        return _json({"ok": True, "model": client.model, "reply": reply[:60]})
    except LLMError as exc:
        return _json({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)


# ---------------------------------------------------------------------------
# 工具 / 技能 / MCP / 定时任务
# ---------------------------------------------------------------------------

@app.get("/api/tools")
def list_tools() -> Any:
    return _json(tools.describe())


@app.post("/api/tools/{name}/toggle")
def toggle_tool(name: str, payload: dict = Body(default={})) -> Any:
    if name not in tools.ALL_NAMES:
        return _fail("没有这个工具", status=404)
    enabled = set(config.get().get("enabled_tools") or tools.default_enabled())
    if payload.get("enabled"):
        enabled.add(name)
    else:
        enabled.discard(name)
    config.update({"enabled_tools": sorted(enabled)})
    return _json({"ok": True, "enabled": sorted(enabled)})


@app.get("/api/skills")
def list_skills() -> Any:
    """技能清单（内置 + 自己的）。

    **每次调用都重新扫盘**，不缓存。缓存过一次的代价我见过：agent 刚写完一个技能，
    面板里怎么刷都不出现，只能重启 —— 而「刚写完就看得见」正是这个接口唯一的用处。
    """
    return _json(skills.catalog())


@app.post("/api/skills")
def create_skill(payload: dict = Body(default={})) -> Any:
    """新建一个自己的技能。重名要显式 overwrite。"""
    try:
        item = skills.create(
            str(payload.get("name") or ""),
            str(payload.get("description") or ""),
            str(payload.get("body") or ""),
            overwrite=bool(payload.get("overwrite")),
        )
    except ValueError as exc:
        return _fail(str(exc), status=400)
    except OSError as exc:
        return _fail(f"写不进去：{exc}", status=500)
    return _json({"ok": True, "skill": item})


@app.post("/api/skills/{name}/delete")
def delete_skill(name: str) -> Any:
    """删掉一个自己的技能（进回收站）。内置技能拒绝 —— 说清为什么。"""
    try:
        result = skills.remove(name)
    except ValueError as exc:
        return _fail(str(exc), status=400)
    return _json(result)


@app.get("/api/skills/{name}")
def read_skill(name: str) -> Any:
    return _json({"ok": True, "name": name, "text": skills.load(name)})


@app.post("/api/skills/{name}/toggle")
def toggle_skill(name: str, payload: dict = Body(default={})) -> Any:
    """开 / 关一个技能。

    关掉不是删掉：正文还在磁盘上，只是不进系统提示、不进 @ 候选、
    `skill_load` 也读不出来。道理和不删内置技能一样 ——
    「这次别让它掺和」和「我不想要它了」是两件事。
    """
    # 先确认它真的存在：不存在的技能是 404，不是 400 ——
    # 「没有这个东西」和「这个东西的名字不合法」是两件事，状态码别混。
    item = skills.find(name)
    if not item:
        return _fail(f"没有叫「{name}」的技能", status=404)
    # 没显式给 enabled 就当成「翻一下」，省得前端为了一枚开关先读一次再写一次
    wants = payload.get("enabled")
    if wants is None:
        wants = not item.get("enabled", True)
    try:
        item = skills.set_enabled(name, bool(wants))
    except ValueError as exc:
        return _fail(str(exc), status=400)
    return _json({"ok": True, "skill": item, "enabled": bool(item.get("enabled"))})


@app.post("/api/skills/enable-all")
def enable_all_skills() -> Any:
    """一键全开。关掉一批之后想反悔，不该再一个个点回去。"""
    count = skills.enable_all()
    return _json({"ok": True, "opened": count, "items": skills.catalog()})


@app.get("/api/mcp")
def list_mcp() -> Any:
    return _json(mcp.servers())


@app.post("/api/mcp")
def add_mcp(payload: dict = Body(default={})) -> Any:
    name = str(payload.get("name") or "").strip()
    url = str(payload.get("url") or "").strip()
    command = str(payload.get("command") or "").strip()
    if not name:
        return _fail("先给它起个名字")
    if not url and not command:
        return _fail("要么填启动命令（本地），要么填 URL（远程）")
    mcp.add_server(name, command, payload.get("args") or [], payload.get("env") or {},
                   url=url, headers=payload.get("headers") or {})
    if payload.get("connect", True):
        return _json({"ok": True, **mcp.connect(name)})
    return _json({"ok": True})


@app.post("/api/mcp/{name}/connect")
def connect_mcp(name: str) -> Any:
    return _json(mcp.connect(name))


@app.post("/api/mcp/{name}/disconnect")
def disconnect_mcp(name: str) -> Any:
    mcp.close(name)
    return _json({"ok": True})


@app.post("/api/mcp/{name}/delete")
def delete_mcp(name: str) -> Any:
    mcp.remove_server(name)
    return _json({"ok": True})


# ---------------------------------------------------------------------------
# 记忆（三层）
# ---------------------------------------------------------------------------

@app.get("/api/memory")
def list_memory(project: str = "") -> Any:
    # 只认调用方给的目录（前端传的是**当前会话**的工作区）。
    # 不回落「上次用的目录」—— 那会让 A 项目的记忆显示在 B 项目的会话里。
    root = project or ""
    return _json({"ok": True, "project": root, "items": memory.list_all(root)})


@app.get("/api/memory/item")
def read_memory(item: str, project: str = "") -> Any:
    root = project or ""
    return _json(memory.read(item, root))


@app.post("/api/memory/item")
def write_memory(payload: dict = Body(default={})) -> Any:
    project = str(payload.get("project") or "")
    item = str(payload.get("item") or "")
    mode = str(payload.get("mode") or "append")
    text = str(payload.get("text") or "")
    if mode == "replace":
        return _json(memory.write(item, text, project))
    return _json(memory.append(item, text, project))


@app.delete("/api/memory/item")
def delete_memory(item: str, project: str = "") -> Any:
    root = project or ""
    return _json(memory.delete(item, root))


# ---------------------------------------------------------------------------
# 专家（人格包）
# ---------------------------------------------------------------------------

@app.get("/api/experts")
def list_experts() -> Any:
    return _json({"ok": True, "items": experts.list_all()})


@app.post("/api/experts")
def save_expert(payload: dict = Body(default={})) -> Any:
    return _json(experts.save(payload))


@app.delete("/api/experts/{expert_id}")
def delete_expert(expert_id: str) -> Any:
    return _json(experts.remove(expert_id))


@app.post("/api/session/{cid}/expert")
def set_session_expert(cid: str, payload: dict = Body(default={})) -> Any:
    sess = _require_session(cid)
    expert_id = str(payload.get("expert") or "")
    if expert_id and not experts.get(expert_id):
        return _fail("没有这个专家")
    sess.expert = expert_id
    # 专家自带的默认模式只在用户没手动切过模式时才套用，不硬抢用户的选择
    if expert_id and payload.get("apply_mode", True):
        mode = experts.default_mode(expert_id)
        if mode:
            sess.mode = modes.normalize(mode)
            sess.permission = modes.effective_permission(sess.mode, sess.permission)
    sess._save()
    return _json({"ok": True, "expert": sess.expert, "mode": sess.mode,
                  "permission": sess.permission})


# ---------------------------------------------------------------------------
# 检查点（按轮回滚）
# ---------------------------------------------------------------------------

@app.get("/api/checkpoints")
def list_checkpoints(limit: int = 60, cid: str = "") -> Any:
    return _json({"ok": True, "items": checkpoints.list_all(limit, cid)})


@app.post("/api/checkpoints/restore")
def restore_checkpoint(payload: dict = Body(default={})) -> Any:
    checkpoint_id = str(payload.get("id") or "")
    if not checkpoint_id:
        return _fail("要退回到哪一个检查点？")
    return _json(checkpoints.restore(checkpoint_id))


@app.get("/api/schedules")
def list_schedules() -> Any:
    return _json(scheduler.describe_all())


@app.get("/api/schedules/runs")
def list_schedule_runs(limit: int = 80) -> Any:
    """自动化的运行记录。新的在前，最多 MAX_RUNS 条。

    它不是日志文件的投影：只在「真的开了一轮 / 跳过 / 启动失败」时写一行，
    所以这里每一条都能对上一次真实动作。
    """
    return _json(scheduler.runs(limit))


@app.post("/api/schedules")
def add_schedule(payload: dict = Body(default={})) -> Any:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return _fail("到点要做什么？")
    # 定时任务也是会话，也得有工作区。没有目录就到时跑在某个谁也说不清的文件夹里。
    # 这里**不回落**到「上次用的目录」—— 定时任务会无人值守地跑，猜错代价更大。
    project = str(payload.get("project") or "").strip()
    if not project:
        return _fail("先给这个定时任务选一个工作区", status=412)
    job = scheduler.add(
        {
            "title": payload.get("title") or prompt[:20],
            "prompt": prompt,
            "project": project,
            "freq": payload.get("freq") or "daily",
            "time": payload.get("time") or "09:00",
            "weekday": payload.get("weekday") or 1,
            "minutes": payload.get("minutes") or 60,
            "enabled": bool(payload.get("enabled", True)),
        }
    )
    return _json({"ok": True, "job": job})


@app.patch("/api/schedules/{job_id}")
def patch_schedule(job_id: str, payload: dict = Body(default={})) -> Any:
    job = scheduler.update(job_id, payload)
    if not job:
        return _fail("找不到这个任务", status=404)
    return _json({"ok": True, "job": job})


@app.delete("/api/schedules/{job_id}")
def delete_schedule(job_id: str) -> Any:
    return _json({"ok": scheduler.remove(job_id)})


@app.post("/api/schedules/{job_id}/run")
def run_schedule(job_id: str) -> Any:
    """手动跑一次。和到点自动跑走同一条路，所以它会真的开一条会话。"""
    ok = scheduler.fire(job_id)
    if not ok:
        return _fail("找不到这个任务", status=404)
    audit.append("user", "schedule.run", job_id, "allow", "手动试跑", "")
    return _json({"ok": True})


# ---------------------------------------------------------------------------
# 本机软件
# ---------------------------------------------------------------------------

@app.get("/api/software")
def software_list(query: str = "", limit: int = 80, refresh: int = 0) -> Any:
    """能给 agent 用的软件清单（开始菜单 + 注册表 App Paths + 内置别名）。"""
    limit = max(1, min(int(limit or 80), 300))
    if refresh:
        software_book.catalogue(refresh=True)
    all_items = software_book.catalogue()
    items = software_book.search(query, limit=limit) if query else all_items[:limit]
    groups: dict[str, int] = {}
    for item in items:
        groups[item["source"]] = groups.get(item["source"], 0) + 1
    return _json({
        "ok": True,
        "total": len(all_items),
        "shown": len(items),
        "sources": groups,
        "items": items,
    })


@app.get("/api/software/installed")
def software_installed(refresh: int = 0) -> Any:
    """卸载注册表里的软件（名字/版本/发布者）——「这台机器装了什么」。"""
    rows = software_book.installed(refresh=bool(refresh))
    return _json({"ok": True, "total": len(rows), "items": rows})


@app.post("/api/software/open")
def software_open(payload: dict = Body(default={})) -> Any:
    """界面上点一下就启动。走的是和 agent 完全同一条路径，不另写一份。"""
    name = str(payload.get("name") or "").strip()
    if not name:
        return _fail("缺少 name", status=400)
    from .tools import pc as pc_tools
    from .tools.base import ToolContext

    text = pc_tools.open_software(
        ToolContext(root=DATA), name=name, args=str(payload.get("args") or "")
    )
    ok = not text.startswith("打不开")
    return _json({"ok": ok, "message": text}, status=200 if ok else 409)


# ---------------------------------------------------------------------------
# 改动与回滚
# ---------------------------------------------------------------------------

@app.get("/api/changes")
def list_changes(cid: str = "", include_dead: int = 1) -> Any:
    """改动记录（平铺）。给 cid 就只看这条对话的，不给就全都要。

    界面走的是下面的三级接口；这个平铺接口留着给「按路径找」和脚本用。
    返回的每一项都带 `session_alive` / `blocked` —— 界面不需要自己再查一遍
    「这条对话还在不在」，那样两个地方各判一次，迟早会不一致。
    """
    rows = change_book.describe(cid, include_dead=bool(include_dead))
    return _json(rows)


@app.get("/api/changes/threads")
def change_threads(include_dead: int = 1) -> Any:
    """第一级：哪些对话留下过改动。"""
    rows = change_book.threads(include_dead=bool(include_dead))
    return _json({"ok": True, "total": len(rows), "items": rows})


@app.get("/api/changes/thread")
def change_thread(cid: str = "") -> Any:
    """第二级：这条对话动过哪些文件产物。"""
    if not cid:
        return _fail("缺少 cid", status=400)
    data = change_book.files(cid)
    # 整轮回退也挂在这一级：它属于「这条对话」，不属于某个文件
    data["turns"] = checkpoints.list_all(limit=40, cid=cid) if data["alive"] else []
    return _json({"ok": True, **data})


@app.get("/api/changes/timeline")
def change_timeline(cid: str = "", path: str = "") -> Any:
    """第三级：一个文件的改动时间线（阶段 0 = 最初）。"""
    if not cid:
        return _fail("缺少 cid", status=400)
    if not path:
        return _fail("缺少 path", status=400)
    return _json({"ok": True, **change_book.timeline(cid, path)})


@app.get("/api/changes/summary")
def changes_summary(cid: str = "") -> Any:
    return _json({"ok": True, **change_book.summary(cid)})


@app.get("/api/changes/diff", response_class=PlainTextResponse)
def change_diff(path: str, cid: str = "", stage: str = "") -> Any:
    """看差异。带 stage 就看那一次改动，不带就看最近一次。"""
    which = int(stage) if str(stage).strip().isdigit() else None
    text, problem = change_book.diff(cid, path, which)
    if problem:
        raise HTTPException(status_code=409 if problem == change_book.REASONS["not_mine"] else 404,
                            detail=problem)
    return PlainTextResponse(text)


@app.post("/api/changes/restore")
def restore_change(payload: dict = Body(default={})) -> Any:
    """退回到时间线上的某个阶段。**必须带 cid**，理由同下面的 revert。"""
    cid = str(payload.get("cid") or "")
    path = str(payload.get("path") or "")
    if not cid:
        return _fail("缺少 cid：退回必须指明是哪条对话的改动", status=400)
    if not path:
        return _fail("缺少 path", status=400)
    try:
        stage = int(payload.get("stage"))
    except (TypeError, ValueError):
        return _fail("stage 要是整数（0 = 最初）", status=400)

    result = change_book.restore(cid, path, stage, force=bool(payload.get("force")))
    if not result.get("ok"):
        code = result.get("code")
        # 404 = 没有这条记录 / 这个阶段；409 = 记录在，但现在不能退
        status = 404 if code in ("no_record", "not_mine", "bad_stage") else 409
        return _fail(result.get("error") or "退不回去", status=status, code=code)
    return _json(result)


@app.post("/api/changes/purge")
def purge_changes(payload: dict = Body(default={})) -> Any:
    """清掉失效的改动记录。

    - 带 cid：删这条对话名下的记录（对话已经不在了才有意义，但接口不管这个 ——
      真删了也还能再改回来，靠的是 impact 那道确认）；
    - 不带：清掉所有孤儿 —— 没有归属的、主人已经删掉的。
    """
    cid = str(payload.get("cid") or "")
    if cid:
        swept = store.forget_cid(cid)
        return _json({"ok": True, "scope": "cid", "cid": cid, **swept})
    swept = store.purge_orphans()
    return _json({"ok": True, "scope": "orphans", **swept})


@app.post("/api/changes/revert")
def revert_change(payload: dict = Body(default={})) -> Any:
    """退回**最近一次**改动。等价于 restore 到阶段 0 的简化版，保留给老调用方。

    没有 cid 就分不清同名文件的改动是谁的。宁可让调用方补参数，
    也不能在这里猜一个 cid 出来：猜错的后果是替另一个对话撤销了改动。
    """
    cid = str(payload.get("cid") or "")
    path = str(payload.get("path") or "")
    if not cid:
        return _fail("缺少 cid：退回必须指明是哪条对话的改动", status=400)
    if not path:
        return _fail("缺少 path", status=400)

    result = change_book.revert(cid, path, force=bool(payload.get("force")))
    if not result.get("ok"):
        # 404 表示「这条记录不存在」，409 表示「记录在，但现在不能退」
        status = 404 if result.get("code") in ("no_record", "not_mine") else 409
        return _fail(result.get("error") or "退不回去", status=status)
    return _json(result)


# ---------------------------------------------------------------------------
# 安全：规则 / 审计 / 出站打码
# ---------------------------------------------------------------------------

@app.get("/api/security")
def security() -> Any:
    return _json({
        "ok": True,
        "rules": rules.describe(),
        "audit": {
            "stats": audit.stats(),
            "verify": audit.verify(),
            "entries": audit.entries(120),
        },
        "dlp": dlp.describe(),
    })


@app.post("/api/security/dlp")
def set_dlp(payload: dict = Body(default={})) -> Any:
    config.update({"dlp": bool(payload.get("enabled"))})
    audit.append("user", "dlp.toggle", "开" if payload.get("enabled") else "关",
                 "allow", "用户自己改的", "")
    return _json({"ok": True, "enabled": dlp.enabled()})


@app.post("/api/security/rules")
def add_rule(payload: dict = Body(default={})) -> Any:
    kind = str(payload.get("kind") or "")
    action = str(payload.get("action") or "gate")
    patterns = payload.get("patterns") or []
    if isinstance(patterns, str):
        patterns = [line.strip() for line in patterns.splitlines() if line.strip()]
    patterns = [str(p) for p in patterns if str(p).strip()]
    if kind not in ("path", "command", "net"):
        return _fail("规则类型只能是 path / command / net")
    if action not in ("deny", "gate"):
        return _fail("动作只能是 deny 或 gate")
    if not patterns:
        return _fail("至少写一条匹配内容")
    rule = {
        "id": f"user-{int(time.time())}",
        "kind": kind,
        "action": action,
        "patterns": patterns,
        "why": str(payload.get("why") or "你加的规则"),
        "tools": [str(t) for t in (payload.get("tools") or [])],
    }
    current = list(config.get().get("rules") or [])
    current.append(rule)
    config.update({"rules": current})
    audit.append("user", "rule.add", rule["id"], "allow", rule["why"], "")
    return _json({"ok": True, "rule": rule})


@app.delete("/api/security/rules/{rule_id}")
def drop_rule(rule_id: str) -> Any:
    current = [item for item in (config.get().get("rules") or [])
               if str(item.get("id")) != rule_id]
    config.update({"rules": current})
    audit.append("user", "rule.drop", rule_id, "allow", "", "")
    return _json({"ok": True})


@app.post("/api/security/audit/clear")
def clear_audit() -> Any:
    return _json({"ok": True, "removed": audit.clear()})


# ---------------------------------------------------------------------------
# 助理与群聊
# ---------------------------------------------------------------------------

@app.get("/api/assistants")
def list_assistants() -> Any:
    data = assistants.describe()
    for item in data["items"]:
        item["stats"] = assistants.stats(item["id"])
    return _json(data)


@app.post("/api/assistants")
def create_assistant(payload: dict = Body(default={})) -> Any:
    name = str(payload.get("name") or "").strip()
    if not name:
        return _fail("助理得有个名字")
    try:
        record = assistants.create(
            name=name,
            persona=payload.get("persona") or "",
            emoji=payload.get("emoji") or "🙂",
            tagline=payload.get("tagline") or "",
            model=payload.get("model") or "",
            think=payload.get("think") or "",
            permission=payload.get("permission") or "",
            mode=payload.get("mode") or "",
        )
    except ValueError as exc:
        return _fail(str(exc))
    audit.append("user", "assistant.create", record["id"], "allow", record["name"])
    record["stats"] = assistants.stats(record["id"])
    return _json({"ok": True, "assistant": record})


@app.patch("/api/assistants/{aid}")
def patch_assistant(aid: str, payload: dict = Body(default={})) -> Any:
    try:
        record = assistants.update(aid, **payload)
    except ValueError as exc:
        return _fail(str(exc))
    record["stats"] = assistants.stats(aid)
    return _json({"ok": True, "assistant": record})


@app.delete("/api/assistants/{aid}")
def drop_assistant(aid: str) -> Any:
    try:
        result = assistants.remove(aid)
    except ValueError as exc:
        return _fail(str(exc))
    audit.append("user", "assistant.delete", aid, "allow", "文件保留")
    return _json(result)


@app.get("/api/assistants/{aid}/stats")
def assistant_stats(aid: str) -> Any:
    if not assistants.get(aid):
        return _fail("没有这个助理", status=404)
    return _json({"ok": True, **assistants.stats(aid)})


@app.get("/api/groups")
def list_groups() -> Any:
    return _json({"ok": True, "items": assistants.list_groups(),
                  "rules": assistants.GROUP_RULES})


@app.post("/api/groups")
def create_group(payload: dict = Body(default={})) -> Any:
    try:
        record = assistants.create_group(
            name=payload.get("name") or "",
            members=payload.get("members") or [],
            rule=payload.get("rule") or "round_robin",
            host=payload.get("host") or "",
            topic=payload.get("topic") or "",
        )
    except ValueError as exc:
        return _fail(str(exc))
    return _json({"ok": True, "group": record})


@app.patch("/api/groups/{gid}")
def patch_group(gid: str, payload: dict = Body(default={})) -> Any:
    try:
        record = assistants.update_group(gid, **payload)
    except ValueError as exc:
        return _fail(str(exc))
    return _json({"ok": True, "group": record})


@app.delete("/api/groups/{gid}")
def drop_group(gid: str) -> Any:
    assistants.remove_group(gid)
    return _json({"ok": True})


# ---------------------------------------------------------------------------
# 运行时探测
# ---------------------------------------------------------------------------

@app.get("/api/runtime")
def runtime_state() -> Any:
    return _json(runtime.self_check())


@app.post("/api/runtime/detect")
def runtime_redetect() -> Any:
    runtime.detect(force=True)
    return _json(runtime.self_check())


@app.post("/api/runtime/path")
def runtime_set_path(payload: dict = Body(default={})) -> Any:
    name = str(payload.get("name") or "")
    if name not in runtime.PROBES:
        return _fail("不认识这个运行时")
    try:
        record = runtime.set_path(name, payload.get("path") or "")
    except ValueError as exc:
        return _fail(str(exc))
    return _json({"ok": True, "item": record})


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------

@app.get("/api/channels")
def channel_state() -> Any:
    return _json(channels.state())


@app.post("/api/channels/api")
def channel_api(payload: dict = Body(default={})) -> Any:
    state = channels.set_api(bool(payload.get("enabled")))
    audit.append("user", "channel.api", "开" if payload.get("enabled") else "关", "allow", "")
    return _json(state)


@app.post("/api/channels/token")
def channel_token() -> Any:
    fresh = channels.rotate_token()
    audit.append("user", "channel.token", "轮换", "allow", "")
    return _json({"ok": True, "token": fresh})


# ---------------------------------------------------------------------------
# 产物：这一条会话往磁盘上写了什么
# ---------------------------------------------------------------------------

@app.get("/api/session/{cid}/artifacts")
def session_artifacts(cid: str) -> Any:
    sess = _require_session(cid)
    return _json(artifacts.collect(cid, sess.project))


@app.get("/api/session/{cid}/artifacts/diff", response_class=PlainTextResponse)
def session_artifact_diff(cid: str, path: str) -> Any:
    text = artifacts.diff_text(cid, path)
    if text is None:
        raise HTTPException(status_code=404, detail="这条会话没有改过这个文件")
    return PlainTextResponse(text)


def _workspace_roots() -> list[Path]:
    """所有会话记过的工作区目录（含已归档的会话）。"""
    roots: list[Path] = []
    for meta in store.list_traces(include_archived=True):
        raw = str(meta.get("project") or "")
        if not raw:
            continue
        try:
            resolved = Path(raw).resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _inside(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


@app.get("/api/artifact/raw")
def artifact_raw(path: str) -> Any:
    """把产物里的图片字节读出来，供面板预览。

    只放图片：这个口子存在的唯一理由是「截图得能看」，
    顺手放开成任意文件下载，就成了一个本地文件读取器。
    另外还要求文件**在某个工作区里** —— 产物本来就只可能落在工作区，
    放开到「磁盘上任意图片」没有任何好处，只是把口子开大了一寸。
    """
    target = Path(path)
    suffix = target.suffix.lower().lstrip(".")
    if suffix not in {"png", "jpg", "jpeg", "webp", "gif", "bmp"}:
        return _fail("只有图片能预览", status=400)
    if not target.is_file():
        return _fail("这个文件已经不在了", status=404)
    try:
        resolved = target.resolve()
    except OSError as exc:
        return _fail(f"读不了这个路径：{exc}", status=400)
    if not any(_inside(resolved, root) for root in _workspace_roots()):
        return _fail("这个文件不在任何工作区里，不能预览", status=403)
    media = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
    }[suffix]
    return Response(target.read_bytes(), media_type=media)


@app.post("/api/reveal")
def reveal(payload: dict = Body(default={})) -> Any:
    """在系统文件管理器里定位到一个文件。

    只做「定位」这一件事：不执行、不打开文件本身。传进来的东西先确认存在，
    免得成了一个「随便点一下就调系统命令」的口子。
    """
    target = Path(str(payload.get("path") or ""))
    if not target.exists():
        return _fail("这个文件已经不在了", status=404)
    try:
        if os.name == "nt":
            # explorer 认的是 `explorer /select,"路径"` 这一整串；
            # 拆成列表参数时它会把逗号后的空格当分隔，带空格的路径就定位不到。
            subprocess.Popen(f'explorer /select,"{target}"')
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
    except OSError as exc:
        return _fail(f"打不开文件管理器：{exc}")
    return _json({"ok": True, "path": str(target)})


# ---------------------------------------------------------------------------
# 选目录
# ---------------------------------------------------------------------------

@app.get("/api/fs")
def browse(path: str = "") -> Any:
    if not path:
        if os.name == "nt":
            drives = [
                {"name": f"{letter}:", "path": f"{letter}:\\"}
                for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                if Path(f"{letter}:\\").exists()
            ]
            home = Path.home()
            return _json({"path": "", "parent": "", "entries": [], "drives": drives,
                          "home": str(home)})
        path = str(Path.home())

    target = Path(path).expanduser()
    if not target.is_dir():
        return _fail("这不是一个目录", status=404)

    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith((".", "$", "System Volume", "Windows", "Config.Msi")):
                continue
            try:
                entries.append({"name": child.name, "path": str(child), "dir": child.is_dir()})
            except OSError:
                continue
    except PermissionError:
        return _fail("这个目录不让看", status=403)
    except OSError as exc:
        return _fail(str(exc))

    parent = str(target.parent) if target.parent != target else ""
    return _json({"path": str(target), "parent": parent, "entries": entries[:500],
                  "drives": [], "home": str(Path.home())})


@app.post("/api/fs/mkdir")
def make_dir(payload: dict = Body(default={})) -> Any:
    parent = str(payload.get("parent") or "")
    name = str(payload.get("name") or "").strip()
    if not parent or not name:
        return _fail("缺目录或名字")
    if any(char in name for char in '\\/:*?"<>|'):
        return _fail("名字里有不能用的字符")
    target = Path(parent) / name
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _fail("这个目录已经存在")
    except OSError as exc:
        return _fail(str(exc))
    return _json({"ok": True, "path": str(target)})


@app.post("/api/fs/pick-folder")
def pick_folder(title: str = "选择工作区") -> Any:
    """弹系统自带的「选文件夹」对话框，等用户选完再返回。

    这个接口会**一直挂着**直到用户点确定或取消（最长 15 分钟）—— 同步接口跑在
    线程池里，所以不会卡住别的事件。前端那边是一次普通的 await，弹窗一关就回来。
    """
    from . import folderdialog

    result = folderdialog.pick_folder(title=title)
    if "error" in result:
        return _fail(result["error"])
    return _json({"ok": True, "path": result.get("path")})


@app.post("/api/fs/pick-file")
def pick_file(title: str = "选一个文件", images: int = 0) -> Any:
    """系统自带的「选文件」对话框。

    `images=1` 时只列常见图片格式（但仍然留一条「所有文件」）——
    「图片 / 截图」和「文件」如果弹的是同一个什么都收的框，
    那两项就是同一个按钮，菜单里摆两项只会让人以为哪一项坏了。
    """
    from . import folderdialog

    exts = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"] if images else None
    result = folderdialog.pick_file(title=title, exts=exts)
    if "error" in result:
        return _fail(result["error"])
    path = result.get("path")
    info: dict[str, Any] = {"ok": True, "path": path}
    if path:
        target = Path(path)
        try:
            info["name"] = target.name
            info["size"] = target.stat().st_size
        except OSError:
            pass
    return _json(info)


@app.post("/api/workspace/trash")
def workspace_trash(payload: dict = Body(default={})) -> Any:
    """把一个工作区目录移进系统回收站。

    这是整个程序里**唯一**一个「删目录」的接口，所以护栏写在这里而不是只写在前端：
    前端的输入名字确认是防手滑，这里防的是「任何一个能发请求的东西」。
    拒绝的条件都是「删了之后这个程序自己就活不下去」或者「等于把整台机器删了」：
    盘符根、用户主目录、程序自己的数据目录、以及它们的任何祖先目录。
    """
    raw = str(payload.get("path") or "").strip()
    if not raw:
        return _fail("没给路径", status=400)
    target = Path(raw).expanduser()
    try:
        target = target.resolve()
    except OSError as exc:
        return _fail(f"路径解析不了：{exc}")

    if not target.is_dir():
        return _fail("这不是一个目录", status=404)
    if target.parent == target:
        return _fail("不能删盘符根目录", status=403)

    home = Path.home().resolve()
    guarded = [home, DATA.resolve(), RESOURCE.resolve()]
    for keep in guarded:
        if target == keep:
            return _fail("这是程序自己或用户主目录，不能删", status=403)
        # 反过来也要拦：删掉了它们的父目录，等于连它们一起删了
        try:
            keep.relative_to(target)
            return _fail("这个目录里有程序自己的数据，不能整块删", status=403)
        except ValueError:
            pass

    audit.append("user", "workspace.trash", str(target), "allow", "用户确认删除工作区", "")
    ok = trash.to_trash(target)
    if not ok:
        return _fail("没能移进回收站（可能是没有权限，或者目录正在被占用）")
    return _json({"ok": True, "path": str(target)})


# ---------------------------------------------------------------------------
# 静态前端（必须最后挂：它会把剩下的所有路径都吃掉）
# ---------------------------------------------------------------------------

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:  # pragma: no cover

    @app.get("/")
    def no_web() -> Any:
        return _json(
            {"ok": False, "error": "找不到 web/ 目录，前端没有一起打包进来",
             "looked_at": str(WEB_DIR)},
            status=500,
        )
