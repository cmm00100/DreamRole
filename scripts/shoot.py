"""截一张界面的图，用来人工看一眼有没有画错。

它做的事：造一个假项目 → 起服务 → 让 agent 真的跑一轮 →
用 Edge 无头模式打开页面截图。因为是真的跑了一轮，所以截图里的内容
和用户第一次用时会看到的一致。

    python scripts/shoot.py            # 截对话页（假模型，秒出）
    python scripts/shoot.py --graph    # 截过程图
    python scripts/shoot.py --live     # 用真模型跑一轮再截（花钱，但看得见真实效果）

产物在 screenshots/ 下。
"""
from __future__ import annotations

import os
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

# 真模型模式：不装假模型，直接读本机已经配好的 key。
# 注意这里**不打印任何密钥**，也不把它写进任何文件。
LIVE = "--live" in sys.argv

SANDBOX = Path(tempfile.mkdtemp(prefix="dreamrole-shot-"))
os.environ["DREAMROLE_DATA_DIR"] = str(SANDBOX / "data")
os.environ["DREAMROLE_API_KEY"] = "fake-key"
if not LIVE:
    os.environ["DREAMROLE_FAKE_LLM"] = "1"

if not LIVE:
    import fake_llm  # noqa: E402

    fake_llm.install()
else:
    # 把真实设置搬进沙盒：脚本里不出现密钥，但服务能读到它
    real = ROOT / ".data" / "settings.json"
    if not real.is_file():
        sys.exit("没有找到 .data/settings.json，先在软件里配好模型再跑 --live")
    target = Path(os.environ["DREAMROLE_DATA_DIR"])
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real, target / "settings.json")

from app import config, session as sessions, skills  # noqa: E402
from app.paths import ensure_dirs  # noqa: E402

PORT = 8802
BASE = f"http://127.0.0.1:{PORT}"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    for path in EDGE_CANDIDATES:
        if Path(path).is_file():
            return path
    return None


def make_project() -> Path:
    project = SANDBOX / "demo-project"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text(
        "# 留言板\n\n一个练手用的小项目。\n\n## 怎么跑\n\n```\npython src/main.py\n```\n",
        encoding="utf-8",
    )
    (project / "src" / "main.py").write_text(
        "from store import Store\n\n\ndef main():\n    store = Store()\n"
        "    for note in store.all():\n        print(note)\n\n\nif __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    (project / "src" / "store.py").write_text(
        "class Store:\n    def __init__(self):\n        self.items = []\n\n"
        "    def all(self):\n        return self.items\n",
        encoding="utf-8",
    )
    return project


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


def run_demo(project: Path) -> str:
    config.update({"permission": "staged", "mode": "teach"})
    # 走界面建会话时后端会记「最近用过」；这里手建，补记一下，
    # 截图里的选目录弹窗才不会缺那一行。
    config.remember_project(str(project))
    sess = sessions.create(str(project), mode="teach", permission="standard")

    prompt = ("带我看看这个项目，先认目录，再读一下 src/store.py，"
              "最后把 README 里的「怎么跑」补上。一步一步来。")
    if not LIVE:
        prompt = "带我看看这个项目，把说明文件补一下"

    drive_turn(sess, prompt)
    return sess.cid


def drive_turn(sess, prompt: str) -> None:
    """跑一轮，并替用户把中途的审批都点掉。"""
    thread = threading.Thread(target=sess.run_turn, args=(prompt,), daemon=True)
    thread.start()

    deadline = time.time() + (240 if LIVE else 40)
    while time.time() < deadline:
        with sess._cond:
            pending = list(sess._pending.values())
        if pending:
            request = pending[0]
            sess.answer(request["id"], "continue" if request["kind"] == "stage" else "allow")
            time.sleep(0.2)
            continue
        if not thread.is_alive():
            break
        time.sleep(0.2)
    thread.join(timeout=5)


def run_second_turn(cid: str) -> None:
    """再派一句话。

    流程图是按「轮」分泳道的 —— 只有一轮的会话画出来是一条道飘在一大片空画布上，
    看不出「泳道」这回事。截图要说明的事，得让它自己站得住。
    """
    sess = sessions.get(cid)
    if sess is None:
        return
    drive_turn(sess, "刚才那步里 store.py 是怎么存的？顺手把注释补一句。")


