"""子代理：把「翻很多文件但只要一个结论」的活儿分包出去。

为什么值得单独做一层：主线程的上下文是最贵的资源。一次探索要读 10 个文件，
如果都在主线程读，这 10 个文件的内容会一直占着上下文，直到被压缩掉。
子代理有自己的 messages，读完就扔，只把结论带回来 —— 主线程只付一份摘要的钱。

这里刻意只做**只读**子代理。能写文件的子代理要处理「两个线程同时改一个文件」
「改到一半失败谁负责回滚」这些问题，成本远大于收益。
"""
from __future__ import annotations

import threading
import time

from . import tools
from .llm import LLMError, ChatClient
from .paths import RESOURCE
from .tools.base import ToolContext

MAX_ROUNDS = 8
MAX_RETURN = 8000

ROLES: dict[str, dict[str, str]] = {
    "explore": {"label": "探索", "prompt": "subagent-explore.md",
                "why": "摸清一块代码是怎么工作的"},
    "review": {"label": "审查", "prompt": "subagent-review.md",
               "why": "找问题和风险"},
    "summarize": {"label": "整理", "prompt": "subagent-summarize.md",
                  "why": "把一堆文件压成摘要"},
    "plan": {"label": "方案", "prompt": "subagent-plan.md",
             "why": "给一份能落地的改法"},
    "verify": {"label": "核对", "prompt": "subagent-verify.md",
               "why": "查「说的」和「做的」对不对得上"},
}

# 一次最多并几个。再多，模型调用就会撞上同一条限流，
# 省下的时间还不如排队重试花掉的多。
MAX_PARALLEL = 4

PROMPT_DIR = RESOURCE / "app" / "prompts"
_ROLE_CACHE: dict[str, str] = {}


def _role_prompt(role: str) -> str:
    if role in _ROLE_CACHE:
        return _ROLE_CACHE[role]
    meta = ROLES.get(role) or ROLES["explore"]
    try:
        text = (PROMPT_DIR / meta["prompt"]).read_text(encoding="utf-8").strip()
    except OSError:
        text = "你是一个只读的探索代理。给出结论、证据（路径:行号）和未确认的部分。"
    _ROLE_CACHE[role] = text
    return text


def _system(role: str, project: str) -> str:
    return "\n\n".join(
        [
            f"你在 DreamRole 里当子代理。项目根目录：`{project}`。",
            "你能用的工具只有这几个（全部只读）："
            + "、".join(sorted(tools.READ_ONLY)),
            "你只跑这一件事，跑完就交结论。不要问用户问题——用户看不到你。",
            "-" * 30,
            _role_prompt(role),
        ]
    )


