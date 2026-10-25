# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

两条原则：

1. **前端、技能、提示词模板都是「数据」，不是代码。** 它们必须显式列进 `datas`，
   否则打包出来运行时会找不到 `web/index.html`、`app/prompts/base.md`，
   表现是「窗口里一片白」—— 这是 PyInstaller 最常见的坑。
2. **不打包没用到的东西。** 排除 tkinter / numpy / pandas / PIL，
   它们的体积加起来比整个应用还大，而且一行都用不到。

默认打成一个 exe（onefile）。想要启动更快的目录版：
    DREAMROLE_ONEDIR=1 pyinstaller dreamrole.spec
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve()
ONEFILE = os.environ.get("DREAMROLE_ONEDIR") != "1"
# 窗口程序出错时只会弹一个对话框，堆栈看不到 —— 排查打包问题时打一个控制台版：
#     DREAMROLE_CONSOLE=1 pyinstaller dreamrole.spec
CONSOLE = os.environ.get("DREAMROLE_CONSOLE") == "1"
ICON = ROOT / "assets" / "dreamrole.ico"

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "skills"), "skills"),
    (str(ROOT / "app" / "prompts"), "app/prompts"),
    (str(ROOT / "app" / "promptopt" / "templates"), "app/promptopt/templates"),
    (str(ROOT / "app" / "promptopt" / "prompts"), "app/promptopt/prompts"),
]
# HTTPS 证书：不带上这个，打包后的 exe 调 API 会报 SSL 证书错误
datas += collect_data_files("certifi")

hiddenimports = [
    # uvicorn 的协议实现全是运行时按名字导入的，静态分析看不到
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
    # 桌面外壳：Windows 走 EdgeChromium，底层依赖 pythonnet
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "pythonnet",
    "encodings.idna",
]
hiddenimports += collect_submodules("uvicorn")

excludes = [
    "tkinter",
    "unittest",
    "pydoc_data",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "pytest",
    "IPython",
    "notebook",
    "PIL",
    "setuptools",
    "pip",
    "wheel",
]

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="DreamRole",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX 压过的 exe 很容易被安全软件误杀
        runtime_tmpdir=None,
        console=CONSOLE,    # 窗口程序；出错会弹窗，不靠 stdout（排查时用 DREAMROLE_CONSOLE=1）
        disable_windowed_traceback=False,
        icon=str(ICON) if ICON.is_file() else None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="DreamRole",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=CONSOLE,
        icon=str(ICON) if ICON.is_file() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="DreamRole",
    )