def run_artifact_demo(project: Path, cid: str) -> None:
    """再跑一轮，让这一条会话真的留下几个没被删掉的产物。

    默认那套剧本最后会把自己写的文件删掉（那是为了验证回收站），
    产物面板里就只剩一条「已经不在」的记录 —— 截图看不出效果。
    """
    if LIVE:
        return   # 真模型模式下默认那一轮本来就会留下文件
    import fake_llm

    fake_llm.run_keep_turn(sessions.get(cid))


def shoot(url: str, name: str, height: int = 1000, budget: int = 8000) -> Path | None:
    browser = find_browser()
    if not browser:
        print("没找到 Edge / Chrome，跳过截图")
        return None

    out_dir = ROOT / "screenshots"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"{name}.png"
    if target.exists():
        target.unlink()

    profile = SANDBOX / "browser-profile"
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        "--force-device-scale-factor=1",
        # 无头模式下的 CSS 动画走的是真实时钟，而截图发生在虚拟时钟到点时 ——
        # 入场动画常常只播了一半，抓出来的卡片是半透明的。要的是「布局对不对」，
        # 不是「第三帧长什么样」，所以直接按「减少动态效果」取一张定妆照。
        "--force-prefers-reduced-motion",
        f"--window-size=1600,{height}",
        f"--screenshot={target}",
        f"--virtual-time-budget={budget}",
        url,
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        # 无头浏览器有时不肯自己退出，但只要图已经写出来了就算成功
        process.kill()
        time.sleep(0.5)

    if target.is_file():
        print(f"截图：{target}")
        return target
    print("截图失败")
    return None


_SEED_JOBS: dict = {}


def seed_schedules(project: Path) -> None:
    """给自动化页放三条任务（定时 / 间隔 / 暂停），否则截出来是一屏空态。"""
    from app import scheduler

    daily = scheduler.add({"title": "每日选题与发布", "prompt":
                           "挑三条争议最大的热搜，各写一稿 500 字，配好图存到 out/ 下",
                           "project": str(project), "freq": "daily", "time": "09:00"})
    scheduler.update(daily["id"], {"runs": 12, "note": "上次成功",
                                   "last_run": time.time() - 7200})
    hourly = scheduler.add({"title": "每两小时自检", "prompt":
                            "跑一遍自检，失败了把原因总结成一段话",
                            "project": str(project), "freq": "interval", "minutes": 120})
    weekly = scheduler.add({"title": "周一汇总上周改动", "prompt":
                            "把上周所有会话里的改动整理成周报，存成 周报.md",
                            "project": str(project), "freq": "weekly", "weekday": 1,
                            "time": "09:30"})
    scheduler.update(weekly["id"], {"enabled": False, "runs": 3})
    _SEED_JOBS.update({"daily": daily, "hourly": hourly, "weekly": weekly})


def seed_schedule_runs(cid: str) -> None:
    """给「运行记录」放几行真的记录。

    为什么非要造：一屏空态看不出这一格是干什么的；而假数据只要不假装
    「耗时 / token」这类算不出来的东西，就还是在说实话 —— 每一行都是
    一次真实的触发结果形状（开了会话 / 跳过 / 启动失败）。
    """
    from app import scheduler

    daily, hourly = _SEED_JOBS.get("daily"), _SEED_JOBS.get("hourly")
    if not daily:
        return
    rows = [
        (daily, "定时", "started", cid, "", 2.1),
        (hourly, "定时", "skipped", "", "上一次还没跑完", 4.0),
        (daily, "手动", "started", cid, "", 26.0),
        (daily, "定时", "started", cid, "", 50.0),
        (hourly, "定时", "failed", "", "启动失败：模型没配", 73.0),
    ]
    for job, why, state, run_cid, note, hours in rows:
        row = scheduler.record_run(job, why, state, cid=run_cid, note=note)
        # record_run 记的是「现在」，这里只想让截图里的时间戳散开
        row["at"] = time.time() - 3600 * hours
        with scheduler._lock:
            for item in scheduler._load_runs():
                if item.get("id") == row["id"]:
                    item["at"] = row["at"]
            scheduler._save_runs()


