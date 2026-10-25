"""联网：抓网页、搜网页。只用标准库。

两条硬边界：
1. **只许公网**。域名解析出来是 127/10/192.168/169.254（云元数据）一律拒——
   本地跑着的服务（包括本程序自己）不该被模型顺手摸走；
2. **只回正文**。整页 HTML 倒给模型既费 token 又难读。

搜索走 HTML 页面解析（Bing → DuckDuckGo → 百度），一个解析不到就换下一个。
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 20
MAX_BYTES = 900_000

_SSL: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """优先 certifi 的证书包。

    Windows 上 Python 自带的 OpenSSL 不读系统证书库，打包成 exe 之后
    可能一个根证书都找不到，所以把 certifi 那份带上。
    """
    global _SSL
    if _SSL is not None:
        return _SSL
    try:
        import certifi

        _SSL = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - 没 certifi 也别拦住能用的环境
        _SSL = ssl.create_default_context()
    return _SSL


class NetError(RuntimeError):
    pass


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise NetError(f"解析不了 {host}：{exc}") from exc
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def check_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise NetError(f"只支持 http/https，收到的是 {parsed.scheme or '空'}")
    if not parsed.hostname:
        raise NetError("这个地址没有域名")
    if not _public_host(parsed.hostname):
        raise NetError(f"{parsed.hostname} 指向内网/本机，不抓（安全边界）")
    return parsed.geturl()


def _request(url: str, data: bytes | None = None, headers: dict | None = None):
    head = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    head.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=head)
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context())
    except urllib.error.HTTPError as exc:
        raise NetError(f"服务器返回 {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise NetError(f"连不上：{exc.reason}") from exc
    except TimeoutError as exc:
        raise NetError(f"超时了（{TIMEOUT} 秒）") from exc


def _decode(raw: bytes, charset: str | None) -> str:
    for enc in (charset, "utf-8", "gbk", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def html_to_text(raw: str, limit: int = 6000) -> str:
    """把 HTML 压成能读的正文。标题单独拎出来，因为 head 会被整块丢掉。"""
    title = ""
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    if match:
        title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()

    text = re.sub(r"(?is)<(script|style|noscript|svg|head|iframe|nav|footer)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<li[^>]*>", "\n· ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|tr|h[1-6]|section|article|blockquote|pre)>", "\n\n", text)
    text = re.sub(r"(?is)</t[dh]>", " | ", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)

    lines: list[str] = []
    blank = 0
    for line in text.splitlines():
        line = re.sub(r"[ \t\u00a0\u3000]+", " ", line).strip()
        if not line:
            blank += 1
            if blank > 1 or not lines:
                continue
            lines.append("")
            continue
        blank = 0
        lines.append(line)

    body = "\n".join(lines).strip()
    if len(body) > limit:
        body = body[:limit] + f"\n…[截断，原文 {len(body)} 字符]"
    return f"《{title}》\n{body}" if title else (body or "（这一页没有可读正文，可能需要 JS 渲染）")


def fetch(url: str, max_chars: int = 6000) -> str:
    target = check_url(url)
    with _request(target) as res:
        ctype = res.headers.get_content_type()
        raw = res.read(MAX_BYTES)
        charset = res.headers.get_content_charset()
        final_url = res.geturl()

    text = _decode(raw, charset)
    head = f"[{target}]" if final_url == target else f"[{target} → {final_url}]"

    if "json" in ctype:
        try:
            pretty = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except json.JSONDecodeError:
            pretty = text
        return f"{head} JSON\n{pretty[:max_chars]}"

    if ctype.startswith("text/plain") or ctype in ("text/xml", "application/xml"):
        return f"{head}\n{text[:max_chars]}"

    if "html" in ctype:
        return f"{head}\n{html_to_text(text, limit=max_chars)}"

    return f"{head}\n（{ctype}，{len(raw)} 字节，没法当文本读）"


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", "", text))).strip()


def _parse_bing(page: str) -> list[dict]:
    out = []
    for block in re.findall(r'(?is)<li class="b_algo".*?</li>', page):
        link = re.search(r'(?is)<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not link:
            continue
        snippet = re.search(r"(?is)<p[^>]*>(.*?)</p>", block)
        out.append(
            {
                "title": _clean(link.group(2)),
                "url": html.unescape(link.group(1)),
                "snippet": _clean(snippet.group(1))[:240] if snippet else "",
            }
        )
    return out


def _parse_ddg(page: str) -> list[dict]:
    out = []
    for block in re.findall(r'(?is)<div class="result[^"]*results_links.*?</div>\s*</div>', page):
        link = re.search(r'(?is)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not link:
            continue
        snippet = re.search(r'(?is)<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', block)
        out.append(
            {
                "title": _clean(link.group(2)),
                "url": html.unescape(link.group(1)),
                "snippet": _clean(snippet.group(1))[:240] if snippet else "",
            }
        )
    return out


def _parse_baidu(page: str) -> list[dict]:
    out = []
    for block in re.findall(r'(?is)<div[^>]+class="result[^"]*"[^>]*>.*?</h3>', page):
        link = re.search(r'(?is)<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not link:
            continue
        out.append({"title": _clean(link.group(2)), "url": html.unescape(link.group(1)), "snippet": ""})
    return out


ENGINES = [
    ("bing", "https://cn.bing.com/search?q={q}&setlang=zh-CN", _parse_bing),
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={q}", _parse_ddg),
    ("baidu", "https://www.baidu.com/s?wd={q}&rn=10", _parse_baidu),
]


def search(query: str, count: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        raise NetError("搜索词是空的")
    count = max(1, min(int(count or 5), 10))
    tried: list[str] = []

    for name, template, parser in ENGINES:
        url = template.format(q=urllib.parse.quote(query))
        try:
            check_url(url)
            with _request(url) as res:
                page = _decode(res.read(MAX_BYTES), res.headers.get_content_charset())
        except NetError as exc:
            tried.append(f"{name}: {exc}")
            continue
        hits = [h for h in parser(page) if h["title"] and h["url"].startswith("http")][:count]
        if not hits:
            tried.append(f"{name}: 解析不到结果")
            continue
        lines = [f"搜索「{query}」（来源 {name}，{len(hits)} 条）"]
        for i, hit in enumerate(hits, 1):
            lines.append(f"{i}. {hit['title']}\n   {hit['url']}")
            if hit["snippet"]:
                lines.append(f"   {hit['snippet']}")
        return "\n".join(lines)

    return "搜不到结果。\n" + "\n".join(tried)
