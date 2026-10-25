"""第三批工具：把「查一下就知道」的事从模型脑子里拿出来。

前两批解决的是「能不能做」：读、写、跑、搜、看屏幕、搬文件、调接口。
这一批解决的是另一类问题——**模型在这类事上会稳定地出错，而错了没人当场发现**：

- 现在是几点、三天后是几号 —— 心算日期，跨月就错；
- 这个文件多少行、改过没有 —— 靠回忆上一轮读到的内容，中途被别人改了就全错；
- 端口是不是被占了 —— 猜，然后让你去重启一个根本没冲突的服务；
- 两个文件差在哪 —— 把两份内容都读进上下文再「比较」，烧钱且容易漏；
- 这个目录里有没有重复文件 —— 只能靠文件名猜。

共同点：**答案本来就在机器里，却让模型去推理**。这类工具的正确性不依赖模型，
所以它们几乎是免费的可靠度来源。

依然守同一条纪律：只做一件事，返回字符串，绝不抛异常。
"""
from __future__ import annotations

import csv
import datetime as dt
import difflib
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import ToolContext

MAX_READ_BUDGET = 120_000
MAX_DIFF_LINES = 400
MAX_SCAN_FILES = 4000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             "build", ".idea", ".vscode", ".pytest_cache", ".mypy_cache"}


# ------------------------------------------------------------------ 小工具

