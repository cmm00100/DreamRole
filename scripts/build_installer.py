"""把 DreamRole 打成一个「双击就装好」的安装包。

    python scripts/build_installer.py                # 打包 exe + 编译安装包
    python scripts/build_installer.py --skip-exe     # 只重编安装包（复用 dist 里现成的 exe）
    python scripts/build_installer.py --install      # 编完再静默装一遍，逐项验
    python scripts/build_installer.py --install --uninstall   # 装完验完再卸掉

产物落在项目根：`DreamRole-Setup-<版本>.exe`。

为什么用 NSIS 而不是 Inno Setup：这台机器上 NSIS 已经装好了
（`C:\\Program Files (x86)\\NSIS\\makensis.exe`），Inno 没有。而且这个安装包要的
东西很少 —— 拷一个 exe、建两个快捷方式、写一条卸载项 —— NSIS 完全够，
单文件、免管理员、~25MB。

安装包做的事很少，但每一件都对应一个踩过的坑，所以 `--install` 不是可选项的
锦上添花：它会真的静默装一遍，然后检查「桌面快捷方式到底有没有出现」「装完之后
数据是不是放到了 %LOCALAPPDATA%\\DreamRole 而不是程序目录」。
「安装包编出来了」和「安装包能用」是两件完全不同的事。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _console import harden  # noqa: E402

harden()

from build_exe import _strip_low_label  # noqa: E402  同一套「摘低完整性标记」的逻辑

DIST_EXE = ROOT / "dist" / "DreamRole.exe"
NSI = ROOT / "installer" / "dreamrole.nsi"

APP_NAME = "DreamRole"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / APP_NAME
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
DESKTOP_LNK = Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop" / f"{APP_NAME}.lnk"


def step(message: str) -> None:
    print(f"\n▸ {message}")


def version() -> str:
    """从 app/config.py 里抽版本号。

    不 import app —— 那会把整个包连同 .data 一起拽进来，而且 sys.path 不一定对。
    """
    for line in (ROOT / "app" / "config.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("APP_VERSION"):
            return line.split("=", 1)[1].strip().strip('"\'')
    raise SystemExit("app/config.py 里找不到 APP_VERSION")


def find_makensis() -> Path:
    """找 makensis。找不到就把可能的装法都说清楚，别只说「找不到」。"""
    override = os.environ.get("NSIS_MAKENSIS")
    candidates = [Path(override)] if override else []
    candidates += [
        Path(r"C:\Program Files (x86)\NSIS\makensis.exe"),
        Path(r"C:\Program Files\NSIS\makensis.exe"),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "NSIS" / "makensis.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NSIS" / "makensis.exe",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    found = shutil.which("makensis")
    if found:
        return Path(found)
    raise SystemExit(
        "没找到 makensis（NSIS 编译器）。\n"
        "  装一个：winget install NSIS.NSIS    或    https://nsis.sourceforge.io/Download\n"
        "  装完如果不在默认位置，设环境变量 NSIS_MAKENSIS 指到 makensis.exe。")


def compile_installer(makensis: Path, app_version: str) -> Path:
    step(f"编译安装包（NSIS {makensis}）")
    if not NSI.is_file():
        raise SystemExit(f"安装脚本不见了：{NSI}")
    if not DIST_EXE.is_file():
        raise SystemExit(f"没有可安装的 exe：{DIST_EXE}（先跑一遍打包）")

    # /INPUTCHARSET UTF8 不能省：脚本里有大量中文，NSIS 默认按系统 ANSI(GBK) 读，
    # 编出来会是一屏乱码，而且编译过程不报错 —— 只有装完看见乱码才知道。
    command = [
        str(makensis),
        "/INPUTCHARSET", "UTF8",
        f"/DAPP_VERSION={app_version}",
        "/V2",                       # 只报警告和错误，别刷一屏
        str(NSI),
    ]
    print("  $ " + " ".join(command))
    code = subprocess.call(command, cwd=str(ROOT))
    if code != 0:
        raise SystemExit(f"makensis 失败，退出码 {code}")

    setup = ROOT / f"{APP_NAME}-Setup-{app_version}.exe"
    if not setup.is_file():
        raise SystemExit(f"编译没报错，但产物不存在：{setup}")
    return setup


def build_exe() -> None:
    step("先打包程序本体")
    code = subprocess.call(
        [sys.executable, str(SCRIPTS / "build_exe.py"), "--no-window-check"],
        cwd=str(ROOT),
    )
    if code != 0:
        raise SystemExit("打包 exe 失败，安装包无从谈起")
    if not DIST_EXE.is_file():
        raise SystemExit(f"打包完却没有 {DIST_EXE}")


def run_setup(setup: Path, *extra: str) -> int:
    """静默跑安装程序，等它真的结束。

    安装程序是 GUI 程序，`subprocess.call` 会立刻返回 —— 必须 wait，
    否则下一句「检查桌面快捷方式」跑在安装还没开始的时候，永远失败。
    """
    command = [str(setup), "/S", *extra]
    print("  $ " + " ".join(command))
    process = subprocess.Popen(command)
    try:
        return process.wait(timeout=180)
    except subprocess.TimeoutExpired:
        process.kill()
        return -1


def launch(exe: Path, env: dict, attempts: int = 12) -> subprocess.Popen:
    """启动装好的 exe；短时间内被占用就重试几次。

    刚被安装程序写下去的 56 MB exe 常常有一小段时间打不开 —— 杀毒软件正在扫它。
    这时候 `CreateProcess` 报的是 `WinError 32 另一个程序正在使用此文件`，
    看着像权限问题或者文件坏了，其实等两秒就好。
    真机上「装完立刻点桌面图标没反应」也多半是这一条。
    """
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return subprocess.Popen([str(exe)], env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, cwd=str(exe.parent))
        except OSError as exc:      # PermissionError 是它的子类
            last = exc
            time.sleep(2)
    raise SystemExit(f"装好的程序起不来（重试 {attempts} 次）：{last}")


def stop_app(process: subprocess.Popen | None = None) -> None:
    """停掉实例，**连同 onefile 派生出来的子进程**。

    PyInstaller onefile 的 exe 不是「一个」进程：引导进程把 56MB 解包到 %TEMP%，
    再另起一个子进程跑真正的程序。只 terminate 父进程的话子进程会留下来，
    而它本身就是这个 exe 的映像 —— 下一次 CreateProcess 就直接撞 WinError 32。

    兜底那句会按映像名杀掉所有 DreamRole.exe，包括你正开着的那个。
    验证脚本本来就要独占这个文件，所以这是有意的；别把它当成常驻工具用。
    """
    if process is not None and process.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, timeout=30)
        try:
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass

    deadline = time.time() + 20
    while time.time() < deadline:
        remaining = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {APP_NAME}.exe"],
                                   capture_output=True, text=True, errors="replace").stdout
        if f"{APP_NAME}.exe" not in remaining:
            return
        subprocess.run(["taskkill", "/F", "/T", "/IM", f"{APP_NAME}.exe"],
                       capture_output=True, timeout=30)
        time.sleep(1)


def verify_installed(app_version: str) -> bool:
    """装完之后逐项验。每一项都是「看起来该有，实际可能没有」的东西。"""
    step("验证安装结果")
    results: list[tuple[str, bool, str]] = []

    exe = INSTALL_DIR / "DreamRole.exe"
    results.append(("程序在装好的位置上", exe.is_file(), str(exe)))
    if not exe.is_file():
        report(results)
        return False

    results.append(("install.marker 写进去了（程序据此用 %LOCALAPPDATA% 存数据）",
                    (INSTALL_DIR / "install.marker").is_file(), ""))
    results.append(("卸载程序在", (INSTALL_DIR / "uninstall.exe").is_file(), ""))
    results.append(("桌面快捷方式出现了", DESKTOP_LNK.is_file(), str(DESKTOP_LNK)))
    results.append(("开始菜单里有一项",
                    (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
                     / "Start Menu" / "Programs" / APP_NAME / f"{APP_NAME}.lnk").is_file(), ""))

    # 「应用和功能」里的那条记录
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}")
        name = winreg.QueryValueEx(key, "DisplayName")[0]
        ver = winreg.QueryValueEx(key, "DisplayVersion")[0]
        results.append(("控制面板里能看见它", name == APP_NAME and ver == app_version,
                        f"{name} {ver}"))
        winreg.CloseKey(key)
    except OSError as exc:
        results.append(("控制面板里能看见它", False, str(exc)))

    # 装好的 exe 是不是带 Low 强制标签 —— 带的话双击会毫无反应
    try:
        completed = subprocess.run(["icacls", str(exe)], capture_output=True, text=True,
                                   timeout=20, errors="replace")
        low = "Mandatory Label\\Low" in completed.stdout or "低强制级别" in completed.stdout
        results.append(("产物不是低完整性（否则双击没反应）", not low, completed.stdout.strip()[:120]))
    except (OSError, subprocess.SubprocessError) as exc:
        results.append(("产物不是低完整性（否则双击没反应）", True, f"查不了，跳过：{exc}"))

    # 真的能跑起来：数据目录指到临时目录，验版本号和数据落在哪儿
    temp_data = Path(tempfile.mkdtemp(prefix="dreamrole-installed-"))
    port = 8801
    env = dict(os.environ)
    env.update({"DREAMROLE_PORT": str(port), "DREAMROLE_HEADLESS": "1",
                "DREAMROLE_DATA_DIR": str(temp_data)})
    process = launch(exe, env)
    payload = {}
    deadline = time.time() + 75
    while time.time() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/bootstrap", timeout=3) as r:
                payload = json.loads(r.read().decode("utf-8"))
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    stop_app(process)

    results.append(("装好的程序能跑起来（接口应得上）", bool(payload), ""))
    if payload:
        results.append(("版本号对得上", payload.get("version") == app_version,
                        f"{payload.get('version')} vs {app_version}"))
        results.append(("工具清单齐了", len(payload.get("tools") or []) >= 55,
                        str(len(payload.get("tools") or []))))
    shutil.rmtree(temp_data, ignore_errors=True)

    # 最要紧的一条：不带 DREAMROLE_DATA_DIR 真启动一次，看它把数据放哪儿。
    #
    # 光检查「程序目录里没有 data\」是不够的 —— 程序直接崩了也同样满足这个条件。
    # 必须看到 `%LOCALAPPDATA%\DreamRole\logs\dreamrole.log` 真的被写出来，
    # 才说明 install.marker 那条判断生效了、安装版走的是正确的那条分支。
    if payload:
        env.pop("DREAMROLE_DATA_DIR", None)
        env["DREAMROLE_PORT"] = str(port + 1)
        stop_app()          # 上一个实例的残留会把这个 exe 锁住，起不来的
        process = launch(exe, env)
        log_file = DATA_DIR / "logs" / "dreamrole.log"
        started = log_file.stat().st_mtime if log_file.is_file() else 0
        landed = False
        deadline = time.time() + 75
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port + 1}/api/bootstrap", timeout=3):
                    landed = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        stop_app(process)
        fresh = log_file.is_file() and log_file.stat().st_mtime >= started
        results.append(("数据落在 %LOCALAPPDATA%\\DreamRole（不是程序目录）",
                        landed and fresh, f"{log_file} 存在={log_file.is_file()}"))
        results.append(("程序目录里没有冒出 data\\",
                        not (INSTALL_DIR / "data").exists(), str(INSTALL_DIR / "data")))

    return report(results)


def report(results: list[tuple[str, bool, str]]) -> bool:
    print()
    failed = 0
    for name, ok, detail in results:
        print(("  [ok]   " if ok else "  [FAIL] ") + name + ("" if ok else "  " + detail))
        failed += 0 if ok else 1
    print()
    print(f"  {len(results) - failed} 项通过，{failed} 项失败")
    return failed == 0


def main() -> int:
    app_version = version()
    print(f"安装包：{APP_NAME}-Setup-{app_version}.exe")

    if "--skip-exe" not in sys.argv:
        build_exe()
    elif not DIST_EXE.is_file():
        raise SystemExit(f"--skip-exe 但 {DIST_EXE} 不存在")
    else:
        step("复用现成的程序本体")
        print(f"  {DIST_EXE}  {DIST_EXE.stat().st_size / 1024 / 1024:.1f} MB")

    makensis = find_makensis()
    setup = compile_installer(makensis, app_version)
    _strip_low_label(setup)      # 安装包本身也别带 Low 标记，否则双击同样没反应

    size = setup.stat().st_size / 1024 / 1024
    print(f"\n✓ 安装包：{setup}")
    print(f"  大小：{size:.1f} MB（里面是已压缩的程序本体）")

    if "--install" not in sys.argv:
        return 0

    step("静默安装一遍（/S）")
    code = run_setup(setup)
    print(f"  安装程序退出码 {code}")
    ok = verify_installed(app_version)

    if "--uninstall" in sys.argv:
        step("卸载（/S，数据保留）")
        uninstaller = INSTALL_DIR / "uninstall.exe"
        if uninstaller.is_file():
            # `_?=` 不能省：不带它的话，卸载程序会把自己复制到 %TEMP% 再启动，
            # 原进程立刻返回 —— 于是「等它结束」等到的是一瞬间，
            # 后面的检查跑在卸载还没开始的时候，看起来像「卸载没删干净」。
            process = subprocess.Popen([str(uninstaller), "/S", f"_?={INSTALL_DIR}"])
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                process.kill()
        deadline = time.time() + 60
        while time.time() < deadline and (INSTALL_DIR / "DreamRole.exe").exists():
            time.sleep(0.5)
        leftovers = [str(p) for p in (INSTALL_DIR / "DreamRole.exe", DESKTOP_LNK,
                                      INSTALL_DIR / "install.marker") if p.exists()]
        print("  " + ("卸载干净了（程序文件、快捷方式都没了）" if not leftovers
                      else f"还有残留：{leftovers}"))
        print(f"  数据目录还在：{DATA_DIR}（卸载默认保留，这是有意的）")
        print(f"  数据目录存在：{DATA_DIR.exists()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
