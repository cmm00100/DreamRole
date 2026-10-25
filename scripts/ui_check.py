"""前端自检跑一次。

做的事：起服务（假模型）→ 让页面加载 → 页面自己跑一遍 `web/js/uitest.js`
里的断言 → 把结论写进 `<html data-uitest="pass|fail">` → 这里读回来。

为什么不写单元测试：`@` 补全的判据、图投影的连边规则、命令面板的开合，
它们的输入输出都和浏览器里的真实对象有关（光标位置、QuerySelector）。
在 Python 里复制一份规则去测，测的是复制品，不是跑在用户机器上的那份。

    python scripts/ui_check.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _console import harden  # noqa: E402
from _seed_demo import seed_assistants  # noqa: E402

harden()

SANDBOX = Path(tempfile.mkdtemp(prefix="dreamrole-ui-"))
os.environ["DREAMROLE_DATA_DIR"] = str(SANDBOX / "data")
os.environ["DREAMROLE_API_KEY"] = "fake-key"
os.environ["DREAMROLE_FAKE_LLM"] = "1"

import fake_llm  # noqa: E402

fake_llm.install()

from app import config, session as sessions  # noqa: E402
from app.paths import ensure_dirs  # noqa: E402

PORT = 8806
BASE = f"http://127.0.0.1:{PORT}"

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    return next((path for path in BROWSERS if Path(path).is_file()), None)


def start_server() -> None:
    import uvicorn

    from app.server import app as fastapi_app

    threading.Thread(
        target=lambda: uvicorn.run(fastapi_app, host="127.0.0.1", port=PORT,
                                   log_level="error", access_log=False),
        daemon=True,
    ).start()

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/api/bootstrap", timeout=1).read()
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError("服务没起来")


def make_project() -> Path:
    project = SANDBOX / "ui-project"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text("# 自检项目\n", encoding="utf-8")
    (project / "src" / "main.py").write_text("print('x')\n", encoding="utf-8")
    return project


def seed_session(project: Path) -> None:
    config.update({"permission": "full", "mode": "craft"})
    sess = sessions.create(str(project), mode="craft", permission="full")
    thread = threading.Thread(target=lambda: sess.run_turn("看看这个项目"), daemon=True)
    thread.start()
    thread.join(timeout=60)
    while sess.running:
        time.sleep(0.1)
    # 再跑一轮，留下两个不会被删掉的产物 —— 只有一条「已不在」的记录时，
    # 「按钮上的数字和列表条数一致」这种断言等于没测（1 对 1 恒成立）。
    fake_llm.run_keep_turn(sess)


def seed_dead_change(project: Path) -> None:
    """造一条「对话已经不在、改动记录还留着」的记录。

    这不是为了凑测试数据：它是真实会发生的状态 —— 用户在资源管理器里
    删掉了 data 里的轨迹文件，或者把别人的 data 目录拷了过来。
    没有这个种子，「失效态的按钮是禁用的」那条断言永远是空转。
    """
    from app import store

    ghost = SANDBOX / "ghost.txt"
    ghost.write_text("这是被删掉的对话改过的文件\n", encoding="utf-8")

    store.save_trace("ghost-cid", [], {"cid": "ghost-cid", "title": "已经删掉的对话",
                                       "project": str(project)})
    store.record_change(str(ghost), "", "这是被删掉的对话改过的文件\n",
                        "ghost-cid", str(project), turn=1, existed=False)

    # 只抹文件，不走级联 —— 模拟「外部删除」，记录因此变成孤儿
    for path in store.delete_trace("ghost-cid"):
        path.unlink(missing_ok=True)


def seed_archived(project: Path) -> str:
    """再造一条**已归档**的会话，但保留它。

    为什么非要有这条：接口默认 `archived=0`，所以「显示已归档」这个开关
    很容易写成「点了没反应」—— 数据根本没到前端，过滤再对也是空的。
    没有归档会话的种子，前端自检里关于它的断言全部是空转。
    """
    from app import store

    cid = "archived-ui-cid"
    store.save_trace(cid, [], {"cid": cid, "title": "归档了但还在的会话",
                               "project": str(project), "created": time.time() - 60,
                               "archived": True})
    return cid


def seed_tree(project: Path) -> None:
    """给左栏的树凑出「不止一个分组」。

    一棵按工作区分组的树，只有一条会话时是验不出东西的：分组、计数、收起
    这些全部退化成 1。这里补两条 —— 一条没选过目录（进「默认」），一条在
    另一个目录（第二个工作区）。两条都不带事件，点开是空会话，但树只看 meta。
    """
    from app import store

    other = SANDBOX / "另一处工作区"
    other.mkdir(parents=True, exist_ok=True)
    store.save_trace("tree-loose-cid", [], {"cid": "tree-loose-cid",
                                            "title": "没选工作区的会话"})
    store.save_trace("tree-other-cid", [], {"cid": "tree-other-cid",
                                            "title": "另一个工作区里的会话",
                                            "project": str(other)})
    # 时间往回拨。`save_trace` 把 updated 写成「现在」，这两条就会比演示会话
    # 还新 —— 页面默认打开最新的那条，于是「这个对话的产物」验的变成一条空会话
    # （它当然没有任何产物）。这是踩过的坑：加种子数据把别人的断言弄红。
    for index, cid in enumerate(("tree-loose-cid", "tree-other-cid")):
        when = time.time() - 3600 * (index + 3)
        store.patch_meta(cid, created=when, updated=when, events=0)


def seed_schedules(project: Path) -> None:
    """给自动化页放两条任务（一条定时、一条间隔），外加一条暂停的。

    没有种子的话，那一页永远是空态 —— 而空态是「最不会坏」的那种状态：
    卡片、开关、筛选全都没被画出来，断言全在空转。
    """
    from app import scheduler

    daily = scheduler.add({"title": "每日选题", "prompt": "挑三条争议最大的热搜，写成一稿",
                           "project": str(project), "freq": "daily", "time": "09:00"})
    scheduler.add({"title": "每两小时自检", "prompt": "跑一遍自检，失败就总结原因",
                   "project": str(project), "freq": "interval", "minutes": 120})
    # 一条暂停的：筛选条上的「已暂停」才有东西可数
    scheduler.update(daily["id"], {"enabled": False})


def dump_dom(browser: str) -> str:
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={SANDBOX / 'browser'}",
        # 按截图那个尺寸跑：默认的 800×600 会把「底部状态条被切掉」这类问题
        # 挡在门外 —— 窗口高一点，一行都放不下的时候才看得出来。
        "--window-size=1600,900",        # 虚拟时间预算。自检里现在有真回合（建助理 → 建群 → 发一句 → 等群成员说话），
        # 预算给小了会在「还没跑完」的时候就把 DOM dump 出来，
        # 结果读成「页面没有跑自检脚本」—— 那是超时，不是失败。
        "--virtual-time-budget=30000",
        "--dump-dom",
        BASE + "/?still=1&uitest=1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        out, _ = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        out, _ = process.communicate()
    return (out or b"").decode("utf-8", "replace")


def main() -> int:
    browser = find_browser()
    if not browser:
        print("没找到 Edge / Chrome，跳过前端自检")
        return 0

    ensure_dirs()
    config.load()
    project = make_project()
    start_server()
    seed_session(project)
    seed_dead_change(project)
    seed_archived(project)
    seed_tree(project)
    seed_schedules(project)
    seed_assistants(str(project))

    html = dump_dom(browser)
    if not html:
        print("拿不到页面 DOM")
        return 1

    verdict = re.search(r'data-uitest="(\w+)"', html)
    summary = re.search(r'data-uitest-summary="([^"]*)"', html)
    failures = re.search(r'data-uitest-failures="([^"]*)"', html)
    report = re.search(r'<pre id="uitestReport"[^>]*>(.*?)</pre>', html, re.S)

    if report:
        text = report.group(1)
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        print(text.rstrip())

    if not verdict:
        print("页面没有跑自检脚本（可能是 JS 报错，或者脚本没加载上）")
        return 1

    if verdict.group(1) != "pass":
        print("\n失败项：" + (failures.group(1) if failures else "未知"))
        return 1

    print("\n" + (summary.group(1) if summary else "全部通过"))
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