def seed_prompts() -> None:
    """给提示词库放几条真的收藏。

    空收藏夹截出来只有一个空态，看不出「行内长什么样」—— 而那正是设计稿
    D 段（提示词库）要说明的东西：标题、一句说明、标签、插入按钮各在哪。
    这几条用的是这个软件自己常用的说法，不是设计稿里那三条原文。
    """
    from app import prompts as library

    rows = [
        ("代码审查（结构化）", "先列出改动点，再按「能不能跑 / 有没有边界问题 / 有没有更简单的写法」"
                              "三段说，每段不超过四行，最后给一句结论。", ["审查", "结构化"]),
        ("查清一个报错再动手", "把这个报错当线索：先复现，再定位到具体文件和行，"
                              "把原因写成一句话给我看，我点头你再改。", ["排错", "先问后做"]),
        ("长文压缩成要点", "把下面这段压成不超过十条要点，每条一句，"
                          "保留数字和专有名词，不要加评论。", ["总结"]),
        ("换个说法再讲一遍", "刚才那段我没懂。换成给一个新同事讲的方式重讲，"
                            "少用术语，多举一个具体例子。", ["解释"]),
    ]
    for title, text, tags in rows:
        library.add(title, text, tags=tags, source="收藏")

    # 让「共 N 条」看起来像用过一阵子，而不是刚装的
    backend = library.FILE
    if backend.is_file():
        import json
        data = json.loads(backend.read_text(encoding="utf-8"))
        for index, row in enumerate(data):
            row["created"] = time.time() - 3600 * (index + 1) * 9
        backend.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    library._cache = None


def seed_dead_change(project: Path) -> None:
    """造一条「对话已经不在、改动记录还留着」的记录。

    截图要能说明「失效态长什么样」，就得真的有失效的记录。
    这不是为了让图好看 —— 它是用户把 data 目录里某个轨迹文件删掉之后
    真实会出现的状态，而那种状态平时截不到。
    """
    from app import store

    ghost = SANDBOX / "ghost.txt"
    ghost.write_text("被删掉的对话改过这个文件\n", encoding="utf-8")
    store.save_trace("ghost-cid", [], {"cid": "ghost-cid", "title": "已经删掉的对话",
                                       "project": str(project)})
    store.record_change(str(ghost), "", "被删掉的对话改过这个文件\n",
                        "ghost-cid", str(project), turn=1, existed=False)
    for path in store.delete_trace("ghost-cid"):     # 只抹文件，不走级联
        path.unlink(missing_ok=True)


def seed_archived(project: Path) -> None:
    """造两条已经归档的会话。

    为什么截图里非要它们：归档会话默认不在列表里（接口 `archived=0`），
    所以「显示已归档」这个开关曾经点下去什么都没发生 —— 图上看就是「点了没反应」。
    要拍到「开关打开后归档的会话确实回来了」，就得先有归档的会话。
    """
    from app import store

    for index, title in enumerate(("上周的接口梳理", "早就收尾的排查")):
        cid = f"archived-shot-{index}"
        store.save_trace(cid, [], {"cid": cid, "title": title,
                                   "project": str(project), "archived": True})
        # save_trace 会把 updated 写成「现在」、events 写成 0（它就是照着轨迹文件算的）。
        # 想让截图里的旧会话看起来像躺了两三天，只能事后再补一次 meta。
        when = time.time() - 86400 * (index + 2)
        store.patch_meta(cid, created=when, updated=when, events=6 + index * 5)


def seed_grants(cid: str, project: Path) -> None:
    """给演示会话挂两张通行证，纯粹为了截图能看清那一块。

    选的是「看起来真实、但不会暴露个人目录」的两个路径：演示项目的上一层，
    和系统里的「公用文档」。没插盘的时候 getmappeddrives 那种更真实，但
    截图不该因为机器不同而长得不一样。
    """
    from app import session as session_mod

    sess = session_mod.get(cid)
    if sess is None:
        return
    sess.update_grants("add", str(project.parent), "ro")
    public = Path(os.environ.get("PUBLIC") or r"C://Users//Public") / "Documents"
    if public.is_dir():
        sess.update_grants("add", str(public), "rw")


