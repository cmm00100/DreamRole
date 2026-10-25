"""DreamRole 的启动入口。

为什么要套一层桌面外壳，而不是让用户去浏览器里打开 localhost：
用户要的是「双击一个 exe」。真正的产品差别不在功能多少，而在第一步有没有摩擦。

启动顺序是有讲究的，不能随便换：
1. 先把数据目录、配置、目录结构准备好（后面所有东西都依赖它）；
2. 再起调度器、连 MCP（这些跑在后台线程里，失败也只是少个功能）；
3. 然后才起 HTTP 服务，并**等端口真的能连上**；
4. 最后才开窗口。

第 3 步的「等」不能省。窗口只要比服务快 200 毫秒打开，用户看到的就是一次白屏，
然后他会在心里给这个软件扣很多分 —— 哪怕界面 300 毫秒后就正常了。
"""
from __future__ import annotations

# 启动路标要在**第一个 import 之前**就位。
#
# 打包成窗口程序之后，出错既没有 stdout 也没有退出码，只有一个小对话框 ——
# 用户看到的是「双击没反应」，我们看到的是一片空白。更麻烦的是：真出问题时
# 往往是**模块导入阶段**就死了，那时候本文件的函数一个都还没定义，
# 想记一笔都记不了。所以这里只用 os/sys/time 三个模块，先把笔拿起来。
import os
import sys
import time


def breadcrumb(message: str) -> None:
    """写一行启动路标。不依赖数据目录、不依赖日志目录 —— 它俩可能正是失败的那一步。"""
    try:
        target = os.path.join(os.environ.get("TEMP") or os.getcwd(), "dreamrole-boot.log")
        try:
            if os.path.getsize(target) > 64 * 1024:
                os.remove(target)          # 只留最近这一段，别让它长成垃圾
        except OSError:
            pass
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


breadcrumb("desktop.py 开始执行")

import socket  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

# webbrowser 是这批里最重的一个（导入时会扫注册表、枚举浏览器），
# 而且只在「没有桌面外壳」时用得到 —— 放函数里用的时候再导。
breadcrumb("标准库已导入")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"
TITLE = "DreamRole"
WINDOW_SIZE = (1440, 920)
MIN_SIZE = (1024, 680)
BOOT_WAIT = 8.0   # 等页面回执最多这么久；等不到就换浏览器窗口打开


def log(message: str) -> None:
    from app.paths import LOGS

    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOGS / "dreamrole.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


class _NullStream:
    """没有控制台时的替身。API 面只做到「够被误用而不炸」。"""

    encoding = "utf-8"
    errors = "replace"

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