def _decode(data: bytes) -> tuple[str, str]:
    """按 utf-8 → gbk → latin-1 试，返回 (文本, 编码名)。中文环境只试这三种够了。"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8?"


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(2048)
    except OSError:
        return False
    return b"\x00" in head


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _stamp(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _walk(root: Path, limit: int = MAX_SCAN_FILES):
    """遍历时跳过依赖目录 —— 用户要的是自己的文件，不是 node_modules。"""
    seen = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            seen += 1
            if seen > limit:
                return
            yield Path(base) / name


def _parse_time(raw: str) -> dt.datetime:
    """只认几种明确写法。猜不出来就抛 ValueError —— 与其猜错，不如让模型换个说法。"""
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("now", "现在", "今天"):
        return dt.datetime.now()
    if raw.lower() in ("today", "今天零点"):
        return dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if re.fullmatch(r"\d{10}", raw):
        return dt.datetime.fromtimestamp(int(raw))
    if re.fullmatch(r"\d{13}", raw):
        return dt.datetime.fromtimestamp(int(raw) / 1000)
    text = raw.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    text = text.replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%m-%d %H:%M", "%m-%d", "%H:%M"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        now = dt.datetime.now()
        if fmt.startswith("%H"):
            return now.replace(hour=parsed.hour, minute=parsed.minute,
                               second=0, microsecond=0)
        if fmt.startswith("%m"):
            return parsed.replace(year=now.year)
        return parsed
    raise ValueError(f"看不懂这个时间：{raw}（试试 2026-10-22 或 2026-10-22 21:30）")


# ------------------------------------------------------------------ 文件

def read_many(ctx: ToolContext, paths: list, max_chars: int = 24000) -> str:
    """一次读多个文件，带总预算。

    为什么不是让模型连调四次 read_file：每次调用都是一次完整往返，而且它
    常常在读第三个时忘了第一个的内容。一次给全，它反而更容易横向比较。
    """
    if not isinstance(paths, list) or not paths:
        return "paths 要是非空数组，每项一个文件路径。"
    budget = max(1000, min(int(max_chars or 24000), MAX_READ_BUDGET))
    used = 0
    chunks: list[str] = []
    missed: list[str] = []

    for raw in paths[:24]:
        try:
            target = ctx.resolve(str(raw))
        except ValueError as exc:
            missed.append(f"{raw}：{exc}")
            continue
        if not target.is_file():
            missed.append(f"{raw}：不是文件")
            continue
        try:
            text, enc = _decode(target.read_bytes())
        except OSError as exc:
            missed.append(f"{raw}：{exc}")
            continue
        lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        if used + len(text) > budget:
            room = budget - used
            if room < 300:
                missed.append(f"{raw}：总额度用完了（{budget} 字），没读")
                continue
            text = text[:room] + "\n…（文件太长，后面截断了）"
        used += len(text)
        note = "" if enc == "utf-8" else f" · {enc}"
        chunks.append(f"===== {ctx.relative(target)}（{lines} 行{note}）=====\n{text}")

    out = "\n\n".join(chunks)
    if missed:
        out += "\n\n没读到的：\n" + "\n".join(f"- {m}" for m in missed)
    return out or "一个都没读到。"


def file_stat(ctx: ToolContext, path: str) -> str:
    """文件/目录的身份证：多大、多少行、什么时候改的、内容指纹。"""
    try:
        target = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.exists():
        return f"不存在：{path}"
    try:
        st = target.stat()
    except OSError as exc:
        return f"读不到状态：{exc}"

    head = [f"{ctx.relative(target)}",
            f"类型：{'目录' if target.is_dir() else '文件'}",
            f"大小：{_human(st.st_size)}（{st.st_size} 字节）",
            f"修改：{_stamp(st.st_mtime)}",
            f"创建：{_stamp(st.st_ctime)}"]

    if target.is_dir():
        files, dirs, total = 0, 0, 0
        for child in _walk(target, limit=MAX_SCAN_FILES):
            files += 1
            try:
                total += child.stat().st_size
            except OSError:
                pass
        try:
            dirs = sum(1 for _ in target.iterdir() if _.is_dir())
        except OSError:
            pass
        head.append(f"内容：{files} 个文件 / {dirs} 个子目录，合计 {_human(total)}")
        return "\n".join(head)

    if _looks_binary(target):
        head.append("内容：二进制，不算行数")
    else:
        try:
            text, enc = _decode(target.read_bytes())
        except OSError as exc:
            head.append(f"读失败：{exc}")
            return "\n".join(head)
        lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        head.append(f"行数：{lines}")
        head.append(f"编码：{enc}")

    digest = hashlib.sha256()
    try:
        with target.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 16), b""):
                digest.update(block)
        head.append(f"sha256：{digest.hexdigest()[:32]}")
    except OSError:
        pass
    return "\n".join(head)


def tail_file(ctx: ToolContext, path: str, lines: int = 40) -> str:
    """看文件尾巴。查日志只需要最后几十行，整份读进来是浪费。"""
    try:
        target = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.is_file():
        return f"不是文件：{path}"
    want = max(1, min(int(lines or 40), 2000))
    window = 0
    try:
        size = target.stat().st_size
        with target.open("rb") as fh:
            window = min(size, max(8192, want * 400))
            fh.seek(max(0, size - window))
            data = fh.read()
    except OSError as exc:
        return f"读失败：{exc}"

    text, _ = _decode(data)
    whole = size <= window
    rows = text.splitlines()
    if not whole:
        # 从中间切进来的，第一行多半是半截的，扔掉
        rows = rows[1:]
    rows = rows[-want:]
    if not rows:
        return f"{ctx.relative(target)} 是空的。"

    if whole:
        total = len(text.splitlines())
        start = max(1, total - len(rows) + 1)
        head = f"{ctx.relative(target)} 最后 {len(rows)} 行（第 {start}–{total} 行）"
    else:
        head = (f"{ctx.relative(target)} 最后 {len(rows)} 行"
                f"（文件 {_human(size)}，只读了结尾 {_human(window)}）")
    return head + "：\n" + "\n".join(rows)


def diff_text(ctx: ToolContext, a: str, b: str) -> str:
    """比较两份内容，只报差异。

    模型「把两份都读进上下文然后自己比较」有两个毛病：烧 token，且它会漏掉
    中间某一段。difflib 不会漏。
    """
    texts = []
    for item in (a, b):
        try:
            target = ctx.resolve(item)
        except ValueError as exc:
            return f"拒绝：{exc}"
        if not target.is_file():
            return f"不是文件：{item}"
        try:
            texts.append(_decode(target.read_bytes())[0].splitlines())
        except OSError as exc:
            return f"读 {item} 失败：{exc}"

    left, right = texts
    diff = list(difflib.unified_diff(left, right, fromfile=a, tofile=b,
                                     lineterm="", n=3))
    if not diff:
        return f"{a} 和 {b} 内容完全一样（各 {len(left)} 行）。"
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [f"…（还有更多差异，只显示前 {MAX_DIFF_LINES} 行）"]
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    return f"{a} → {b}：+{added} 行 / -{removed} 行\n" + "\n".join(diff)


def search_replace(ctx: ToolContext, pattern: str, find: str, replace: str,
                   dry_run: bool = True, regex: bool = False,
                   max_files: int = 200) -> str:
    """跨文件批量替换。默认只报告、不落盘。

    这是这批里唯一会改文件的工具，所以默认 dry_run=True —— 拿不准的先看清单，
    确认了再把 dry_run 关掉。跟 `apply_patch` 一个道理：批量操作要能先彩排。
    """
    if not find:
        return "find 不能是空的。"
    glob = (pattern or "**/*").strip()
    if glob.endswith("/"):
        glob += "**/*"
    try:
        candidates = list(ctx.root.glob(glob))
    except (ValueError, OSError) as exc:
        return f"pattern 不合法：{exc}"
    candidates = [c for c in candidates if c.is_file()]
    if not candidates:
        return f"没有匹配 {glob} 的文件。"

    matcher = None
    if regex:
        try:
            matcher = re.compile(find)
        except re.error as exc:
            return f"正则写错了：{exc}"

    hits: list[tuple[Path, int, str]] = []
    for path in candidates[:max_files]:
        if _looks_binary(path):
            continue
        try:
            text, _ = _decode(path.read_bytes())
        except OSError:
            continue
        count = len(matcher.findall(text)) if matcher else text.count(find)
        if count:
            hits.append((path, count, text))

    if not hits:
        return f"{glob} 里 {len(candidates)} 个文件都没有出现 {find[:60]!r}。"

    total = sum(c for _, c, _ in hits)
    lines = [f"命中 {len(hits)} 个文件 / {total} 处："]
    for path, count, _ in hits[:40]:
        lines.append(f"  {ctx.relative(path)}  ×{count}")
    if len(hits) > 40:
        lines.append(f"  …（另有 {len(hits) - 40} 个文件）")

    if dry_run:
        lines.append("\n这是彩排，一个字都没改。确认无误后把 dry_run 设成 false 再调一次。")
        return "\n".join(lines)

    changed, failed = 0, []
    for path, _, text in hits:
        new = matcher.sub(replace, text) if matcher else text.replace(find, replace)
        if new == text:
            continue
        try:
            path.write_bytes(new.encode("utf-8"))
            changed += 1
        except OSError as exc:
            failed.append(f"{ctx.relative(path)}：{exc}")
    lines.append(f"\n已写入 {changed} 个文件。")
    if failed:
        lines.append("失败：" + "；".join(failed))
    return "\n".join(lines)


def find_duplicates(ctx: ToolContext, path: str = ".", min_size: int = 1024) -> str:
    """找内容重复的文件。先按大小分组，再只对同尺寸的算 hash —— 不白读一遍全部文件。"""
    try:
        root = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not root.is_dir():
        return f"不是目录：{path}"
    floor = max(0, int(min_size or 1024))

    by_size: dict[int, list[Path]] = {}
    scanned = 0
    for item in _walk(root):
        scanned += 1
        try:
            size = item.stat().st_size
        except OSError:
            continue
        if size < floor:
            continue
        by_size.setdefault(size, []).append(item)

    groups: dict[str, list[Path]] = {}
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        for item in items:
            digest = hashlib.sha256()
            try:
                with item.open("rb") as fh:
                    for block in iter(lambda: fh.read(1 << 16), b""):
                        digest.update(block)
            except OSError:
                continue
            groups.setdefault(f"{size}:{digest.hexdigest()}", []).append(item)

    dups = {k: v for k, v in groups.items() if len(v) > 1}
    if not dups:
        return f"扫了 {scanned} 个文件，没有发现重复（阈值 {_human(floor)} 以上）。"

    wasted = sum(int(k.split(":")[0]) * (len(v) - 1) for k, v in dups.items())
    out = [f"扫了 {scanned} 个文件，{len(dups)} 组重复，白占 {_human(wasted)}："]
    for index, (key, items) in enumerate(sorted(dups.items(), key=lambda kv: -len(kv[1])), 1):
        size = int(key.split(":")[0])
        out.append(f"\n[{index}] {_human(size)} × {len(items)}")
        for item in items:
            out.append(f"    {ctx.relative(item)}")
    out.append("\n（只报告，没有删任何东西。要清理请自己确认后调 delete_file。）")
    return "\n".join(out)


# ------------------------------------------------------------------ 执行

_VERSION_FLAGS = ("--version", "-V", "-v", "version")
_SAFE_COMMANDS = {
    "python", "python3", "py", "node", "npm", "pnpm", "yarn", "git", "rg", "grep",
    "ffmpeg", "ffprobe", "pwsh", "powershell", "sqlite3", "curl", "wget", "docker",
    "go", "cargo", "rustc", "java", "javac", "mvn", "gradle", "cmake", "make",
    "gcc", "clang", "dotnet", "php", "ruby", "perl", "7z", "tar", "ssh", "kubectl",
}


def which_command(ctx: ToolContext, name: str) -> str:
    """这个命令在不在、装在哪、什么版本。

    `run_command` 也能问，但那会真的执行一遍——问一句「装没装」不该跑起来。
    只对白名单里的命令试 --version，别的一律只报路径。
    """
    stem = Path(name.strip()).stem.lower()
    if not stem:
        return "name 不能是空的。"
    found = shutil.which(name) or shutil.which(stem)
    if not found:
        return f"PATH 里没有 {name}。装了但找不到的话，去「运行」栏的手动路径里指一下。"

    lines = [f"{name} → {found}"]
    try:
        lines.append(f"体积：{_human(Path(found).stat().st_size)}")
    except OSError:
        pass

    if stem in _SAFE_COMMANDS:
        for flag in _VERSION_FLAGS:
            try:
                proc = subprocess.run([found, flag], capture_output=True, text=True,
                                      timeout=4, errors="replace")
            except (OSError, subprocess.SubprocessError):
                break
            blob = (proc.stdout or proc.stderr or "").strip()
            if proc.returncode == 0 and blob:
                lines.append("版本：" + blob.splitlines()[0][:120])
                break
        else:
            lines.append("版本：问不出来（命令没有可用的 --version）")
    else:
        lines.append("版本：没问（不在白名单里，不替你执行）")
    return "\n".join(lines)


# ------------------------------------------------------------------ 联网

def download(ctx: ToolContext, url: str, dest: str, overwrite: bool = False,
             max_mb: int = 200) -> str:
    """把一个 URL 存成本地文件。带体积上限 —— 下载一个 2G 的镜像不该悄悄跑完。"""
    if not url.startswith(("http://", "https://")):
        return "只支持 http/https。"
    try:
        target = ctx.resolve(dest, need="write")
    except ValueError as exc:
        return f"拒绝：{exc}"
    if target.is_dir():
        return f"{dest} 是目录，dest 要写到文件名。"
    if target.exists() and not overwrite:
        return f"目标已存在：{ctx.relative(target)}。要覆盖就把 overwrite 设成 true。"

    cap = max(1, int(max_mb or 200)) * 1024 * 1024
    request = urllib.request.Request(url, headers={"User-Agent": "DreamRole/1.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared and declared > cap:
                return f"文件 {_human(declared)}，超过上限 {max_mb}MB，没下。"
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            got = 0
            with tmp.open("wb") as fh:
                while True:
                    block = response.read(1 << 16)
                    if not block:
                        break
                    got += len(block)
                    if got > cap:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return f"下到 {_human(got)} 时超过上限 {max_mb}MB，已中止并删掉半截文件。"
                    fh.write(block)
            tmp.replace(target)
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}：{exc.reason}"
    except urllib.error.URLError as exc:
        return f"连不上：{exc.reason}"
    except (OSError, TimeoutError) as exc:
        return f"下载失败：{exc}"
    return f"已保存 {ctx.relative(target)}（{_human(got)}）"


# ------------------------------------------------------------------ 数据

def _cell(value: str):
    text = (value or "").strip()
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


def _match(row: dict, where: str) -> bool:
    """迷你筛选语法：`age>18 and name~张`。

    为什么不用 eval：这是模型写的字符串，直接 eval 等于把执行权交出去。
    只认 = != > < >= <= ~（包含）六种比较，够用且不可能出事。
    """
    for clause in re.split(r"\s+and\s+", where.strip(), flags=re.I):
        clause = clause.strip()
        if not clause:
            continue
        m = re.match(r"^([\w\u4e00-\u9fa5 \-]+?)\s*(>=|<=|!=|=|>|<|~)\s*(.+)$", clause)
        if not m:
            raise ValueError(f"看不懂这个条件：{clause}")
        column, op, want = m.group(1).strip(), m.group(2), m.group(3).strip().strip("'\"")
        if column not in row:
            raise ValueError(f"没有这一列：{column}")
        have = row[column]
        if op == "~":
            if want.lower() not in str(have).lower():
                return False
            continue
        left, right = _cell(str(have)), _cell(want)
        if isinstance(left, str) or isinstance(right, str):
            left, right = str(left), str(right)
        result = {"=": left == right, "!=": left != right, ">": left > right,
                  "<": left < right, ">=": left >= right, "<=": left <= right}[op]
        if not result:
            return False
    return True


def csv_query(ctx: ToolContext, path: str, where: str = "", columns: str = "",
              group_by: str = "", sort: str = "", limit: int = 50) -> str:
    """对 CSV 做筛选/挑列/分组计数/排序。纯标准库，不用装 pandas。"""
    try:
        target = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.is_file():
        return f"不是文件：{path}"
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return f"读失败：{exc}"
    text, enc = _decode(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    if not rows:
        return "这个 CSV 没有数据行（只有表头或完全为空）。"
    headers = list(rows[0].keys())

    try:
        if where:
            rows = [r for r in rows if _match(r, where)]
    except ValueError as exc:
        return f"筛选条件有问题：{exc}（可用列：{', '.join(headers)}）"
    kept = len(rows)

    if group_by:
        if group_by not in headers:
            return f"没有这一列：{group_by}（可用：{', '.join(headers)}）"
        counter: dict[str, int] = {}
        for row in rows:
            key = str(row.get(group_by) or "(空)")
            counter[key] = counter.get(key, 0) + 1
        ranked = sorted(counter.items(), key=lambda kv: -kv[1])[:max(1, int(limit or 50))]
        body = "\n".join(f"  {k}  {v}" for k, v in ranked)
        return (f"{ctx.relative(target)} · {kept} 行{enc_note(enc)} · 按「{group_by}」分组：\n"
                + body + (f"\n（共 {len(counter)} 个不同取值）" if len(counter) > len(ranked) else ""))

    if sort:
        column = sort.lstrip("-+")
        if column not in headers:
            return f"没有这一列：{column}"
        reverse = sort.startswith("-")
        rows = sorted(rows, key=lambda r: _cell(str(r.get(column) or "")), reverse=reverse)

    picked = [c.strip() for c in columns.split(",") if c.strip()] if columns else headers
    missing = [c for c in picked if c not in headers]
    if missing:
        return f"没有这些列：{', '.join(missing)}（可用：{', '.join(headers)}）"

    show = rows[:max(1, int(limit or 50))]
    table = [" | ".join(picked), " | ".join("---" for _ in picked)]
    for row in show:
        table.append(" | ".join(str(row.get(c) or "")[:40] for c in picked))
    tail = f"\n（共 {kept} 行，显示前 {len(show)} 行）" if kept > len(show) else f"\n（共 {kept} 行）"
    return (f"{ctx.relative(target)} · 列：{', '.join(headers)}{enc_note(enc)}\n"
            + "\n".join(table) + tail)


def enc_note(enc: str) -> str:
    return "" if enc == "utf-8" else f" · {enc}"


def json_query(ctx: ToolContext, expr: str = "", path: str = "", text: str = "") -> str:
    """按路径从 JSON 里取值：`data.items[0].name`。

    比让模型读整份 JSON 再自己找省得多，而且缩进和键顺序都不会骗它。
    """
    if path:
        try:
            target = ctx.resolve(path)
        except ValueError as exc:
            return f"拒绝：{exc}"
        if not target.is_file():
            return f"不是文件：{path}"
        try:
            text = _decode(target.read_bytes())[0]
        except OSError as exc:
            return f"读失败：{exc}"
    if not text:
        return "要么给 path，要么直接给 text。"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"不是合法 JSON：{exc}（第 {exc.lineno} 行第 {exc.colno} 列）"

    if not expr.strip():
        preview = json.dumps(data, ensure_ascii=False, indent=2)
        if len(preview) > 4000:
            preview = preview[:4000] + "\n…（太长，给个 expr 精确取）"
        return preview

    node = data
    walked = "$"
    for token in re.findall(r"[^.\[\]]+|\[\d+\]|\[\*\]", expr.strip().lstrip("$.")):
        if token.startswith("["):
            inner = token[1:-1]
            if not isinstance(node, list):
                return f"{walked} 不是数组，取不了 {token}"
            if inner == "*":
                return json.dumps(node, ensure_ascii=False, indent=2)[:8000]
            index = int(inner)
            if not -len(node) <= index < len(node):
                return f"{walked} 只有 {len(node)} 项，没有第 {index} 项"
            node = node[index]
            walked += token
        else:
            if not isinstance(node, dict):
                return f"{walked} 不是对象，取不了 .{token}"
            if token not in node:
                return f"{walked} 没有 {token} 这个键（有：{', '.join(list(node)[:20])}）"
            node = node[token]
            walked += f".{token}"

    if isinstance(node, (dict, list)):
        body = json.dumps(node, ensure_ascii=False, indent=2)
        if len(body) > 8000:
            body = body[:8000] + "\n…（截断）"
        size = len(node)
        return f"{walked}（{size} 项）：\n{body}"
    return f"{walked} = {json.dumps(node, ensure_ascii=False)}"


def text_stats(ctx: ToolContext, path: str) -> str:
    """字数、行数、中文字数、高频词。写给「这份稿子多长」这类问题。"""
    try:
        target = ctx.resolve(path)
    except ValueError as exc:
        return f"拒绝：{exc}"
    if not target.is_file():
        return f"不是文件：{path}"
    try:
        text, enc = _decode(target.read_bytes())
    except OSError as exc:
        return f"读失败：{exc}"

    lines = text.splitlines()
    chars = len(text)
    nonspace = sum(1 for ch in text if not ch.isspace())
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    longest = max((len(l) for l in lines), default=0)
    blank = sum(1 for l in lines if not l.strip())
    words = re.findall(r"[A-Za-z]{3,}", text)
    freq: dict[str, int] = {}
    for word in words:
        key = word.lower()
        freq[key] = freq.get(key, 0) + 1
    top = sorted(((k, v) for k, v in freq.items() if v > 1), key=lambda kv: -kv[1])[:10]

    out = [f"{ctx.relative(target)}",
           f"行数：{len(lines)}（空行 {blank}）  最长一行：{longest} 字符",
           f"字符：{chars}  非空白：{nonspace}  中文：{cjk}",
           f"英文词：{len(words)}  编码：{enc}  体积：{_human(target.stat().st_size)}"]
    if top:
        out.append("高频词：" + "、".join(f"{k}({v})" for k, v in top))
    return "\n".join(out)


# ------------------------------------------------------------------ 系统

def system_info(ctx: ToolContext) -> str:
    """这台机器的底细：系统、CPU、内存、磁盘、开机多久。"""
    out = [f"系统：{platform.system()} {platform.release()}（{platform.machine()}）",
           f"主机名：{platform.node()}",
           f"Python：{sys.version.split()[0]} · {sys.executable}",
           f"CPU：{os.cpu_count() or '?'} 核"]
    try:
        out.append(f"工作区磁盘：{_disk(ctx.root)}")
    except OSError:
        pass

    memory = _memory()
    if memory:
        total, available = memory
        out.append(f"内存：{_human(total)} 总计，{_human(available)} 可用"
                   f"（用了 {100 * (total - available) / total:.0f}%）")

    uptime = _uptime()
    if uptime:
        out.append(f"已开机：{uptime}")
    return "\n".join(out)


def _disk(path: Path) -> str:
    usage = shutil.disk_usage(str(path.anchor or path))
    return (f"{_human(usage.free)} 可用 / {_human(usage.total)} 总计"
            f"（{100 * usage.used / usage.total:.0f}% 已用）")


def _memory() -> tuple[int, int] | None:
    """Windows 走 GlobalMemoryStatusEx，其他平台读 /proc/meminfo。都不成就返回 None。"""
    try:
        if os.name == "nt":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
            return None
        with open("/proc/meminfo", encoding="utf-8") as fh:
            table = {line.split(":")[0]: int(line.split()[1]) * 1024
                     for line in fh if ":" in line and line.split()[1].isdigit()}
        return table.get("MemTotal"), table.get("MemAvailable")
    except Exception:  # noqa: BLE001 - 探测环境信息失败不值得让主循环知道
        return None


def _uptime() -> str:
    try:
        if os.name == "nt":
            import ctypes
            millis = ctypes.windll.kernel32.GetTickCount64()
        else:
            with open("/proc/uptime", encoding="utf-8") as fh:
                millis = float(fh.read().split()[0]) * 1000
    except Exception:  # noqa: BLE001
        return ""
    total = int(millis // 1000)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days} 天" if days else "", f"{hours} 小时" if hours else "",
             f"{minutes} 分钟"]
    return " ".join(p for p in parts if p)


def port_check(ctx: ToolContext, port: int, owner: bool = False) -> str:
    """这个端口现在能不能用。

    用「试着绑一下」判断，而不是去猜 —— bind 失败就是有人占着，没有别的解释。
    """
    try:
        number = int(port)
    except (TypeError, ValueError):
        return "port 要是数字。"
    if not 1 <= number <= 65535:
        return "端口要在 1..65535 之间。"

    busy = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", number))
        except OSError:
            busy = True

    if not busy:
        return f"端口 {number} 空闲，可以监听。"

    lines = [f"端口 {number} 已被占用。"]
    if owner:
        lines.append(_port_owner(number) or "查不到是哪个进程（可能是别的用户或系统服务）。")
    else:
        lines.append("要看是谁占的，把 owner 设成 true。")
    return "\n".join(lines)


def _port_owner(port: int) -> str:
    try:
        if os.name == "nt":
            proc = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                  capture_output=True, text=True, timeout=8,
                                  errors="replace")
            pattern = re.compile(rf":{port}\s+\S+\s+LISTENING\s+(\d+)", re.I)
        else:
            proc = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                                  timeout=8, errors="replace")
            pattern = re.compile(rf":{port}\s.*?pid=(\d+)")
        found = pattern.search(proc.stdout or "")
        if not found:
            return ""
        pid = found.group(1)
        name = ""
        if os.name == "nt":
            listing = subprocess.run(["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                                     capture_output=True, text=True, timeout=8,
                                     errors="replace")
            parts = [p.strip('"') for p in (listing.stdout or "").splitlines()[0].split(",")]
            name = parts[0] if parts else ""
        return f"占用者：{name or '?'}（PID {pid}）"
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


_SENSITIVE = re.compile(r"(key|token|secret|password|passwd|pwd|auth|credential)", re.I)


def env_var(ctx: ToolContext, name: str = "", show_value: bool = False) -> str:
    """看环境变量。名字里带 key/token/secret 的默认打码 —— 别把密钥读进对话里。"""
    if not name:
        names = sorted(os.environ)
        out = [f"共 {len(names)} 个环境变量（值都省略了，要看某个就在 name 里指定）："]
        out.extend("  " + n for n in names[:120])
        if len(names) > 120:
            out.append(f"  …（还有 {len(names) - 120} 个）")
        return "\n".join(out)

    value = os.environ.get(name)
    if value is None:
        near = [n for n in os.environ if name.lower() in n.lower()][:10]
        hint = f"是不是想问：{', '.join(near)}" if near else "确实没有这个变量。"
        return f"环境里没有 {name}。{hint}"

    if _SENSITIVE.search(name) and not show_value:
        masked = value[:4] + "*" * max(4, len(value) - 8) + value[-4:] if len(value) > 12 else "*" * len(value)
        return (f"{name} = {masked}（长度 {len(value)}）\n"
                "名字带 key/token/secret 的值默认打码。确实要明文就把 show_value 设成 true。")
    # PATH 这类变量能有好几 KB，整段塞进上下文没有意义
    clipped = value if len(value) <= 600 else value[:600] + f"…（共 {len(value)} 字符）"
    return f"{name} = {clipped}"


# ------------------------------------------------------------------ 时间

def now(ctx: ToolContext, offset: int = 8) -> str:
    """现在几点。别心算日期 —— 这是机器最擅长、模型最容易错的事。"""
    try:
        hours = max(-12, min(14, int(offset)))
    except (TypeError, ValueError):
        hours = 8
    zone = dt.timezone(dt.timedelta(hours=hours))
    moment = dt.datetime.now(zone)
    weekdays = "一二三四五六日"
    return "\n".join([
        f"{moment.strftime('%Y-%m-%d %H:%M:%S')}（UTC{hours:+d}，星期{weekdays[moment.weekday()]}）",
        f"时间戳：{int(moment.timestamp())}",
        f"今天已过：{moment.hour} 小时 {moment.minute} 分",
        f"日期：{moment.year} 年 {moment.month} 月 {moment.day} 日，"
        f"本年第 {moment.timetuple().tm_yday} 天",
    ])


def date_math(ctx: ToolContext, start: str = "now", end: str = "",
              add_days: int = 0) -> str:
    """日期加减、两日期相隔多久。跨月跨年交给它，别交给心算。"""
    try:
        begin = _parse_time(start)
    except ValueError as exc:
        return str(exc)

    if end:
        try:
            finish = _parse_time(end)
        except ValueError as exc:
            return str(exc)
        delta = finish - begin
        seconds = int(delta.total_seconds())
        sign = "" if seconds >= 0 else "（后者更早，下面是绝对值）"
        seconds = abs(seconds)
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        return (f"{begin:%Y-%m-%d %H:%M} → {finish:%Y-%m-%d %H:%M} {sign}\n"
                f"相隔 {days} 天 {hours} 小时 {minutes} 分（共 {abs(delta.days)} 天）")

    try:
        shift = int(add_days or 0)
    except (TypeError, ValueError):
        return "add_days 要是整数。"
    result = begin + dt.timedelta(days=shift)
    weekdays = "一二三四五六日"
    verb = "加" if shift > 0 else ("减" if shift < 0 else "不动")
    return (f"{begin:%Y-%m-%d %H:%M:%S} {verb} {abs(shift)} 天 → "
            f"{result:%Y-%m-%d %H:%M:%S}（星期{weekdays[result.weekday()]}）")


# ------------------------------------------------------------------ 注册

SCHEMAS = [
    {"type": "function", "function": {
        "name": "read_many",
        "description": ("一次读多个文件，总字数有上限。要横向比较多份材料时用它，"
                        "比自己连着调几次读文件省事，也不会读到一半忘了前面。"),
        "parameters": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "文件路径清单，最多 24 个"},
            "max_chars": {"type": "integer", "description": "总量上限，默认 24000 字"},
        }, "required": ["paths"]}}},
    {"type": "function", "function": {
        "name": "file_stat",
        "description": "一个文件或目录的详情：大小、行数、编码、修改时间、sha256。想确认「改没改过」用它。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "tail_file",
        "description": "读文件最后 N 行。查日志、看长文件结尾，不要整份读。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "lines": {"type": "integer", "description": "要几行，默认 40"},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "diff_text",
        "description": "比较两个文件，只返回差异（unified diff）。不要自己把两份内容都读进来比对。",
        "parameters": {"type": "object", "properties": {
            "a": {"type": "string", "description": "旧文件路径"},
            "b": {"type": "string", "description": "新文件路径"},
        }, "required": ["a", "b"]}}},
    {"type": "function", "function": {
        "name": "search_replace",
        "description": ("跨多个文件批量查找替换。默认 dry_run=true，只报清单不动文件；"
                        "确认清单对了再把 dry_run 设成 false 才会写入。"),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "路径通配，例如 **/*.py"},
            "find": {"type": "string", "description": "要找的内容"},
            "replace": {"type": "string", "description": "换成什么"},
            "dry_run": {"type": "boolean", "description": "true=只报告（默认），false=真改"},
            "regex": {"type": "boolean", "description": "find 当正则用"},
            "max_files": {"type": "integer", "description": "最多扫几个文件，默认 200"},
        }, "required": ["pattern", "find", "replace"]}}},
    {"type": "function", "function": {
        "name": "find_duplicates",
        "description": "找内容重复的文件（按内容 hash，不是按文件名）。只报告，不删。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "在哪个目录里找，默认当前工作区"},
            "min_size": {"type": "integer", "description": "小于多少字节的忽略，默认 1024"},
        }}}},
    {"type": "function", "function": {
        "name": "which_command",
        "description": "某个命令在不在、装在哪、什么版本。问「装没装」用这个，不要用 run_command 真跑一遍。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "命令名，例如 ffmpeg"},
        }, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "download",
        "description": "把 URL 存成本地文件（有体积上限，默认 200MB）。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "dest": {"type": "string", "description": "存到哪个路径，要写到文件名"},
            "overwrite": {"type": "boolean"},
            "max_mb": {"type": "integer", "description": "上限多少 MB，默认 200"},
        }, "required": ["url", "dest"]}}},
    {"type": "function", "function": {
        "name": "csv_query",
        "description": ("查 CSV：筛选、挑列、分组计数、排序。where 支持 `age>18 and name~张` "
                        "这种写法（= != > < >= <= ~ 六种）。不用装 pandas。"),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "where": {"type": "string", "description": "筛选条件，例如 category=硬件 and price>100"},
            "columns": {"type": "string", "description": "要哪几列，逗号分隔；不填就是全部"},
            "group_by": {"type": "string", "description": "给这一列做分组计数"},
            "sort": {"type": "string", "description": "排序列名，前面加 - 表示降序"},
            "limit": {"type": "integer", "description": "最多几行，默认 50"},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "json_query",
        "description": "按路径从 JSON 里取值，例如 `data.items[0].name`。不填 expr 就返回格式化预览。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "JSON 文件路径，和 text 二选一"},
            "text": {"type": "string", "description": "直接给一段 JSON"},
            "expr": {"type": "string", "description": "取值路径，例如 items[2].title"},
        }}}},
    {"type": "function", "function": {
        "name": "text_stats",
        "description": "字数、行数、中文字数、高频词、编码。回答「这份稿子多长」用它。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "system_info",
        "description": "这台机器的系统版本、CPU 核数、内存、磁盘余量、开机时长。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "port_check",
        "description": "某个端口是不是被占了。要查是谁占的把 owner 设成 true。",
        "parameters": {"type": "object", "properties": {
            "port": {"type": "integer"},
            "owner": {"type": "boolean", "description": "true=顺便查占用进程"},
        }, "required": ["port"]}}},
    {"type": "function", "function": {
        "name": "env_var",
        "description": ("读环境变量。不填 name 就列出所有变量名；"
                        "名字里带 key/token/secret 的值默认打码。"),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "show_value": {"type": "boolean", "description": "true=敏感值也明文显示"},
        }}}},
    {"type": "function", "function": {
        "name": "now",
        "description": "现在是几点、今天几号、星期几、时间戳。任何涉及「今天/现在」的问题都先用它，不要心算。",
        "parameters": {"type": "object", "properties": {
            "offset": {"type": "integer", "description": "时区偏移小时数，默认 8（北京时间）"},
        }}}},
    {"type": "function", "function": {
        "name": "date_math",
        "description": ("日期加减、或算两个日期相隔多久。跨月跨年的推算一律用它，别心算。"
                        "start/end 支持 2026-10-22、2026-10-22 21:30、now、时间戳。"),
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "起点，默认 now"},
            "end": {"type": "string", "description": "给了它就返回两者相隔多久"},
            "add_days": {"type": "integer", "description": "start 往后加几天（负数就是往前）"},
        }}}},
]

HANDLERS = {
    "read_many": read_many,
    "file_stat": file_stat,
    "tail_file": tail_file,
    "diff_text": diff_text,
    "search_replace": search_replace,
    "find_duplicates": find_duplicates,
    "which_command": which_command,
    "download": download,
    "csv_query": csv_query,
    "json_query": json_query,
    "text_stats": text_stats,
    "system_info": system_info,
    "port_check": port_check,
    "env_var": env_var,
    "now": now,
    "date_math": date_math,
}
