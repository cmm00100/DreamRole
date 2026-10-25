"""操作这台电脑：开应用、切窗口、截屏、键鼠、剪贴板。

只用 Windows 自带能力（ctypes + Pillow），不引 pyautogui 那一套。

几条自己划的线：
- 启动应用一律走 `ShellExecute`（见 app/software.py），不自己 Popen、不挂控制台窗口；
- 截屏只写项目的 shots/，路径明确告诉模型和用户（截图可能含隐私）；
- 「不点登录、不填密码、不点支付」这类判断没法用代码写死，写在角色规则里由模型自觉，
  但**所有会动到屏幕的工具都要过权限确认**（见 app/permissions.py 的 DANGER_TOOLS）。
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

from .. import software as software_book
from .base import ToolContext

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from ctypes import wintypes


def _unsupported() -> str:
    return "这台机器不是 Windows，应用/窗口/键鼠这些只做了 Windows 实现。"


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


# ---------------------------------------------------------------------------
# 打开
# ---------------------------------------------------------------------------

def _launch_app(name: str, args: str = "", wait: float = 1.8) -> dict:
    """按名字启动，一个落点不行就换下一个。

    顺序有讲究：
    1. **名字本身**（或内置别名）—— ShellExecute 会查 App Paths、UWP 执行别名、PATH，
       和用户双击是一模一样的路径；
    2. **清单里记的落点** —— 开始菜单的 .lnk 带着启动参数和起始目录，起 exe 会丢这些；
    3. **PATH 里第一个真 PE** —— 这条是补漏，挡住 git-bash `usr/bin/` 那种 sh 脚本。
    """
    tried: list[str] = []
    seen: set[str] = set()

    def attempt(target: str) -> dict | None:
        key = (target or "").strip().lower()
        if not key or key in seen:
            return None
        seen.add(key)
        result = software_book.launch(target, args, wait=wait)
        if result.get("ok"):
            return result
        tried.append(f"{target}（{result.get('message', '打不开')}）")
        return None

    hit = software_book.find(name)
    targets: list[str] = [name]
    if hit:
        if hit.get("alias"):
            targets.append(str(hit["alias"]))
        targets.append(str(hit["target"]))
    real, why_skipped = software_book.which_real(name)
    if real:
        targets.append(str(real))

    for target in targets:
        done = attempt(target)
        if done:
            done["via"] = target
            return done

    hint = ""
    if hit is None:
        near = [item["name"] for item in software_book.search(name, limit=5)]
        if near:
            hint = "，清单里没有同名的。相近的有：" + "、".join(near)
        else:
            hint = "，清单里也没有相近的名字，先用 list_software 看看这台机器上有什么"
    if why_skipped:
        hint += f"（{why_skipped}）"
    detail = "；".join(tried[:3])
    return {
        "ok": False,
        "message": f"打不开「{name}」{hint}" + (f"\n试过：{detail}" if detail else ""),
    }


def open_target(ctx: ToolContext, target: str, args: str = "") -> str:
    """打开网址 / 文件 / 目录 / 应用。四种落点各走各的分支，都交给 shell。"""
    target = (target or "").strip().strip('"')
    if not target:
        return "要给个名字：应用名（notepad）、文件路径，或 http(s) 网址"

    low = target.lower()
    if low.startswith(("http://", "https://", "mailto:")):
        ok, _code, why = software_book.shell_execute(target)
        return f"已用默认浏览器打开 {target}" if ok else f"打不开这个网址：{why}"

    path = Path(target).expanduser()
    if path.exists():
        ok, _code, why = software_book.shell_execute(str(path), args)
        if not ok:
            return f"打不开 {path}：{why}"
        return f"已用默认程序打开 {path}"

    folder = software_book.open_folder(target)
    if folder is not None:
        ok, _code, why = software_book.shell_execute(str(folder))
        return f"已打开文件夹 {folder}" if ok else f"打不开 {folder}：{why}"

    result = _launch_app(target, args)
    if not result.get("ok"):
        return str(result.get("message") or f"找不到应用「{target}」")
    tail = "" if result.get("confirmed") else "（没等到新窗口，可以 list_windows 复查）"
    return f"{result.get('message')}{tail} → {result.get('via') or target}"


def list_software(ctx: ToolContext, query: str = "", limit: int = 60, refresh: bool = False) -> str:
    """这台机器上有什么软件可以开。"""
    if not IS_WINDOWS:
        return _unsupported()
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 60

    items = software_book.search(query, limit=limit) if query else software_book.catalogue(refresh=refresh)[:limit]
    total = len(software_book.catalogue(refresh=False))
    if not items:
        return f"没有名字含「{query}」的软件。清单里一共 {total} 项，可以不带 query 再列一次。"

    lines = [f"可启动的软件 {total} 项，这里是 {len(items)} 项（名字 → 落点）："]
    for item in items:
        tag = {
            "start-menu": "开始菜单",
            "app-paths": "注册表",
            "builtin": "内置",
        }.get(item["source"], item["source"])
        where = item["target"]
        if len(where) > 96:
            where = "…" + where[-93:]
        lines.append(f"- {item['name']}  [{tag}]  {where}")
    lines.append(
        "要开哪个就用 open_software（或 open_target）。"
        "打开之后可以用 focus_window / type_text / press_keys 接着操作它，"
        "但**每次动屏幕前都要先确认焦点窗口**。"
    )
    return "\n".join(lines)


def open_software(ctx: ToolContext, name: str, args: str = "") -> str:
    """启动一个本地软件，并回报它到底有没有出窗口。"""
    if not IS_WINDOWS:
        return _unsupported()
    name = (name or "").strip()
    if not name:
        return "要给个软件名。不知道有什么就先 list_software。"

    hit = software_book.find(name)
    label = hit["name"] if hit else name
    result = _launch_app(name, args, wait=2.2)

    if not result.get("ok"):
        return str(result.get("message") or f"打不开「{name}」")

    window = result.get("window") or {}
    if window:
        return (
            f"已启动「{label}」：窗口「{window.get('title')}」"
            f"（进程 {window.get('process') or '?'} · hwnd {window.get('hwnd')}）\n"
            "接着要操作它的话，先 focus_window 切到它，再 type_text / press_keys。"
        )
    return f"已把「{label}」交给系统启动，但 2 秒内没看到新窗口，可以 list_windows 复查一次。"



# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

def _process_name(pid: int) -> str:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_windows(ctx: ToolContext, limit: int = 40) -> str:
    if not IS_WINDOWS:
        return _unsupported()
    user32 = _user32()
    rows: list[tuple[str, str, int]] = []

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
        if not title or title in ("Program Manager", "Windows 输入体验"):
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rows.append((title, _process_name(pid.value), int(hwnd)))
        return True

    user32.EnumWindows(collect, 0)
    if not rows:
        return "没抓到任何可见窗口。"
    rows.sort(key=lambda r: r[1])
    lines = [f"当前可见窗口 {len(rows)} 个（最多列 {limit} 个）："]
    for title, proc, hwnd in rows[: max(1, min(limit, 80))]:
        lines.append(f"- {title}\n  进程 {proc or '?'} · hwnd {hwnd}")
    return "\n".join(lines)


def _match_windows(fragment: str) -> list[tuple[str, int]]:
    user32 = _user32()
    found: list[tuple[str, int]] = []
    needle = fragment.strip().lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle in buf.value.lower():
            found.append((buf.value, int(hwnd)))
        return True

    user32.EnumWindows(collect, 0)
    found.sort(key=lambda r: (r[0].lower() != needle, not r[0].lower().startswith(needle), len(r[0])))
    return found


def focus_window(ctx: ToolContext, title: str) -> str:
    if not IS_WINDOWS:
        return _unsupported()
    title = (title or "").strip()
    if not title:
        return "要给一个窗口标题里的片段，比如「记事本」「Chrome」"

    hits = _match_windows(title)
    if not hits:
        return f"没有标题包含「{title}」的窗口。先 list_windows 看看现在开着什么。"

    name, hwnd = hits[0]
    user32 = _user32()
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE

    # 直接 SetForegroundWindow 常被系统拒（前台锁），先接上当前前台线程的输入队列
    fg = user32.GetForegroundWindow()
    if fg and int(fg) != hwnd:
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        my_thread = ctypes.WinDLL("kernel32").GetCurrentThreadId()
        user32.AttachThreadInput(my_thread, fg_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(my_thread, fg_thread, False)
    else:
        user32.SetForegroundWindow(hwnd)

    extra = ""
    if len(hits) > 1:
        others = "、".join(f"「{n}」" for n, _ in hits[1:4])
        extra = f"（还有 {len(hits) - 1} 个也匹配：{others}，要换哪个说得更具体些）"
    return f"已切到「{name}」{extra}"


def _window_rect(title: str):
    hits = _match_windows(title) if title else []
    if not hits:
        return None
    rect = wintypes.RECT()
    _user32().GetWindowRect(hits[0][1], ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


# ---------------------------------------------------------------------------
# 截屏
# ---------------------------------------------------------------------------

def screenshot(ctx: ToolContext, window: str = "") -> str:
    try:
        from PIL import ImageGrab
    except ImportError:
        return "没装 Pillow，截不了屏（打包版自带）。"

    box = None
    label = "整个屏幕"
    if window.strip():
        if not IS_WINDOWS:
            return _unsupported()
        box = _window_rect(window)
        if not box:
            return f"没找到标题包含「{window}」的窗口，先 list_windows 确认。"
        label = f"窗口「{window}」"

    try:
        shot = ImageGrab.grab(bbox=box, all_screens=True)
    except Exception as exc:  # noqa: BLE001
        return f"截屏失败：{exc}"

    folder = ctx.root / "shots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    shot.save(path)
    return (
        f"已截图（{label}，{shot.width}×{shot.height}）→ {path}\n"
        "注意：截图里可能有用户隐私，只在本机用，别把内容原样贴给别人。"
    )


# ---------------------------------------------------------------------------
# 键鼠
# ---------------------------------------------------------------------------

if IS_WINDOWS:

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1

VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "printscreen": 0x2C,
}
for _i in range(1, 13):
    VK[f"f{_i}"] = 0x6F + _i


def _send_key(vk: int, up: bool = False) -> None:
    item = _INPUT(type=INPUT_KEYBOARD)
    item.union.ki = _KEYBDINPUT(0, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    item.union.ki.wVk = vk
    _user32().SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))


def _send_char(ch: str) -> None:
    for up in (False, True):
        item = _INPUT(type=INPUT_KEYBOARD)
        item.union.ki = _KEYBDINPUT(
            0, ord(ch), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, None
        )
        _user32().SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))


def _vk_of(token: str):
    token = token.strip().lower()
    if token in VK:
        return VK[token]
    if len(token) == 1:
        code = ord(token.upper())
        if 0x30 <= code <= 0x5A:
            return code
    return None


def type_text(ctx: ToolContext, text: str) -> str:
    if not IS_WINDOWS:
        return _unsupported()
    if not text:
        return "没有要输入的内容"
    count = 0
    for ch in text:
        if ch == "\n":
            _send_key(VK["enter"])
            _send_key(VK["enter"], up=True)
        elif ch == "\t":
            _send_key(VK["tab"])
            _send_key(VK["tab"], up=True)
        else:
            _send_char(ch)
        count += 1
        time.sleep(0.004)  # 太快某些程序会吞键
    return f"已输入 {count} 个字符（发到当前焦点窗口；打错地方可以用 focus_window 切回来）"


def press_keys(ctx: ToolContext, keys: str) -> str:
    if not IS_WINDOWS:
        return _unsupported()
    combo = [p for p in (keys or "").replace(" ", "").split("+") if p]
    if not combo:
        return "要说明按什么键，比如 ctrl+s"

    codes = []
    for token in combo:
        vk = _vk_of(token)
        if vk is None:
            return f"不认识的键：{token}（支持 a-z、0-9、f1-f12、ctrl/alt/shift/win、enter/tab/esc/方向键等）"
        codes.append(vk)

    for vk in codes:
        _send_key(vk)
        time.sleep(0.01)
    for vk in reversed(codes):
        _send_key(vk, up=True)
        time.sleep(0.01)
    return f"已按下 {'+'.join(combo)}"


def mouse_click(ctx: ToolContext, x: int, y: int, button: str = "left", double: bool = False) -> str:
    if not IS_WINDOWS:
        return _unsupported()
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return "坐标要是整数"
    if x < 0 or y < 0:
        return "坐标不能是负数"

    flags = {
        "left": (0x0002, 0x0004),
        "right": (0x0008, 0x0010),
        "middle": (0x0020, 0x0040),
    }.get((button or "left").lower())
    if flags is None:
        return "button 只能是 left / right / middle"

    user32 = _user32()
    user32.SetCursorPos(x, y)
    time.sleep(0.06)
    down, up = flags
    for _ in range(2 if double else 1):
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.06)
    return f"已在 ({x}, {y}) {'双击' if double else '单击'}{button}键"


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def clipboard(ctx: ToolContext, action: str = "read", text: str = "") -> str:
    if not IS_WINDOWS:
        return _unsupported()
    user32 = _user32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # 64 位下句柄是 8 字节指针：不显式声明 argtypes，ctypes 会按 int 传，
    # 大句柄直接 OverflowError: int too long to convert。
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    if (action or "read").lower() == "write":
        data = str(text).encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(None):
            return "剪贴板被别的程序占着，稍后再试"
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return "分配剪贴板内存失败"
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, handle)
        finally:
            user32.CloseClipboard()
        return f"已写入剪贴板（{len(text)} 字符）"

    if not user32.OpenClipboard(None):
        return "剪贴板被别的程序占着，稍后再试"
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return "剪贴板里没有文本（可能是图片或空）"
        ptr = kernel32.GlobalLock(handle)
        value = ctypes.wstring_at(ptr)
        kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    if not value:
        return "剪贴板是空的"
    clipped = value if len(value) <= 4000 else value[:4000] + f"…（共 {len(value)} 字符）"
    return f"剪贴板内容（{len(value)} 字符）：\n{clipped}"


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "open_target",
            "description": (
                "用系统默认程序打开一个东西：应用（notepad/微信/画图…）、文件、目录或网址。"
                "启动不阻塞对话，并且会回头确认真的出了窗口。"
                "只知道要做什么、不确定装没装，就先 list_software 查一遍。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "应用名、文件路径、目录或 http(s) 网址"},
                    "args": {"type": "string", "description": "可选：传给程序的启动参数，例如给记事本一个文件路径"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_software",
            "description": (
                "列出这台电脑上可以启动的软件（开始菜单快捷方式 + 注册表 App Paths + 内置别名）。"
                "不确定某个软件叫什么、装没装，先用这个查，别硬猜名字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "可选：按名字模糊过滤，例如「浏览器」「微信」"},
                    "limit": {"type": "integer", "description": "最多列几项，默认 60"},
                    "refresh": {"type": "boolean", "description": "刚装完软件可以传 true 重扫一遍"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_software",
            "description": (
                "启动本机上的某个软件，并回报是否真的出现了窗口（标题/进程/hwnd）。"
                "名字可以模糊（「记事本」「微信」「vscode」都会去清单里匹配）。"
                "要接着操作它，打开后先 focus_window 再 type_text。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "软件名，可以说中文"},
                    "args": {"type": "string", "description": "可选：启动参数"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_windows",
            "description": "列出当前打开的窗口（标题 + 进程名 + 句柄）。切窗口、截某个窗口之前先看这个。",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多列几个，默认 40"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_window",
            "description": "按标题里的一段文字把窗口切到最前面。标题不用完整，但别太短。",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string", "description": "窗口标题里的一段，例如「记事本」"}},
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "截屏存 PNG 到项目 shots/ 并返回路径。不传 window 就截整屏。你（模型）看不到图，是给用户看的。",
            "parameters": {
                "type": "object",
                "properties": {"window": {"type": "string", "description": "可选：窗口标题片段"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": (
                "往当前焦点窗口输入文字（支持中文，走 Unicode）。"
                "**输入前务必先 focus_window 确认焦点**，否则会打到别处。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要输入的文字，\\n 表示回车"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_keys",
            "description": "按组合键，如 ctrl+s、alt+tab、f5、enter。支持 a-z、0-9、f1-f12、ctrl/alt/shift/win、方向键等。",
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string", "description": "用 + 连起来，例如 ctrl+s"}},
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_click",
            "description": "把鼠标移到屏幕绝对坐标 (x,y) 点一下，原点在左上角。不知道坐标就先 screenshot 让用户看。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"}, "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "double": {"type": "boolean", "description": "是否双击，默认 false"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard",
            "description": "读写系统剪贴板。action=read 读出当前文本；action=write 把 text 写进去。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write"]},
                    "text": {"type": "string", "description": "action=write 时要写进去的文字"},
                },
                "required": ["action"],
            },
        },
    },
]

HANDLERS = {
    "open_target": open_target,
    "list_software": list_software,
    "open_software": open_software,
    "list_windows": list_windows,
    "focus_window": focus_window,
    "screenshot": screenshot,
    "type_text": type_text,
    "press_keys": press_keys,
    "mouse_click": mouse_click,
    "clipboard": clipboard,
}
