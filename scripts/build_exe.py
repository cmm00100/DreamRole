"""打包成 DreamRole.exe，并顺手验一遍「打出来的东西真的能跑」。

    python scripts/build_exe.py              # 打包 + 冒烟
    python scripts/build_exe.py --no-smoke   # 只打包
    python scripts/build_exe.py --onedir     # 目录版（启动更快，但是一堆文件）

冒烟测试是重点：打包失败很容易看出来，**打包成功但运行时报 ImportError** 才是常态。
所以打完一定要真的启动一次、请求一次 /api/bootstrap 再关掉。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
EXE = DIST / "DreamRole.exe"

# 打包输出里有 `▸` 这类 GBK 编不出来的字符：重定向到日志时会崩在 print 上，
# 而「边打包边存一份日志」正是最常用的姿势。
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from _console import harden  # noqa: E402

harden()


def step(message: str) -> None:
    print(f"\n▸ {message}")


def run(command: list[str], **kwargs) -> int:
    print("  $ " + " ".join(command))
    return subprocess.call(command, cwd=str(ROOT), **kwargs)


def ensure_icon() -> None:
    icon = ROOT / "assets" / "dreamrole.ico"
    if icon.is_file():
        return
    step("生成图标")
    run([sys.executable, str(ROOT / "scripts" / "make_icon.py")])


def _clear(folder: Path) -> None:
    """删掉一个产物目录；删不掉就整体改名挪开。

    为什么不是一句 `shutil.rmtree(ignore_errors=True)` 就完事：有些环境（沙箱、
    带「安全删除」钩子的 Python 发行版）会把 `os.remove` 改写成「丢进回收站」。
    回收站拒绝的时候 `rmtree` 配 `ignore_errors=True` 会**静默失败**，
    接着 PyInstaller 自己去 `os.remove(dist/DreamRole.exe)`，报出来的是一句
    完全看不出根因的 `OSError: 操作失败 ... Some operations were aborted`。
    改名（`os.replace`）不是删除，任何环境都做得到 —— 先挪开，构建就能继续。
    """
    if not folder.exists():
        return
    shutil.rmtree(folder, ignore_errors=True)
    if not folder.exists():
        return
    aside = folder.with_name(f"{folder.name}.old-{time.strftime('%H%M%S')}")
    try:
        os.replace(folder, aside)
    except OSError as exc:
        raise SystemExit(
            f"{folder} 既删不掉也挪不开（{exc}）。先手动删掉它再跑一次。") from exc
    print(f"  {folder.name} 删不掉（被安全策略拦了？），已挪到 {aside.name}，构建结束后再清")


def clean_old() -> None:
    """清掉「删不掉所以只能改名挪开」的那几份旧产物。尽力而为。

    它们只是占地方，不影响功能：`os.remove` 在某些环境里会被改写成进回收站，
    回收站拒绝时就删不动，于是构建只能靠改名往前走。
    """
    leftovers = (list(ROOT.glob("dist.old-*")) + list(ROOT.glob("build.old-*"))
                 + [DIST / "DreamRole.exe.old", DIST / "DreamRole.old"])
    for leftover in leftovers:
        if not leftover.exists():
            continue
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        if leftover.exists():
            print(f"  {leftover.name} 还留着（占点地方，不影响使用）")


def clean() -> None:
    r"""清掉上一次的构建中间产物。

    **这里不删 `dist` 目录**，也不再「把 dist/data 搬到临时目录再搬回来」。

    为什么改：`dist/data` 里是用户真在用的东西（settings.json、轨迹、记忆），
    它落在 dist 下面纯属绿色版布局的巧合。老做法是「搬走 → 删 dist → 构建 → 搬回」，
    只要中途被打断（Ctrl+C、被超时 kill），data 就留在临时目录里 —— 而脚本已经把
    dist 删了，于是「打开软件要重新配 key」。这个坑真踩到了：一次中断之后
    `dist/data` 整个不见了，配置在 `%TEMP%\dreamrole-dist-data-*` 里躺着。

    现在构建输出先落临时目录（`--distpath`），成功之后才搬进 dist，
    `dist/data` 从头到尾没人碰。
    """
    step("清理上一次的构建中间产物")
    if BUILD.exists():
        _clear(BUILD)
        print(f"  删除 {BUILD}")


def _strip_low_label(target: Path) -> None:
    """把产物上的「低完整性」标记摘掉。

    这个坑值得单独留一段说明，因为它和代码一点关系都没有，症状却最像代码坏了：

    Windows 有一条规则 —— 可执行文件带 Low 强制标签时，通过它启动的**进程也是 Low**。
    Low 进程不能往中等完整性的位置写东西，而 onefile 的 exe 启动第一件事就是
    往 `%TEMP%\\_MEIxxxx` 解包。于是：解包被拒 → 进程静默退出 → **双击完全没反应**，
    没有报错、没有日志、没有事件、连弹窗都不给。

    这个标记是从目录继承来的。有些沙箱/体检工具会给整个工作区打 Low（好让受限进程
    能写文件），结果连带把打包出来的 exe 也打上了。产物不该背这个锅，
    所以每次构建完成都把它的完整性级别改回 Medium。

    验过：同一份字节，放在打过 Low 的目录里启动就死；复制到 %TEMP% 就能跑。
    """
    if sys.platform != "win32" or not target.exists():
        return
    args = ["icacls", str(target), "/setintegritylevel"]
    if target.is_dir():
        args += ["(OI)(CI)Medium", "/T", "/C", "/Q"]
    else:
        args += ["Medium"]
    try:
        code = subprocess.call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if code == 0:
            print("  已把产物标记改回中等完整性（否则双击会因为解包被拒而毫无反应）")
    except OSError:
        pass


def _replace_into_place(built: Path, target: Path) -> None:
    """把构建产物搬到 dist 里的正式位置。

    先「改名挪开」旧产物再搬新的：有些环境禁止删除（os.remove 被改写成进回收站），
    改名却总是可以。挪开的那份在构建成功后会被 `clean_old()` 再试一次删掉。
    """
    DIST.mkdir(parents=True, exist_ok=True)
    if target.exists():
        aside = target.with_name(target.name + ".old")
        if aside.is_dir():
            shutil.rmtree(aside, ignore_errors=True)
        else:
            aside.unlink(missing_ok=True)
        try:
            os.replace(target, aside)
        except OSError:
            pass       # 挪不开就让下面那句直接覆盖
    if built.is_dir():
        shutil.copytree(built, target, dirs_exist_ok=True)
    else:
        shutil.copy2(built, target)



def build(onedir: bool) -> Path:
    step("调用 PyInstaller")
    env = dict(os.environ)
    if onedir:
        env["DREAMROLE_ONEDIR"] = "1"
    else:
        env.pop("DREAMROLE_ONEDIR", None)

    # 输出先落临时目录：这样 dist 里那份用户数据（dist/data）在整个构建过程中
    # 都不需要被搬走、更不会被删掉。构建失败也只是白跑一趟，dist 原样不动。
    staging = Path(tempfile.mkdtemp(prefix="dreamrole-stage-"))
    code = subprocess.call(
        [sys.executable, "-m", "PyInstaller", str(ROOT / "dreamrole.spec"),
         "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", str(staging)],
        cwd=str(ROOT),
        env=env,
    )
    if code != 0:
        raise SystemExit("PyInstaller 失败，退出码 " + str(code))

    name = "DreamRole" if onedir else "DreamRole.exe"
    built = staging / name
    if not built.exists():
        raise SystemExit(f"没找到产物：{built}")
    if onedir:
        _replace_into_place(built, DIST / "DreamRole")
        target = DIST / "DreamRole" / "DreamRole.exe"
    else:
        _replace_into_place(built, DIST / "DreamRole.exe")
        target = DIST / "DreamRole.exe"
    shutil.rmtree(staging, ignore_errors=True)
    if not target.exists():
        raise SystemExit(f"没找到产物：{target}")
    _strip_low_label(target)
    return target


def _spawn(exe: Path, env: dict, cwd: str | None = None, tries: int = 20, **kwargs) -> subprocess.Popen:
    """拉起 exe，遇到「文件被占用」就等一下再试。

    刚写完的 56 MB 单文件 exe 几乎一定会先被别的东西打开一遍：Defender 的实时扫描、
    SearchIndexer、某些同步盘。这时候 CreateProcess 报 `WinError 32 另一个程序正在使用此文件`——
    不是构建失败，是**还没轮到我们**。直接抛出去的话，前面十几分钟的构建全白干，
    而且报错信息长得完全像「打包坏了」。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            return subprocess.Popen([str(exe)], env=env, cwd=cwd, **kwargs)
        except (PermissionError, OSError) as exc:
            last = exc
            if i == 0:
                print("  文件正被别人占着（多半是杀毒在扫），等一下再试")
            time.sleep(0.5 + min(i * 0.25, 2.0))
    raise SystemExit(f"拉不起 {exe}：{last}")


