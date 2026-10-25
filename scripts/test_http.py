"""接口冒烟测试：起服务 → 建会话 → 发一句 → 收事件 → 校验图。

不连真模型（靠 DREAMROLE_FAKE_LLM=1），所以跑一次只要几秒。

    python scripts/test_http.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _console import harden  # noqa: E402

harden()

SANDBOX = Path(tempfile.mkdtemp(prefix="dreamrole-http-"))
os.environ["DREAMROLE_DATA_DIR"] = str(SANDBOX / "data")
os.environ["DREAMROLE_API_KEY"] = "fake-key"
os.environ["DREAMROLE_FAKE_LLM"] = "1"

import fake_llm  # noqa: E402

fake_llm.install()

from app import config  # noqa: E402
from app.paths import ensure_dirs  # noqa: E402

ensure_dirs()
config.load()

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  [ok]   " if ok else "  [FAIL] ") + name + ("" if ok else "  " + str(detail)))


def call(method: str, path: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            # 有的口子回的是二进制（图片预览），按 utf-8 硬解会在这里抛
            # UnicodeDecodeError —— 那会把「接口对不对」测成「解码器对不对」。
            body = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def start_server() -> threading.Thread:
    import uvicorn

    from app.server import app as fastapi_app

    def run() -> None:
        uvicorn.run(fastapi_app, host="127.0.0.1", port=PORT, log_level="error", access_log=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            call("GET", "/api/bootstrap")
            return thread
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError("服务没起来")


def main() -> int:
    project = SANDBOX / "project"
    project.mkdir(parents=True, exist_ok=True)
    # 「## 怎么跑」这一节是给产物那节用的：追加的那一轮要在这个锚点上插内容，
    # 文件里没有这个锚点，edit_file 会原样失败，也就没有「修改」这一类可验。
    (project / "README.md").write_text(
        "# 演示项目\n\n这是一个用来验证链路的目录。\n\n## 怎么跑\n\n```\npython main.py\n```\n",
        encoding="utf-8",
    )
    (project / "main.py").write_text("print('hi')\n", encoding="utf-8")

    start_server()
    print(f"服务已起在 {BASE}\n")

    print("[1] 启动数据")
    status, boot = call("GET", "/api/bootstrap")
    check("bootstrap 200", status == 200, status)
    check("有模式清单", len(boot.get("modes") or []) == 4, boot.get("modes"))
    check("有工具清单", len(boot.get("tools") or []) >= 20, len(boot.get("tools") or []))
    check("有提示词模板", len(boot.get("templates") or []) == 5, boot.get("templates"))
    check("静态首页可访问", "DreamRole" in str(call("GET", "/")[1])[:4000])

    # 桌面外壳靠这个回执判断「界面真的跑起来了」，不能只靠「窗口开出来了」——
    # 窗口开着、里面一片背景色，所有检查都会点头，用户却在对着空气发呆。
    status, ping = call("GET", "/api/boot-ok?t=tok123")
    check("页面回执接口 200", status == 200 and ping.get("ok") is True, ping)
    from app import server as server_mod
    check("回执记下来了", server_mod.boot_seen("tok123"))
    check("没过界面渲染就没有 ui 回执", not server_mod.boot_seen("tok123", ui_only=True))
    call("GET", "/api/boot-ok?t=tok123&ui=1")
    check("界面渲染完成会单独记一笔", server_mod.boot_seen("tok123", ui_only=True))
    call("GET", "/api/boot-ok?t=tok123&err=" + urllib.parse.quote("TypeError: x is undefined"))
    check("页面报错能回传到服务端", "TypeError" in server_mod.boot_error(),
          server_mod.boot_error())

    print("\n[2] 建会话")
    status, session = call("POST", "/api/session", {"project": str(project), "mode": "teach"})
    check("建会话 200", status == 200, session)
    cid = session.get("cid")
    check("拿到 cid", bool(cid), cid)
    check("教学模式权限被提到 staged", session.get("effective") == "staged",
          session.get("effective"))

    print("\n[3] 发一句，等它跑")
    status, sent = call("POST", f"/api/session/{cid}/send", {"text": "带我看看这个项目"})
    check("发送成功", status == 200 and sent.get("ok"), sent)

    deadline = time.time() + 60
    data = {}
    answered = 0
    while time.time() < deadline:
        _status, data = call("GET", f"/api/session/{cid}")
        pending = data.get("pending") or []
        if pending:
            request = pending[0]
            decision = "continue" if request.get("kind") == "stage" else "allow"
            call("POST", f"/api/session/{cid}/answer", {"id": request["id"], "decision": decision})
            answered += 1
            continue
        if not data.get("running"):
            break
        time.sleep(0.25)

    events = data.get("events") or []
    kinds = [event["type"] for event in events]
    check("跑完了", not data.get("running"), "still running")
    check("中途被断点拦过", answered > 0, answered)
    check("事件里有 user", "user" in kinds, kinds)
    check("有工具调用", "action" in kinds and "result" in kinds, kinds)
    check("有最终答复", "answer" in kinds, kinds)
    check("阶段数 >= 2", len((data.get("graph") or {}).get("stages") or []) >= 2,
          (data.get("graph") or {}).get("stages"))

    print("\n[4] 教学模式断点")
    check("断点进了事件流",
          any(event["type"] == "notice" and (event.get("payload") or {}).get("breakpoint")
              for event in events), kinds)

    print("\n[5] 文件与改动记录")
    status, changes = call("GET", "/api/changes")
    check("改动记录存在", status == 200 and isinstance(changes, list), changes)
    if changes:
        first = changes[0]
        check("每条改动都带归属对话", bool(first.get("cid")), first.get("cid"))
        check("每条改动都带会话存亡与能否退回",
              "session_alive" in first and "revertible" in first, list(first))
        query = ("/api/changes/diff?cid=" + urllib.parse.quote(str(first.get("cid") or ""))
                 + "&path=" + urllib.parse.quote(first["path"]))
        status, diff = call("GET", query)
        check("能取到 diff", status == 200 and ("+" in diff or "一致" in diff), str(diff)[:80])

        status, wrong = call("GET", "/api/changes/diff?cid=nope&path="
                             + urllib.parse.quote(first["path"]))
        check("拿一个不存在的对话去要 diff，会被拒（对话没了就没有入口）",
              status == 404 and "已删除" in str(wrong), (status, wrong))

        status, missing = call("POST", "/api/changes/revert", {"path": first["path"]})
        check("不带 cid 的退回直接被拒", status == 400 and "cid" in str(missing), (status, missing))

    print("\n[6] Mermaid 导出")
    status, mermaid = call("GET", f"/api/session/{cid}/mermaid")
    check("Mermaid 200", status == 200, status)
    check("Mermaid 有 flowchart", "flowchart" in str(mermaid), str(mermaid)[:60])

    print("\n[7] 提示词优化")
    status, optimized = call("POST", "/api/promptopt/optimize",
                             {"text": "帮我改一下这个README", "template": "structure"})
    check("优化接口 200", status == 200, optimized)
    check("优化返回了内容", bool(optimized.get("optimized")), optimized)
    check("优化带上了警告位", "warnings" in optimized, list(optimized))

    status, judge = call("POST", "/api/promptopt/compare",
                         {"original": "改一下", "optimized": optimized.get("optimized", "")})
    check("评审接口可用", status == 200 and judge.get("ok"), judge)

    print("\n[8] 设置与工具开关")
    status, settings = call("GET", "/api/settings")
    check("设置可读", status == 200 and "providers" in settings, status)
    status, _ = call("POST", "/api/settings", {"permission": "full", "max_steps": 30})
    _s, settings2 = call("GET", "/api/settings")
    check("设置可写", settings2.get("permission") == "full", settings2.get("permission"))
    status, toggled = call("POST", "/api/tools/read_file/toggle", {"enabled": False})
    check("工具能关", status == 200 and "read_file" not in (toggled.get("enabled") or []), toggled)

    # ---- 上下文窗口与自动记忆：这两个开关必须能存下来、也得能读回来 ----
    check("设置里读得到上下文窗口", "context_window" in settings2, list(settings2)[:6])
    check("设置里读得到自动记忆", settings2.get("auto_memory") in ("off", "daily", "smart"),
          settings2.get("auto_memory"))

    _s, _ = call("POST", "/api/settings", {"context_window": "128k", "auto_memory": "daily"})
    _s, ctx_settings = call("GET", "/api/settings")
    check("上下文窗口能存住", ctx_settings.get("context_window") == "128k",
          ctx_settings.get("context_window"))
    check("自动记忆档位能存住", ctx_settings.get("auto_memory") == "daily",
          ctx_settings.get("auto_memory"))

    from app import context as context_mod
    check("存下来的窗口真的被用上了",
          context_mod.context_limit("deepseek-chat") == 128_000,
          context_mod.context_limit("deepseek-chat"))

    _s, _ = call("POST", "/api/settings", {"context_window": "", "auto_memory": "smart"})
    _s, back = call("GET", "/api/settings")
    check("能改回「自动」", back.get("context_window") == "", back.get("context_window"))
    check("自动记忆能改回默认", back.get("auto_memory") == "smart", back.get("auto_memory"))

    print("\n[9] 目录浏览")
    status, listing = call("GET", "/api/fs?path=" + urllib.parse.quote(str(SANDBOX)))
    check("能列目录", status == 200 and any(item["name"] == "project" for item in listing.get("entries", [])),
          listing)

    print("\n[10] 导出")
    status, md = call("GET", f"/api/session/{cid}/export?fmt=md&download=0")
    check("Markdown 200", status == 200, status)
    check("Markdown 有标题和过程图", "# " in str(md) and "```mermaid" in str(md), str(md)[:60])
    status, page = call("GET", f"/api/session/{cid}/export?fmt=html&download=0")
    check("HTML 200", status == 200, status)
    check("HTML 是完整文档", str(page).lstrip().startswith("<!DOCTYPE html>"), str(page)[:40])
    status, mmd = call("GET", f"/api/session/{cid}/export?fmt=mermaid&download=0")
    check("Mermaid 导出可用", "flowchart" in str(mmd), str(mmd)[:50])
    status, raw = call("GET", f"/api/session/{cid}/export?fmt=json&download=0")
    check("JSON 导出可用", isinstance(raw, dict) and "events" in raw, type(raw).__name__)
    status, missing = call("GET", "/api/session/nope-missing/export?fmt=md")
    check("不存在的会话给 404", status == 404, status)

    print("\n[11] 搜索与用量")
    status, hits = call("GET", "/api/search?q=" + urllib.parse.quote("看看这个项目"))
    check("搜索 200", status == 200 and isinstance(hits, list), status)
    check("搜到了刚跑的会话", any(hit.get("cid") == cid for hit in hits or []),
          [hit.get("cid") for hit in hits or []][:3])
    status, empty = call("GET", "/api/search?q=")
    check("空查询不炸", status == 200 and empty == [], str(empty)[:40])

    status, usage = call("GET", "/api/usage")
    check("用量 200", status == 200 and "total" in usage, status)
    check("用量记到了 token", int((usage.get("total") or {}).get("total") or 0) > 0,
          usage.get("total"))

    print("\n[12] 重试与改一句")
    before = len((call("GET", f"/api/session/{cid}")[1].get("events") or []))
    status, retried = call("POST", f"/api/session/{cid}/retry")
    check("重试接口 200", status == 200 and retried.get("ok"), retried)
    check("重试带回了原话", bool(retried.get("text")), retried)

    deadline = time.time() + 60
    while time.time() < deadline:
        _status, data = call("GET", f"/api/session/{cid}")
        pending = data.get("pending") or []
        if pending:
            request = pending[0]
            decision = "continue" if request.get("kind") == "stage" else "allow"
            call("POST", f"/api/session/{cid}/answer", {"id": request["id"], "decision": decision})
            continue
        if not data.get("running"):
            break
        time.sleep(0.25)
    after = len(data.get("events") or [])
    check("重试后事件更少了（旧的被删掉）", after <= before, f"{before} → {after}")

    status, edited = call("POST", f"/api/session/{cid}/edit",
                          {"index": 0, "text": "换一句话重新问"})
    check("改一句接口 200", status == 200 and edited.get("ok"), edited)

    deadline = time.time() + 60
    while time.time() < deadline:
        _status, data = call("GET", f"/api/session/{cid}")
        pending = data.get("pending") or []
        if pending:
            request = pending[0]
            decision = "continue" if request.get("kind") == "stage" else "allow"
            call("POST", f"/api/session/{cid}/answer", {"id": request["id"], "decision": decision})
            continue
        if not data.get("running"):
            break
        time.sleep(0.25)
    users = [event.get("content") for event in (data.get("events") or [])
             if event.get("type") == "user"]
    check("改完之后第一条用户消息是新话", users and users[0] == "换一句话重新问", users[:2])
    check("旧话没留在事件流里", "带我看看这个项目" not in users, users[:2])

    print("\n[13] 静态资源")
    for asset in ("js/palette.js", "js/app.js", "css/app.css"):
        status, _body = call("GET", "/" + asset)
        check(f"{asset} 能取到", status == 200, status)

    print("\n[14] 专家")
    status, experts_payload = call("GET", "/api/experts")
    items = experts_payload.get("items") or []
    check("专家列表 200", status == 200 and len(items) >= 5, len(items))
    check("内置专家带方法论", all((item.get("prompt") or "") for item in items[:3]),
          [item.get("id") for item in items[:3]])

    status, created = call("POST", "/api/experts", {
        "name": "测试专家", "emoji": "🧪", "desc": "只用于测试",
        "prompt": "你只回答一个字。", "mode": "ask",
    })
    made = created.get("expert") or {}
    check("能新建专家", status == 200 and created.get("ok") and made.get("id"), created)
    check("新建的专家能被读回来",
          any(item.get("id") == made.get("id") for item in (call("GET", "/api/experts")[1].get("items") or [])))

    status, bound = call("POST", f"/api/session/{cid}/expert",
                         {"expert": made.get("id"), "apply_mode": True})
    check("会话能绑定专家", status == 200 and bound.get("expert") == made.get("id"), bound)
    check("绑专家时套用了它的默认模式", bound.get("mode") == "ask", bound.get("mode"))

    status, bad = call("POST", f"/api/session/{cid}/expert", {"expert": "不存在的专家"})
    check("绑不存在的专家被拒", status == 400 and not bad.get("ok"), bad)

    # 专家 id 允许中文（名字就是 id 的来源），所以路径必须 quote 一下
    status, _gone = call("DELETE", "/api/experts/" + urllib.parse.quote(str(made.get("id")), safe=""))
    check("能删专家", status == 200, status)
    check("删完就查不到了",
          not any(item.get("id") == made.get("id")
                  for item in (call("GET", "/api/experts")[1].get("items") or [])))

    print("\n[15] 记忆")
    status, mem = call("GET", "/api/memory")
    check("记忆列表 200", status == 200 and mem.get("ok"), status)
    check("三层都在（项目/偏好/每日）",
          {"project", "pref", "daily"} <= {item.get("kind") for item in mem.get("items") or []},
          [item.get("kind") for item in mem.get("items") or []])

    project_item = next((item["id"] for item in mem.get("items") or []
                         if item.get("kind") == "project"), "")
    status, wrote = call("POST", "/api/memory/item",
                         {"item": project_item, "text": "测试：这个项目用 pnpm"})
    check("能往项目记忆里加一条", status == 200 and wrote.get("ok"), wrote)

    status, proj_read = call("GET", "/api/memory/item?item=" + urllib.parse.quote(project_item))
    check("项目记忆读得回来", "这个项目用 pnpm" in (proj_read.get("text") or ""),
          (proj_read.get("text") or "")[:80])

    status, pref = call("POST", "/api/memory/item",
                        {"item": "pref", "text": "回答别用 emoji"})
    check("能往用户偏好里加一条", status == 200 and pref.get("ok"), pref)

    status, pref_read = call("GET", "/api/memory/item?item=pref")
    check("偏好读得回来且带时间戳",
          "回答别用 emoji" in (pref_read.get("text") or "")
          and "- [" in (pref_read.get("text") or ""), (pref_read.get("text") or "")[:80])

    status, bad_mem = call("POST", "/api/memory/item", {"item": "nope", "text": "x"})
    check("不认识的记忆类型给 400", status == 400 or not bad_mem.get("ok"), bad_mem)

    print("\n[16] 检查点")
    status, cp = call("GET", "/api/checkpoints")
    check("检查点列表 200", status == 200 and cp.get("ok"), status)
    # 假模型这一轮改过文件，应该留下了可退回的记录
    mine = [item for item in (cp.get("items") or []) if item.get("cid") == cid]
    check("这一轮留下了检查点", bool(mine), [item.get("id") for item in (cp.get("items") or [])][:5])
    if mine:
        check("检查点里带着文件清单", int(mine[0].get("count") or 0) >= 1, mine[0].get("files"))
        status, restored = call("POST", "/api/checkpoints/restore", {"id": mine[0]["id"]})
        check("能整体退回", status == 200 and restored.get("ok"), restored)

    status, nowhere = call("POST", "/api/checkpoints/restore", {"id": "nope#999"})
    check("退回不存在的检查点会被拒", not nowhere.get("ok"), nowhere)

    print("\n[17] 工作区是会话级的")
    status, refused = call("POST", "/api/session", {"mode": "craft"})
    check("不给工作区，建会话直接被拒", status == 412 and not refused.get("ok"), refused)

    # 绕过 HTTP 造一条「没有工作区」的老会话，验证服务端真的不让它跑
    from app import session as session_mod
    bare = session_mod.create(project="")
    status, blocked = call("POST", f"/api/session/{bare.cid}/send", {"text": "你好"})
    check("没有工作区的会话发不出消息", status == 412 and not blocked.get("ok"), blocked)
    check("被拒的原因说得清是工作区", "工作区" in str(blocked.get("error")), blocked.get("error"))

    new_ws = SANDBOX / "another-workspace"
    status, moved = call("POST", f"/api/session/{cid}/workspace", {"project": str(new_ws)})
    check("能给已有会话换工作区", status == 200 and moved.get("ok"), moved)
    check("换完真的落到了会话上",
          call("GET", f"/api/session/{cid}")[1].get("project") == str(new_ws.resolve()),
          call("GET", f"/api/session/{cid}")[1].get("project"))

    status, empty_ws = call("POST", f"/api/session/{cid}/workspace", {"project": ""})
    check("工作区不能设成空", status == 400 and not empty_ws.get("ok"), empty_ws)

    # ---- 额外授权目录 ----
    share = SANDBOX / "another-share"
    share.mkdir(parents=True, exist_ok=True)

    saved = call("GET", f"/api/session/{cid}")[1].get("grants")
    check("会话一开始没有授权目录", saved == [], saved)

    status, added = call("POST", f"/api/session/{cid}/grants",
                         {"op": "add", "path": str(share)})
    check("能加一张通行证", status == 200 and added.get("ok"), added)
    check("加完列表里有它",
          [g["path"] for g in (added.get("grants") or [])] == [str(share.resolve())],
          added.get("grants"))
    check("默认是只读", (added.get("grants") or [{}])[0].get("mode") == "ro", added.get("grants"))

    status, again = call("POST", f"/api/session/{cid}/grants",
                         {"op": "add", "path": str(share)})
    check("同一目录重复加不会出现两条", len(again.get("grants") or []) == 1, again.get("grants"))

    status, toggled = call("POST", f"/api/session/{cid}/grants",
                           {"op": "mode", "path": str(share), "mode": "rw"})
    check("能把档位改成可写", (toggled.get("grants") or [{}])[0].get("mode") == "rw", toggled)

    status, removed = call("POST", f"/api/session/{cid}/grants",
                           {"op": "remove", "path": str(share)})
    check("能撤掉一张", (removed.get("grants") or []) == [], removed)

    status, bad = call("POST", f"/api/session/{cid}/grants",
                       {"op": "nonsense", "path": str(share)})
    check("不认识的操作不会炸，只是说一句", status == 200 and bad.get("ok"), bad)

    # ---- 全局通行证（默认权限）----
    # 用户的原话是「不想一个一个添加，设置全局权限」。这一组守的是三条：
    # 新会话会自动带上、老会话不会被回头改、带上来的能单独撤掉。
    status, g0 = call("GET", "/api/grants/global")
    check("全局通行证一开始是空的", status == 200 and g0.get("grants") == [], str(g0)[:80])

    gshare = SANDBOX / "global-share"
    gshare.mkdir(parents=True, exist_ok=True)
    status, g1 = call("POST", "/api/grants/global",
                      {"op": "add", "path": str(gshare), "mode": "rw"})
    check("能加一张全局通行证",
          status == 200 and len(g1.get("grants") or []) == 1, str(g1)[:90])

    status, fresh = call("POST", "/api/session", {"project": str(project)})
    fresh_grants = fresh.get("grants") or []
    check("新会话自动带上全局通行证",
          [g["path"] for g in fresh_grants] == [str(gshare.resolve())], fresh_grants)
    check("带过来的是「全局」那一张（界面要能标出来）",
          bool(fresh_grants) and fresh_grants[0].get("global") is True, fresh_grants)

    # 「改全局不会动老对话」—— 这条不守住，用户会发现某条跑到一半的会话
    # 突然能去新的目录了，而那是没法解释的。
    _status, old = call("GET", f"/api/session/{cid}")
    check("已经在的会话不会因为改了全局就多出目录",
          (old.get("grants") or []) == [], old.get("grants"))

    call("POST", f"/api/session/{fresh.get('cid')}/grants",
         {"op": "remove", "path": str(gshare)})
    _status, after = call("GET", f"/api/session/{fresh.get('cid')}")
    check("在某一条会话里能单独撤掉它",
          (after.get("grants") or []) == [], after.get("grants"))

    status, g2 = call("POST", "/api/grants/global", {"op": "clear"})
    check("全局通行证能一次撤干净",
          status == 200 and (g2.get("grants") or []) == [], str(g2)[:80])

    # 建会话时直接带上授权（前端在 cid 还不存在时走的那条路）
    status, pre = call("POST", "/api/session",
                       {"project": str(project), "grants": [{"path": str(share)}]})
    check("建会话时可以一起带上授权", status == 200, pre)
    check("建完之后权限已经在会话上",
          [g["path"] for g in (pre.get("grants") or [])] == [str(share.resolve())],
          pre.get("grants"))

    status, boot2 = call("GET", "/api/bootstrap")
    recents = (boot2.get("settings") or {}).get("recent_projects") or []
    check("用过的工作区进了「最近」", any(str(new_ws.resolve()) == item for item in recents), recents)

    # 定时任务同样不许没有工作区
    status, no_ws_job = call("POST", "/api/schedules", {"prompt": "每天早上看看日志"})
    check("定时任务没有工作区也不给建", status == 412 and not no_ws_job.get("ok"), no_ws_job)

    print("\n[18] 产物是按会话算的")
    # 先把工作区换回去，不然下面算出来的相对路径会挂在上一个临时目录上
    call("POST", f"/api/session/{cid}/workspace", {"project": str(project)})
    # 再跑一轮「只写不删」的，样本里才有「新建」和「修改」两种，不是只有一条已删除记录
    fake_llm.run_keep_turn(session_mod.get(cid))

    status, art = call("GET", f"/api/session/{cid}/artifacts")
    items = art.get("items") or []
    check("产物接口 200", status == 200 and art.get("ok"), art)
    check("产物里有一条会话写过的文件", any(i.get("name") == "dreamrole-demo.txt" for i in items),
          [i.get("name") for i in items])
    check("分得清「新建」和「修改」",
          {i.get("kind") for i in items} >= {"created", "modified"},
          sorted({i.get("kind") for i in items}))
    check("按这一条会话过滤，不串到别的会话",
          all(i.get("path") for i in items) and
          any(i.get("name") == "NOTES.md" for i in items),
          [i.get("name") for i in items])
    check("每条都带轮次和是否存在",
          all("turns" in i and "exists" in i for i in items),
          items[:1])
    demo = next((i for i in items if i.get("name") == "dreamrole-demo.txt"), {})
    check("删掉的文件标成「已不在」", demo.get("exists") is False, demo)

    if demo.get("path"):
        status, diff = call("GET", f"/api/session/{cid}/artifacts/diff?path="
                            + urllib.parse.quote(demo["path"]))
        check("能看这条会话对这个文件的改动", status == 200 and "dreamrole-demo.txt" in str(diff),
              str(diff)[:120])
    status, missing = call("GET", f"/api/session/{cid}/artifacts/diff?path=/nope/none.txt")
    check("没改过的文件不给 diff", status == 404, status)

    # 别的会话拿不到这条会话的产物
    other = session_mod.create(str(project), mode="craft", permission="full")
    status, other_art = call("GET", f"/api/session/{other.cid}/artifacts")
    check("新会话的产物是空的", status == 200 and (other_art.get("total") or 0) == 0, other_art)

    print("\n[19] 产物周边的两个口子")
    status, revealed = call("POST", "/api/reveal", {"path": str(SANDBOX / "没有这个文件.txt")})
    check("不存在的文件不给你开文件管理器", status == 404 and not revealed.get("ok"), revealed)
    status, raw = call("GET", "/api/artifact/raw?path=" + urllib.parse.quote(str(project / "README.md")))
    check("非图片不走图片预览口子", status == 400, status)

    # 图片预览只放行工作区里的图。产物本来只可能落在工作区，
    # 放开到「磁盘上任意图片」等于白送一个本地文件读取器。
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDAT\x78\x9c\x63"
           b"\x00\x01\x00\x00\x05\x00\x01\x0d\x0a\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    (project / "shot.png").write_bytes(png)
    inside = SANDBOX / "outside.png"
    inside.write_bytes(png)
    status, _ = call("GET", "/api/artifact/raw?path=" + urllib.parse.quote(str(project / "shot.png")))
    check("工作区里的图片能预览", status == 200, status)
    status, refused = call("GET", "/api/artifact/raw?path=" + urllib.parse.quote(str(inside)))
    check("工作区外的图片不给预览", status == 403, (status, str(refused)[:80]))

    print("\n[20] 助理：自己的屋子、自己的记录")
    status, made = call("POST", "/api/assistants", {
        "name": "测试助理", "emoji": "🧪", "tagline": "只在这条测试里存在",
        "persona": "你说话很短。",
    })
    check("能建一个助理", status == 200 and made.get("ok"), made)
    aid = (made.get("assistant") or {}).get("id", "")
    check("助理有 id", bool(aid), aid)

    status, listing = call("GET", "/api/assistants")
    mine = next((a for a in (listing.get("items") or []) if a["id"] == aid), None)
    check("列表里能查到这个助理", bool(mine), listing)
    check("助理有自己的工作目录", bool(mine and mine.get("workspace")), mine)
    workspace = Path(mine["workspace"]) if mine else Path()
    check("那间屋子真的建出来了", workspace.is_dir(), workspace)
    check("它还没聊过，所以是 0 条对话",
          bool(mine and (mine.get("stats") or {}).get("sessions") == 0), mine)

    status, patched = call("PATCH", f"/api/assistants/{aid}", {"tagline": "改过的定位"})
    check("能改人设", status == 200 and (patched.get("assistant") or {}).get("tagline") == "改过的定位",
          patched)

    status, sess_payload = call("POST", "/api/session", {"assistant": aid})
    check("能给助理开一条会话", status == 200 and sess_payload.get("cid"), sess_payload)
    aid_cid = sess_payload.get("cid", "")
    check("会话的工作区就是助理那间屋子",
          str(sess_payload.get("project") or "") == str(workspace.resolve()),
          sess_payload.get("project"))
    check("会话记住了自己属于谁", sess_payload.get("assistant") == aid, sess_payload.get("assistant"))

    call("POST", f"/api/session/{aid_cid}/send", {"text": "你好"})
    session_obj = session_mod.get(aid_cid)
    if session_obj:
        session_obj.wait_idle(20)
    status, after = call("GET", f"/api/session/{aid_cid}")
    check("助理会话能正常跑一轮", status == 200 and len(after.get("events") or []) >= 2, status)
    status, stats = call("GET", f"/api/assistants/{aid}/stats")
    check("跑完之后它的对话数变成 1", status == 200 and stats.get("sessions") == 1, stats)
    check("它的对话记录落在自己的子目录里",
          (SANDBOX / "data" / "traces" / aid / f"{aid_cid}.json").is_file(),
          list((SANDBOX / "data" / "traces").iterdir()))

    print("\n[21] 群聊：谁先说、谁收尾")
    status, second = call("POST", "/api/assistants", {"name": "第二个助理", "emoji": "🧭"})
    bid = (second.get("assistant") or {}).get("id", "")
    check("再建一个助理", status == 200 and bool(bid), second)

    status, bad_group = call("POST", "/api/groups", {"name": "一个人", "members": [aid]})
    check("只有一个人不许建群", status == 400, bad_group)

    status, group = call("POST", "/api/groups", {
        "name": "测试群", "members": [aid, bid], "rule": "round_robin",
    })
    check("两个人就能建群", status == 200 and (group.get("group") or {}).get("id"), group)
    gid = (group.get("group") or {}).get("id", "")

    status, group_sess = call("POST", "/api/session", {"group": gid})
    check("能给群开一条会话", status == 200 and group_sess.get("cid"), group_sess)
    g_cid = group_sess.get("cid", "")
    check("群会话带上了成员", len(group_sess.get("members") or []) == 2, group_sess.get("members"))

    call("POST", f"/api/session/{g_cid}/send", {"text": "大家说说该先做什么"})
    group_obj = session_mod.get(g_cid)
    if group_obj:
        group_obj.wait_idle(25)
    status, g_after = call("GET", f"/api/session/{g_cid}")
    speakers = [e for e in (g_after.get("events") or [])
                if e.get("type") == "answer" and (e.get("payload") or {}).get("member")]
    check("轮流规则下两个人都说话了", len(speakers) >= 2, len(speakers))
    check("每句话都标了是谁说的",
          all((e.get("payload") or {}).get("member", {}).get("name") for e in speakers), speakers[:1])

    status, mention = call("PATCH", f"/api/groups/{gid}", {"rule": "mention"})
    check("能把规则改成「只让点名的说」",
          status == 200 and (mention.get("group") or {}).get("rule") == "mention", mention)
    call("POST", f"/api/session/{g_cid}/send", {"text": "没人被点名"})
    if group_obj:
        group_obj.wait_idle(25)
    status, g_after2 = call("GET", f"/api/session/{g_cid}")
    last = (g_after2.get("events") or [])[-1] if g_after2.get("events") else {}
    check("没人点名时它会提示要点名，而不是自己找人说",
          bool((last.get("payload") or {}).get("need_mention")), last.get("content"))

    status, host = call("PATCH", f"/api/groups/{gid}", {"rule": "host", "host": bid})
    check("能把群改成主持人制",
          status == 200 and (host.get("group") or {}).get("host") == bid, host)

    # ---- 成员能加能减 ----
    # 用户原话是「智能团成员没有删除功能」。这一组守两件事：
    # 移出真的生效、剩一个人时不许移（那就不再是群了）。
    status, third = call("POST", "/api/assistants", {"name": "第三个助理", "emoji": "🎯"})
    tid = (third.get("assistant") or {}).get("id", "")
    status, added_m = call("PATCH", f"/api/groups/{gid}", {"members": [aid, bid, tid]})
    check("能把人拉进群",
          status == 200 and len((added_m.get("group") or {}).get("members") or []) == 3, added_m)

    status, removed_m = call("PATCH", f"/api/groups/{gid}", {"members": [aid, bid]})
    check("能把人移出群",
          status == 200 and (removed_m.get("group") or {}).get("members") == [aid, bid],
          removed_m)
    check("移出群不等于删角色，它还在",
          any(a["id"] == tid for a in (call("GET", "/api/assistants")[1].get("items") or [])))

    status, too_few = call("PATCH", f"/api/groups/{gid}", {"members": [aid]})
    check("剩一个人就不算群了，拒掉", status == 400, too_few)
    call("DELETE", f"/api/assistants/{tid}")

    status, gone = call("DELETE", f"/api/assistants/{aid}")
    check("能删掉这个角色", status == 200 and gone.get("ok"), gone)
    check("删角色时文件留在原地", workspace.is_dir(), workspace)
    check("删完之后列表里没有它了",
          all(a["id"] != aid for a in (call("GET", "/api/assistants")[1].get("items") or [])))

    print("\n[22] 安全：规则 / 审计链 / 出站打码")
    status, sec = call("GET", "/api/security")
    check("安全接口 200", status == 200 and sec.get("ok"), status)
    check("内置规则至少 5 条", len((sec.get("rules") or {}).get("rules") or []) >= 5,
          len((sec.get("rules") or {}).get("rules") or []))
    check("审计链是完整的", bool((sec.get("audit") or {}).get("verify", {}).get("ok")),
          (sec.get("audit") or {}).get("verify"))
    check("出站打码默认是开的", bool((sec.get("dlp") or {}).get("enabled")),
          (sec.get("dlp") or {}).get("enabled"))

    status, rule = call("POST", "/api/security/rules", {
        "kind": "path", "action": "deny", "patterns": ["**/绝对不许碰/**"],
        "why": "测试用",
    })
    check("能加一条自己的规则", status == 200 and (rule.get("rule") or {}).get("id"), rule)
    rule_id = (rule.get("rule") or {}).get("id", "")
    status, sec2 = call("GET", "/api/security")
    check("加完之后规则条数 +1",
          len((sec2.get("rules") or {}).get("rules") or []) >= 6,
          len((sec2.get("rules") or {}).get("rules") or []))

    status, _ = call("POST", "/api/security/rules", {"kind": "乱写", "patterns": ["x"]})
    check("规则类型乱写会被拒", status == 400, status)
    status, _ = call("POST", "/api/security/rules", {"kind": "path", "patterns": []})
    check("一条匹配都不给会被拒", status == 400, status)

    status, _ = call("DELETE", f"/api/security/rules/{rule_id}")
    check("能删掉自己的规则", status == 200, status)
    status, sec3 = call("GET", "/api/security")
    check("删完之后它不是用户规则了",
          not any(r.get("user") and r.get("id") == rule_id
                  for r in (sec3.get("rules") or {}).get("rules") or []))

    status, dlp_off = call("POST", "/api/security/dlp", {"enabled": False})
    check("能关掉出站打码", status == 200 and dlp_off.get("enabled") is False, dlp_off)
    call("POST", "/api/security/dlp", {"enabled": True})

    status, cleared = call("POST", "/api/security/audit/clear")
    check("能清空审计记录", status == 200 and cleared.get("ok"), cleared)
    status, sec4 = call("GET", "/api/security")
    check("清空之后链还是完整的（清空本身也留了一条）",
          bool((sec4.get("audit") or {}).get("verify", {}).get("ok")),
          (sec4.get("audit") or {}).get("verify"))
    check("清空之后链从 1 条重新开始",
          (sec4.get("audit") or {}).get("stats", {}).get("total") == 1,
          (sec4.get("audit") or {}).get("stats"))

    print("\n[23] 运行时与通道")
    status, rt = call("GET", "/api/runtime")
    check("运行时接口 200", status == 200 and rt.get("ok"), status)
    check("探了不止三个运行时", len(rt.get("items") or []) >= 6, len(rt.get("items") or []))
    check("每一项都说清了可用不可用",
          all("usable" in item and "found" in item for item in (rt.get("items") or [])))
    check("当前进程的 python 版本带上来了", bool((rt.get("this") or {}).get("python")), rt.get("this"))

    status, rt2 = call("POST", "/api/runtime/detect")
    check("能重新探一遍", status == 200 and rt2.get("ok"), status)
    status, bad_path = call("POST", "/api/runtime/path", {"name": "python", "path": "/nope/none.exe"})
    check("指定一个不存在的路径会被拒", status == 400, bad_path)
    status, bad_name = call("POST", "/api/runtime/path", {"name": "不存在的运行时", "path": ""})
    check("不认识的运行时会被告知", status == 400, bad_name)

    status, ch = call("GET", "/api/channels")
    check("通道接口 200", status == 200 and len((ch.get("items") or [])) == 3, status)
    check("三个门的 id 对得上",
          [i["id"] for i in (ch.get("items") or [])] == ["desktop", "cli", "api"],
          [i["id"] for i in (ch.get("items") or [])])
    check("令牌长度够（不是占位串）", len(ch.get("token") or "") >= 24, len(ch.get("token") or ""))
    check("桌面窗口这条道已经记到流量了",
          next((i["count"] for i in (ch.get("items") or []) if i["id"] == "desktop"), 0) > 0,
          ch.get("items"))

    status, opened = call("POST", "/api/channels/api", {"enabled": True})
    check("能打开外部接口", status == 200 and opened.get("api_open") is True, opened)
    first_token = opened.get("token")
    status, rotated = call("POST", "/api/channels/token")
    check("能换令牌", status == 200 and rotated.get("token") and rotated.get("token") != first_token,
          rotated.get("token"))
    call("POST", "/api/channels/api", {"enabled": False})

    # 来源判定：绑回环时不管，绑外网时才要令牌。直接测这一层，
    # 因为它决定了中间件放不放行，而本机测不到「远端的 IP」。
    from app import channels as ch_mod

    ch_mod.note_listen("0.0.0.0")
    check("绑到外网地址后，非本机请求要令牌",
          ch_mod.allowed_origin("192.168.1.9", "")[0] is False)
    check("带对令牌就放行",
          ch_mod.allowed_origin("192.168.1.9", ch_mod.token())[0] is True)
    check("令牌不对不放行",
          ch_mod.allowed_origin("192.168.1.9", "x" * 24)[0] is False)
    check("本机永远不要令牌", ch_mod.allowed_origin("127.0.0.1", "")[0] is True)
    ch_mod.note_listen("127.0.0.1")
    check("绑回环时不算「对外」", ch_mod.listening_wide() is False)

    print("\n[24] 命令行通道：同一个内核，另一个门")
    from app import cli as cli_mod

    cli_project = SANDBOX / "cli-project"
    cli_project.mkdir(parents=True, exist_ok=True)
    (cli_project / "README.md").write_text("# cli\n\n## 怎么跑\n\npython x.py\n", encoding="utf-8")

    check("不给目录时会拒绝", cli_mod.drive(PORT, []) == 2, "")
    check("--help 打得开", cli_mod.drive(PORT, ["--help"]) == 0, "")

    code = cli_mod.drive(PORT, ["--project", str(cli_project), "看看这个项目", "--json"])
    check("能跑完一轮并返回 0", code == 0, code)

    status, cli_traces = call("GET", "/api/traces")
    rows = cli_traces if isinstance(cli_traces, list) else (
        cli_traces.get("items") or cli_traces.get("traces") or cli_traces.get("data") or [])
    cli_items = [t for t in rows
                 if str(t.get("project") or "") == str(cli_project.resolve())]
    check("命令行真建出了一条会话", bool(cli_items), len(cli_items))

    status, ch2 = call("GET", "/api/channels")
    cli_hits = next((i["count"] for i in (ch2.get("items") or []) if i["id"] == "cli"), 0)
    check("命令行这条道记到了流量", cli_hits > 0, cli_hits)

    print("\n[25] 改动归属 · 会话删除的级联")
    # 这个会话在本文档前段跑过一轮，改过文件，所以它名下有改动记录
    status, mine = call("GET", "/api/changes?cid=" + urllib.parse.quote(cid))
    check("按 cid 拿得到这条对话的改动", status == 200 and isinstance(mine, list), str(mine)[:80])
    mine = mine if isinstance(mine, list) else []
    check("筛出来的一定都属于这条对话",
          all(row.get("cid") == cid for row in mine), [row.get("cid") for row in mine])

    status, only_live = call("GET", "/api/changes?cid=" + urllib.parse.quote(cid)
                             + "&include_dead=0")
    check("include_dead=0 时不返回失效记录",
          status == 200 and all(row.get("session_alive") is not False for row in only_live),
          str(only_live)[:80])

    status, summary = call("GET", "/api/changes/summary?cid=" + urllib.parse.quote(cid))
    check("汇总接口可用", status == 200 and "revertible" in summary, summary)

    # 置顶 / 归档 / 改名
    for field, value, label in (("pinned", True, "置顶"), ("archived", True, "归档")):
        status, patched = call("PATCH", f"/api/traces/{cid}", {field: value})
        check(f"能给会话{label}", status == 200 and patched.get(field) is True,
              (status, str(patched)[:60]))
    status, patched = call("PATCH", f"/api/traces/{cid}", {"title": "  改个名字  "})
    check("能改名（首尾空白被收拾掉）",
          status == 200 and patched.get("title") == "改个名字", str(patched)[:60])
    status, _ = call("PATCH", f"/api/traces/{cid}", {"title": "   "})
    check("名字不能改成空的", status == 400, status)
    status, _ = call("PATCH", "/api/traces/nope", {"pinned": True})
    check("改不存在的会话要 404（不凭空造幽灵会话）", status == 404, status)

    # 归档的会话默认不出现在列表里。前端「显示已归档」必须显式带 archived=1 ——
    # 少传这一次，用户点开开关看到的是空的，会以为归档把会话弄丢了。
    # 这是接口的默认值决定的，不是前端过滤没写对。这两条把默认值钉住。
    status, live = call("GET", "/api/traces")
    live = live if isinstance(live, list) else []
    check("默认列表里没有归档的会话",
          status == 200 and all(row.get("cid") != cid for row in live), len(live))
    status, everything = call("GET", "/api/traces?archived=1")
    everything = everything if isinstance(everything, list) else []
    check("archived=1 才拿得到归档的会话",
          status == 200 and any(row.get("cid") == cid and row.get("archived") for row in everything),
          len(everything))
    check("archived=1 拿到的不会比默认少",
          len(everything) > len(live), f"{len(live)} → {len(everything)}")

    status, impact = call("GET", f"/api/traces/{cid}/impact")
    check("删除前能拿到影响面",
          status == 200 and "changes" in impact and "checkpoints" in impact, str(impact)[:80])
    check("影响面里有会话标题", bool(impact.get("title")), impact.get("title"))
    changes_before = int(impact.get("changes") or 0)
    check("这条对话确实改过文件（下面的级联断言才有意义）", changes_before > 0, changes_before)

    status, _ = call("GET", "/api/traces/nope/impact")
    check("不存在的会话问影响面要 404", status == 404, status)

    print("\n[26] 三级下钻：对话 → 产物 → 阶段")
    status, threads = call("GET", "/api/changes/threads")
    check("第一级能列出对话",
          status == 200 and isinstance(threads.get("items"), list), str(threads)[:60])
    rows = threads.get("items") or []
    check("当前这条对话在列表里", any(row.get("cid") == cid for row in rows),
          [row.get("cid") for row in rows][:5])
    got = next((row for row in rows if row.get("cid") == cid), {})
    check("第一级带标题、文件数、改动数",
          bool(got.get("title")) and int(got.get("files") or 0) > 0, str(got)[:80])

    status, thread = call("GET", "/api/changes/thread?cid=" + urllib.parse.quote(cid))
    check("第二级能列出这条对话的产物",
          status == 200 and bool(thread.get("files")), str(thread)[:80])
    first = (thread.get("files") or [{}])[0]
    check("产物带阶段数和是否存在",
          "stages" in first and "exists" in first, str(first)[:80])
    status, _ = call("GET", "/api/changes/thread")
    check("第二级不带 cid 要 400", status == 400, status)

    victim_path = first.get("path") or ""
    status, line = call("GET", "/api/changes/timeline?cid=" + urllib.parse.quote(cid)
                        + "&path=" + urllib.parse.quote(victim_path))
    check("第三级能给出时间线",
          status == 200 and len(line.get("stages") or []) >= 2, str(line)[:80])
    check("时间线以「最初」打头",
          (line.get("stages") or [{}])[0].get("kind") == "origin", str(line)[:60])
    check("时间线里只有一个「当前」阶段",
          sum(1 for item in (line.get("stages") or []) if item.get("current")) == 1,
          [item.get("current") for item in (line.get("stages") or [])])
    status, _ = call("GET", "/api/changes/timeline?cid=" + urllib.parse.quote(cid))
    check("第三级不带 path 要 400", status == 400, status)

    status, _ = call("GET", "/api/changes/diff?cid=" + urllib.parse.quote(cid)
                     + "&path=" + urllib.parse.quote(victim_path) + "&stage=1")
    check("能按阶段看 diff", status == 200, status)
    status, _ = call("GET", "/api/changes/diff?cid=" + urllib.parse.quote(cid)
                     + "&path=" + urllib.parse.quote(victim_path) + "&stage=99")
    check("不存在的阶段给 404", status == 404, status)

    status, _ = call("POST", "/api/changes/restore",
                     {"cid": cid, "path": victim_path, "stage": 0})
    check("能退回到最初那一阶段", status == 200, status)
    status, again = call("POST", "/api/changes/restore",
                         {"cid": cid, "path": victim_path, "stage": 0})
    check("同一阶段再退一次是 409（不是静默成功）", status == 409, str(again)[:60])
    status, _ = call("POST", "/api/changes/restore", {"path": victim_path, "stage": 0})
    check("退回不带 cid 要 400", status == 400, status)
    status, _ = call("POST", "/api/changes/restore",
                     {"cid": cid, "path": victim_path, "stage": "abc"})
    check("阶段号不是整数要 400", status == 400, status)

    status, purged = call("POST", "/api/changes/purge", {})
    check("孤儿清理接口可用",
          status == 200 and "changes" in purged and "history" in purged, str(purged)[:60])

    status, gone = call("POST", f"/api/traces/{cid}/delete")
    check("删除成功", status == 200 and gone.get("ok"), str(gone)[:80])
    check("删除回执里带上了作废条数",
          isinstance(gone.get("swept"), dict)
          and int(gone["swept"].get("changes") or 0) == changes_before, gone.get("swept"))

    status, after = call("GET", "/api/changes?cid=" + urllib.parse.quote(cid))
    check("会话删掉之后，改动栏里已经没有它了",
          status == 200 and after == [], str(after)[:80])

    if mine:
        victim = mine[0]
        status, _ = call("POST", "/api/changes/revert", {"cid": cid, "path": victim["path"]})
        check("会话删掉之后，退回入口不再生效", status in (404, 409), status)
        status, _ = call("GET", "/api/changes/diff?cid=" + urllib.parse.quote(cid)
                         + "&path=" + urllib.parse.quote(victim["path"]))
        check("会话删掉之后，diff 也不再给看", status in (404, 409), status)

    status, _ = call("POST", f"/api/traces/{cid}/delete")
    check("再删一次是 404，不是 500", status == 404, status)

    status, cps = call("GET", "/api/checkpoints")
    check("检查点里不再有这条对话",
          status == 200 and all(item.get("cid") != cid for item in (cps.get("items") or [])),
          len(cps.get("items") or []))

    print("\n[27] 本机软件清单")
    status, software = call("GET", "/api/software")
    check("软件清单接口可用",
          status == 200 and int(software.get("total") or 0) > 20, str(software)[:80])
    items = software.get("items") or []
    check("清单里每一项都有名字和落点",
          all(item.get("name") and item.get("target") for item in items), len(items))
    status, found = call("GET", "/api/software?query=" + urllib.parse.quote("记事本"))
    check("按中文名能搜到", status == 200 and bool(found.get("items")), str(found)[:80])
    status, inst = call("GET", "/api/software/installed")
    check("已安装清单接口可用",
          status == 200 and isinstance(inst.get("items"), list), str(inst)[:60])
    status, bad = call("POST", "/api/software/open", {"name": "绝对不存在的软件zzz"})
    check("打开不存在的软件给 409 并说清原因",
          status == 409 and "打不开" in str(bad.get("error") or bad.get("message") or ""),
          str(bad)[:80])
    status, _ = call("POST", "/api/software/open", {})
    check("不给名字要 400", status == 400, status)

    print("\n[28] 技能：能看见、能新增、能删")
    status, items = call("GET", "/api/skills")
    check("技能清单接口可用", status == 200 and isinstance(items, list), str(items)[:60])
    names = {item.get("name") for item in items or []}
    check("内置技能在清单里", {"explain-code", "tidy-folder"} <= names, sorted(names))
    check("每一项都标了来源",
          all(item.get("source") in ("user", "builtin") for item in items or []), "")

    status, created = call("POST", "/api/skills",
                           {"name": "接口技能", "description": "接口建出来的",
                            "body": "第一步：测。\n第二步：过。"})
    check("新建技能 200", status == 200 and created.get("ok"), str(created)[:80])

    status, items = call("GET", "/api/skills")
    mine = [item for item in items or [] if item.get("name") == "接口技能"]
    # 这一条是「加了却看不见」的直接回归：接口建完立刻列出来，不用重启
    check("新建之后立刻能列出来", bool(mine), names)
    check("它被标成自己的、可改",
          bool(mine) and mine[0].get("editable") is True
          and mine[0].get("source") == "user", mine[:1])

    status, again = call("POST", "/api/skills",
                         {"name": "接口技能", "description": "x", "body": "y"})
    check("重名不覆盖时给 400",
          status == 400 and "已经有" in str(again.get("error") or ""), str(again)[:80])
    status, overwritten = call("POST", "/api/skills",
                               {"name": "接口技能", "description": "x", "body": "y",
                                "overwrite": True})
    check("带 overwrite 能覆盖", status == 200 and overwritten.get("ok"),
          str(overwritten)[:80])
    status, bad = call("POST", "/api/skills",
                       {"name": "a/b", "description": "x", "body": "y"})
    check("坏名字给 400", status == 400, str(bad)[:80])

    status, denied = call("POST", "/api/skills/explain-code/delete", {})
    check("内置技能不能删，并且说清为什么",
          status == 400 and "内置" in str(denied.get("error") or ""), str(denied)[:90])

    # ---- 开关：关掉不是删掉 ----
    quoted = urllib.parse.quote("接口技能")
    status, off = call("POST", f"/api/skills/{quoted}/toggle", {"enabled": False})
    check("技能能关掉", status == 200 and off.get("enabled") is False, str(off)[:80])
    _status, items = call("GET", "/api/skills")
    hit = [item for item in items or [] if item.get("name") == "接口技能"]
    check("关掉之后还在清单里，只是标成关",
          bool(hit) and hit[0].get("enabled") is False, hit[:1])
    status, on = call("POST", f"/api/skills/{quoted}/toggle", {"enabled": True})
    check("能再打开", status == 200 and on.get("enabled") is True, str(on)[:80])

    # 不带 enabled 就是「翻一下」—— 前端那枚开关不用先读再写
    status, flipped = call("POST", f"/api/skills/{quoted}/toggle", {})
    check("不给 enabled 就翻面", status == 200 and flipped.get("enabled") is False,
          str(flipped)[:80])
    call("POST", f"/api/skills/{quoted}/toggle", {"enabled": True})

    status, missing = call("POST", "/api/skills/" + urllib.parse.quote("根本没这个") + "/toggle",
                           {"enabled": True})
    check("给不存在的技能关开关是 404", status == 404, status)

    status, allon = call("POST", "/api/skills/enable-all", {})
    check("一键全开能用", status == 200 and allon.get("ok"), str(allon)[:80])

    status, detail = call("GET", "/api/skills/" + urllib.parse.quote("接口技能"))
    # 注意这里别断言原来那段正文：上面「带 overwrite 能覆盖」已经把正文改成 y 了，
    # 断言旧正文只会测出测试自己写错了顺序。断言「读回来的是这份 SKILL.md」。
    check("能读到全文",
          status == 200 and detail.get("name") == "接口技能"
          and "name: 接口技能" in str(detail.get("text")), str(detail)[:60])

    # 带上技能发一句：接口要收下名字，这一轮的系统提示里要有它的正文
    skill_proj = SANDBOX / "proj-http-skill"
    skill_proj.mkdir(parents=True, exist_ok=True)
    _status, skill_sess = call("POST", "/api/session",
                               {"project": str(skill_proj), "mode": "craft"})
    skill_cid = skill_sess.get("cid")
    status, sent = call("POST", f"/api/session/{skill_cid}/send",
                        {"text": "按技能来", "skills": ["接口技能"]})
    check("发送时能带上技能",
          status == 200 and "接口技能" in (sent.get("skills") or []), str(sent)[:80])

    deadline = time.time() + 25
    events: list = []
    while time.time() < deadline:
        _status, snap = call("GET", f"/api/session/{skill_cid}")
        events = snap.get("events") or []
        if any(e["type"] == "notice" and (e.get("payload") or {}).get("skills") for e in events):
            break
        if not snap.get("running"):
            break
        time.sleep(0.15)
    check("这一轮的对话里写着用了哪个技能",
          any(e["type"] == "notice" and (e.get("payload") or {}).get("skills") == ["接口技能"]
              for e in events), [e["type"] for e in events])
    call("POST", f"/api/session/{skill_cid}/cancel", {})

    # 技能可能刚被删掉：给个不存在的名字不该让整句话发不出去。
    # 等上一轮真的停下来再发 —— 不然测到的是 409，不是这里要验的东西。
    # 注意：`running` 变 false 和 `_starting` 清掉不是同一瞬间（后者在 worker 的
    # finally 里）。只等前者，偶尔会撞上一个 409，于是这条断言有时红有时绿 ——
    # 会自己变色的断言比没有断言更糟，所以这里对 409 重试，直到「真的能发」为止。
    deadline = time.time() + 20
    status, unknown = 0, {}
    while time.time() < deadline:
        _status, snap = call("GET", f"/api/session/{skill_cid}")
        if not snap.get("running"):
            status, unknown = call("POST", f"/api/session/{skill_cid}/send",
                                   {"text": "这个技能不存在", "skills": ["根本没这个"]})
            if status != 409:
                break
        time.sleep(0.2)
    check("不存在的技能名被忽略，话照样发得出去",
          status == 200 and not (unknown.get("skills") or []),
          f"status={status} {str(unknown)[:80]}")
    call("POST", f"/api/session/{skill_cid}/cancel", {})

    status, gone = call("POST", "/api/skills/" + urllib.parse.quote("接口技能") + "/delete", {})
    check("自己的技能能删", status == 200 and gone.get("ok"), str(gone)[:80])
    _status, items = call("GET", "/api/skills")
    check("删掉之后清单里就没有了",
          not any(item.get("name") == "接口技能" for item in items or []), "")

    print("\n[29] 一条在跑，另一条照样能开")
    # 服务端的 running 是按会话各存一份的，本来就不该互相挡。
    # 从前挡人的是前端那句「等这一轮跑完再开新的」，这里从接口这一层确认它不再成立。
    proj_a = SANDBOX / "proj-parallel-a"
    proj_b = SANDBOX / "proj-parallel-b"
    proj_a.mkdir(parents=True, exist_ok=True)
    proj_b.mkdir(parents=True, exist_ok=True)
    _status, sess_a = call("POST", "/api/session", {"project": str(proj_a), "mode": "craft"})
    _status, sess_b = call("POST", "/api/session", {"project": str(proj_b), "mode": "craft"})
    cid_a, cid_b = sess_a.get("cid"), sess_b.get("cid")

    status, _ = call("POST", f"/api/session/{cid_a}/send", {"text": "第一条"})
    check("第一条开跑了", status == 200, status)
    status, second = call("POST", f"/api/session/{cid_b}/send", {"text": "第二条"})
    check("第一条还在跑时，第二条也能发", status == 200, str(second)[:80])

    _status, traces = call("GET", "/api/traces?archived=1")
    check("会话列表带 running 字段",
          isinstance(traces, list) and all("running" in item for item in traces),
          str(traces)[:80])
    check("running 是布尔值，不是字符串",
          isinstance(traces, list) and all(isinstance(item.get("running"), bool)
                                           for item in traces), "")
    call("POST", f"/api/session/{cid_a}/cancel", {})
    call("POST", f"/api/session/{cid_b}/cancel", {})
    time.sleep(0.4)

    print("\n[29b] 停下是「请求」，接口得如实说")
    # 从前这个接口永远回一句 {"ok": true}，前端就照着弹「已让它停下」。
    # 可那一刻这一轮其实还活着（命令还在跑、门闩还占着），用户去打字
    # 按回车什么也不会发生 —— 他描述成「显示已停下但对话框用不了」。
    # 现在它回两个状态，前端才能说「正在停」而不是「已停」。
    proj_s = SANDBOX / "proj-stop"
    proj_s.mkdir(parents=True, exist_ok=True)
    _status, idle_sess = call("POST", "/api/session",
                              {"project": str(proj_s), "mode": "craft"})
    idle_cid = idle_sess.get("cid")
    status, idle_cancel = call("POST", f"/api/session/{idle_cid}/cancel", {})
    check("对一条没在跑的会话按停下，接口回 200", status == 200, status)
    check("并且说清楚「本来就没东西可停」（stopping=false）",
          idle_cancel.get("stopping") is False, str(idle_cancel)[:80])
    check("没有假装还在跑", idle_cancel.get("running") is False, str(idle_cancel)[:80])

    status, _ = call("POST", f"/api/session/{idle_cid}/send", {"text": "跑一下"})
    check("这条会话开跑了", status == 200, status)
    status, busy_cancel = call("POST", f"/api/session/{idle_cid}/cancel", {})
    check("在跑的时候按停下，stopping=true",
          busy_cancel.get("stopping") is True, str(busy_cancel)[:80])

    # 停下之后门闩要放开，否则下一条消息永远发不出去。
    # 重试是因为 finally 和 running=false 不是同一瞬间。
    deadline = time.time() + 15
    again = 0
    while time.time() < deadline:
        _status, snap = call("GET", f"/api/session/{idle_cid}")
        if not snap.get("running"):
            again, body = call("POST", f"/api/session/{idle_cid}/send", {"text": "接着来"})
            if again != 409:
                break
        time.sleep(0.2)
    check("停下之后立刻能接着聊（不是「不可用」）", again == 200, f"status={again}")
    if again == 200:
        call("POST", f"/api/session/{idle_cid}/cancel", {})
        time.sleep(0.3)

    print("\n[30] 两套底层：role1 / role2")
    # 这个接口存在，是因为「刚把 DSH 装上」这件事 bootstrap 感知不到 ——
    # bootstrap 一辈子只拉一次，界面上的「不可用」会一直挂着。
    status, rows = call("GET", "/api/engines")
    check("引擎清单拿得到", status == 200 and isinstance(rows, list), status)
    check("两套都在", [row.get("id") for row in rows or []] == ["role1", "role2"],
          [row.get("id") for row in rows or []])
    check("每套都说得出工具归属和审批方式",
          all(row.get("tools") and row.get("approval") for row in rows or []),
          rows)
    check("role1 恒可用", (rows or [{}])[0].get("ok") is True)
    _status, boot = call("GET", "/api/bootstrap")
    check("bootstrap 里也带着引擎（前端不用多打一次）",
          isinstance(boot.get("engines"), list) and len(boot["engines"]) == 2,
          str(boot.get("engines"))[:60])

    proj_e = SANDBOX / "proj-engine"
    proj_e.mkdir(parents=True, exist_ok=True)
    _status, s_r2 = call("POST", "/api/session",
                         {"project": str(proj_e), "mode": "craft", "engine": "role2"})
    check("建会话时可以指定 role2", s_r2.get("engine") == "role2", s_r2.get("engine"))
    check("快照里带一句人话标签", "Role 2" in str(s_r2.get("engine_label")),
          s_r2.get("engine_label"))
    _status, s_bad = call("POST", "/api/session",
                          {"project": str(proj_e), "mode": "craft", "engine": "../../etc/passwd"})
    check("乱写的引擎名被归一成 role1（不是报错，也不是安全洞）",
          s_bad.get("engine") == "role1", s_bad.get("engine"))
    _status, s_r1 = call("POST", "/api/session", {"project": str(proj_e), "mode": "craft"})
    check("不指定时按设置里的默认（role1）", s_r1.get("engine") == "role1", s_r1.get("engine"))

    _status, s_r2_full = call("GET", f"/api/session/{s_r2.get('cid')}")
    check("重新拉这条会话，引擎还是 role2",
          s_r2_full.get("engine") == "role2", s_r2_full.get("engine"))

    # 列表上必须看得出「这条是哪个内核在跑」—— 否则两套能力边界不同，
    # 用户会以为「同一句话它怎么这次不问我了」。
    #
    # 注意：会话列表是**从磁盘读的**，刚 create 还没落盘的会话本来就不在列表里。
    # 所以这里用一条真的跑过一轮（因而落过盘）的会话验，不能拿刚建的那条去查 ——
    # 那验的不是「引擎有没有标上」，而是「列表里有没有这条」，两件事。
    call("POST", f"/api/session/{s_r1.get('cid')}/send", {"text": "落一次盘"})
    deadline = time.time() + 8
    row_r1 = {}
    while time.time() < deadline:
        _status, traces2 = call("GET", "/api/traces?archived=1")
        row_r1 = next((item for item in traces2 or []
                       if item.get("cid") == s_r1.get("cid")), {})
        if row_r1:
            break
        time.sleep(0.2)
    check("落盘之后列表里有这条会话", bool(row_r1), s_r1.get("cid"))
    check("列表里标着引擎（这条是 role1）", row_r1.get("engine") == "role1",
          row_r1.get("engine"))
    check("没标的老会话默认算 role1",
          all(item.get("engine") in ("role1", "role2") for item in traces2 or []),
          [item.get("engine") for item in traces2 or []][:5])

    # 设置里能改「默认引擎」和「DSH 路径」
    status, saved = call("POST", "/api/settings", {"engine": "role2", "dsh_path": "D:/x"})
    check("设置能存引擎与 DSH 路径", status == 200 and saved.get("ok"), str(saved)[:80])
    _status, got = call("GET", "/api/settings")
    check("存下去的引擎读得回来", got.get("engine") == "role2", got.get("engine"))
    check("DSH 路径也读得回来", got.get("dsh_path") == "D:/x", got.get("dsh_path"))
    call("POST", "/api/settings", {"engine": "role1", "dsh_path": ""})

    # ---- 提示词库：存 / 列 / 改标签 / 删 ----
    print("\n[提示词库]")
    status, made = call("POST", "/api/prompts", {
        "title": "周报口径", "text": "把本周的改动按模块分组，每组一句话",
        "tags": ["周报", "写作"],
    })
    pid = (made.get("item") or {}).get("id")
    check("能把一句话存进提示词库", status == 200 and bool(pid), str(made)[:80])
    _s, listed = call("GET", "/api/prompts")
    check("列表里能读回来", any(row.get("id") == pid for row in listed.get("items") or []))
    check("标签是干净的数组",
          (listed.get("items") or [{}])[0].get("tags") == ["周报", "写作"],
          (listed.get("items") or [{}])[0].get("tags"))
    _s, dup = call("POST", "/api/prompts", {"text": "把本周的改动按模块分组，每组一句话"})
    check("同一句话收两次不会多一条",
          len((call("GET", "/api/prompts")[1].get("items") or [])) == 1,
          len((call("GET", "/api/prompts")[1].get("items") or [])))
    _s, empty = call("POST", "/api/prompts", {"text": "   "})
    check("空的不收", not empty.get("ok"), str(empty)[:60])
    status, _patched = call("PATCH", f"/api/prompts/{pid}", {"tags": ["#x", "", "y"]})
    _s, after = call("GET", "/api/prompts")
    check("改标签会把空的和井号去掉",
          ((after.get("items") or [{}])[0].get("tags")) == ["x", "y"],
          (after.get("items") or [{}])[0].get("tags"))
    status, killed = call("DELETE", f"/api/prompts/{pid}")
    check("删得掉", status == 200 and killed.get("ok"))
    _s, gone = call("GET", "/api/prompts")
    check("删完就没了", not (gone.get("items") or []))

    # ---- 删工作区：护栏比功能重要 ----
    print("\n[删工作区]")
    import tempfile as _tmp
    victim = Path(_tmp.mkdtemp(prefix="dreamrole-victim-")) / "ws1"
    victim.mkdir(parents=True)
    (victim / "a.txt").write_text("x", encoding="utf-8")
    status, bad = call("POST", "/api/workspace/trash", {"path": ""})
    check("不给路径要拒绝", status == 400, str(bad)[:60])
    status, bad = call("POST", "/api/workspace/trash", {"path": "C:/"} if os.name == "nt"
                       else {"path": "/"})
    check("盘符根不许删", status in (403, 404), f"{status} {str(bad)[:40]}")
    status, bad = call("POST", "/api/workspace/trash", {"path": str(Path.home())})
    check("用户主目录不许删", status == 403, f"{status} {str(bad)[:40]}")
    status, bad = call("POST", "/api/workspace/trash",
                       {"path": str(Path.home().anchor or "/")})
    check("主目录的祖先更不许删", status in (403, 404), f"{status} {str(bad)[:40]}")
    status, moved = call("POST", "/api/workspace/trash", {"path": str(victim)})
    # 无头环境里回收站可能不可用（容器/服务账户），那也算「如实失败」：
    # 关键是它绝不能退化成「直接删掉」。
    if status == 200:
        check("真的把目录移走了", not victim.exists(), str(victim))
    else:
        check("回收站不可用时不硬删", victim.exists(), str(moved)[:80])

    print("\n[自动化·运行记录]")
    status, run_rows = call("GET", "/api/schedules/runs")
    check("运行记录接口 200", status == 200 and isinstance(run_rows, list), str(run_rows)[:60])

    # 建一条「手动」任务：它不排班，只在你按「跑一次」的时候真开一轮 ——
    # 正好用来验证「记录是跟着真实动作写的」，而不是按时间猜的。
    status, made = call("POST", "/api/schedules", {
        "prompt": "把 out/ 里超过 7 天的文件列一下",
        "title": "清理检查", "freq": "manual", "project": str(project),
    })
    check("能建一条手动任务", status == 200 and made.get("ok"), str(made)[:80])
    job_id = ((made.get("job") or {}).get("id") or "")
    before = len((call("GET", "/api/schedules/runs")[1] or []))
    status, fired = call("POST", f"/api/schedules/{job_id}/run")
    check("手动跑一次能触发", status == 200 and fired.get("ok"), str(fired)[:80])
    time.sleep(0.6)
    status, after_rows = call("GET", "/api/schedules/runs")
    check("跑一次就多一条记录", len(after_rows) > before, f"{before} → {len(after_rows)}")
    latest = (after_rows or [{}])[0]
    check("记录上写着是哪条任务", latest.get("job") == job_id, latest)
    check("记录上写着是谁触发的（手动）", latest.get("why") == "手动", latest)
    check("记录上带着会话 id，点得回去",
          latest.get("state") == "started" and bool(latest.get("cid")), latest)
    check("记录带时间戳", (latest.get("at") or 0) > 0, latest)

    # 没有工作区的任务：到点会被跳过，而且**跳过也要留痕** ——
    # 「昨晚为什么没跑」就是靠这一行回答的。
    import app.scheduler as _sched
    lonely = _sched.add({"title": "没有工作区的一条", "prompt": "随便",
                         "project": "", "freq": "daily", "time": "09:00"})
    _sched._fire(lonely["id"], "定时")
    rows2 = call("GET", "/api/schedules/runs")[1] or []
    skipped = [r for r in rows2 if r.get("job") == lonely["id"]]
    check("跳过的也记一条", len(skipped) == 1, skipped[:1])
    check("跳过的写着原因", "工作区" in (skipped[0].get("note") or ""), skipped[:1])
    check("跳过的不假装跑了会话", skipped[0].get("state") == "skipped"
          and not skipped[0].get("cid"), skipped[:1])
    _sched.remove(lonely["id"])

    status, capped = call("GET", "/api/schedules/runs?limit=1")
    check("limit 能收窄条数", len(capped or []) <= 1, capped)

    print("\n" + "=" * 52)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for name in FAIL:
        print("  失败：" + name)
    if not FAIL:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
