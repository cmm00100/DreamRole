"""资源目录与数据目录分离。

打包成 exe（PyInstaller onefile）之后 `__file__` 指向解压的临时目录，退出就删。
所以必须把两类东西分开：

- **资源**（web/dist、skills、模板）跟着程序走，只读；
- **数据**（settings.json、traces、state、memory、projects）放用户能写、且不会被清掉的地方。

没打包时两者都是项目根，行为跟直接跑源码一致。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "DreamRole"

# 更名兼容：这个项目原来叫 Aster，环境变量是 `ASTER_*`。改个名字不该让已经写好的
# 脚本、快捷方式、CI 一夜之间全部失效 —— 新名字优先，旧名字只在没设新值时顶上。
_LEGACY_ENV = ("DATA_DIR", "API_KEY", "CUSTOM_API_KEY", "PORT", "HEADLESS",
               "FAKE_LLM", "ONEDIR", "CONSOLE", "DEBUG", "OPEN_BROWSER")
for _suffix in _LEGACY_ENV:
    _old, _new = "ASTER_" + _suffix, "DREAMROLE_" + _suffix
    if _old in os.environ and _new not in os.environ:
        os.environ[_new] = os.environ[_old]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """只读资源目录。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _writable(folder: Path) -> bool:
    """比 os.access 可靠：真去写一个探针文件。

    Windows 上某些被虚拟化/重定向的目录，os.access 会说能写，实际写不进。
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("1", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """可写目录：环境变量 > 源码模式 > exe 同级（绿色版）> LOCALAPPDATA。

    `DREAMROLE_DATA_DIR` 必须第一个判断 —— 否则「跑测试时指到临时目录」这件事
    在源码模式下会失效，测试会把 settings.json 和一堆轨迹写进仓库。
    """
    override = os.environ.get("DREAMROLE_DATA_DIR")
    if override:
        target = Path(override).expanduser()
        if _writable(target):
            return target

    if not is_frozen():
        # 源码模式放 .data/，不跟源码混在一起
        return Path(__file__).resolve().parent.parent / ".data"

    exe_dir = Path(sys.executable).resolve().parent

    # 安装版不把数据放在程序目录里。
    #
    # 为什么：安装后程序在 `%LOCALAPPDATA%\Programs\DreamRole`，如果数据也塞在那儿，
    # 「卸载」就只有两个都不好的选项 —— 要么连用户几十条会话、记忆、配置一起删掉，
    # 要么留下一个删不干净的目录。分开之后，卸载程序可以放心清空程序目录，
    # 升级重装也不会碰到数据。安装器会放一个 `install.marker` 来声明「我是安装版」。
    if not (exe_dir / "install.marker").is_file():
        beside = exe_dir / "data"
        if _writable(beside):
            return beside

    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    fallback = local / APP_NAME
    _adopt_legacy(fallback)
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _adopt_legacy(target: Path) -> None:
    """把旧名字（Aster）留下的数据接过来。

    只在新目录里**还没有 settings.json** 时接 —— 用户已经在新目录配过东西之后，
    再拿旧数据盖上去就是删他的配置。搬的时候是复制，旧目录原样留着。
    """
    if (target / "settings.json").is_file():
        return
    legacy = target.parent / "Aster"
    if not legacy.is_dir() or not (legacy / "settings.json").is_file():
        return
    try:
        shutil.copytree(legacy, target, dirs_exist_ok=True)
    except OSError:
        pass


RESOURCE = resource_dir()
DATA = data_dir()

# 数据目录下的固定位置，别在各处硬编码字符串
SETTINGS_FILE = DATA / "settings.json"
TRACES = DATA / "traces"
STATE = DATA / "state"
MEMORY = DATA / "memory"
PROJECTS = DATA / "projects"
LOGS = DATA / "logs"
# 自己的技能。内置技能跟着程序走（打包后在 %TEMP%\_MEIxxxx，退出就删），
# 所以「新增技能」只能写这里 —— 见 skills.py 开头那段。
USER_SKILLS = DATA / "skills"
SHOTS = "shots"  # 相对项目根


def ensure_dirs() -> None:
    for folder in (TRACES, STATE, MEMORY, PROJECTS, LOGS, USER_SKILLS):
        folder.mkdir(parents=True, exist_ok=True)
