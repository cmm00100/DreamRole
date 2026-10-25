"""给所有测试脚本一个「输出永不因编码崩掉」的兜底。

**为什么需要**：Windows 的中文控制台默认是 GBK（cp936）。脚本里只要 print 出一个
GBK 编不出来的字符（比如界面文案里的 `⋯`、`→`、`✓`），就会抛
`UnicodeEncodeError: 'gbk' codec can't encode character '\\u22ef'` ——
而且**只在输出被重定向到文件或管道时**才炸（控制台里 Python 用 WriteConsoleW，
不受影响）。于是最典型的用法「跑一遍、把输出存成日志」正好是最容易崩的那种：

    python scripts/ui_check.py > check.log     # 跑到最后一步崩在 print 上

修法是保留控制台自己的编码（不然中文会变乱码），只把「编不出来的字符」
换成替代字符。断言结果一个都不会丢。
"""
from __future__ import annotations

import sys


def harden() -> None:
    """让输出在「控制台」和「重定向到文件」两种情况下都不崩。幂等，可以随便调。

    - 控制台（isatty）：保留它自己的编码（中文 Windows 是 GBK，改了会变乱码），
      只把编不出来的字符换成替代字符；
    - 重定向到文件/管道：改用 UTF-8 丢掉替代字符这一步，日志是完整可读的，
      而不是「那一行全是问号」。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="strict")
            else:
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # 被换成了不认识的对象（比如测试里的替身），那就别管它
            continue