def seed_tree(project: Path) -> None:
    """把左栏的会话树喂成「用过一阵子」的样子。

    为什么非要造：一棵按工作区分组的树，只有一条会话的时候是截不出效果的 ——
    「默认」里一条、两个工作区各几条、外加一条置顶，才能看出分组、计数、
    折叠箭头、行尾时间是不是都在对的位置上。

    这些是**纯粹的展示数据**（只写 meta，不写事件），所以对话区点开是空的。
    截图只截左栏，正好不需要内容。
    """
    from app import store

    peer = SANDBOX / "exam-system-v2"
    peer.mkdir(parents=True, exist_ok=True)
    rows = [
        # (cid, 标题, 工作区, 几个小时前, 置顶)
        ("tree-a1", "自定义 agent 需求提问", "", 49, False),
        ("tree-a2", "AI 助手与 WorkBuddy 对比", "", 6, False),
        ("tree-b1", "主界面重设计 v2", str(project), 0.05, False),
        ("tree-b2", "构建专属工作台：左栏与对话页", str(project), 2, False),
        ("tree-b3", "把已安装的 skill 塞进输入框", str(project), 26, False),
        ("tree-c1", "项目全面体检与修复 bug", str(peer), 30, False),
        ("tree-c2", "考试系统 UI 无头验收", str(peer), 74, False),
        ("tree-p1", "这条置顶了：上线前要过的清单", str(peer), 9, True),
    ]
    for cid, title, where, hours, pinned in rows:
        store.save_trace(cid, [], {"cid": cid, "title": title,
                                   "project": where, "pinned": pinned,
                                   "engine": "role2" if cid.endswith("b2") else "role1"})
        when = time.time() - 3600 * hours
        store.patch_meta(cid, created=when, updated=when, events=12)


def seed_skill_off() -> None:
    """关掉一个技能，截「开关真的关了一个」那张图。

    关掉的状态必须落到 settings.json 里才会被界面读到 —— 只在内存里改，
    服务进程是另一份，截图拍出来还是全开的。
    """
    items = skills.catalog()
    if len(items) < 2:
        return
    skills.set_enabled(items[1]["name"], False)


