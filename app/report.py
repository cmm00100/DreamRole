"""把一条会话导出成人能读的东西。

为什么值得写：事件流是给机器看的（谁挂在谁下面、第几步、哪一列），
但用户想存下来发给同事的是一份**能直接读**的东西 —— 谁问了什么、
agent 动了哪些文件、结论是什么。这两个需求长得很不一样，
所以这里单独做一层投影，而不是把事件 JSON 直接丢出去。

三种格式：
- `md`       Markdown，带过程图，适合贴进文档或 PR 描述
- `mermaid`  纯图，适合贴进任何支持 mermaid 的地方
- `html`     单文件静态页，双击就能看，不依赖服务
"""
from __future__ import annotations

import html
import time

from . import graph


def _fmt_time(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _users(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "user"]


def title_of(events: list[dict], meta: dict | None = None) -> str:
    given = (meta or {}).get("title")
    if given:
        return str(given)
    first = next((e.get("content") for e in events if e.get("type") == "user"), "")
    return " ".join(str(first).split())[:60] or "未命名会话"


def _usage_line(meta: dict) -> str:
    usage = meta.get("usage") or {}
    total = int(usage.get("total") or 0)
    if not total:
        return ""
    parts = [f"输入 {int(usage.get('prompt') or 0)}", f"输出 {int(usage.get('completion') or 0)}"]
    calls = int(usage.get("calls") or 0)
    if calls:
        parts.append(f"{calls} 次调用")
    secs = float(usage.get("seconds") or 0)
    if secs:
        parts.append(f"耗时 {secs:.1f}s")
    return f"共 {total} token（{' / '.join(parts)}）"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def to_markdown(events: list[dict], meta: dict | None = None) -> str:
    meta = meta or {}
    title = title_of(events, meta)
    out: list[str] = [f"# {title}", ""]

    bits = []
    if meta.get("project"):
        bits.append(f"项目：`{meta['project']}`")
    if meta.get("mode"):
        bits.append(f"模式：{meta['mode']}")
    if meta.get("created"):
        bits.append(_fmt_time(meta["created"]))
    usage = _usage_line(meta)
    if usage:
        bits.append(usage)
    if bits:
        out += [" · ".join(bits), ""]

    # 把「工具动作 + 它的结果」先配好对，正文里才能写成一行
    results = {e.get("parent_id"): e for e in events if e.get("type") == "result"}

    step = 0
    last_stage = None
    for event in events:
        etype = event.get("type")
        content = str(event.get("content") or "")

        if etype == "user":
            step += 1
            out += ["", "---", "", f"## {step}. 我说", "", content.strip(), ""]
            last_stage = None
            continue

        stage = event.get("stage") or 0
        if stage and stage != last_stage and etype != "notice":
            out += [f"### 阶段 {stage} · {event.get('stage_title') or ''}".rstrip(" ·"), ""]
            last_stage = stage

        if etype == "notice":
            if event.get("payload", {}).get("stage_marker"):
                out += [f"### 阶段 {stage} · {event.get('stage_title') or ''}".rstrip(" ·"), ""]
                last_stage = stage
            elif event.get("payload", {}).get("todos_update"):
                out += ["**待办**", ""]
                marks = {"pending": "- [ ]", "doing": "- [~]", "done": "- [x]"}
                for item in event["payload"].get("todos") or []:
                    out.append(f"{marks.get(item.get('status'), '- [ ]')} {item.get('text')}")
                out.append("")
            else:
                out += [f"> {content.strip()}", ""]
            continue

        if etype == "action":
            name = event.get("tool") or ""
            label = (event.get("payload") or {}).get("label") or name
            detail = (event.get("payload") or {}).get("detail") or ""
            detail = " ".join(str(detail).split())
            if len(detail) > 160:
                detail = detail[:160] + "…"
            out.append(f"- **{label}** `{detail}`")
            hit = results.get(event.get("id"))
            if hit is not None:
                body = str(hit.get("content") or "")
                ok = (hit.get("payload") or {}).get("ok", True)
                head = body.strip().splitlines()[0][:140] if body.strip() else "（没有输出）"
                mark = "✓" if ok else "✗"
                out.append(f"  - {mark} {head}")
            continue

        if etype == "result":
            # 已经在 action 下面写过了，跳过
            continue

        if etype == "reasoning":
            body = content.strip()
            if body:
                out += ["<details><summary>思考过程（深度思考）</summary>", "", body, "",
                        "</details>", ""]
            continue

        if etype == "think":
            body = content.strip()
            if body:
                out += ["<details><summary>过程说明</summary>", "", body, "", "</details>", ""]
            continue

        if etype == "answer":
            body = content.strip()
            if body:
                out += ["", body, ""]

    out += ["", "---", "", "## 过程图", "", "```mermaid", graph.to_mermaid(events, title), "```", ""]
    return "\n".join(out).replace("\n\n\n", "\n\n")


# ---------------------------------------------------------------------------
# 单文件 HTML
# ---------------------------------------------------------------------------

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#1c1f24;
  font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:40px 22px 80px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}
