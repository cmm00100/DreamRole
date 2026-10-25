r"""这台电脑上装了哪些软件，以及怎么把它们打开。

为什么单独一个模块：`open_target` 之前用 `shutil.which()` + `subprocess.Popen` 起进程，
踩了两个坑，都是「看起来成功了、其实没有」：

1. PATH 里混进了 git-bash 的 `usr/bin/`，`which("notepad")` 命中的是那个 **635 字节的 sh 脚本**
   （头两字节是 `#!`，不是 `MZ`）。Popen 起它 → `WinError 193 不是有效的 Win32 应用程序`，
   窗口一秒都没出现过。
2. `cmd.exe` 用 `DETACHED_PROCESS` 起，进程在、**控制台窗口没有**。用户看到的就是
   「启用一次，关一次」。

正解不是加 try/except 兜底，而是换主力通道：`ShellExecuteW` 就是双击那一下，
它自己会走 App Paths、文件关联、UWP 执行别名（`WindowsApps\*.exe` 那种 0 字节的
reparse point），也天生不阻塞。所以本模块只做三件事：

- **找**：按开始菜单快捷方式 / 注册表 App Paths / 内置别名，建一份「能打开的东西」清单；
- **判**：真 PE 校验（`MZ`）、跳过无后缀的 sh 脚本、认执行别名；
- **开**：一律 ShellExecute，然后**回头看一眼**有没有真出窗口 —— 报「已启动」而不看，
  就是上面那两个坑的成因。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# 一次扫描要遍历两个开始菜单和三个注册表根，不算便宜但也就几十毫秒。
# 缓存 5 分钟：用户装完软件刷新一下就能看到，不用每次问都重扫。
CACHE_TTL = 300.0
_CACHE: dict[str, object] = {"ts": 0.0, "items": [], "installed": []}

# 内置别名：开始菜单里叫「记事本」的那一项在有些精简系统上不存在，
# 而这几个名字模型张口就来，写死比查快了不止一个数量级。
BUILTIN = {
    "记事本": ("notepad.exe", "notepad"),
    "notepad": ("notepad.exe", "notepad"),
    "计算器": ("calc.exe", "calc"),
    "calc": ("calc.exe", "calc"),
    "画图": ("mspaint.exe", "mspaint"),
    "mspaint": ("mspaint.exe", "mspaint"),
    "资源管理器": ("explorer.exe", "explorer"),
    "explorer": ("explorer.exe", "explorer"),
    "终端": ("wt.exe", "wt"),
    "windows terminal": ("wt.exe", "wt"),
    "cmd": ("cmd.exe", "cmd"),
    "命令行": ("cmd.exe", "cmd"),
    "提示符": ("cmd.exe", "cmd"),
    "powershell": ("powershell.exe", "powershell"),
    "任务管理器": ("taskmgr.exe", "taskmgr"),
    "taskmgr": ("taskmgr.exe", "taskmgr"),
    "控制面板": ("control.exe", "control"),
    "截图工具": ("snippingtool.exe", "snippingtool"),
    "写字板": ("write.exe", "write"),
    "注册表": ("regedit.exe", "regedit"),
    "服务": ("services.msc", "services.msc"),
    "设备管理器": ("devmgmt.msc", "devmgmt.msc"),
    "计算器-旧": ("calc.exe", "calc"),
    "code": ("code.exe", "code"),
    "vscode": ("code.exe", "code"),
    "vs code": ("code.exe", "code"),
    "edge": ("msedge.exe", "msedge"),
    "浏览器": ("msedge.exe", "msedge"),
    "chrome": ("chrome.exe", "chrome"),
    "word": ("winword.exe", "winword"),
    "excel": ("excel.exe", "excel"),
    "powerpoint": ("powerpnt.exe", "powerpnt"),
}

# 开始菜单里这些不是「软件」，是文档/卸载入口，列出来只会让模型误点
_NOISE = (
    "uninstall", "卸载", "readme", "read me", "帮助", "help", "help center",
    "文档", "documentation", "website", "网站", "官网", "release notes",
    "更新日志", "license", "许可", "cmd prompt", "command prompt",
)

_WINAPPS = "\\windowsapps\\"

# ShellExecuteW 的返回码：<= 32 都是失败，而且每个数字的意思不一样，
# 直接报「失败」等于让用户自己去猜。
_SHELL_ERRORS = {
    0: "系统内存不足",
    2: "找不到这个文件",
    3: "找不到这个路径",
    5: "系统拒绝了这次启动（可能没有权限）",
    26: "文件被别的程序占着",
    27: "文件关联不完整",
    28: "DDE 超时",
    29: "DDE 启动失败",
    30: "DDE 正忙",
    31: "这个文件没有关联的打开方式",
    32: "缺少运行所需的 DLL",
}


# ---------------------------------------------------------------------------
# ShellExecute：唯一的启动通道
# ---------------------------------------------------------------------------

def shell_execute(target: str, args: str = "", cwd: str = "") -> tuple[bool, int, str]:
    """交给 shell 打开，和双击完全一样。返回 (成功, 返回码, 说明)。

    `lpVerb="open"` + `nShowCmd=SW_SHOWNORMAL(1)`：GUI 程序正常出窗口，
    控制台程序由系统给它分配一个真正的控制台 —— 这就是 `cmd` 之前不出窗口的原因，
    `DETACHED_PROCESS` 是「我不要窗口」，而用户要的恰恰是窗口。
    """
    if not IS_WINDOWS:
        return False, -1, "这台机器不是 Windows"
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError as exc:
        return False, -1, f"加载 shell32 失败：{exc}"

    # 不声明 restype 的话，64 位下 HINSTANCE 被截成 int，返回码判断会随机出错
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    code = int(
        shell32.ShellExecuteW(
            None, "open", target, args or None, cwd or None, 1
        )
        or 0
    )
    if code > 32:
        return True, code, ""
    return False, code, _SHELL_ERRORS.get(code, f"ShellExecute 返回 {code}")


# ---------------------------------------------------------------------------
# 什么才算「能启动的东西」
# ---------------------------------------------------------------------------

def _has_mz(path: Path) -> bool:
    """PE 文件头两字节是 `MZ`。读不出来就别判死，交给 shell 定夺。"""
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return True


def launchable(path: Path) -> bool:
    """这个路径能不能当可执行文件起。

    - 目录、不存在的：不能；
    - `WindowsApps\\*.exe`：能 —— 那是 0 字节的 reparse point（UWP 执行别名），
      读不出内容但系统认；
    - `.exe/.com`：头必须是 `MZ`；
    - `.bat/.cmd`：能，交给 cmd；
    - **无后缀**：不能。git-bash 的 `usr/bin/*` 全是这种 sh 脚本，正是 WinError 193 的来源。
    """
    try:
        if not path.exists() or path.is_dir():
            return False
    except OSError:
        return False
    if _WINAPPS in str(path).lower():
        return True
    suffix = path.suffix.lower()
    if suffix in (".exe", ".com"):
        return _has_mz(path)
    if suffix in (".bat", ".cmd"):
        return True
    return False


def which_real(name: str) -> tuple[Path | None, str]:
    """在 PATH 里找同名可执行文件，**只认真 PE**。

    不能用 `shutil.which`：它只看「文件在、可执行」，于是 git-bash 的 `notepad`
    （sh 脚本）排在 `C:\\Windows\\system32\\notepad.exe` 前面被选中。
    这里逐个候选过 `launchable()`，被跳过的原因一并带回去，方便解释。
    """
    skipped: list[str] = []
    raw = name if os.path.splitext(name)[1] else name
    names = [raw] if os.path.splitext(raw)[1] else [
        f"{raw}{ext}" for ext in (".exe", ".com", ".bat", ".cmd")
    ]
    for folder in (os.environ.get("PATH") or "").split(os.pathsep):
        folder = (folder or "").strip().strip('"')
        if not folder or not os.path.isdir(folder):
            continue
        for candidate in names:
            path = Path(folder) / candidate
            if not path.exists():
                continue
            if launchable(path):
                return path, ""
            skipped.append(str(path))
    return None, ("跳过了 " + "、".join(skipped[:3]) if skipped else "")


# ---------------------------------------------------------------------------
# 建目录
# ---------------------------------------------------------------------------

def _start_menu_dirs() -> list[Path]:
    dirs = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        folder = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if folder.is_dir():
            dirs.append(folder)
    return dirs


def _scan_start_menu() -> list[dict]:
    out: list[dict] = []
    for folder in _start_menu_dirs():
        try:
            shortcuts = list(folder.rglob("*.lnk"))
        except OSError:
            continue
        for lnk in shortcuts:
            name = lnk.stem.strip()
            if not name:
                continue
            low = name.lower()
            if any(noise in low for noise in _NOISE):
                continue
            out.append({
                "name": name,
                "target": str(lnk),
                "kind": "app",
                "source": "start-menu",
                "group": lnk.parent.name if lnk.parent != folder else "",
            })
    return out


def _scan_app_paths() -> list[dict]:
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    )
    out: list[dict] = []
    for hive, sub in roots:
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        with key:
            total = winreg.QueryInfoKey(key)[0]
            for index in range(total):
                try:
                    sub_name = winreg.EnumKey(key, index)
                except OSError:
                    continue
                if sub_name.lower() in ("default",):
                    continue
                try:
                    item = winreg.OpenKey(key, sub_name)
                except OSError:
                    continue
                with item:
                    try:
                        value, _kind = winreg.QueryValueEx(item, "")
                    except OSError:
                        continue
                exe = os.path.expandvars(str(value).strip().strip('"'))
                if not exe or not Path(exe).exists():
                    continue
                name = Path(sub_name).stem
                out.append({
                    "name": name,
                    "target": exe,
                    "kind": "app",
                    "source": "app-paths",
                    "group": "",
                })
    return out


def _scan_builtin() -> list[dict]:
    out = []
    for label, (exe, alias) in BUILTIN.items():
        out.append({
            "name": label,
            "target": exe,
            "alias": alias,
            "kind": "app",
            "source": "builtin",
            "group": "",
        })
    return out


def _merge(*batches: list[dict]) -> list[dict]:
    """同名只留一份：开始菜单 > App Paths > 内置。

    开始菜单排第一是因为快捷方式**带启动参数和起始目录**（Chrome 的 profile、
    VS Code 的 workspace 都可能在里面），直接起 exe 会丢掉这些。
    """
    merged: dict[str, dict] = {}
    for batch in batches:
        for item in batch:
            key = item["name"].lower()
            if key in merged:
                continue
            merged[key] = item
    items = list(merged.values())
    items.sort(key=lambda i: (i["name"].lower(), i["source"]))
    return items


def catalogue(refresh: bool = False) -> list[dict]:
    """所有「能打开的东西」。有缓存，5 分钟过期。"""
    now = time.time()
    cached = _CACHE.get("items") or []
    if not refresh and cached and now - float(_CACHE.get("ts") or 0) < CACHE_TTL:
        return list(cached)  # type: ignore[arg-type]

    items = _merge(_scan_start_menu(), _scan_app_paths(), _scan_builtin())
    _CACHE["items"] = items
    _CACHE["ts"] = now
    return list(items)


def installed(refresh: bool = False) -> list[dict]:
    """卸载注册表里的软件清单（名字/版本/发布者）。用来回答「装了些什么」。"""
    now = time.time()
    cached = _CACHE.get("installed") or []
    if not refresh and cached and now - float(_CACHE.get("ts") or 0) < CACHE_TTL:
        return list(cached)  # type: ignore[arg-type]
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    rows: list[dict] = []
    seen: set[str] = set()
    for hive, sub in roots:
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        with key:
            total = winreg.QueryInfoKey(key)[0]
            for index in range(total):
                try:
                    item = winreg.OpenKey(key, winreg.EnumKey(key, index))
                except OSError:
                    continue
                with item:
                    def read(field: str) -> str:
                        try:
                            value, _ = winreg.QueryValueEx(item, field)
                            return str(value).strip()
                        except OSError:
                            return ""

                    if read("SystemComponent") in ("1", "True"):
                        continue  # 系统组件，不是用户装的软件
                    release = read("ReleaseType").lower()
                    if "update" in release or "hotfix" in release:
                        continue
                    name = read("DisplayName")
                    if not name or name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    rows.append({
                        "name": name,
                        "version": read("DisplayVersion"),
                        "publisher": read("Publisher"),
                        "where": read("InstallLocation"),
                    })
    rows.sort(key=lambda r: r["name"].lower())
    _CACHE["installed"] = rows
    return rows


# ---------------------------------------------------------------------------
# 查
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    value = (text or "").strip().lower()
    for ext in (".exe", ".lnk", ".com", ".msc"):
        if value.endswith(ext):
            value = value[: -len(ext)]
    return value


def search(query: str = "", limit: int = 40) -> list[dict]:
    """模糊找软件。完全相等 > 开头相同 > 包含，同级按名字短的在前。"""
    needle = _norm(query)
    items = catalogue()
    if not needle:
        return items[: max(1, limit)]
    scored: list[tuple[int, int, dict]] = []
    for item in items:
        name = _norm(item["name"])
        if name == needle:
            rank = 0
        elif name.startswith(needle):
            rank = 1
        elif needle in name:
            rank = 2
        else:
            continue
        scored.append((rank, len(name), item))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in scored[: max(1, limit)]]


def find(query: str) -> dict | None:
    hits = search(query, limit=1)
    return hits[0] if hits else None


# ---------------------------------------------------------------------------
# 开 + 回头看
# ---------------------------------------------------------------------------

def _visible_windows() -> dict[int, tuple[str, str]]:
    """hwnd → (标题, 进程名)。启动前后各拍一张，差集就是新窗口。"""
    if not IS_WINDOWS:
        return {}
    try:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return {}

    def process_name(pid: int) -> str:
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name
            return ""
        finally:
            kernel32.CloseHandle(handle)

    found: dict[int, tuple[str, str]] = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title == "Program Manager":
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found[int(hwnd)] = (title, process_name(pid.value))
        return True

    user32.EnumWindows(collect, 0)
    return found


def launch(target: str, args: str = "", wait: float = 1.8) -> dict:
    """启动并回头看：真的出窗口了才说「打开了」。

    `wait` 是给窗口出现留的时间。UWP 应用（计算器、画图）冷启动要一两秒，
    等太短会误报「没起来」，等太长又拖慢对话，1.8 秒是个折中。
    """
    target = (target or "").strip()
    if not target:
        return {"ok": False, "message": "没有说要启动什么"}

    before = _visible_windows()
    ok, code, why = shell_execute(target, args)
    if not ok:
        return {"ok": False, "code": code, "message": f"打不开 {target}：{why}"}

    deadline = time.time() + max(0.0, wait)
    new: list[tuple[int, str, str]] = []
    while True:
        time.sleep(0.25)
        now = _visible_windows()
        new = [
            (hwnd, title, proc)
            for hwnd, (title, proc) in now.items()
            if hwnd not in before
        ]
        if new or time.time() >= deadline:
            break

    if new:
        head = new[0]
        extra = f"（另有 {len(new) - 1} 个新窗口）" if len(new) > 1 else ""
        return {
            "ok": True,
            "confirmed": True,
            "window": {"title": head[1], "process": head[2], "hwnd": head[0]},
            "message": f"已打开「{head[1]}」{extra}",
        }
    return {
        "ok": True,
        "confirmed": False,
        "message": (
            f"已经交给系统启动 {target}，但 {wait:.0f} 秒内没看到新窗口。"
            "可能它启动更慢、只驻留托盘、或本来就没有窗口 —— 用 list_windows 复查一下"
        ),
    }


def open_folder(key: str) -> Path | None:
    """常用目录。模型说「打开下载文件夹」时不用去猜路径。"""
    homes = {
        "桌面": "USERPROFILE/Desktop",
        "下载": "USERPROFILE/Downloads",
        "文档": "USERPROFILE/Documents",
        "图片": "USERPROFILE/Pictures",
        "音乐": "USERPROFILE/Music",
        "视频": "USERPROFILE/Videos",
        "临时": "TEMP",
    }
    want = (key or "").strip()
    for label, spec in homes.items():
        if label in want:
            root, _, rest = spec.partition("/")
            base = os.environ.get(root, "")
            if not base:
                return None
            folder = Path(base) / rest if rest else Path(base)
            return folder if folder.is_dir() else None
    return None