def main() -> int:
    graph = "--graph" in sys.argv
    panel = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--panel=")), "")
    section = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--section=")), "")
    ensure_dirs()
    config.load()
    if "--skilloff" in sys.argv:
        seed_skill_off()
    if section in ("assistants", "security", "runtime") or "--assistants" in sys.argv:
        # 带上工作区：没有工作区，群聊那一轮会被「没选工作区就不许跑」挡在门口，
        # 于是团队页截出来永远是一屏「这个群还没说过话」。
        seed_assistants(str(SANDBOX / "demo-project"))
    if section in ("changes", "checkpoints"):
        seed_dead_change(SANDBOX / "demo-project")
    if section == "automation" or panel == "schedules":
        seed_schedules(SANDBOX / "demo-project")
    project = make_project()
    if "--archived" in sys.argv:
        seed_archived(project)
    if "--tree" in sys.argv:
        seed_tree(project)
        # 中栏头那几枚胶囊（几个智能体 / 几个定时任务）和右栏的「自动化」卡，
        # 只有真的种出来才画得出来。截「用过一阵子」的图，就得真的有这些东西。
        seed_assistants(str(project))
        seed_schedules(project)
    start_server()
    cid = run_demo(project)
    print(f"演示会话：{cid}")
    if section == "automation" or panel == "schedules":
        seed_schedule_runs(cid)

    if "--tree" in sys.argv:
        # 树是「视口高度」约束的：条目多了要靠滚动，而截图只拍第一屏。
        # 造的数据刚好一屏放得下，所以不用 --tall。
        shoot(BASE + "/?still=1", "tree", height=900)
    elif "--artifacts" in sys.argv:
        run_artifact_demo(project, cid)
        shoot(BASE + "/?still=1&artifacts=1", "artifacts")
    elif "--sched" in sys.argv:
        # 新建自动化的表单：三段式，需要真的种出技能才有得挂
        shoot(BASE + "/?still=1&panel=automation&newSchedule=1", "automation-editor")
    elif "--plus" in sys.argv:
        # 「＋」菜单的八项：设计稿 12 号文档 C 表
        shoot(BASE + "/?still=1&plus=1", "plus-menu")
    elif any(arg.startswith("--ref=") for arg in sys.argv) or "--ref" in sys.argv:
        kind = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--ref=")), "ws")
        if kind == "lib":
            seed_prompts()
        shoot(BASE + f"/?still=1&refPicker={kind}", "ref-" + kind, budget=12000)
    elif "--wsdel" in sys.argv:
        shoot(BASE + "/?still=1&wsDelete=1", "workspace-delete", budget=12000)
    elif "--chips" in sys.argv:
        # 指令条上的 @ chip（设计稿 12 号文档 A3 的「已选」态）
        shoot(BASE + "/?still=1&demoChips=1", "ref-chips")
    elif "--dock" in sys.argv:
        # 日志抽屉：跑完一轮之后里面才有东西（工具判定、审批、写文件）
        shoot(BASE + "/?still=1&dock=1", "dock")
    elif "--archived" in sys.argv:
        # 开关是打开的状态：列表里要能同时看到普通会话和带「已归档」标签的会话
        shoot(BASE + "/?still=1&archived=1", "archived")
    elif section:
        url = f"{BASE}/?still=1&section={section}"
        # 侧栏是「视口高度」约束的，所以想看更长的栏（比如五十多个工具）只能把
        # 视口调高 —— 这不是在取巧，它就是「用户屏幕更高时看到的样子」。
        # 团队页要多等一会儿：它要拉 /api/traces，再按最近那条会话去拉一次事件。
        # 8 秒的虚拟时间预算里这两跳经常还没回来，截出来就是「这个群还没说过话」+
        # 「会话 0」—— 版式是对的，数据是空的，最容易让人以为页面坏了。
        shoot(url, "side-" + section,
              height=1900 if "--tall" in sys.argv else 1000,
              budget=30000 if section == "assistants" else 8000)
    elif "--pick" in sys.argv:
        # 不带 fresh：先打开刚才那条演示会话，弹窗里才有「现在用的是」那一行 ——
        # 那才是用户平时点顶栏胶囊看到的样子。首次选（三样只剩两样）是 no-workspace.png。
        # 顺手挂两张通行证：截图里要能看见「还能去的地方」长什么样，
        # 否则这一块只能靠文字描述。
        seed_grants(cid, project)
        shoot(BASE + "/?still=1&pick=1", "pick-workspace")
    elif "--fresh" in sys.argv:
        # 不自动打开旧会话，看到的就是「新会话 + 还没选工作区」
        shoot(BASE + "/?still=1&fresh=1", "no-workspace")
    elif panel:
        au = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--au=")), "")
        url = f"{BASE}/?still=1&panel={panel}"
        if au:
            url += f"&auTab={au}"
        # 面板也是「视口高度」约束的。设置页现在一节比一节长（模型 → 行为 → 上下文与记忆 →
        # 个性化），默认 1000 高度会把「上下文与记忆」拦腰截断 —— 而那正好是这版要给人看的东西。
        shoot(url, "panel-" + panel + (("-" + au) if au else ""),
              height=1900 if "--tall" in sys.argv else 1000)
    elif graph:
        card = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--card=")), "")
        gran = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--gran=")), "")
        suffix = f"&card={card}" if card else ""
        if gran:
            suffix += f"&gran={gran}"
        run_second_turn(cid)
        shoot(BASE + f"/?still=1{suffix}#graph",
              "graph" + (f"-card{card}" if card else "") + (f"-{gran}" if gran else ""))
    else:
        shoot(BASE + "/?still=1", "chat")
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