def smoke(exe: Path) -> bool:
    """真的把 exe 拉起来，请求一次接口，再关掉。"""
    step("冒烟测试：启动 exe")
    port = 8797
    data_dir = Path(tempfile.mkdtemp(prefix="dreamrole-smoke-"))
    env = dict(os.environ)
    env.update(
        {
            "DREAMROLE_PORT": str(port),
            "DREAMROLE_HEADLESS": "1",
            "DREAMROLE_DATA_DIR": str(data_dir),
        }
    )

    process = _spawn(
        exe,
        env,
        cwd=str(exe.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 75
    ready = False
    while time.time() < deadline:
        if process.poll() is not None:
            print(f"  exe 退出了，退出码 {process.returncode}")
            break
        try:
            with urllib.request.urlopen(base + "/api/bootstrap", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            ready = True
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.6)

    results: list[tuple[str, bool, str]] = []
    if ready:
        results.append(("服务起来了", True, ""))
        # 版本号别写死，也别在打包脚本里 import app ——
        # 那会把整个包连同 .data 一起拽进来，而且 sys.path 不一定对。
        # 直接从源文件里抽一行就够。
        expected = ""
        try:
            for line in (ROOT / "app" / "config.py").read_text(encoding="utf-8").splitlines():
                if line.startswith("APP_VERSION"):
                    expected = line.split("=", 1)[1].strip().strip('"\'')
                    break
        except OSError:
            pass
        results.append(("版本号对", bool(expected) and payload.get("version") == expected,
                        f"{payload.get('version')} vs {expected}"))
        results.append(("工具清单齐了", len(payload.get("tools") or []) >= 55,
                        str(len(payload.get("tools") or []))))
        results.append(("新工具在包里（不是只在源码里）",
                        {"read_many", "diff_text", "csv_query", "now",
                         "list_software", "open_software"} <=
                        {t["name"] for t in payload.get("tools") or []},
                        sorted({"read_many", "diff_text", "csv_query", "now",
                                "list_software", "open_software"} -
                               {t["name"] for t in payload.get("tools") or []})))
        results.append(("技能已打包", len(payload.get("skills") or []) >= 2,
                        str([s["name"] for s in payload.get("skills") or []])))
        results.append(("提示词模板已打包", len(payload.get("templates") or []) == 5,
                        str(len(payload.get("templates") or []))))
        mode_ids = [m.get("id") for m in payload.get("modes") or []]
        results.append(("模式已就绪", mode_ids == ["craft", "plan", "ask", "teach", "free"],
                        str(mode_ids)))
        results.append(("子代理已就绪", len(payload.get("subagents") or []) >= 5,
                        str([s.get("role") for s in payload.get("subagents") or []])))
        results.append(("通道已就绪", len((payload.get("channels") or {}).get("items") or []) == 3,
                        str((payload.get("channels") or {}).get("items"))))
        # 这三栏是新增的，接口在打包后最容易因为「模块进了但数据没进」而 500
        for path, name in (("/api/assistants", "助理接口"),
                           ("/api/security", "安全接口"),
                           ("/api/runtime", "运行时接口"),
                           ("/api/changes/summary", "改动汇总接口"),
                           ("/api/changes/threads", "改动第一级接口"),
                           ("/api/software", "软件清单接口")):
            try:
                with urllib.request.urlopen(base + path, timeout=8) as response:
                    body = json.loads(response.read().decode("utf-8"))
                results.append((name + "能打开", bool(body.get("ok")), str(body)[:80]))
            except Exception as exc:  # noqa: BLE001
                results.append((name + "能打开", False, str(exc)))
        # 软件清单光「能打开」不够：打包漏了 winreg 相关逻辑时它会返回空清单而不报错
        try:
            with urllib.request.urlopen(base + "/api/software", timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
            results.append(("软件清单真扫出了东西",
                            int(body.get("total") or 0) > 20, body.get("total")))
        except Exception as exc:  # noqa: BLE001
            results.append(("软件清单真扫出了东西", False, str(exc)))

        # 选文件夹端点只能「查在不在」，不能真调 —— 调了会弹一个模态对话框把冒烟测试挂住。
        # 查 openapi 就够了：路由注册上了，说明 server.py 里那段装饰器跑到了。
        # （`app.folderdialog` 是函数里延迟导入的，PyInstaller 的静态分析也认它，
        #   打包后它在 PYZ.pyz 的 TOC 里 —— 这条断言管不到那一步，所以两边都查。）
        try:
            with urllib.request.urlopen(base + "/openapi.json", timeout=8) as response:
                schema = json.loads(response.read().decode("utf-8"))
            paths = schema.get("paths") or {}
            ok = "/api/fs/pick-folder" in paths
            results.append(("系统选文件夹端点注册上了", ok,
                            f"{len(paths)} 条路由" + ("" if ok else "，缺 pick-folder")))
        except Exception as exc:  # noqa: BLE001
            results.append(("系统选文件夹端点注册上了", False, str(exc)))

        # 删除会话的影响面接口：路由在不在，用「不存在的 cid」探一下最省事 ——
        # 返回 404 且是 JSON，说明路由通、逻辑跑了；405/500 才是没进去。
        try:
            urllib.request.urlopen(base + "/api/traces/nope/impact", timeout=6)
            results.append(("删除影响面接口在", False, "居然没 404"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            results.append(("删除影响面接口在", exc.code == 404 and "找不到" in body,
                            f"{exc.code} {body[:60]}"))
        except Exception as exc:  # noqa: BLE001
            results.append(("删除影响面接口在", False, str(exc)))

        try:
            with urllib.request.urlopen(base + "/", timeout=5) as response:
                html = response.read().decode("utf-8", errors="replace")
            results.append(("前端首页能打开", "DreamRole" in html and "app.js" in html, str(len(html))))
        except Exception as exc:  # noqa: BLE001
            results.append(("前端首页能打开", False, str(exc)))
        try:
            with urllib.request.urlopen(base + "/css/app.css", timeout=5) as response:
                css = response.read().decode("utf-8", errors="replace")
            results.append(("样式文件能取到", len(css) > 5000, str(len(css))))
        except Exception as exc:  # noqa: BLE001
            results.append(("样式文件能取到", False, str(exc)))
    else:
        results.append(("服务起来了", False, "启动超时"))

    try:
        kill_tree(process.pid)
        process.wait(timeout=8)
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass

    log_file = data_dir / "logs" / "dreamrole.log"
    if log_file.is_file():
        print("\n  应用日志：")
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]:
            print("    " + line)

    failed = 0
    print()
    for name, ok, detail in results:
        print(("  [ok]   " if ok else "  [FAIL] ") + name + ("" if ok else "  " + detail))
        failed += 0 if ok else 1

    shutil.rmtree(data_dir, ignore_errors=True)
    return failed == 0


def process_tree(root: int) -> set[int]:
    """父进程 + 它的所有后代。

    onefile 打包出来的 exe 不是「一个」进程：引导进程先把 55MB 解压到临时目录，
    再把真正的程序**另起一个子进程**来跑。窗口属于子进程。
    所以只盯父 pid 会得出「窗口没开」的假结论 —— 我就这么误判过一次。
    而只盯「窗口标题里含 dreamrole」又会把上一次没关干净的实例算进来。
    两个都不对，要认的是这棵树。
    """
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (0, -1):
        return {root}

    parents: dict[int, int] = {}
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)
    ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
    while ok:
        parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
        ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    kernel32.CloseHandle(snapshot)

    tree = {root}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in tree and pid not in tree:
                tree.add(pid)
                changed = True
    return tree


def titles_of(pids: set[int]) -> list[str]:
    """这些进程的可见顶层窗口标题。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles: list[str] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value not in pids:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value:
                titles.append(buffer.value)
        return True

    user32.EnumWindows(callback_type(collect), 0)
    return titles


def any_window_titled(needle: str) -> list[str]:
    """全桌面上标题含 needle 的可见窗口。

    兜底路径（`msedge --app`）开出来的窗口**不一定还在我们的进程树里**：
    Edge 是单实例的，新窗口可能挂到已经存在的浏览器进程上；
    而且 exe 退出之后子进程会被重新挂到别处。只认进程树的话，
    界面明明开着，检查却说「没有窗口」—— 那是测试眼瞎，不是程序坏了。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[str] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if needle.lower() in buffer.value.lower():
            found.append(buffer.value)
        return True

    user32.EnumWindows(callback_type(collect), 0)
    return found


def kill_tree(pid: int) -> None:
    """连子进程一起杀。

    单文件 exe 真正干活的是它派生出来的子进程；只 `terminate()` 父进程的话，
    子进程和它拉起的 msedgewebview2 会活下来。跑几轮之后机器上就是一片
    没人认领的浏览器进程，而且它们还占着 WebView2 的用户数据目录 ——
    下一次启动的壳反而起不来。这个坑在安装包脚本里踩过一次，这里一样。
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


def smoke_window(exe: Path) -> bool:
    """不带 --no-window 启动一次，确认桌面窗口真的出来了。

    这一步不能省：HTTP 起来了、页面也能取到，但窗口壳子挂了的话，
    用户双击看到的还是「什么也没发生」。而窗口这条路平时跑源码永远测不到。
    """
    step("冒烟测试：确认窗口能起来（脱离控制台，跟双击一样）")
    port = 8798
    data_dir = Path(tempfile.mkdtemp(prefix="dreamrole-window-"))
    env = dict(os.environ)
    env.update({"DREAMROLE_PORT": str(port), "DREAMROLE_DATA_DIR": str(data_dir)})
    env.pop("DREAMROLE_HEADLESS", None)

    log_file = data_dir / "logs" / "dreamrole.log"

    process = _spawn(
        exe,
        env,
        cwd=str(exe.parent),
        # 关键：DETACHED_PROCESS 且**不重定向任何标准句柄** —— 这才是「双击」的样子。
        # 以前这里用 DEVNULL，那等于给了子进程一根有效的管道，sys.stdout 不是 None，
        # 于是 uvicorn 配日志时会炸的那一步被掩盖了：真正双击启动时没有控制台、
        # sys.stdout 是 None，`sys.stdout.isatty()` 抛 AttributeError，服务永远起不来，
        # 用户看到的是「双击没反应」。冒烟测试对着假的环境点头，就是这个下场。
        creationflags=subprocess.DETACHED_PROCESS,
    )

    # onefile 要先把自己解压到临时目录，头几秒什么都还没发生 ——
    # 这时候轮询到的窗口、日志都不能算数，所以条件写成「等到它成立」而不是「看一眼」。
    ready = False
    titles: list[str] = []
    exited = None
    deadline = time.time() + 120
    title_deadline: float | None = None
    while time.time() < deadline:
        exited = process.poll()
        if log_file.is_file():
            text = log_file.read_text(encoding="utf-8", errors="replace")
            # 「界面能看」的两种合法结局：桌面壳里渲染出来了，或者退回浏览器窗口。
            usable = ("界面已渲染" in text) or ("改用浏览器窗口打开" in text)
            if "服务就绪" in text and usable:
                if not ready:
                    ready = True
                # 给窗口留一点出现的时间，而不是「读到日志的这一瞬间必须已经有窗口」。
                # 兜底那条路上，日志先落地、浏览器后起来，中间差个一两秒很正常。
                if title_deadline is None:
                    title_deadline = time.time() + 25
                titles = titles_of(process_tree(process.pid))
                if not titles:
                    # 进程树里找不到就换个更宽的判据：桌面上有没有一个叫 DreamRole 的窗口。
                    titles = any_window_titled("dreamrole")
                if titles:
                    break
                if time.time() > title_deadline:
                    break
        if exited is not None:
            # 进程退出了也别立刻走：兜底窗口是独立进程，可能比它晚一步出现。
            titles = titles or titles_of(process_tree(process.pid)) or any_window_titled("dreamrole")
            break
        time.sleep(0.5)

    kill_tree(process.pid)
    try:
        process.wait(timeout=8)
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass

    log_text = ""
    if log_file.is_file():
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
        print("  应用日志：")
        for line in log_text.splitlines()[-12:]:
            print("    " + line)

    if exited is not None:
        print(f"  exe 中途退出，退出码 {exited}")

    # 窗口开出来还不够：窗口比服务快一步打开的话，用户看到的是白屏。
    # 这两个条件必须同时成立才算过。
    rendered = "界面已渲染" in log_text
    fell_back = "改用浏览器窗口打开" in log_text
    found = any("dreamrole" in title.lower() for title in titles)
    print(("  [ok]   " if "服务就绪" in log_text else "  [FAIL] ") + "服务就绪（日志里真的写到了）")
    if rendered:
        print("  [ok]   界面在桌面壳里渲染完成")
    elif fell_back:
        print("  [ok]   桌面壳没渲染出来，已自动退回浏览器窗口（这条路也得能用）")
    else:
        print("  [FAIL] 界面既没渲染，也没退回浏览器窗口")
    print(("  [ok]   " if found else "  [FAIL] ") + "有可见窗口（先认进程树，兜底扫桌面标题）")
    shutil.rmtree(data_dir, ignore_errors=True)
    return found and ready


def webview_probe() -> str:
    """本机的 WebView2 现在到底能不能用 —— 用最小壳子问一遍。

    为什么要问：这台机器上见过 WebView2 时好时坏（尤其在 Edge / WebView2 运行时
    刚自动更新完、还没重启的时候）。那时候**任何一个** pywebview 窗口都起不来，
    包括只有一个 `<h1>` 的空白页。构建脚本如果只会说「窗口没起来」，
    你根本分不清是自己刚写的代码坏了，还是这台机器今天不适合测窗口。
    这个函数不碰项目代码，只用三行 pywebview，所以它失败就一定是环境。

    返回 "" 表示能用，否则返回一句人话。
    """
    import textwrap

    script = textwrap.dedent(
        """
        import threading, time, webview
        w = webview.create_window("probe", html="<h1>ok</h1>")
        got = {}
        def watch():
            time.sleep(5)
            try:
                got["text"] = w.evaluate_js("document.body.innerText")
            except Exception as exc:
                got["error"] = str(exc)
            try:
                w.destroy()
            except Exception:
                pass
        threading.Thread(target=watch, daemon=True).start()
        webview.start()
        print(repr(got))
        """
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "最小壳子 30 秒没结束（WebView2 卡住了）"
    out = (proc.stdout or "") + (proc.stderr or "")
    if "'text': 'ok'" in out or '"text": "ok"' in out:
        return ""
    if "'text': None" in out or '"text": null' in out:
        return "窗口起来了，但页面是空的（WebView2 没把 html 加载进去）"
    tail = " / ".join(line for line in out.strip().splitlines()[-3:] if line.strip())
    return tail or f"最小壳子退出码 {proc.returncode}，没有任何输出"


def main() -> int:
    onedir = "--onedir" in sys.argv
    ensure_icon()
    clean_old()
    clean()
    exe = build(onedir)
    clean_old()          # 这一次如果又挪开了旧产物，构建完之后再试一次删

    size = exe.stat().st_size / 1024 / 1024
    print(f"\n✓ 产物：{exe}")
    print(f"  大小：{size:.1f} MB")

    if "--no-smoke" in sys.argv:
        return 0

    ok = smoke(exe)
    if "--no-window-check" not in sys.argv and sys.platform == "win32":
        problem = webview_probe()
        if problem:
            # 环境问题，不算构建失败 —— 但必须说清楚，别让人以为窗口这条路验过了
            print("\n▸ 跳过窗口冒烟：本机的 WebView2 现在起不来")
            print(f"  最小壳子也失败：{problem}")
            print("  这跟本次构建无关（同一台机器上空白页也打不开）。")
            print("  重启之后想补验这一项，直接 `python scripts/build_exe.py` 再跑一次即可。")
        else:
            ok = smoke_window(exe) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
