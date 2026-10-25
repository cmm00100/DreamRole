"""读写用户文件时保住原来的**编码**和**换行风格**。

**为什么需要这一层**：`Path.read_text()` / `Path.write_text()` 走的是 Python 的
通用换行翻译 —— 读的时候把 `\\r\\n` 变成 `\\n`，写的时候再把 `\\n` 变回
`os.linesep`。在 Windows 上这就意味着一件很糟的事：

    一个 LF 结尾的仓库文件，被 `edit_file` 改了**一行**，整份文件变成 CRLF。

git 看到的是「每一行都改了」，review 时没人能看出真正变了哪一行。

**还有更糟的一半**：编码。中文 Windows 上大把 `.txt` / `.ini` / `.bat` / 老 `.csv`
还是 GBK。按 UTF-8 硬读会得到一串替换字符，再写回去就变成「锟斤拷」——
而且原始字节已经没了，这不是格式变了，是**内容没了**。

所以这一层做三件事：

1. 读：先认编码（UTF-8 / UTF-8 带 BOM / UTF-16 / GB18030），再统一成 `\\n`
   （逐行匹配、行号、diff 全都按 `\\n` 想，这也是模型看到的样子）；
2. 写：按**原来的**编码和换行风格写回去；新文件用 UTF-8 + `\\n`；
3. 写不回去就**明确失败**：内容里有原编码表示不了的字符（比如往 GBK 文件里写 emoji），
   先把字节编出来再动文件 —— 宁可这次不写，也不留一份改坏的。
"""
from __future__ import annotations

import codecs
from pathlib import Path

# 判断换行风格 / 编码时最多看这么多字节。几 MB 的文件没必要全读一遍，
# 而且「开头是什么样」几乎总是和整份文件一致。
SNIFF_BYTES = 65536

# 认得出来的编码，按顺序试。
#
# `gbk` 必须排在 `gb18030` 前面，而且要分开认：gb18030 是全 Unicode 超集
# （连 emoji 都能编成 4 个字节），拿它当「写回用的编码」，就会出现
# 「往 GBK 文件里塞了个 😀，写成功了，但记事本打开是四个乱码字节」。
# 认成 gbk 才能在该拒绝的时候拒绝。真正的 gb18030 老文件（含 GBK 表示不了的生僻字）
# 会在 gbk 那一步解码失败，落到第三个候选上，照样读得对。
CANDIDATES = ("utf-8", "gbk", "gb18030")


class EncodingError(Exception):
    """这份内容没法用文件原来的编码写回去。

    调用方必须把它当成**明确的失败**（这次没写），不能当成写成功。
    这类错误以前是「悄悄写坏」，比报错难查一百倍。
    """


def _sniff(raw: bytes) -> str:
    """从字节里认出编码。

    顺序很重要：先看 BOM，再试 UTF-8，最后才 GB18030 ——
    反过来会把所有 UTF-8 中文文件误判成 GBK（gb18030 几乎什么都收）。

    试 UTF-8 用的是**增量解码器且 final=False**：采样在 64KB 处可能正好切断
    一个三字节汉字，严格解码会失败、于是误判成 GBK，写回去就真的坏了一个字。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if not raw:
        return "utf-8"
    for name in CANDIDATES:
        decoder = codecs.getincrementaldecoder(name)()
        try:
            decoder.decode(raw, False)      # False = 末尾允许不完整
            return name
        except UnicodeDecodeError:
            continue
    # 四种都不认：按 UTF-8 读（读的时候用 replace 兜底），至少不会崩
    return "utf-8"


def encoding_of(path: Path) -> str:
    """这个文件是什么编码。文件不存在、或者读不了，都按新文件算（UTF-8）。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(SNIFF_BYTES)
    except OSError:
        return "utf-8"
    return _sniff(raw)


def newline_of(path: Path) -> str:
    """这个文件现在的换行风格：`"\\r\\n"` 或 `"\\n"`。

    取多数派，不是「遇到过 CRLF 就算 CRLF」：混排的文件（比如手改过一段）
    按多数派写回去，至少不会让情况变得更糟。文件不存在时算新文件，用 `\\n`。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(SNIFF_BYTES)
    except OSError:
        return "\n"
    if _sniff(raw) == "utf-16":
        # UTF-16 里每个字符都夹着 0x00，按字节数 `\n` 还能数，但 `\r\n` 数不出来 ——
        # 那就永远判成 LF，把一份 CRLF 的 UTF-16 文件改成 LF。先解码再数才对。
        text = raw.decode("utf-16", "ignore")
        crlf = text.count("\r\n")
        return "\r\n" if crlf > text.count("\n") - crlf else "\n"
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def read_text(path: Path, encoding: str | None = None, errors: str = "replace") -> str:
    """读文本，编码按文件自己的来，换行统一成 `\\n`。"""
    charset = encoding or encoding_of(path)
    with open(path, "r", encoding=charset, errors=errors, newline=None) as handle:
        return handle.read()


def write_text(path: Path, text: str, encoding: str | None = None,
               newline: str | None = None) -> None:
    """写文本，编码和换行都按文件原来的来（`encoding` / `newline` 给值时强制用它）。

    写入前把三种换行统一成 `\\n` 再按目标风格展开 —— 否则一段本来就带 `\\r\\n`
    的字符串会被换成 `\\r\\r\\n`。

    编码这一步**先编成字节再动文件**：边写边编的话，编到一半失败会在磁盘上
    留一个被截断的文件，那比报错糟得多。
    """
    style = newline if newline is not None else newline_of(path)
    charset = encoding or encoding_of(path)
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    body = normalized.replace("\n", style)
    try:
        blob = body.encode(charset)
    except UnicodeEncodeError as exc:
        raise EncodingError(
            f"这个文件是 {charset} 编码，内容里有它表示不了的字符"
            f"（{exc.object[exc.start:exc.end]!r}）。为了不改坏它，这次没有写入。"
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(blob)
