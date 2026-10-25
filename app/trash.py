"""删东西一律进回收站。

理由很直接：agent 删错是迟早的事，而「能还原」把一次事故降级成一次尴尬。
Windows 走 SHFileOperationW（资源管理器删文件那条路）。

**失败不退回 os.remove** —— 宁可删不掉，也不能绕过回收站误删。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def to_trash(path: Path | str) -> bool:
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return False
    if IS_WINDOWS:
        return _windows_recycle(target)
    return _plain_delete(target)


def _windows_recycle(target: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    # ALLOWUNDO 让它进回收站；其余三个只是别弹窗
    flags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError:
        return False  # 连 shell32 都加载不了，那就放弃，绝不用 rm 兜底

    # pFrom 必须以两个 \0 结尾
    raw = str(target)
    buf = ctypes.create_unicode_buffer(raw + "\0", len(raw) + 2)
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = flags
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    # 返回码不可靠：实测有些情况下文件已经进回收站了，rc 仍然非 0。
    # 真正该信的是「它还在不在」。
    if not target.exists():
        return True
    return rc == 0 and not op.fAnyOperationsAborted


def _plain_delete(target: Path) -> bool:
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            os.remove(target)
        return True
    except OSError:
        return False


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"
