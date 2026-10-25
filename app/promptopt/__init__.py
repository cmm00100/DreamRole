"""提示词优化。

抄了 PromptOptimizer 的三条骨架，砍掉了它的四分之三：

1. **模板是 .md 文件，不是代码里的字符串。** 想改语气就改文件，不用碰 Python，
   也不会因为一个引号把程序改坏。
2. **主链路固定不变**：选模板 → 提取变量 → 渲染 → 调模型 → 校验输出。
   任何一步出问题都在那一步处理，不往下糊。
3. **假设模型会不听话。** 这是最值钱的一条：模型会说「以下是优化后的提示词：」，
   会把结果包进代码块，会懒得改写直接复读，会拒绝，会编造原文没有的路径。
   所以输出必须过一遍 `clean()` + `validate()`，问题写成 warnings 给用户看，
   而不是假装没发生。

没有照抄的部分：多轮迭代、多方案并排、模板变量自动补全的整套 DSL。
那些东西的价值在「批量做提示词工程」，而这里的使用场景是
「在输入框边上按一下，看看能不能说得更清楚」—— 一条路走通就够了。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..llm import ChatClient, LLMError
from .. import config

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
JUDGE_FILE = Path(__file__).resolve().parent / "prompts" / "judge.md"

MAX_INPUT = 12_000
MAX_OUTPUT = 20_000

# 用户这句话里「有没有提到」这几类信息。
# 这不是要凑满五项，而是为了让模型知道**哪里可以补、哪里不能编**。
SLOTS: list[tuple[str, str, str]] = [
    ("task", "要做什么", r"帮|请|做|改|写|生成|实现|分析|检查|整理|翻译|画"),
    ("context", "背景 / 在哪儿做", r"这个项目|这个文件|这段代码|目录|仓库|当前|之前|我的|里面"),
    ("constraints", "约束", r"不要|别|必须|只能|最多|不超过|禁止|除了|保持|别动"),
    ("output", "输出形式", r"表格|列表|json|markdown|文档|报告|一句话|字以内|格式|步骤"),
    ("role", "角色", r"作为|扮演|以.{0,8}身份|你是一个"),
]

_SAFE_NAME = re.compile(r"^[a-z0-9_-]+$")


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

def _parse_template(text: str) -> tuple[dict, str]:
    meta: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            head, body = parts[1], parts[2]
            for line in head.splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if key:
                    meta[key] = value
    return meta, body.strip()


def catalog() -> list[dict]:
    items: list[dict] = []
    if not TEMPLATE_DIR.is_dir():
        return items
    for path in sorted(TEMPLATE_DIR.glob("*.md")):
        try:
            meta, body = _parse_template(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        tid = str(meta.get("id") or path.stem)
        if not _SAFE_NAME.match(tid):
            continue
        items.append(
            {
                "id": tid,
                "name": meta.get("name") or tid,
                "description": meta.get("description") or "",
                "hint": meta.get("hint") or "",
                "order": int(meta.get("order") or 99),
                "chars": len(body),
            }
        )
    items.sort(key=lambda item: (item["order"], item["id"]))
    return items


def _load_template(template_id: str) -> tuple[dict, str] | None:
    for path in TEMPLATE_DIR.glob("*.md"):
        try:
            meta, body = _parse_template(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if str(meta.get("id") or path.stem) == template_id:
            return meta, body
    return None


def suggest_template(text: str) -> str:
    """按内容猜一个最合适的模板 —— 前端默认选中它。"""
    raw = (text or "").lower()
    if re.search(r"画|插画|出图|海报|配图|image|logo|头像|封面", raw):
        return "image"
    if re.search(r"讲|教|解释|入门|学|带我|原理|为什么", raw):
        return "teach"
    if re.search(r"代码|函数|bug|报错|接口|重构|测试|项目|文件|脚本|部署", raw):
        return "code"
    if re.search(r"报告|方案|总结|分析|文档|流程|步骤|计划", raw):
        return "structure"
    return "general"


# ---------------------------------------------------------------------------
# 变量
# ---------------------------------------------------------------------------

def detect_slots(text: str) -> tuple[list[str], list[str]]:
    """返回 (原文里已经有的, 没提到的)，元素是槽位的中文名。"""
    found: list[str] = []
    missing: list[str] = []
    for _key, label, pattern in SLOTS:
        if re.search(pattern, text or "", re.IGNORECASE):
            found.append(label)
        else:
            missing.append(label)
    # 「要做什么」几乎每句话都有，缺了也不算问题，免得吓唬人
    missing = [label for label in missing if label != "要做什么"]
    return found, missing


def _context_block(text: str) -> str:
    found, missing = detect_slots(text)
    lines = []
    if found:
        lines.append("用户这句话里已经有的：" + "、".join(found) + "。")
    if missing:
        lines.append("没提到的：" + "、".join(missing) + "。")
        lines.append(
            "没提到的地方**不要替他想**，在需要补的位置写 `【待补充：" + missing[0] + "】` 这样的标记。"
        )
    else:
        lines.append("用户这句话说得比较完整。重点是把模糊的词换成具体的，不要加内容。")
    return "\n".join(lines)


def _render(body: str, values: dict[str, str]) -> str:
    """把 {{key}} 换掉。没给值的键保留原样 —— 让错误显式暴露，别静默变成空。"""
    out = body
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    return out


# ---------------------------------------------------------------------------
# 输出清洗与校验
# ---------------------------------------------------------------------------

_PREAMBLE_A = re.compile(
    r"^[ \t]*(?:(?:好的|嗯|ok)[，,、]?[ \t]*)?"
    r"(?:以下是|下面是|这是|这就是)"
    r"[^\n:：]{0,30}?(?:提示词|版本|内容|结果|文案|文本)[^\n:：]{0,6}?[:：]?[ \t]*\n?",
    re.IGNORECASE,
)
_PREAMBLE_B = re.compile(
    r"^[ \t]*(?:(?:好的|嗯|ok)[，,、]?[ \t]*)?"
    r"(?:优化|改写|重写|润色|整理)(?:后|过)?的?(?:提示词|版本|内容|结果)[ \t]*[:：][ \t]*\n?",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)
_REFUSAL = ("我无法", "我不能", "抱歉", "作为一个", "as an ai", "i cannot")


def clean(raw: str) -> str:
    """模型很爱加的那几层壳，全扒掉。

    要循环两遍：模型的典型输出是「以下是优化后的提示词：」+ 一个代码块，
    先扒前言才能看见代码块，先扒代码块又扒不掉前言。一遍不够。
    """
    text = (raw or "").strip()
    if not text:
        return ""

    for _ in range(2):
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()

        for pattern in (_PREAMBLE_A, _PREAMBLE_B):
            stripped = pattern.sub("", text, count=1)
            if stripped.strip() and stripped != text:
                text = stripped.strip()
                break

    # 整段被引号包住
    for left, right in (('"', '"'), ("“", "”"), ("'", "'"), ("『", "』")):
        if text.startswith(left) and text.endswith(right) and len(text) > 2:
            text = text[1:-1].strip()
            break

    return text.strip()


def validate(original: str, optimized: str) -> list[str]:
    """返回问题清单。空列表 = 没问题。"""
    warnings: list[str] = []
    if not optimized:
        warnings.append("模型什么也没返回，再试一次")
        return warnings

    head = optimized[:80].lower()
    if any(marker in head for marker in _REFUSAL):
        warnings.append("模型这次没改写，而是回了一段拒绝/说明。换一种说法或换模板试试")
        return warnings

    if optimized.strip() == original.strip():
        warnings.append("模型原样返回了，没做改动。可能这段话已经够清楚了")
        return warnings

    if len(optimized) < max(24, int(len(original) * 0.4)):
        warnings.append("改写结果比原文短不少，可能有内容被漏掉了")

    if len(optimized) > MAX_OUTPUT:
        warnings.append("改写结果特别长，用之前先看看有没有跑题")
        warnings.append(f"（共 {len(optimized)} 字，建议先删掉用不上的部分）")

    placeholders = optimized.count("【待补充")
    if placeholders:
        warnings.append(f"有 {placeholders} 处标了【待补充】，用之前把它们填上")
    return warnings


# ---------------------------------------------------------------------------
# 调模型
# ---------------------------------------------------------------------------

def _client() -> ChatClient:
    conn = config.provider_conn()
    override = str(config.get().get("optimize_model") or "").strip()
    if override:
        conn = {**conn, "model": override}
    return ChatClient(conn)


GOAL_LINES = {
    "clear": "更清晰：一句话只说一件事，去掉含糊的代词和口语填充",
    "structure": "结构化：拆成「角色 / 背景 / 任务 / 约束 / 输出」几块，能分条就分条",
    "shorter": "更短：删掉所有可删的字，目标是不超过原文的八成",
    "constraints": "加约束：写清不能做什么、边界在哪、什么情况下该停下来问",
    "examples": "加示例：给一个最小输入输出样例，让预期不用猜",
    "role": "角色化：开头点明「你是谁、按什么标准判断」",
}


def optimize(text: str, template_id: str = "", target: str = "",
             goals: list[str] | None = None) -> dict:
    """把一段话改写成更清楚的提示词。永远返回 dict，不抛异常。"""
    original = (text or "").strip()
    if not original:
        return {"ok": False, "error": "输入是空的"}
    if len(original) > MAX_INPUT:
        return {"ok": False, "error": f"太长了（{len(original)} 字），先删到 {MAX_INPUT} 字以内"}

    tid = template_id if template_id and _SAFE_NAME.match(template_id) else suggest_template(original)
    loaded = _load_template(tid)
    if not loaded:
        return {"ok": False, "error": f"没有叫 {tid} 的模板"}

    meta, body = loaded
    prompt = _render(
        body,
        {
            "original": original,
            "target": target or "（未指定）",
            "context_block": _context_block(original),
        },
    )
    # 「期望效果」是叠在模板之上的一层要求，不是另一个模板。
    # 为什么不做成六个模板：模板决定**结构**，期望效果决定**取舍** ——
    # 「更短」和「加示例」可以同时要，模板只能选一个。
    wanted = [GOAL_LINES[key] for key in (goals or []) if key in GOAL_LINES]
    if wanted:
        prompt += "\n\n另外，这一版还必须做到：\n" + "\n".join(f"- {line}" for line in wanted)

    try:
        client = _client()
        raw = client.complete(
            [
                {"role": "system", "content": "你只输出改写后的提示词正文。不要解释，不要客套。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=2000,
        )
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"调用模型出错：{type(exc).__name__}: {exc}"}

    optimized = clean(raw)
    warnings = validate(original, optimized)
    found, missing = detect_slots(original)

    return {
        "ok": bool(optimized),
        "original": original,
        "optimized": optimized,
        "template": tid,
        "template_name": meta.get("name") or tid,
        "warnings": warnings,
        "known": found,
        "missing": missing,
        "chars": {"before": len(original), "after": len(optimized)},
    }


# ---------------------------------------------------------------------------
# 对比评审
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict | None:
    """从模型的回话里挖出第一个 JSON 对象。

    模型会包代码块、会在前面说「好的，我的评估如下」、会少一个逗号。
    这里不追求万能，只追求「常见情况能救回来，救不回来就老实返回 None」。
    """
    text = (raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : index + 1]
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        repaired = re.sub(r",\s*([}\]])", r"\1", chunk)
                        try:
                            return json.loads(repaired)
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None


def _as_list(value, limit: int = 3) -> list[str]:
    if isinstance(value, str):
        return [value][:limit]
    if isinstance(value, list):
        return [str(item)[:200] for item in value if str(item).strip()][:limit]
    return []


def _score(value) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(10, number))


def compare(original: str, optimized: str) -> dict:
    """让模型当裁判对比两版。失败就返回 ok=False，前端把这块藏起来。"""
    if not original.strip() or not optimized.strip():
        return {"ok": False, "reason": "两版都得有内容才能比"}
    try:
        template = JUDGE_FILE.read_text(encoding="utf-8")
    except OSError:
        return {"ok": False, "reason": "评审模板缺失"}
    prompt = _render(template, {"original": original[:6000], "optimized": optimized[:6000]})

    try:
        raw = _client().complete(
            [
                {"role": "system", "content": "你只输出一个 JSON 对象，不要任何别的文字。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"评审没跑起来：{type(exc).__name__}: {exc}"}

    data = _extract_json(raw)
    if not data:
        # 模型没给出能解析的 JSON。这不是错误，是常态之一 —— 前端不显示这块即可。
        return {"ok": False, "reason": "评审给的不是结构化结果，已忽略"}

    winner = str(data.get("winner") or "").strip().lower()
    if winner not in ("optimized", "original", "tie"):
        winner = "tie"

    return {
        "ok": True,
        "winner": winner,
        "winner_label": {"optimized": "改写版更好", "original": "原话更好", "tie": "各有取舍"}[winner],
        "scores": {
            "original": _score(data.get("score_original")),
            "optimized": _score(data.get("score_optimized")),
        },
        "learnable": _as_list(data.get("learnable")),
        "overfit": _as_list(data.get("overfit")),
        "conflicts": _as_list(data.get("conflicts")),
    }