.meta{color:#6b7280;font-size:13px;margin-bottom:28px}
.turn{background:#fff;border:1px solid #e6e8ec;border-radius:12px;
  padding:18px 20px;margin-bottom:18px}
.turn h2{font-size:16px;margin:0 0 10px;color:#0f766e}
.stage{display:inline-block;font-size:12px;color:#7c3aed;background:#f3e8ff;
  border-radius:999px;padding:2px 10px;margin:14px 0 8px}
.ask{white-space:pre-wrap;margin:0 0 14px;padding-left:12px;border-left:3px solid #0f766e}
ul.tools{list-style:none;padding:0;margin:0}
ul.tools li{padding:7px 0;border-top:1px dashed #eceef2;font-size:13.5px}
ul.tools li:first-child{border-top:0}
code{background:#f1f3f5;padding:1px 5px;border-radius:4px;font-size:12.5px;
  font-family:ui-monospace,Consolas,monospace;word-break:break-all}
.ok{color:#0f9d58}.bad{color:#d93025}
.answer{white-space:pre-wrap;margin:14px 0 0;padding:12px 14px;background:#fbfcfd;
  border:1px solid #eef0f4;border-radius:9px}
details{margin:10px 0 0;font-size:13px;color:#6b7280}
summary{cursor:pointer}
.note{font-size:13px;color:#92400e;background:#fffbeb;border-radius:8px;
  padding:7px 11px;margin:10px 0}
.todo{font-size:13px;margin:10px 0}
.todo div{padding:2px 0;color:#4b5563}
.todo .done{color:#9ca3af;text-decoration:line-through}
.svgbox{margin-top:34px}
.svgbox h2{font-size:15px;color:#374151}
.hint{color:#9ca3af;font-size:12.5px}
"""


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def to_html(events: list[dict], meta: dict | None = None) -> str:
    meta = meta or {}
    title = title_of(events, meta)
    results = {e.get("parent_id"): e for e in events if e.get("type") == "result"}

    body: list[str] = []
    step = 0
    last_stage: int | None = None
    open_turn = False

    def close_turn() -> None:
        nonlocal open_turn
        if open_turn:
            body.append("</div>")
            open_turn = False

    for event in events:
        etype = event.get("type")
        content = str(event.get("content") or "")
        stage = int(event.get("stage") or 0)
        marker = (event.get("payload") or {}).get("stage_marker")

        if etype == "user":
            close_turn()
            step += 1
            last_stage = None
            body.append('<div class="turn">')
            open_turn = True
            body.append(f"<h2>{step}. 我说</h2>")
            body.append(f'<p class="ask">{_esc(content.strip())}</p>')
            continue

        if etype == "notice":
            if event.get("payload", {}).get("todos_update"):
                items = event["payload"].get("todos") or []
                if items:
                    rows = "".join(
                        f'<div class="{ "done" if item.get("status") == "done" else "" }">'
                        f'{"☑" if item.get("status") == "done" else ("◐" if item.get("status") == "doing" else "☐")} '
                        f'{_esc(item.get("text") or "")}</div>'
                        for item in items
                    )
                    body.append(f'<div class="todo">{rows}</div>')
                continue
            if not marker:
                body.append(f'<div class="note">{_esc(content.strip())}</div>')
                continue

        if marker or (stage and stage != last_stage and etype == "action"):
            if stage and stage != last_stage:
                body.append(f'<div class="stage">阶段 {stage} · {_esc(event.get("stage_title") or "")}</div>')
                last_stage = stage
            if marker:
                continue

        if etype == "action":
            body.append('<ul class="tools">')
            label = (event.get("payload") or {}).get("label") or event.get("tool") or ""
            detail = " ".join(str((event.get("payload") or {}).get("detail") or "").split())
            if len(detail) > 200:
                detail = detail[:200] + "…"
            body.append(f"<li><strong>{_esc(label)}</strong> <code>{_esc(detail)}</code>")
            hit = results.get(event.get("id"))
            if hit is not None:
                text = str(hit.get("content") or "").strip()
                head = text.splitlines()[0][:200] if text else "（没有输出）"
                ok = (hit.get("payload") or {}).get("ok", True)
                cls = "ok" if ok else "bad"
                body.append(f'<br><span class="{cls}">{"✓" if ok else "✗"} {_esc(head)}</span>')
            body.append("</li></ul>")
            continue

        if etype == "reasoning":
            text = content.strip()
            if text:
                body.append(f'<details class="reason"><summary>思考过程</summary>'
                            f"{_esc(text)}</details>")
            continue

        if etype == "think":
            text = content.strip()
            if text:
                body.append(f"<details><summary>过程说明</summary>{_esc(text)}</details>")
            continue

        if etype == "answer":
            text = content.strip()
            if text:
                body.append(f'<p class="answer">{_esc(text)}</p>')

    close_turn()

    if not body:
        body.append('<p class="hint">这个会话还没有内容。</p>')

    head_bits = []
    if meta.get("project"):
        head_bits.append(f"项目 {_esc(meta['project'])}")
    if meta.get("mode"):
        head_bits.append(f"模式 {_esc(meta['mode'])}")
    if meta.get("created"):
        head_bits.append(_fmt_time(meta["created"]))
    usage = _usage_line(meta)
    if usage:
        head_bits.append(_esc(usage))

    diagram = graph.to_mermaid(events, title)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>{_esc(title)}</h1>
<div class="meta">{' · '.join(head_bits)}</div>
{''.join(body)}
<div class="svgbox">
<h2>过程图</h2>
<p class="hint">把下面这段贴到任何支持 mermaid 的编辑器里就能出图。</p>
<pre><code>{_esc(diagram)}</code></pre>
</div>
</div></body></html>
"""
