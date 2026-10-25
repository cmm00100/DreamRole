"""事件流 → 图。纯投影，不改任何状态。

这个文件存在的意义是：**图不是画出来的，是算出来的**。
同一份事件，前端用它算节点坐标，导出的报告用它算 Mermaid 文本，
两处互不感知，但结论一致 —— 因为它们读的是同一个真相源。

两种投影：

- `build()`   给界面：节点 + 边 + 阶段列，只讲结构不讲像素（像素归 CSS 管）；
- `to_mermaid()` 给人看：贴进任何支持 Mermaid 的地方就能渲染。
"""
from __future__ import annotations

from .events import Event, EventType

# 哪些事件配成为图上的一等节点。notice 里有一类是「阶段标记」，
# 它承载列的标题，本身不占格子 —— 否则每一列顶上会多一个空格子。
_STAGE_MARKER = "stage_marker"


def _is_marker(event: dict) -> bool:
    return bool((event.get("payload") or {}).get(_STAGE_MARKER))


def build(events: list[dict]) -> dict:
    """返回 {nodes, edges, stages, cols}。"""
    nodes: list[dict] = []
    edges: list[dict] = []
    stage_titles: dict[int, str] = {}
    rows: dict[int, int] = {}
    remap: dict[str, str] = {}      # 被折叠掉的节点 id → 它前面那个真实节点

    previous_id: str | None = None
    previous_stage = -1

    for raw in events:
        stage = int(raw.get("stage") or 0)
        title = raw.get("stage_title") or ""
        if title:
            stage_titles[stage] = title

        if _is_marker(raw):
            # 阶段标记只贡献列标题，不当节点。但下一个节点的 parent 指向它，
            # 所以记下「它等价于前面那个节点」，否则列与列之间的连线会断掉。
            # 注意这里**不动** previous_stage —— 保持为上一个真实节点的阶段，
            # 这样「从上一列跨到这一列」的那条边才会被判成虚线。
            if previous_id:
                remap[raw.get("id")] = previous_id
            continue

        row = rows.get(stage, 0)
        rows[stage] = row + 1

        node = {
            "id": raw.get("id"),
            "type": raw.get("type"),
            "content": raw.get("content") or "",
            "tool": raw.get("tool"),
            "stage": stage,
            "stage_title": title or stage_titles.get(stage, ""),
            "col": stage,
            "row": row,
            "step": raw.get("step_id"),
            "ts": raw.get("ts"),
            "payload": raw.get("payload") or {},
            "parent": raw.get("parent_id"),
        }
        nodes.append(node)

        parent = raw.get("parent_id") or previous_id
        parent = remap.get(parent, parent) if parent else parent
        if parent and parent != node["id"]:
            edges.append(
                {
                    "source": parent,
                    "target": node["id"],
                    "kind": "same" if stage == previous_stage else "cross",
                }
            )
        previous_id = node["id"]
        previous_stage = stage

    known = {n["id"] for n in nodes}
    edges = [e for e in edges if e["source"] in known and e["target"] in known]

    stages = [
        {
            "index": index,
            "title": stage_titles.get(index, "") or ("起始" if index == 0 else f"阶段 {index}"),
            "count": rows.get(index, 0),
        }
        for index in sorted(rows)
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "stages": stages,
        "cols": len(stages),
    }


# ---------------------------------------------------------------------------
# Mermaid 导出
# ---------------------------------------------------------------------------

_MERMAID_SHAPE = {
    EventType.USER.value: ('(["', '"])'),
    EventType.THINK.value: ('["', '"]'),
    EventType.ACTION.value: ('[["', '"]]'),
    EventType.RESULT.value: ('("', '")'),
    EventType.ANSWER.value: ('{{"', '"}}'),
    EventType.NOTICE.value: ('>"', '"]'),
}


def _escape(text: str, limit: int = 42) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    return flat.replace('"', "'").replace("[", "(").replace("]", ")").replace("{", "(").replace("}", ")")


def to_mermaid(events: list[dict], title: str = "") -> str:
    """把事件流写成 Mermaid flowchart。阶段用 subgraph 包起来。"""
    data = build(events)
    if not data["nodes"]:
        return "flowchart TD\n  empty[（还没有内容）]"

    lines = ["flowchart TD"]
    if title:
        lines.insert(0, f"%% {title}")

    by_stage: dict[int, list[dict]] = {}
    for node in data["nodes"]:
        by_stage.setdefault(node["col"], []).append(node)

    for stage in data["stages"]:
        index = stage["index"]
        lines.append(f'  subgraph S{index}["{_escape(stage["title"], 28)}"]')
        for node in by_stage.get(index, []):
            opener, closer = _MERMAID_SHAPE.get(node["type"], ('["', '"]'))
            text = node["content"]
            if node["type"] == EventType.ACTION.value:
                text = f"{node['tool'] or ''} · {text}"
            lines.append(f'    n{node["id"]}{opener}{_escape(text)}{closer}')
        lines.append("  end")

    for edge in data["edges"]:
        arrow = "-->" if edge["kind"] == "same" else "-.->"
        lines.append(f'  n{edge["source"]} {arrow} n{edge["target"]}')

    return "\n".join(lines)


def stats(events: list[dict]) -> dict:
    """给时间线用的一行摘要。"""
    counts: dict[str, int] = {}
    tools: dict[str, int] = {}
    for raw in events:
        etype = raw.get("type") or "?"
        counts[etype] = counts.get(etype, 0) + 1
        tool = raw.get("tool")
        if etype == EventType.ACTION.value and tool:
            tools[tool] = tools.get(tool, 0) + 1
    return {
        "total": len(events),
        "counts": counts,
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
    }


def events_from(rows: list[dict]) -> list[Event]:
    """磁盘上的 dict 转回 Event，给导出功能用。"""
    out: list[Event] = []
    for row in rows:
        try:
            out.append(Event.from_dict(row))
        except (KeyError, ValueError):
            continue
    return out
