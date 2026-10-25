"""拍一张「待办收起前后」的对照图，用来肉眼确认这块长什么样。

不是回归测试 —— click_check.py 已经在真鼠标层面验过行为。
这个脚本只做一件事：把 展开 / 收起 两种状态截出来，人看一眼。
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import click_check as cc          # noqa: E402  (Cdp / pick_page / kill_tree)
import shoot                      # noqa: E402

OUT = ROOT / "shots"
PORT = 9340

TODOS = [
    {"text": "摸清现状：目录、同步脚本、运行时 skill 格式", "status": "done"},
    {"text": "确定要写的 skill 及其触发场景", "status": "done"},
    {"text": "写 SKILL.md（含 agent_created 标记）", "status": "done"},
    {"text": "用 quick_validate.py 校验", "status": "done"},
    {"text": "跑 sync_skills.py 同步进运行时并验证", "status": "done"},
    {"text": "清理临时探查脚本", "status": "done"},
]


def main() -> int:
    cc.free_port(PORT)
    shoot.ensure_dirs()
    shoot.config.load()
    project = shoot.make_project()
    shoot.start_server()
    shoot.run_demo(project)
    browser = shoot.find_browser()
    if not browser:
        sys.exit("没找到 Edge / Chrome")

    profile = shoot.SANDBOX / "todo-shot-profile"
    if profile.exists():
        import shutil
        shutil.rmtree(profile, ignore_errors=True)

    url = f"http://127.0.0.1:{shoot.PORT}/?still=1"
    process = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
         "--window-size=1400,900", "--proxy-server=direct://",
         "--proxy-bypass-list=*", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    target = None
    deadline = time.time() + 25
    while time.time() < deadline and target is None:
        try:
            pages = json.loads(cc.no_proxy_open(f"http://127.0.0.1:{PORT}/json"))
            target = cc.pick_page(pages)
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    if not target:
        cc.kill_tree(process)
        sys.exit("连不上调试端口")

    cdp = cc.Cdp(target["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Network.enable")
    cdp.call("Network.setCacheDisabled", cacheDisabled=True)
    time.sleep(2.5)

    cdp.js("state.todos = " + json.dumps(TODOS, ensure_ascii=False) + ";"
           "state.todoUser = false; state.todoSig = ''; renderTodos();")
    time.sleep(0.6)
    OUT.mkdir(exist_ok=True)
    # 全部完成 + 没手动开合过 → 渲染出来就是收起的一行，这正是默认样子
    shot(cdp, OUT / "todo-folded.png", "默认（全做完自动收成一行）")
    cdp.js("document.querySelector('#todosHost .todosHead').click()")
    time.sleep(0.6)
    shot(cdp, OUT / "todo-open.png", "点标题展开")
    cdp.js("document.querySelector('#todosHost .todosHead').click()")
    time.sleep(0.6)
    folded = cdp.js("document.getElementById('todosHost').classList.contains('folded')")
    print(f"  再点一下回到收起：{folded}")

    cc.kill_tree(process)
    print("好了")
    return 0


def shot(cdp, path: Path, label: str) -> None:
    """把待办那一块连同输入框裁出来。"""
    box = cdp.js(
        "JSON.stringify((() => {"
        "  const t = document.getElementById('todosHost');"
        "  const c = document.querySelector('.composer') || t.parentElement;"
        "  const a = t.getBoundingClientRect(), b = c.getBoundingClientRect();"
        "  return {x: Math.max(0, Math.min(a.left, b.left) - 12),"
        "          y: Math.max(0, Math.min(a.top, b.top) - 12),"
        "          w: Math.min(1200, Math.max(a.width, b.width) + 24),"
        "          h: (b.bottom - Math.min(a.top, b.top)) + 24};"
        "})())"
    )
    clip = json.loads(box)
    data = cdp.call(
        "Page.captureScreenshot",
        format="png",
        captureBeyondViewport=True,
        clip={"x": clip["x"], "y": clip["y"], "width": clip["w"],
              "height": clip["h"], "scale": 1},
    )
    path.write_bytes(base64.b64decode(data["data"]))
    print(f"  {label}: {path}  {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    raise SystemExit(main())
