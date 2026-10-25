"""Windows 原生的「选文件夹」对话框。

为什么要自己封一层：界面里原来那个目录浏览是**我们自己画的列表**，
只能在当前盘里上下走 —— 想换盘得一层层退到根目录，再点一次「上一级」才
看到盘符。用户的原话是「浏览就只能浏览 F 盘」。

系统那个对话框不一样：左边有快速访问，上面有地址栏和盘符下拉，可以直接
粘路径，还有「新建文件夹」按钮。它是所有 Windows 程序长的同一副样子，
用户不用学。

用到的接口（Vista 以后的通用文件对话框）：

    CoCreateInstance(CLSID_FileOpenDialog) → IFileOpenDialog
    IFileOpenDialog->SetOptions(FOS_PICKFOLDERS)
    IFileOpenDialog->Show(hwnd)              // 阻塞，直到用户点确定或取消
    IFileOpenDialog->GetResult(&IShellItem)
    IShellItem->GetDisplayName(SIGDN_FILESYSPATH)

没有第三方依赖：COM 的虚表用 ctypes 直接调。这不是图省事 ——
为了一个选目录的对话框引一个 comtypes/pywin32，打出来的包要多几 MB，
而这几个方法的位置是稳定的，写死索引是可接受的代价。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import POINTER, byref, c_void_p, wintypes

# {DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}
_CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
# {D57C7288-D4AD-4768-BE02-9D969532D960}
_IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"

FOS_PICKFOLDERS = 0x00000020        # 选目录而不是选文件
FOS_FORCEFILESYSTEM = 0x00000040    # 只要真实路径，别给我虚拟文件夹
FOS_FILEMUSTEXIST = 0x00001000      # 选文件时必须真的存在
FOS_PATHMUSTEXIST = 0x00000800

SIGDN_FILESYSPATH = 0x80058000

CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2

_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = -2147417850   # 0x80010106
_ERROR_CANCELLED = -2147023673      # 0x800704C7

# 虚表下标。IFileOpenDialog 继承 IModalWindow → IFileDialog，
# 所以这些数字是按「从 IUnknown 数起」的绝对位置。
#
# IShellItem 那几个尤其容易数错：3 是 BindToHandler，4 是 GetParent，
# 5 才是 GetDisplayName。写成 4 的后果不是「拿不到路径」，而是把 SIGDN 当成
# 输出指针交给 GetParent —— 直接 access violation（`writing 0x80058000`），
# 而且崩在服务线程里，前端只会看到请求没响应。
_SHOW = 3
_SET_FILE_TYPES = 4
_SET_OPTIONS = 9
_GET_OPTIONS = 10
_SET_TITLE = 17
_GET_RESULT = 20
_SET_DEFAULT_EXT = 22
_RELEASE = 2
_GET_DISPLAY_NAME = 5


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _FILTERSPEC(ctypes.Structure):
    """COMDLG_FILTERSPEC：{显示名, 通配} 两段宽字符串指针。"""
    _fields_ = [("pszName", ctypes.c_wchar_p), ("pszSpec", ctypes.c_wchar_p)]


def _guid(text: str) -> _GUID:
    value = _GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(
        ctypes.c_wchar_p(text), ctypes.byref(value)
    )
    if hr != _S_OK:
        raise OSError(f"GUID 解析失败：{text}")
    return value


def _call(ptr: int, index: int, restype, argtypes: tuple, *args):
    """按虚表下标调一个 COM 方法。"""
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtable[index])(ptr, *args)


def available() -> bool:
    """这台机器上能不能用（只判平台，不真的弹窗）。"""
    return sys.platform == "win32"


def _pick_on_thread(title: str, owner: int | None, out: dict,
                    files: bool = False, exts: list[str] | None = None) -> None:
    ole32 = ctypes.windll.ole32

    # 对话框要 STA。请求线程通常不是，所以开一个专用线程来跑 ——
    # 顺便也让它的消息循环跟 WebView 那边互不干扰。
    hr_init = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    need_uninit = hr_init in (_S_OK, _S_FALSE)
    if hr_init == _RPC_E_CHANGED_MODE:
        # 这个线程已经在 MTA 里了。仍然可以试，只是不推荐 —— 不行就返回。
        pass

    dialog = c_void_p()
    try:
        clsid = _guid(_CLSID_FileOpenDialog)
        iid = _guid(_IID_IFileOpenDialog)
        hr = ole32.CoCreateInstance(
            byref(clsid), None, CLSCTX_INPROC_SERVER, byref(iid), byref(dialog)
        )
        if hr != _S_OK or not dialog:
            out["error"] = f"系统对话框创建失败（HRESULT 0x{hr & 0xFFFFFFFF:08X}）"
            return

        current = wintypes.DWORD()
        _call(dialog, _GET_OPTIONS, ctypes.c_long, (POINTER(wintypes.DWORD),),
              byref(current))
        if files:
            # 选文件：把「只挑文件夹」拿掉，加上「文件必须存在」。
            # 选项是**位**，所以这里不是覆盖而是先清掉 pickfolders 再或上去 ——
            # 直接 `|` 上去的话 FOS_PICKFOLDERS 还在，对话框依然只让你选目录。
            options = (current.value & ~FOS_PICKFOLDERS) | FOS_FORCEFILESYSTEM | FOS_FILEMUSTEXIST
            if exts:
                # {名称, 通配} 两段一组；数组里两段的指针都指向本函数的局部变量，
                # 必须在 Show() 返回后才能释放 —— 所以放在这个作用域里，不放进内层函数。
                specs = [(f"{ext.upper()} 图片", f"*{ext}") for ext in exts]
                specs.append(("所有文件", "*.*"))
                array = (_FILTERSPEC * len(specs))()
                for index, (name, pattern) in enumerate(specs):
                    array[index].pszName = name
                    array[index].pszSpec = pattern
                _call(dialog, _SET_FILE_TYPES, ctypes.c_long,
                      (ctypes.c_uint, POINTER(_FILTERSPEC)), len(specs), array)
                if exts:
                    _call(dialog, _SET_DEFAULT_EXT, ctypes.c_long, (ctypes.c_wchar_p,), exts[0])
        else:
            options = current.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
        _call(dialog, _SET_OPTIONS, ctypes.c_long, (wintypes.DWORD,), options)
        if title:
            _call(dialog, _SET_TITLE, ctypes.c_long, (ctypes.c_wchar_p,), title)


        # 没给 owner 就拿当前前台窗口。不指定 owner 的话，对话框可能开在
        # 主窗口后面 —— 用户会以为点了没反应（这个坑在别处踩过）。
        if not owner:
            try:
                owner = ctypes.windll.user32.GetForegroundWindow() or 0
            except Exception:  # noqa: BLE001
                owner = 0

        hr = _call(dialog, _SHOW, ctypes.c_long, (wintypes.HWND,), owner or 0)
        if hr == _ERROR_CANCELLED:
            out["path"] = None          # 用户点了取消，这不是错误
            return
        if hr != _S_OK:
            out["error"] = f"对话框没能打开（HRESULT 0x{hr & 0xFFFFFFFF:08X}）"
            return

        item = c_void_p()
        hr = _call(dialog, _GET_RESULT, ctypes.c_long, (POINTER(c_void_p),), byref(item))
        if hr != _S_OK or not item:
            out["error"] = "对话框没给结果"
            return
        try:
            name = ctypes.c_wchar_p()
            hr = _call(item, _GET_DISPLAY_NAME, ctypes.c_long,
                       (ctypes.c_uint32, POINTER(ctypes.c_wchar_p)),
                       SIGDN_FILESYSPATH, byref(name))
            if hr != _S_OK or not name.value:
                out["error"] = "拿不到这个目录的真实路径"
                return
            out["path"] = str(name.value)
            ole32.CoTaskMemFree(name)
        finally:
            _call(item, _RELEASE, ctypes.c_ulong, ())
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    finally:
        if dialog:
            try:
                _call(dialog, _RELEASE, ctypes.c_ulong, ())
            except Exception:  # noqa: BLE001
                pass
        if need_uninit:
            ole32.CoUninitialize()


def pick_folder(title: str = "选择工作区",
                owner: int | None = None, timeout: float = 900.0) -> dict:
    """弹系统对话框，返回 {"path": 路径 或 None} / {"error": 说明}。

    取消不是错误：`path` 为 None 表示用户自己关掉了。
    """
    return _pick(title, None, timeout, files=False, exts=None)


def pick_file(title: str = "选一个文件", exts: list[str] | None = None,
              owner: int | None = None, timeout: float = 900.0) -> dict:
    """同一个对话框，选的是**文件**。

    `exts` 给了就只列这几种（比如图片），但永远留一条「所有文件」——
    用户的文件可能扩展名不常规，卡死在白名单上等于不让选。
    """
    return _pick(title, owner, timeout, files=True, exts=exts)


def _pick(title: str, owner: int | None, timeout: float,
          files: bool, exts: list[str] | None) -> dict:
    if not available():
        return {"error": "这个系统上没有系统文件对话框"}

    out: dict = {}
    thread = threading.Thread(
        target=_pick_on_thread, args=(title, owner, out, files, exts), daemon=True
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return {"error": "对话框一直没有结果"}
    return out