def ensure_std_streams() -> None:
    """给 sys.stdout / sys.stderr 兜底。

    打包成窗口程序（console=False）之后没有控制台，Python 就把这两个设成 None。
    这不是「看不见输出」这么轻的事：uvicorn 配日志时会对 `sys.stdout.isatty()`
    求值，None 上取属性直接抛 AttributeError，被 logging 包成
    「Unable to configure formatter 'default'」——服务因此起不来。
    而用户看到的现象是「双击没反应」，从现象到原因隔了三层，谁猜得到。

    冒烟测试抓不到它：脚本用管道拉起来，句柄是有效的，sys.stdout 不为 None。
    只有真正双击（没有任何标准句柄）才会复现。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return

    from app.paths import LOGS

    sink = None
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        console = LOGS / "console.log"
        # 一个只写不读的文件该有上限
        if console.is_file() and console.stat().st_size > 2 * 1024 * 1024:
            console.unlink()
        sink = open(console, "a", encoding="utf-8", errors="replace")
    except OSError:
        sink = None

    fallback = sink if sink is not None else _NullStream()
    if sys.stdout is None:
        sys.stdout = fallback
    if sys.stderr is None:
        sys.stderr = fallback


def _open_browser(url: str) -> None:
    """用系统默认浏览器打开。这个模块导入很重，只在真的要开浏览器时才导。"""
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        log(f"打不开浏览器：{exc}")


def free_port(start: int = 8760, tries: int = 40) -> int:
    forced = os.environ.get("DREAMROLE_PORT")
    if forced:
        try:
            return int(forced)
        except ValueError:
            pass
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((HOST, port))
                return port
            except OSError:
                continue
    # 全被占了就让系统随便给一个
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def wait_until_ready(port: int, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.08)
    return False


def prepare() -> None:
    """所有「启动前必须成立」的事。任何一件失败都直接报错，不带着问题往下走。"""
    from app import config
    from app.paths import DATA, ensure_dirs

    ensure_dirs()
    config.load()
    log(f"启动：data={DATA}")

    if os.environ.get("DREAMROLE_FAKE_LLM") == "1":
        # 开发用：不连真模型。scripts/ 不会被打进 exe，所以这行在发行版里是死代码。
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import fake_llm

            fake_llm.install()
            log("已启用假模型")
        except Exception as exc:  # noqa: BLE001
            log(f"假模型没装上：{exc}")


def start_background() -> None:
    from app import mcp, scheduler

    try:
        scheduler.start()
    except Exception as exc:  # noqa: BLE001
        log(f"调度器没起来：{exc}")

    # MCP 可能要拉子进程、握手，慢的话会挡住窗口。放后台，连上就连上了。
    threading.Thread(target=_connect_mcp, name="mcp-boot", daemon=True).start()


def _connect_mcp() -> None:
    from app import mcp

    try:
        mcp.connect_all()
        log("MCP 已连接：" + ", ".join(s["name"] for s in mcp.servers() if s["connected"]))
    except Exception as exc:  # noqa: BLE001
        log(f"MCP 连接失败：{exc}")


def run_server(port: int, block: bool = True) -> threading.Thread | None:
    import uvicorn

    from app import channels

    from app.server import app as fastapi_app

    # 告诉通道层「我们绑在哪」—— 它据此决定要不要查令牌。
    # 绑回环的时候外来请求根本连不进来，再验一遍只是给自己添堵。
    channels.note_listen(HOST)

    def worker() -> None:
        try:
            uvicorn.run(fastapi_app, host=HOST, port=port, log_level="warning", access_log=False)
        except Exception as exc:  # noqa: BLE001
            log(f"HTTP 服务退出：{exc}")

    thread = threading.Thread(target=worker, name="uvicorn", daemon=True)
    thread.start()
    if block:
        thread.join()
        return None
    return thread


def shutdown() -> None:
    from app import mcp, scheduler

    try:
        scheduler.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        mcp.close_all()
    except Exception:  # noqa: BLE001
        pass
    log("退出")


def browser_exe() -> str | None:
    """找一个能开「无边框窗口」的 Chromium 浏览器。

    为什么要 `--app=` 而不是 `webbrowser.open(url)`：后者会带地址栏、标签页，
    还会塞进用户已有的浏览器窗口里 —— 那就不是这个软件了，是一个网页。
    """
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


def open_fallback(url: str, reason: str):
    """桌面壳没能把界面显示出来时的退路。返回浏览器进程（拿不到就是 None）。

    留一块黑窗口给用户是最糟的选择：他不知道是没启动、还是在加载、还是坏了。
    宁可换一个普通浏览器窗口，也要让他看见界面。
    """
    log(f"改用浏览器窗口打开（{reason}）")
    exe = browser_exe()
    if exe:
        try:
            profile = Path(tempfile.mkdtemp(prefix="dreamrole-shell-"))
            process = subprocess.Popen(
                [exe, f"--app={url}", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check",
                 f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}"],
                close_fds=True,
            )
            log(f"已用 {Path(exe).stem} --app 打开")
            return process
        except Exception as exc:  # noqa: BLE001
            log(f"浏览器窗口打开失败：{exc}")
    _open_browser(url)
    log("已交给系统默认浏览器")
    return None


def main() -> int:
    # 必须在任何会碰 stdout 的东西之前 —— uvicorn 配日志时就会碰
    breadcrumb("进入 main")
    ensure_std_streams()
    breadcrumb("标准流已兜底")
    prepare()
    breadcrumb("目录与配置已就绪")
    start_background()
    breadcrumb("后台线程已起")

    port = free_port()
    breadcrumb(f"选定端口 {port}")
    run_server(port, block=False)
    breadcrumb("HTTP 服务已启动")
    if not wait_until_ready(port):
        from app.paths import LOGS

        message = f"服务没能在预期时间内起来（端口 {port}）。\n\n日志：{LOGS / 'dreamrole.log'}"
        print(message)
        log(message)
        # 窗口程序里 print 是没人看得见的，必须弹窗 ——
        # 「双击没反应」是最难排查的一种失败，不能让它再发生一次
        fatal(message)
        return 1

    url = f"http://{HOST}:{port}"
    log(f"服务就绪：{url}")

    # 命令行通道：`DreamRole.exe chat …`。
    # 它和界面共用同一个服务进程，只是不打开窗口 —— 三条通道共用一套内核，
    # 才谈得上「从哪个门进来都一样」。
    if len(sys.argv) > 1 and sys.argv[1] in ("chat", "run"):
        from app import cli

        try:
            code = cli.main(port, sys.argv[2:])
        finally:
            shutdown()
        return code

    # 启动令牌：页面加载时带着它回一句话，壳才知道「界面真的跑起来了」而不只是
    # 「窗口开出来了」。等不到就换浏览器窗口打开，见 open_fallback。
    token = uuid.uuid4().hex[:16]
    url = f"{url}/?boot={token}"

    headless = os.environ.get("DREAMROLE_HEADLESS") == "1" or "--no-window" in sys.argv
    try:
        import webview  # pywebview
    except Exception:  # noqa: BLE001 - 没装就退回浏览器，不能因为外壳缺失就跑不了
        webview = None

    if headless or webview is None:
        if not headless:
            log("没有可用的桌面外壳，改用系统浏览器打开")
        if not headless or os.environ.get("DREAMROLE_OPEN_BROWSER") == "1":
            _open_browser(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        shutdown()
        return 0

    window = webview.create_window(
        TITLE,
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        background_color="#0e1014",
        text_select=True,
    )

    def show_window() -> None:
        """把窗口「顶到眼前」，把它的真实状态写进日志，并在它始终不出现时兜底。

        为什么需要这一步：打包成没有控制台的窗口程序之后，出现过页面已经加载完
        （pywebview 的 loaded 事件都触发了）、日志一切正常，但桌面上没有任何窗口
        的情况。用户看到的就是「双击没反应」，而所有自动化检查都在点头。

        所以这里做三件事：
        1. 显式 show 一次（幂等：已经可见的窗口再 show 不会有任何变化）；
        2. 把句柄、可见性、矩形、屏幕尺寸记下来 —— 再出这种「没人看得见」的故障时，
           日志里至少有能对质的东西；
        3. 等几秒还不出现，就打开系统浏览器。宁可开在浏览器里，也不能什么都没有。
        """
        import ctypes

        try:
            win = webview.windows[0] if webview.windows else window
            win.show()
        except Exception as exc:  # noqa: BLE001
            log(f"显式显示窗口失败：{exc}")
            win = window

        handle = 0
        try:
            user32 = ctypes.windll.user32
            native = getattr(win, "native", None)
            handle = int(native.Handle.ToInt64()) if native is not None else 0
            log(f"窗口句柄：{handle}")
            if handle:
                user32.ShowWindow(handle, 5)   # SW_SHOW
                user32.ShowWindow(handle, 9)   # SW_RESTORE
                rect = (ctypes.c_long * 4)()
                user32.GetWindowRect(handle, ctypes.byref(rect))
                screen = (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
                log(f"窗口矩形：{rect[0]},{rect[1]} → {rect[2]},{rect[3]}；屏幕：{screen[0]}x{screen[1]}")
                log(f"IsWindowVisible={user32.IsWindowVisible(handle)}")
            else:
                log("拿不到窗口句柄 —— 窗体可能根本没创建")
        except Exception as exc:  # noqa: BLE001
            log(f"窗口状态检查失败：{exc}")

        # 兜底：等页面回执。等不到就说明这个壳里的浏览器没把界面跑起来 ——
        # 窗口可能好好地开着，里面却是一片背景色，用户以为软件坏了。
        try:
            from app import server as server_mod

            deadline = time.time() + BOOT_WAIT
            script_ok = False
            reported = ""
            while time.time() < deadline:
                if server_mod.boot_seen(token, ui_only=True):
                    log("界面已渲染（收到 ui 回执）")
                    return
                if not script_ok and server_mod.boot_seen(token):
                    script_ok = True
                    log("页面脚本已启动，等界面渲染…")
                problem = server_mod.boot_error()
                if problem and problem != reported:
                    reported = problem
                    log("界面报错：" + problem)
                time.sleep(0.25)
            if script_ok:
                log("脚本跑起来了，但界面一直没渲染完")
            else:
                log(f"{BOOT_WAIT:.0f} 秒内没有任何页面回执 —— 界面没跑起来")
        except Exception as exc:  # noqa: BLE001
            log(f"等待页面回执失败：{exc}")

        process = open_fallback(url, "桌面壳里的界面没有启动")
        if process is None:
            return
        # 浏览器窗口才是用户现在看到的东西：它关掉，这个进程就该退出。
        # 不走 webview 的关闭流程，是因为主线程还卡在 webview.start() 里。
        try:
            win.hide()
        except Exception as exc:  # noqa: BLE001
            log(f"隐藏空窗口失败：{exc}")
        try:
            process.wait()
        except Exception:  # noqa: BLE001
            pass
        log("浏览器窗口已关闭，退出")
        shutdown()
        os._exit(0)

    try:
        # start 的第一个参数会在 GUI 起来之后跑在一个独立线程里 —— 正是我们需要的时机
        webview.start(show_window, debug=os.environ.get("DREAMROLE_DEBUG") == "1")
    except Exception as exc:  # noqa: BLE001
        log(f"窗口起不来：{exc}，改用浏览器")
        _open_browser(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        shutdown()
    return 0


def fatal(message: str) -> None:
    """打包成窗口程序之后 stdout 是看不见的，出错只能弹窗。

    没有这个函数的话，用户双击 exe 得到的体验是「什么也没发生」——
    那是最难排查的一种失败。
    """
    log("致命错误：" + message)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0, message, "DreamRole 启动失败", 0x10
        )
    except Exception:  # noqa: BLE001
        print(message)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import multiprocessing

            multiprocessing.freeze_support()
        except Exception:  # noqa: BLE001
            pass
    breadcrumb("进入 __main__")
    try:
        code = main()
    except BaseException as exc:  # noqa: BLE001
        import traceback

        detail = traceback.format_exc()
        breadcrumb("崩了：" + detail)
        fatal(f"{type(exc).__name__}: {exc}\n\n{detail[-1200:]}")
        code = 1
    breadcrumb(f"退出，code={code}")
    sys.exit(code)