def run(role: str, task: str, parent, max_rounds: int = MAX_ROUNDS,
        root_id: str | None = None) -> str:
    """在父会话的事件流里跑一个只读子代理，返回给主模型看的字符串。

    `root_id` 只在并行时传：几个子代理同时在同一个事件流上发事件，如果都挂在
    `parent.last_parent` 上，图就会画成一条在几个代理之间来回跳的乱线。
    给每个子代理一条自己的链，起点是那条「派了几个」的提示事件。
    """
    role = role if role in ROLES else "explore"
    label = ROLES[role]["label"]
    try:
        client = ChatClient()
    except LLMError as exc:
        return f"子代理没起来：{exc}"

    # 并行时自己维护链条；串行时沿用主链（和以前行为一致）
    link = {"last": root_id}

    def emit(etype: str, content: str, **kwargs):
        if root_id is None:
            return parent.emit(etype, content, **kwargs)
        event = parent.emit(etype, content, parent_id=link["last"], **kwargs)
        link["last"] = event.id
        return event

    # 子代理只读，但它替用户干的活可能本来就在授权目录里（「去看看 D://资料 里那份表」）。
    # 不继承 grants 的话，用户明明授权过，子代理却看不见 —— 这种不一致最难解释。
    ctx = ToolContext(root=parent.root, cid=parent.cid, project=parent.project,
                      grants=list(getattr(parent, "grants", []) or []))
    names = set(tools.READ_ONLY)
    schemas = tools.schemas_for(names)
    messages: list[dict] = [
        {"role": "system", "content": _system(role, parent.project)},
        {"role": "user", "content": task},
    ]

    started = time.monotonic()
    rounds = 0
    answer = ""

    for rounds in range(1, max_rounds + 1):
        if parent.cancelled.is_set():
            return "（子代理已中止）"
        try:
            result = client.chat(
                messages,
                tools=schemas,
                is_cancelled=parent.cancelled.is_set,
                temperature=0.2,
            )
        except LLMError as exc:
            return f"子代理调用模型失败：{exc}"

        content = (result.get("content") or "").strip()
        calls = result.get("tool_calls") or []

        assistant: dict = {"role": "assistant", "content": result.get("content") or ""}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": call.get("id") or f"sub_{i}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call.get("arguments") or "{}"},
                }
                for i, call in enumerate(calls)
            ]
        messages.append(assistant)

        if not calls:
            answer = content
            break

        for call in calls:
            if parent.cancelled.is_set():
                return "（子代理已中止）"
            name = call["name"]
            if name not in names:
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id") or "",
                     "content": f"子代理不能使用 {name}，只有只读工具。"}
                )
                continue
            action = emit(
                "action",
                f"子代理·{label} · {name}",
                tool=name,
                payload={"subagent": role, "tool": name},
            )
            output = tools.call(ctx, name, call.get("arguments") or "{}")
            emit(
                "result",
                output[:4000],
                tool=name,
                payload={"subagent": role, "tool": name, "chars": len(output)},
            )
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id") or "", "content": output[:12000]}
            )

    seconds = round(time.monotonic() - started, 1)
    if not answer:
        return f"子代理（{label}）跑了 {rounds} 轮还没得出结论。把任务范围说小一点再派一次。"

    emit(
        "notice",
        f"子代理（{label}）回来了：{rounds} 步 / {seconds}s",
        payload={"subagent_done": role, "rounds": rounds, "seconds": seconds},
    )
    body = answer[:MAX_RETURN]
    return f"【子代理 {label}｜{rounds} 步 / {seconds}s】\n{body}"


def run_many(tasks: list[dict], parent) -> str:
    """一次派多个，并行跑。

    为什么值得并行：三个互不相干的调查串着跑，用户要等三倍的时间，
    而这三次调查之间没有任何依赖 —— 纯粹是在等 IO。
    但并行有个代价：事件会交错。所以先发一条「派了几个」的提示，
    再让每个子代理从它那里各自拉一条支线，图上就是清楚的三叉，而不是一团毛线。
    """
    cleaned: list[dict] = []
    for item in tasks[:MAX_PARALLEL]:
        if not isinstance(item, dict):
            continue
        task = str(item.get("task") or "").strip()
        if not task:
            continue
        role = str(item.get("role") or "explore")
        cleaned.append({"role": role if role in ROLES else "explore", "task": task})

    if not cleaned:
        return "没给出要查什么（tasks 里每项都要有 task）。"
    if len(cleaned) == 1:
        item = cleaned[0]
        return run(item["role"], item["task"], parent)

    marker = parent.emit(
        "notice",
        "并行派 {} 个子代理：{}".format(
            len(cleaned), "、".join(ROLES[i["role"]]["label"] for i in cleaned)
        ),
        payload={"subagent_batch": [i["role"] for i in cleaned]},
    )

    results: list[str] = [""] * len(cleaned)

    def worker(index: int, item: dict) -> None:
        try:
            results[index] = run(item["role"], item["task"], parent, root_id=marker.id)
        except Exception as exc:  # noqa: BLE001 - 一个挂了不能拖垮其他几个
            results[index] = f"第 {index + 1} 个子代理崩了：{type(exc).__name__}: {exc}"

    threads = [
        threading.Thread(target=worker, args=(index, item), daemon=True)
        for index, item in enumerate(cleaned)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if parent.cancelled.is_set():
        return "（并行子代理已中止）"

    blocks = [f"第 {i + 1} 份｜{ROLES[item['role']]['label']}\n{results[i]}"
              for i, item in enumerate(cleaned)]
    parent.emit(
        "notice",
        f"{len(cleaned)} 个子代理都回来了",
        payload={"subagent_batch_done": len(cleaned)},
    )
    return "\n\n".join(blocks)


def catalog() -> list[dict]:
    return [
        {"role": role, "label": meta["label"], "why": meta.get("why", ""),
         "chars": len(_role_prompt(role))}
        for role, meta in ROLES.items()
    ]
