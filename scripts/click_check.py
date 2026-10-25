"""真点一下：用真实的鼠标事件验证「点卡片能打开」。

为什么不能只靠 `ui_check.py`：那段自检用的是 `dispatchEvent(new PointerEvent(...))`，
合成事件**不会真的捕获指针**，所以「pointerdown 抢了指针 → click 被改派到 svg →
卡片永远收不到 click」这个 bug 在自检里是隐形的。它就是这么发生过一次：
自检全绿，真机上点卡片没反应。

这里走 Chrome DevTools Protocol 的 `Input.dispatchMouseEvent` —— 它和真鼠标走同一条
输入管线（命中测试、指针捕获、click 派发全都一样），所以能测出那条路。

    python scripts/click_check.py          # 只跑断言
    python scripts/click_check.py --shot   # 顺便存一张 screenshots/click-open.png
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _console import harden  # noqa: E402

harden()

# shoot 在 import 时就会：装假模型、把数据目录指到临时目录、造一个演示项目。
# 这里要的正是这一套，所以直接借它，不复制一遍。
import shoot  # noqa: E402

PORT = 9333

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    sys.exit("缺 websocket-client：pip install websocket-client")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  [ok]   {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


def no_proxy_open(url: str, timeout: float = 2.0) -> bytes:
    """本机地址必须绕过系统代理 —— 否则 127.0.0.1 会被代到 502。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout).read()


def pick_page(pages: list[dict]) -> dict | None:
    """从调试端口的目标里挑出「真正是 DreamRole 的那个页面」。

    不能直接取第一个 page 目标：Edge 登录过微软账号时，新窗口会先开到
    `edge://sync-confirmation-dialog/`，它同样是 type=page、同样有调试地址。
    挑错了的话，后面的鼠标事件全打在一个同步弹窗上，看起来像整个界面坏了。
    """
    pages = [p for p in pages if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    needle = f"127.0.0.1:{shoot.PORT}"
    for page in pages:
        if needle in str(page.get("url") or ""):
            return page
    return pages[0] if pages else None


class Cdp:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=15)
        self.seq = 0

    def call(self, method: str, **params) -> dict:
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                if "error" in message:
                    raise RuntimeError(f"{method} 失败：{message['error']}")
                return message.get("result") or {}

    def js(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=False,
        )
        return (result.get("result") or {}).get("value")

    def mouse(self, kind: str, x: float, y: float) -> None:
        self.call(
            "Input.dispatchMouseEvent",
            type=kind,
            x=x,
            y=y,
            button="left",
            buttons=1 if kind != "mouseReleased" else 0,
            clickCount=1 if kind in ("mousePressed", "mouseReleased") else 0,
        )

    def tap(self, x: float, y: float) -> None:
        self.mouse("mouseMoved", x, y)
        self.mouse("mousePressed", x, y)
        self.mouse("mouseReleased", x, y)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def center_of(cdp: Cdp, selector: str, index: int = 0) -> dict | None:
    raw = cdp.js(
        "JSON.stringify((() => {"
        f"  const el = document.querySelectorAll({selector!r})[{index}];"
        "  if (!el) return null;"
        "  const r = el.getBoundingClientRect();"
        "  return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        "})())"
    )
    return json.loads(raw) if raw else None


def wait_cards(cdp: "Cdp", timeout: float = 20) -> int:
    """等过程图把卡片画出来，返回画出来的张数（0 = 一直没画）。"""
    deadline = time.time() + timeout
    cards = 0
    while time.time() < deadline:
        cards = cdp.js("document.querySelectorAll('.gCard').length") or 0
        if cards:
            return cards
        time.sleep(0.3)
    return cards


