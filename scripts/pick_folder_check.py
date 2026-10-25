"""手工验收：点「浏览这台电脑…」→ 真的弹出系统对话框 → 选完 → 工作区跟着变。

为什么不进日常回归：它会在屏幕上**真的弹出一个对话框**、抢焦点。
跑测试的时候莫名其妙弹窗，比测试红了更让人困惑。
改完选目录这条链路之后手动跑一次：

    python scripts/pick_folder_check.py
"""
from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import click_check as cc          # noqa: E402
import shoot                      # noqa: E402

CDP_PORT = 9341
DIALOG_TITLE = "选择工作区"

user32 = ctypes.windll.user32
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  [ok]   " if ok else "  [FAIL] ") + name + ("" if ok else f" :: {detail}"))


def find_dialog(timeout: float = 15.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = user32.FindWindowW("#32770", DIALOG_TITLE)
        if hwnd:
            return hwnd
        time.sleep(0.2)
    return 0


def children(hwnd: int) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(child, _lparam):
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(child, cls, 128)
        length = user32.GetWindowTextLengthW(child)
        text = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(child, text, length + 1)
        out.append((child, cls.value, text.value))
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(collect), 0)
    return out


def click_select_folder(hwnd: int) -> bool:
    """点对话框里的「选择文件夹」—— 等价于用户接受当前打开的那个目录。

    试过先在「文件名」框里 WM_SETTEXT 再点确定：文本写进去了，但对话框不认
    （点完还是原来那个位置）。这条路不通就算了 —— 这个脚本要证明的是
    「点了按钮之后路径能回到程序里」，至于用户怎么在对话框里漫游，
    那是 Windows 自己的事，不是我们的代码。
    """
    kids = children(hwnd)
    ok_btn = next(
        (kid for kid, cls, text in kids
         if cls == "Button" and ("选择文件夹" in text or "Select Folder" in text)), 0)
    if not ok_btn:
        return False
    user32.SendMessageW(ok_btn, 0x00F5, 0, 0)   # BM_CLICK
    return True


def main() -> int:
    cc.free_port(CDP_PORT)
    shoot.ensure_dirs()
    shoot.config.load()
    project = shoot.make_project()
    shoot.start_server()
    shoot.run_demo(project)

    browser = shoot.find_browser()
    if not browser:
        sys.exit("没找到 Edge / Chrome")
    profile = shoot.SANDBOX / "pick-check-profile"
    shutil.rmtree(profile, ignore_errors=True)

    process = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         f"--remote-debugging-port={CDP_PORT}", "--remote-allow-origins=*",
         "--window-size=1400,900", "--proxy-server=direct://",
         "--proxy-bypass-list=*", f"http://127.0.0.1:{shoot.PORT}/?still=1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cdp = None
    deadline = time.time() + 25
    while time.time() < deadline and cdp is None:
        try:
            pages = json.loads(cc.no_proxy_open(f"http://127.0.0.1:{CDP_PORT}/json"))
            page = cc.pick_page(pages)
            if page:
                cdp = cc.Cdp(page["webSocketDebuggerUrl"])
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    if cdp is None:
        cc.kill_tree(process)
        sys.exit("连不上调试端口")

    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    time.sleep(2.5)

    # 先把工作区清空：「选完之后变了」才是能看见的事实，
    # 而不是「本来就是这个目录，什么都没发生也算过」。
    cdp.js("state.project = ''; renderProject(); renderFeed(); updateComposer();")
    cdp.js("pickProject()")
    time.sleep(0.6)
    check("弹窗打开了", cdp.js("Modal.isOpen()") is True, "")

    button = cc.center_of(cdp, "#modalCard .modalFoot .primary")
    check("找到「浏览这台电脑…」按钮", bool(button), button)
    if not button:
        cdp.close()
        cc.kill_tree(process)
        return 1

    cdp.tap(button["x"], button["y"])
    hwnd = find_dialog()
    check("真的弹出了系统选文件夹对话框", bool(hwnd), "15 秒内没等到窗口")

    if hwnd:
        # 截图优先：文档里最想让人看见的就是「此电脑 / 多个盘 / 新建文件夹」都在。
        # 直接对 hwnd 取 GetWindowRect —— 不用 app.tools.pc._window_rect，
        # 那个是按标题枚举顶层窗口的，对 owner 是主窗口的模态对话框不一定认得出来，
        # 而它返回 None 时这里会**静默跳过**（第一次就是这样，图没生成还以为存上了）。
        try:
            from PIL import ImageGrab
            # 抓屏抓的是「屏幕上那块矩形」，不是「那个窗口」。对话框要是被别的窗口压着，
            # 抓回来就是压着它的那个东西（第一次就抓到了背后的 WorkBuddy）。
            # 所以先把对话框提到前台 —— 真实使用里它本来就是前台，这一步只是把
            # 脚本跑出来的状态对齐到真实状态。
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.UpdateWindow(hwnd)
            # 窗口「出现」不等于「画完」：这个对话框里面的东西是 Shell 命名空间，
            # 异步枚举的。0.5 秒抓下来是一整片白（第一次就是这样，图能用但不能看）。
            # 2 秒是实测这个对话框在冷启动下画完的时间，宁慢一点。
            time.sleep(2.0)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            box = (rect.left, rect.top, rect.right, rect.bottom)
            if box[2] - box[0] > 100 and box[3] - box[1] > 100:
                ImageGrab.grab(bbox=box, all_screens=True).save(
                    ROOT / "shots" / "system-picker.png")
                print(f"  截图：shots/system-picker.png  ({box[2]-box[0]}×{box[3]-box[1]})")
            else:
                print(f"  （截图跳过：窗口矩形不对 {box}）")
        except Exception as exc:  # noqa: BLE001
            print("  （截图失败：", exc, "）")

        check("对话框里点得到「选择文件夹」", click_select_folder(hwnd), "没找到按钮")

        chosen = ""
        deadline = time.time() + 15
        while time.time() < deadline:
            chosen = cdp.js("state.project") or ""
            if chosen:
                break
            time.sleep(0.3)

        check("选完之后工作区有值了", bool(chosen), chosen)
        check("拿回来的是个真实路径（带盘符）",
              len(chosen) > 2 and chosen[1:2] == ":", chosen)
        check("弹窗自己关掉了", cdp.js("Modal.isOpen()") is False, "")
        check("顶栏跟着显示这个目录",
              bool(cdp.js("document.querySelector('#projectPath').textContent")),
              cdp.js("document.querySelector('#projectPath').textContent"))

    cdp.close()
    cc.kill_tree(process)
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
