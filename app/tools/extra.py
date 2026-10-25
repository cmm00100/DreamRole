"""第二批工具：补齐「真干活」时会缺的那几件。

第一批（files/exec_cmd/web/pc/memory）覆盖了读、写、跑、搜、看屏幕。
真拿去改一个项目时，最先撞墙的其实是这些：

- 想整体复制/挪动一个目录 —— 只能靠 `run_command` 敲 shell，跨平台就翻车；
- 想一次改五处 —— `edit_file` 得调五次，中间任何一次失败都得自己收拾；
- 想读接口返回的 4xx 正文 —— `web_fetch` 只处理 200；
- 想确认图片多大 —— 没有 Pillow 就没辙；
- 想算一下 —— 让模型心算，它就会算错。

每条都按同一个原则写：**只做一件事，失败要说清为什么**。
工具返回字符串，绝不抛异常（抛出去就是主循环崩）。
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .. import textio
from .base import ToolContext

MAX_HTTP_CHARS = 20_000
MAX_ARCHIVE_ENTRIES = 400


# --------------------------------------------------------------- 文件搬运

def copy_file(ctx: ToolContext, path: str, dest: str, overwrite: bool = False) -> str:
    try:
        src = ctx.resolve(path)
        dst = ctx.resolve(dest, need="write")
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not src.exists():
        return f"源不存在：{path}"
    if src.is_dir():
        return f"{path} 是目录。整目录复制请用 archive（先打包）或 run_command。"
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists() and not overwrite:
        return f"目标已存在：{ctx.relative(dst)}。要覆盖就把 overwrite 设成 true。"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as exc:
        return f"复制失败：{exc}"
    return f"已复制 {ctx.relative(src)} → {ctx.relative(dst)}（{dst.stat().st_size}B）"


def move_file(ctx: ToolContext, path: str, dest: str, overwrite: bool = False) -> str:
    try:
        src = ctx.resolve(path, need="write")
        dst = ctx.resolve(dest, need="write")
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not src.exists():
        return f"源不存在：{path}"
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists() and not overwrite:
        return f"目标已存在：{ctx.relative(dst)}。要覆盖就把 overwrite 设成 true。"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return f"移动失败：{exc}"
    return f"已移动 {ctx.relative(src)} → {ctx.relative(dst)}"


def make_dir(ctx: ToolContext, path: str) -> str:
    try:
        target = ctx.resolve(path, need="write")
    except ValueError as exc:
        return f"拒绝：{exc}"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"建目录失败：{exc}"
    return f"目录已就绪：{ctx.relative(target)}"


# --------------------------------------------------------------- 批量改

def apply_patch(ctx: ToolContext, path: str, edits: list) -> str:
    """一处调用改多处。

    为什么不让模型连调五次 `edit_file`：改到第三处失败时，前两处已经落盘，
    模型通常不会主动回滚，用户看到的是一份「改了一半」的文件。
    这里先全部匹配上再写盘，任何一处找不到就一个字节都不动。
    """
    try:
        target = ctx.resolve(path, need="write")
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.is_file():
        return f"不是文件：{path}"
    if not isinstance(edits, list) or not edits:
        return "edits 要是非空数组，每项 {find, replace}。"

    text = textio.read_text(target)
    original = text
    applied = 0
    problems: list[str] = []

    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            problems.append(f"第 {index} 项不是对象")
            continue
        find = str(edit.get("find") or "")
        replace = str(edit.get("replace") or "")
        every = bool(edit.get("all"))
        if not find:
            problems.append(f"第 {index} 项 find 是空的")
            continue
        hits = text.count(find)
        if not hits:
            problems.append(f"第 {index} 项找不到：{find[:60]!r}")
            continue
        if hits > 1 and not every:
            problems.append(f"第 {index} 项匹配到 {hits} 处，会改错地方；确认要全改就把 all 设成 true")
            continue
        text = text.replace(find, replace) if every else text.replace(find, replace, 1)
        applied += 1

    if problems:
        return "没有写入（一处不对就整份不动）：\n- " + "\n- ".join(problems)
    if text == original:
        return "内容和原来一样，没有写入。"

    before = original.count("\n") + 1
    after = text.count("\n") + 1
    try:
        textio.write_text(target, text)
    except textio.EncodingError as exc:
        return f"没有写入：{exc}"
    except OSError as exc:
        return f"写入失败：{exc}"
    return f"已改 {ctx.relative(target)}：{applied} 处；行数 {before} → {after}"


# --------------------------------------------------------------- 网络

def http_request(ctx: ToolContext, url: str, method: str = "GET",
                 headers: dict | None = None, body: str = "",
                 timeout: int = 20) -> str:
    """任意方法的 HTTP。和 web_fetch 的分工：那个是「读网页」，
    这个是「调接口」—— 4xx/5xx 也要把正文拿回来，不然模型不知道怎么改参数。"""
    from .. import net

    try:
        target = net.check_url(url)
    except net.NetError as exc:
        return f"地址不给过：{exc}"

    verb = (method or "GET").upper()
    if verb not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return f"不支持的 method：{method}"

    data = body.encode("utf-8") if body and verb not in ("GET", "HEAD") else None
    head = {"User-Agent": net.UA, "Accept": "application/json,text/plain,*/*"}
    if isinstance(headers, dict):
        head.update({str(k): str(v) for k, v in headers.items()})
    if data is not None and not any(k.lower() == "content-type" for k in head):
        head["Content-Type"] = "application/json"

    request = urllib.request.Request(target, data=data, headers=head, method=verb)
    try:
        with urllib.request.urlopen(request, timeout=max(3, min(int(timeout), 60)),
                                    context=net._ssl_context()) as resp:
            raw = resp.read(MAX_HTTP_CHARS * 4)
            status = resp.status
            charset = resp.headers.get_content_charset()
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_HTTP_CHARS * 4) if hasattr(exc, "read") else b""
        status = exc.code
        charset = exc.headers.get_content_charset() if exc.headers else None
    except urllib.error.URLError as exc:
        return f"连不上：{exc.reason}"
    except TimeoutError:
        return f"超时了（{timeout} 秒）"

    text = net._decode(raw or b"", charset)[:MAX_HTTP_CHARS]
    ctype = ""
    try:
        ctype = resp.headers.get("Content-Type", "")  # type: ignore[possibly-undefined]
    except Exception:  # noqa: BLE001
        ctype = ""
    if "json" in ctype.lower():
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except json.JSONDecodeError:
            pass
    flag = "OK" if 200 <= status < 300 else "失败"
    return f"{verb} {target} → {status} {flag}\n{text or '(空响应)'}"


# --------------------------------------------------------------- 归档

def archive(ctx: ToolContext, action: str, path: str,
            entries: list | None = None, dest: str = "") -> str:
    """zip 三个动作合成一个工具：list / zip / unzip。

    为什么要合：模型对「打包」和「解开」的处理几乎一样（都是路径 + 一批条目），
    分成三个工具只会让它在选工具上多犹豫一次。
    """
    try:
        target = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    act = (action or "list").lower()

    if act == "zip":
        if not target.exists():
            return f"找不到：{path}"
        out = ctx.resolve(dest, need="write") if dest else target.with_suffix(".zip")
        if out.suffix.lower() != ".zip":
            out = out.with_suffix(".zip")
        picked: list[Path] = []
        if entries:
            for name in entries[:MAX_ARCHIVE_ENTRIES]:
                try:
                    picked.append(ctx.resolve(str(name)))
                except ValueError:
                    continue
        elif target.is_dir():
            picked = [p for p in target.rglob("*") if p.is_file()]
        else:
            picked = [target]
        if not picked:
            return "没有可打包的文件。"
        out.parent.mkdir(parents=True, exist_ok=True)
        base = target if target.is_dir() else target.parent
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
                for item in picked:
                    try:
                        bundle.write(item, item.relative_to(base).as_posix())
                    except ValueError:
                        bundle.write(item, item.name)
        except OSError as exc:
            return f"打包失败：{exc}"
        return f"已打包 {len(picked)} 个文件 → {ctx.relative(out)}（{out.stat().st_size}B）"

    if act == "unzip":
        if not target.is_file():
            return f"不是文件：{path}"
        out_dir = ctx.resolve(dest, need="write") if dest else target.with_suffix("")
        try:
            with zipfile.ZipFile(target) as bundle:
                names = bundle.namelist()
                # 只解到项目内的相对路径：zip 里带 ../ 的那种包不能信
                safe = [n for n in names if not n.startswith(("/", "\\")) and ".." not in n.split("/")]
                skipped = len(names) - len(safe)
                bundle.extractall(out_dir, members=safe[:MAX_ARCHIVE_ENTRIES * 4])
        except (OSError, zipfile.BadZipFile) as exc:
            return f"解压失败：{exc}"
        note = f"，跳过 {skipped} 个可疑条目" if skipped else ""
        return f"已解压到 {ctx.relative(out_dir)}：{len(safe)} 个文件{note}"

    if not target.is_file():
        return f"不是文件：{path}"
    try:
        with zipfile.ZipFile(target) as bundle:
            infos = bundle.infolist()[:MAX_ARCHIVE_ENTRIES]
            lines = [f"{i.filename}  {i.file_size}B" for i in infos]
            more = len(bundle.infolist()) - len(infos)
    except (OSError, zipfile.BadZipFile) as exc:
        return f"读不了这个包：{exc}"
    if more > 0:
        lines.append(f"……还有 {more} 个")
    return "\n".join(lines) or "(空包)"


# --------------------------------------------------------------- 数据库

def sqlite_query(ctx: ToolContext, db: str, sql: str, limit: int = 200) -> str:
    """只读查库。

    用 `mode=ro` 打开，不是「检查一下 SQL 里有没有写关键字」——
    字符串匹配能被注释、大小写、`PRAGMA` 绕过几十种，只读连接才是真的只读。
    """
    try:
        target = ctx.resolve(db)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.is_file():
        return f"找不到数据库：{db}"
    if not sql.strip():
        return "sql 是空的。"
    uri = "file:" + urllib.parse.quote(str(target).replace("\\", "/")) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        return f"打不开：{exc}"
    try:
        cursor = conn.execute(sql)
        if cursor.description is None:
            return "这条语句没有结果集（只读连接也写不了）。"
        cols = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(max(1, min(int(limit), 500)))
    except sqlite3.Error as exc:
        return f"查询失败：{exc}"
    finally:
        conn.close()

    out = [" | ".join(cols)]
    out.append("-" * 40)
    for row in rows:
        out.append(" | ".join("NULL" if v is None else str(v) for v in row))
    if not rows:
        out.append("(没有匹配的行)")
    return "\n".join(out)


# --------------------------------------------------------------- 图片

def _png(size: bytes) -> tuple[int, int]:
    return struct.unpack(">II", size[16:24])


def _gif(size: bytes) -> tuple[int, int]:
    return struct.unpack("<HH", size[6:10])


def _bmp(size: bytes) -> tuple[int, int]:
    width, height = struct.unpack("<ii", size[18:26])
    return abs(width), abs(height)


def _jpeg(raw: bytes) -> tuple[int, int]:
    index = 2
    while index < len(raw) - 9:
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", raw[index + 5:index + 9])
            return width, height
        length = struct.unpack(">H", raw[index + 2:index + 4])[0]
        index += 2 + length
    return 0, 0


def _webp(raw: bytes) -> tuple[int, int]:
    chunk = raw[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(raw[24:27], "little")
        height = 1 + int.from_bytes(raw[27:30], "little")
        return width, height
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", raw[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(raw[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return 0, 0


def image_info(ctx: ToolContext, path: str) -> str:
    """不装 Pillow 也能报尺寸：只读文件头那几十个字节。"""
    try:
        target = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.is_file():
        return f"不是文件：{path}"
    try:
        with target.open("rb") as handle:
            head = handle.read(64)
    except OSError as exc:
        return f"读不了：{exc}"

    size = target.stat().st_size
    kind = "未知"
    width = height = 0
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        kind, (width, height) = "PNG", _png(head)
    elif head[:3] == b"GIF":
        kind, (width, height) = "GIF", _gif(head)
    elif head[:2] == b"BM":
        kind, (width, height) = "BMP", _bmp(head)
    elif head[:2] == b"\xff\xd8":
        kind = "JPEG"
        try:
            raw = target.read_bytes()
            width, height = _jpeg(raw)
        except OSError:
            pass
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        kind = "WEBP"
        try:
            width, height = _webp(target.read_bytes())
        except OSError:
            pass
    elif head[:4] == b"%PDF":
        return f"{ctx.relative(target)}：PDF，{size}B（不是位图，量不出像素）"

    dims = f"{width}×{height}" if width and height else "尺寸没读出来"
    return f"{ctx.relative(target)}：{kind}，{dims}，{size}B"


# --------------------------------------------------------------- 计算

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub,
    ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Call, ast.Name, ast.Load, ast.Tuple, ast.Compare, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.BoolOp, ast.And, ast.Or,
)

_SAFE_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "int": int, "float": float, "len": len, "sorted": sorted,
}


def calculate(ctx: ToolContext, expression: str) -> str:
    """算数交给机器，别交给模型。

    实测模型心算三位数乘法会错，而且错得很自信。这里用 AST 求值，
    白名单之外的东西一律拒绝 —— `eval` 是绝对不能碰的。
    """
    text = (expression or "").strip()
    if not text:
        return "表达式是空的。"
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        return f"这句不是合法表达式：{exc.msg}"

    import math

    env = dict(_SAFE_FUNCS)
    env.update({name: getattr(math, name) for name in dir(math) if not name.startswith("_")})
    env.update({"pi": math.pi, "e": math.e})

    for node in ast.walk(tree):
        if isinstance(node, _ALLOWED_NODES):
            continue
        if isinstance(node, ast.Name) and node.id in env:
            continue
        return f"表达式里有不允许的东西：{type(node).__name__}"

    try:
        value = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, env)  # noqa: S307
    except ZeroDivisionError:
        return "除以 0 了。"
    except Exception as exc:  # noqa: BLE001
        return f"算不出来：{type(exc).__name__}: {exc}"

    if isinstance(value, float):
        shown = f"{value:.10g}"
    else:
        shown = str(value)
    return f"{text} = {shown}"


# --------------------------------------------------------------- 进程

def list_processes(ctx: ToolContext, name: str = "", limit: int = 40) -> str:
    """看现在跑着什么。查「端口被谁占了」这类问题时比翻任务管理器快。"""
    rows: list[tuple[str, str, str]] = []
    if os.name == "nt":
        import subprocess

        try:
            done = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"读不到进程列表：{exc}"
        for line in (done.stdout or "").splitlines():
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 5:
                # tasklist 的列顺序：映像名 / PID / 会话名 / 会话号 / 内存
                rows.append((parts[0], parts[1], parts[4]))
    else:
        import subprocess

        try:
            done = subprocess.run(["ps", "-eo", "pid,comm,rss"], capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"读不到进程列表：{exc}"
        for line in (done.stdout or "").splitlines()[1:]:
            parts = line.split(None, 2)
            if len(parts) == 3:
                rows.append((parts[1], parts[0], parts[2].strip() + "KB"))

    needle = (name or "").lower()
    if needle:
        rows = [r for r in rows if needle in r[0].lower()]
    if not rows:
        return f"没有名字含「{name}」的进程。" if name else "没读到进程。"
    out = ["进程 | PID | 内存"]
    for proc, pid, mem in rows[: max(1, min(int(limit), 200))]:
        out.append(f"{proc} | {pid} | {mem}")
    if len(rows) > limit:
        out.append(f"……还有 {len(rows) - limit} 个")
    return "\n".join(out)


# --------------------------------------------------------------- 注册

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": "复制一个文件。目标目录不存在会自动建。目录整体搬运用 run_command。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "源文件，相对项目根"},
                    "dest": {"type": "string", "description": "目标路径；给目录就复制进去同名"},
                    "overwrite": {"type": "boolean", "description": "目标已存在时是否覆盖，默认 false"},
                },
                "required": ["path", "dest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "移动或改名一个文件。跨盘也能用（内部会退化成先复制再删）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "dest": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["path", "dest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_dir",
            "description": "建目录，多级一起建。已经存在不算错。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "一次性在一个文件里改多处。比连续调多次 edit_file 安全："
                "任何一处 find 匹配不上，整份文件都不写。"
                "find 必须和原文逐字一致（包括缩进）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "description": "要改的地方，按顺序应用",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string", "description": "原文片段，要唯一"},
                                "replace": {"type": "string", "description": "换成什么，空串表示删掉"},
                                "all": {"type": "boolean", "description": "匹配到多处时是否全改，默认否"},
                            },
                            "required": ["find", "replace"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "调一个 HTTP 接口，GET/POST/PUT/PATCH/DELETE 都行。"
                "和 web_fetch 的区别：这个会把 4xx/5xx 的正文也拿回来，"
                "所以调 API 用它。只能访问公网地址。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "default": "GET"},
                    "headers": {"type": "object", "description": "额外请求头"},
                    "body": {"type": "string", "description": "请求体；JSON 就直接给 JSON 字符串"},
                    "timeout": {"type": "integer", "description": "秒，默认 20"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "archive",
            "description": "zip 的三种用法：list 看包里有什么、zip 打包、unzip 解开。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "zip", "unzip"]},
                    "path": {"type": "string", "description": "zip 包路径，zip 动作时是源文件/目录"},
                    "entries": {"type": "array", "description": "zip 时只打这些（可选）",
                                "items": {"type": "string"}},
                    "dest": {"type": "string", "description": "输出路径；zip 默认同名 .zip，unzip 默认同名目录"},
                },
                "required": ["action", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sqlite_query",
            "description": "只读查一个 .db/.sqlite 文件。只读连接，写语句会被数据库拒绝。",
            "parameters": {
                "type": "object",
                "properties": {
                    "db": {"type": "string", "description": "数据库文件路径"},
                    "sql": {"type": "string", "description": "SELECT 之类带结果集的语句"},
                    "limit": {"type": "integer", "description": "最多几行，默认 200"},
                },
                "required": ["db", "sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_info",
            "description": "报图片的格式、像素尺寸、体积。不依赖 Pillow。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "算一个表达式，支持 + - * / // % **、括号、比较、以及 math 里的函数（sqrt、log、sin…）。"
                "涉及具体数字的算术一律用它，不要心算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "例如 (1234*5678)/3"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_processes",
            "description": "列出正在跑的进程（名字/PID/内存）。查「哪个程序占着端口」时用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "只看名字含这个串的"},
                    "limit": {"type": "integer", "description": "最多几条，默认 40"},
                },
            },
        },
    },
]

HANDLERS = {
    "copy_file": copy_file,
    "move_file": move_file,
    "make_dir": make_dir,
    "apply_patch": apply_patch,
    "http_request": http_request,
    "archive": archive,
    "sqlite_query": sqlite_query,
    "image_info": image_info,
    "calculate": calculate,
    "list_processes": list_processes,
}