def kill_tree(process) -> None:
    """把浏览器**整棵树**杀掉。

    `process.kill()` 只杀启动器，`--headless=new` 拉起来的那几个子进程会留下来，
    继续占着 9333。下一次跑的时候新浏览器连不上自己的调试端口，CDP 于是接到了
    **上一轮那个还活着的浏览器**上 —— 页面是旧的、没有新加的元素，
    断言就报「找不到 #skillBtn」，看起来像界面坏了。

    这个坑真踩到了：改完 HTML 反复报「按钮不存在」，查了半天浏览器缓存、
    中间件、静态目录，最后是 netstat 里 9333 还挂着上一轮的进程。
    """
    if process is not None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True)
        try:
            process.wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass


def free_port(port: int) -> None:
    """开跑之前先把占着调试端口的旧进程清掉。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             errors="replace", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        if f":{port} " not in line:
            continue
        pid = line.split()[-1]
        if pid.isdigit() and pid != "0":
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)


def main() -> int:
    free_port(PORT)          # 上一轮如果留了个浏览器挂着 9333，先清掉
    shoot.ensure_dirs()
    shoot.config.load()
    project = shoot.make_project()
    shoot.start_server()
    cid = shoot.run_demo(project)
    print(f"演示会话：{cid}")

    browser = shoot.find_browser()
    if not browser:
        sys.exit("没找到 Edge / Chrome")

    profile = shoot.SANDBOX / "cdp-profile"
    # 每次从干净的 profile 开始。上一次留下的 HTTP 缓存会让页面还是旧的那份 ——
    # 于是「新加的按钮找不到」，看起来像界面坏了，其实是缓存。
    if profile.exists():
        import shutil

        shutil.rmtree(profile, ignore_errors=True)
    url = f"http://127.0.0.1:{shoot.PORT}/?still=1#graph"
    process = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={PORT}",
            # 新版 Chromium 会拒掉带 Origin 的 CDP 连接（websocket-client 默认带）
            "--remote-allow-origins=*",
            "--window-size=1400,900",
            # 走 CDP 的时候最忌讳代理：本机回环被代走，连都连不上
            "--proxy-server=direct://",
            "--proxy-bypass-list=*",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    target = None
    deadline = time.time() + 25
    while time.time() < deadline and target is None:
        try:
            pages = json.loads(no_proxy_open(f"http://127.0.0.1:{PORT}/json"))
            target = pick_page(pages)
        except Exception:  # noqa: BLE001
            time.sleep(0.3)

    if not target:
        kill_tree(process)
        sys.exit("连不上调试端口")

    cdp = Cdp(target["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    # 关掉缓存：断言打的是「磁盘上这份界面」，不是 Chromium 昨天存下来的那份
    cdp.call("Network.enable")
    cdp.call("Network.setCacheDisabled", cacheDisabled=True)

    # 连上之后**先导航一次**，不管当前页面是什么。理由：
    # 1) 第一个 page 目标可能根本不是我们的页面（Edge 登录过账号时是同步弹窗，
    #    在那里点鼠标只会得到一堆「界面坏了」的假象）；
    # 2) 导航之后页面处在一个明确的状态，不用猜它是怎么被打开的。
    cdp.call("Page.navigate", url=url)
    time.sleep(0.8)

    # 等图真的画出来（demo 会话跑完、切到过程图页）
    cards = wait_cards(cdp, timeout=20)

    print(f"\n图上有 {cards} 张卡")
    check("卡片画出来了", cards > 0, cards)

    if cards:
        drawer_open = "(() => { const d = document.getElementById('cardDrawer');" \
                      "  return !!d && !d.classList.contains('hidden'); })()"

        first = center_of(cdp, ".gCard", 0)
        check("拿到第一张卡的位置", bool(first), first)
        if not first:
            cdp.close()
            kill_tree(process)
            return 1

        # 本机实测：连着调试端口时，页面加载后大约 1 秒，第一张卡会自己打开
        # （同样的 URL 用 `--screenshot` 无人值守截一张，抽屉是关着的 ——
        #  所以这是「headless + CDP」这一侧的现象，不是界面自己会乱开）。
        # 这一组断言要测的是「点一下会怎样」，所以先把它复位，并在输出里说清楚，
        # 而不是让「一开始是收起的」这条随机变红、看起来像界面坏了。
        if cdp.js(drawer_open) is True:
            print("  [warn] 加载后抽屉已经是开的（headless + CDP 的现象），先收起再测")
            cdp.js("GraphView.close()")
            time.sleep(0.25)

        check("一开始抽屉是收起的", cdp.js(drawer_open) is False)

        # 记录真实 click 落在谁身上。指针一旦被 svg 捕获，click 会被改派到 svg，
        # 卡片上的 click 就永远收不到 —— 这里直接盯住这一点，别再靠猜。
        cdp.js(
            "window.__clicks = [];"
            "document.addEventListener('click', (e) => {"
            "  const t = e.target;"
            "  const card = t.closest ? t.closest('.gCard') : null;"
            "  window.__clicks.push({"
            "    cls: (t.getAttribute && t.getAttribute('class')) || '',"
            "    tag: t.tagName,"
            "    inCard: !!card,"
            "  });"
            "}, true);"
        )

        # 真鼠标点第一张卡
        cdp.tap(first["x"], first["y"])
        time.sleep(0.45)
        check("鼠标点一下就弹出抽屉", cdp.js(drawer_open) is True)

        clicks = json.loads(cdp.js("JSON.stringify(window.__clicks)") or "[]")
        # 落在卡片里的**任何**元素上都算数：卡上有一堆 svg/text，
        # 点中心大概率正好压在标题字上。真正的失败是「被改派给 svg 画布」，
        # 那才会 closest('.gCard') 为空。
        check("真点一下，click 落在卡片上（没被指针捕获改派给 svg）",
              any(item.get("inCard") for item in clicks), clicks)
        check("打开状态记在了正确的卡上",
              cdp.js("GraphView.debug().open") == cdp.js(
                  "document.querySelectorAll('.gCard')[0].dataset.id"))
        check("抽屉里有内容", (cdp.js(
            "document.querySelectorAll('#cardDrawer .cardEvent').length") or 0) > 0)

        if "--shot" in sys.argv:
            data = cdp.call("Page.captureScreenshot", format="png").get("data")
            out = ROOT / "screenshots" / "click-open.png"
            out.write_bytes(base64.b64decode(data))
            print(f"截图：{out}")

        # 再点一次同一张卡 → 收起
        cdp.tap(first["x"], first["y"])
        time.sleep(0.4)
        check("再点一次就收起", cdp.js(drawer_open) is False)

        # 点空白处 → 打开后也能被空白关掉。
        # 取画布中间偏下那块：左下角现在是图例、右下角是小地图，
        # 点到它们身上是点在浮层上，那不是「点空白」。
        cdp.tap(first["x"], first["y"])
        time.sleep(0.35)
        opened = cdp.js(drawer_open) is True
        blank = cdp.js(
            "JSON.stringify((() => {"
            "  const s = document.querySelector('svg.gCanvas').getBoundingClientRect();"
            "  return {x: s.left + s.width * 0.5, y: s.top + s.height - 46};"
            "})())"
        )
        point = json.loads(blank) if blank else None
        if point:
            cdp.tap(point["x"], point["y"])
            time.sleep(0.35)
        check("点空白处收起", opened and cdp.js(drawer_open) is False, opened)

        # 拖一下不应该被当成点击
        cdp.tap(first["x"], first["y"])
        time.sleep(0.3)
        before = cdp.js(drawer_open)
        cdp.mouse("mouseMoved", first["x"], first["y"])
        cdp.mouse("mousePressed", first["x"], first["y"])
        cdp.mouse("mouseMoved", first["x"] + 60, first["y"] + 24)
        cdp.mouse("mouseReleased", first["x"] + 60, first["y"] + 24)
        time.sleep(0.5)
        check("从卡片上拖动画布，不会被当成点开", cdp.js(drawer_open) == before,
              f"{before} → {cdp.js(drawer_open)}")

    # ------------------------------------------------------------------
    # 会话行的「⋯」：真鼠标点它，不能顺手把会话也打开
    #
    # 这是个真实的坑：`.more` 长在一个整行可点的 `.trace` 里。少写一句
    # stopPropagation，用户每次想置顶都会先跳进那条会话 —— 而且看起来
    # 一切正常，只是「怎么老是跳过去」。合成事件测不出来，真鼠标能。
    # ------------------------------------------------------------------
    cdp.js("Sidebar.show('sessions')")
    time.sleep(0.3)

    row = center_of(cdp, "#traceList .trace", 0)
    check("会话列表里有行", bool(row), row)
    if row:
        cid_before = cdp.js("state.cid")
        # 先移上去：`.more` 平时是 display:none，不 hover 拿不到它的矩形
        cdp.mouse("mouseMoved", row["x"], row["y"])
        time.sleep(0.2)
        more = center_of(cdp, "#traceList .trace .more", 0)
        check("悬停之后「⋯」才出现并拿到位置", bool(more), more)
        if more:
            cdp.tap(more["x"], more["y"])
            time.sleep(0.35)
            node = cdp.js("!!document.querySelector('.traceMenu')")
            check("真点「⋯」弹出菜单", node is True)
            items = cdp.js("document.querySelectorAll('.traceMenu .tmItem').length") or 0
            check("菜单是四项（置顶 / 重命名 / 归档 / 删除）", items == 4, items)
            check("点「⋯」没有顺手打开这条会话", cdp.js("state.cid") == cid_before,
                  f"{cid_before} → {cdp.js('state.cid')}")

            if "--shot" in sys.argv:
                data = cdp.call("Page.captureScreenshot", format="png").get("data")
                out = ROOT / "screenshots" / "side-menu.png"
                out.write_bytes(base64.b64decode(data))
                print(f"截图：{out}")

            # 点空白处关掉
            cdp.tap(row["x"] - 40, row["y"] + 160)
            time.sleep(0.3)
            check("点别处菜单收起", cdp.js("!!document.querySelector('.traceMenu')") is False)
            check("收起菜单也不会误开别的会话", cdp.js("state.cid") == cid_before)

    # ------------------------------------------------------------------
    # 工作区行：悬停出「＋」「⋯」，点「⋯」不能顺手把这一组折叠
    #
    # 和上面那条是同一个坑的另一半：折叠是整行绑的 click，`＋`/`⋯` 长在行里面。
    # 少一句 stopPropagation，用户每次想「在这个工作区新建一条」都会先把这一组
    # 收起来 —— 收起来之后他要的东西还在，只是看不见了，最容易被当成「按钮坏了」。
    # ------------------------------------------------------------------
    ws_row = center_of(cdp, "#traceList .wsRow", 0)
    check("树上有工作区分组行", bool(ws_row), ws_row)
    if ws_row:
        # 先把鼠标挪到空白处：上一段刚在会话行上悬停过，指针很可能正压在这一行上，
        # 那样「没悬停时是藏着的」这条就成了在验上一次的残留状态。
        cdp.mouse("mouseMoved", 900, 300)
        time.sleep(0.2)
        # 看的是外面那层 `.wsOps` 容器：`.opb` 自己是 display:grid，
        # 藏起来靠的是父级 none —— 只查按钮会永远读到 grid。
        ops_hidden = cdp.js(
            "(() => { const b = document.querySelector('#traceList .wsRow .wsOps');"
            "  return b ? getComputedStyle(b).display : '没有'; })()")
        check("没悬停时「＋」「⋯」是藏着的", ops_hidden == "none", ops_hidden)

        cdp.mouse("mouseMoved", ws_row["x"], ws_row["y"])
        time.sleep(0.25)
        ops_shown = cdp.js(
            "(() => { const b = document.querySelector('#traceList .wsRow .opb');"
            "  if (!b) return null; const r = b.getBoundingClientRect();"
            "  return getComputedStyle(b).display !== 'none' && r.width > 0; })()")
        check("悬停之后「＋」「⋯」露出来", ops_shown is True, ops_shown)

        before_fold = cdp.js("document.querySelectorAll('#traceList .trace').length")
        ws_more = center_of(cdp, "#traceList .wsRow .opb", 1)
        check("拿到工作区「⋯」的位置", bool(ws_more), ws_more)
        if ws_more:
            cdp.tap(ws_more["x"], ws_more["y"])
            time.sleep(0.35)
            check("点工作区「⋯」弹出菜单",
                  cdp.js("!!document.querySelector('.traceMenu')") is True)
            ws_items = cdp.js("document.querySelectorAll('.traceMenu .tmItem').length") or 0
            ws_labels = cdp.js(
                "Array.from(document.querySelectorAll('.traceMenu .tmItem'))"
                ".map(function (e) { return e.textContent.trim(); }).join('|')")
            # 五项：开新会话 / 打开 / 重命名 / 收起 / 隐藏 + 删除工作区（设计稿 main-ui-tree-actions）
            check("菜单里有「开新会话 / 打开 / 收起 / 隐藏 / 删除工作区」", ws_items >= 4, ws_items)
            check("工作区菜单最后一项是「删除工作区…」",
                  "删除工作区" in str(ws_labels), ws_labels)
            after_fold = cdp.js("document.querySelectorAll('#traceList .trace').length")
            check("点「⋯」没有把这一组折叠起来", after_fold == before_fold,
                  f"{before_fold} → {after_fold}")
            # 收菜单不能用「点某个坐标」：菜单高度会随着项数变，
            # 原来那个空位现在正好压在「删除工作区…」上 —— 一按就弹删除框，
            # 后面所有真点击都被遮罩吃掉，看起来像下钻坏了。
            cdp.js("document.body.click()")
            time.sleep(0.3)
            check("点别处菜单就收起",
                  cdp.js("!!document.querySelector('.traceMenu')") is False)
            # `#modal` 是常驻节点，关着的时候带 hidden —— 只看类名会永远判成「开着」
            check("没有意外弹出别的弹窗",
                  cdp.js("document.querySelector('.modal')"
                         ".classList.contains('hidden')") is True,
                  cdp.js("(document.querySelector('#modalCard h2') || {})"
                         ".textContent || ''"))

    # ------------------------------------------------------------------
    # 改动栏的三级下钻：整行可点这条路径也要用真鼠标走一遍
    #
    # 合成事件 click() 会绕过命中测试 —— 如果哪天有人在行里叠了一层
    # 盖住内容的伪元素，或者 pointer-events 被父级关掉，cdp.js 里那句
    # element.click() 照样"成功"，用户真点却纹丝不动。这里必须真点。
    # ------------------------------------------------------------------
    cdp.js("Sidebar.show('changes')")
    time.sleep(0.8)

    # 这一栏会记住上次站在第几层（「对话 → 产物 → 阶段」，设计如此）。
    # 不先回到第一级的话，断言可能正对着某个文件的阶段页，于是「第一级是空的」
    # 看起来像功能坏了。点最左边那颗面包屑，把自己放回「对话」这一层。
    cdp.js("(() => { const b = document.querySelector('#panelHost .crumbs .crumb');"
           "  if (b) b.click(); })()")
    time.sleep(0.7)

    thread = center_of(cdp, "#panelHost .drillBody > .listItem.clickable", 0)
    check("改动栏第一级有对话行", bool(thread), thread)
    if thread:
        cdp.tap(thread["x"], thread["y"])
        time.sleep(0.8)
        grew = cdp.js("document.querySelectorAll('#panelHost .crumbs > *').length") or 0
        check("真点对话行能下钻到产物层（面包屑变长）", grew >= 3, grew)
        files = cdp.js("document.querySelectorAll('#panelHost .drillBody > .listItem.clickable').length") or 0
        check("第二级列出了文件", files > 0, files)

        # 再往下钻一层，看时间线是不是真出来了
        if files > 0:
            node = center_of(cdp, "#panelHost .drillBody > .listItem.clickable", 0)
            if node:
                cdp.tap(node["x"], node["y"])
                time.sleep(0.8)
                stages = cdp.js("document.querySelectorAll('#panelHost .stage').length") or 0
                check("真点文件行能看到阶段时间线", stages > 0, stages)

    # ------------------------------------------------------------------
    # 选工作区：现在是系统对话框，弹窗里只留「现在用的是 + 最近用过 + 浏览」
    #
    # 关键的一条：**不要去点「浏览这台电脑…」**。它会在服务端真的弹出
    # Windows 的选文件夹对话框，那个对话框一直等人选，测试就挂死在这儿了。
    # 这里只验它存在、文案对；真正的对话框靠人眼和单独的手工验证。
    # ------------------------------------------------------------------
    # 先回到「会话」这一栏。面板页占的是中栏，输入区和整个 `.view` 都收起来了
    # （`#projectBtn` 长在里面），此时量到的坐标是 0×0，点下去等于点菜单栏。
    cdp.js("Sidebar.show('sessions'); switchView('chat')")
    time.sleep(0.6)
    ws_btn = center_of(cdp, "#projectBtn")
    check("顶栏有工作区按钮", bool(ws_btn), ws_btn)
    if ws_btn:
        cdp.tap(ws_btn["x"], ws_btn["y"])
        time.sleep(1.2)
        opened = cdp.js("Modal.isOpen()")
        check("真点一下就弹出选目录", opened is True, opened)

        now_path = cdp.js(
            "(() => { const p = document.querySelector('#modalCard .wsNowPath');"
            "  return p ? String(p.textContent) : ''; })()")
        check("弹窗里写着「现在用的是」哪个目录", bool(now_path), now_path)
        check("那个路径带盘符（不是…开头）",
              bool(now_path) and now_path[1:2] == ":", now_path)

        browse_btn = cdp.js(
            "(() => { const b = document.querySelector('#modalCard .modalFoot .primary');"
            "  return b ? String(b.textContent) : ''; })()")
        check("主要出口是「浏览这台电脑…」",
              browse_btn.startswith("浏览"), browse_btn)

        chip_path = cdp.js(
            "(() => { const c = document.querySelector('#modalCard .dirChip');"
            "  return c ? c.title : ''; })()")
        chip = center_of(cdp, "#modalCard .dirChip")
        if chip:
            # 点它会 applyWorkspace。临时摘掉 cid，只改本地状态，别动演示会话。
            cdp.js("window.__keptCid = state.cid; state.cid = null;")
            cdp.tap(chip["x"], chip["y"])
            time.sleep(1.0)
            chosen = cdp.js("state.project")
            check("点「最近用过」直接定为工作区", chosen == chip_path,
                  f"{chosen} vs {chip_path}")
            check("定完弹窗自己关掉", cdp.js("Modal.isOpen()") is False, "")
            cdp.js("state.cid = window.__keptCid; delete window.__keptCid;")
        else:
            cdp.js("Modal.close()")

    # ------------------------------------------------------------------
    # 待办那块：点标题要能收起 / 展开
    #
    # 用户报的是「收不回去」—— 这块以前压根没有可点的地方，只有待办被清空
    # 才会消失。于是一轮跑完、6/6 全绿之后，它就那么钉在输入框上方，
    # 六行的高度一直占着。这里用真鼠标点，因为要验的正是「点得动」。
    # ------------------------------------------------------------------
    cdp.js("switchView('chat')")
    time.sleep(0.3)
    cdp.js("state.todos = [{text:'甲', status:'done'}, {text:'乙', status:'doing'}];"
           "state.todoUser = false; state.todoSig = ''; renderTodos();")
    time.sleep(0.3)

    head = center_of(cdp, "#todosHost .todosHead")
    check("待办条露出来了", bool(head), head)
    if head:
        items = cdp.js("document.querySelectorAll('#todosHost .todoItem').length") or 0
        check("默认是展开的", items == 2, items)

        cdp.tap(head["x"], head["y"])
        time.sleep(0.35)
        folded = cdp.js("document.getElementById('todosHost').classList.contains('folded')")
        check("真点标题就收起了", folded is True, folded)

        # 收了还得能展开 ——「收不回去」的另一半是「收回去就打不开了」
        head = center_of(cdp, "#todosHost .todosHead")
        cdp.tap(head["x"], head["y"])
        time.sleep(0.35)
        back = cdp.js("document.querySelectorAll('#todosHost .todoItem').length") or 0
        check("再点一次又展开", back == 2, back)

        # × 是「先别挡着」，下一次待办有变化时它会自己回来
        x_btn = center_of(cdp, "#todosHost .todoX")
        check("待办条上有 × 按钮", bool(x_btn), x_btn)
        if x_btn:
            cdp.tap(x_btn["x"], x_btn["y"])
            time.sleep(0.35)
            gone = cdp.js("document.getElementById('todosHost').classList.contains('hidden')")
            check("点 × 整块消失", gone is True, gone)
    cdp.js("state.todos = []; renderTodos();")
    time.sleep(0.2)

    # ------------------------------------------------------------------
    # 输入栏的「技能」：真点一下要能弹出列表、点一项要能选中
    #
    # 这条路径看着简单，但浮层是绝对定位、又在 composer 里，很容易被
    # 别的元素盖住一角。合成 click 会绕开命中测试，被盖住了照样"成功"；
    # 真鼠标点才会打在盖住它的那个东西上。
    # ------------------------------------------------------------------
    cdp.js("switchView('chat')")
    time.sleep(0.5)

    skill_btn = center_of(cdp, "#skillBtn")
    check("输入栏有「技能」按钮", bool(skill_btn), skill_btn)
    if skill_btn:
        pop_open = "(() => { const p = document.getElementById('skillPop');" \
                   "  return !!p && !p.classList.contains('hidden'); })()"
        # 起始状态未必是收起的（前面几段点过别的地方），先确保关掉再点
        if cdp.js(pop_open) is True:
            cdp.js("closeSkillPop()")
            time.sleep(0.2)

        # 「＋」先弹菜单，技能在菜单里 —— 这也是真点两下才到得了的路
        cdp.tap(skill_btn["x"], skill_btn["y"])
        time.sleep(0.3)
        plus_open = cdp.js("(() => { const p = document.getElementById('plusPop');"
                           "  return !!p && !p.classList.contains('hidden'); })()")
        check("真点「＋」弹出菜单", plus_open is True, "")
        skill_item = None
        for idx in range(6):
            node = center_of(cdp, "#plusPop .mi", idx)
            if node and cdp.js(f"document.querySelectorAll('#plusPop .mi')[{idx}]"
                               f".textContent.indexOf('Skill') >= 0") is True:
                skill_item = node
                break
        if skill_item:
            cdp.tap(skill_item["x"], skill_item["y"])
        deadline = time.time() + 8
        while time.time() < deadline:
            if cdp.js(pop_open) is True and (cdp.js(
                    "document.querySelectorAll('#skillPop .skillItem').length") or 0) > 0:
                break
            time.sleep(0.25)
        check("真点一下弹出技能列表", cdp.js(pop_open) is True, "")
        count = cdp.js("document.querySelectorAll('#skillPop .skillItem').length") or 0
        check("列表里有技能", count > 0, count)

        item = center_of(cdp, "#skillPop .skillItem", 0)
        if item:
            cdp.tap(item["x"], item["y"])
            time.sleep(0.4)
            picked = cdp.js("JSON.stringify(state.skillPick)") or "[]"
            check("真点一项就选中了", picked != "[]", picked)
            chips = cdp.js("document.querySelectorAll('#skillChips .skillChip').length") or 0
            check("输入框上的 chip 出现了", chips > 0, chips)

            # 点别处收起
            away = center_of(cdp, "#feed")
            if away:
                cdp.tap(away["x"], away["y"] - 120)
                time.sleep(0.35)
            check("点别处收起浮层", cdp.js(pop_open) is False, "")

        # 收拾干净：别把选中状态留给后面
        cdp.js("state.skillPick = []; renderSkillChips();")

    cdp.close()
    kill_tree(process)

    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
