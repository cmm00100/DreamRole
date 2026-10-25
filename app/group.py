"""群聊：怎么把「一堆历史」拼成「以我为中心的一段对话」。

这个模块存在的理由只有一个 —— **修一个让群聊假装互相看不见的 bug**。

从前群成员的每句话都以 `assistant` 存进历史，然后原样发给下一位：

```
assistant: [私人秘书] 我觉得这个 deadline 太紧
```

从模型的视角看，那是**它自己**刚说过的话。它没有「对面坐着人」这个概念，
于是接着自己的话往下说，或者把刚才那句换个说法再说一遍。
界面上一人一个头像、一人一句，看着像群聊；模型那边其实是一个人自言自语了四遍。

所以这里做三件事：

1. **`transcript()` 按「我」重组角色** —— 自己说的才是 `assistant`，
   别人说的变成 `user` 并带上 `【名字】`。模型这才有对话对象。
2. **`roster()` 告诉它屋里坐着谁** —— 不知道有谁，就没法「点名反对某人」。
3. **`contract()` 定死回应义务** —— 必须接前面某个人的话，不许重新回答用户最初那句；
   没话说就输出 `[skip]`，闭嘴比复读好。

豆包那份分析里还提到一条「并发时序」：并行调用会让所有人拿到同一版历史。
这条在我们这儿本来就不成立 —— `_group_loop` 一直是串行的，A 说完入库才轮到 B。
所以没动它，也没必要动。
"""
from __future__ import annotations

import re

SKIP = "[skip]"
# 模型可能写成 [skip] / 【skip】 / skip。多认几种，但要求**整句就是它**，
# 否则正文里提到这个词就会被误判成弃权。
_SKIP_RE = re.compile(r"^[\[【(（]?\s*skip\s*[\]】)）]?[。．.!！,，]?$", re.I)
# 存进历史的形状：`[名字] 正文`。名字里不许有换行，也不许有 `]`。
_HEAD_RE = re.compile(r"^\[([^\]\n]{1,40})\]\s?")

MAX_ROUNDS = 3
# 默认走两遍。
#
# 从前默认 1 —— 「各说一句就散会」。那正好是用户投诉的形状：每个人都表态了，
# 但没有一个人回头接上一位。1 轮虽然已经会把别人的话翻成 `user`（技术上看得见），
# 可它只有一次开口机会，说出来的还是「我对这件事的判断」，不是「我对你那句话的看法」。
# 第二遍才是真正的对话：有人反对，有人改条件，有人收口。
# 想省 token 的人在表单里选 1 就行 —— 默认值该给「像群聊」的那个，而不是最便宜的那个。
DEFAULT_ROUNDS = 2


# ---------------------------------------------------------------------------
# 拆与判
# ---------------------------------------------------------------------------

def split_utterance(content: str) -> tuple[str, str] | None:
    """`'[小明] 内容'` → `('小明', '内容')`；不是这个形状 → None。"""
    match = _HEAD_RE.match(content or "")
    if not match:
        return None
    return match.group(1).strip(), (content or "")[match.end():]


def is_skip(reply: str) -> bool:
    return bool(_SKIP_RE.match((reply or "").strip()))


def rounds_of(group: dict) -> int:
    """这个群一轮对话走几遍。1~3。

    **缺字段**（老群，还没有这个设置）跟默认走；显式写了个脏值（0、负数、
    "abc"）就夹回 1~3 —— 那两种情况的区别很重要：前者是「没设过」，
    该给个好默认；后者是「设坏了」，别替它猜成 2 轮又花一遍钱。
    """
    raw = (group or {}).get("rounds")
    if raw is None:
        return DEFAULT_ROUNDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS
    return max(1, min(value, MAX_ROUNDS))


def role_note(group: dict, member: dict) -> str:
    """这个人在**这个群里**负责什么。优先用群里单独设的，否则退回它的定位。"""
    roles = (group or {}).get("roles") or {}
    if isinstance(roles, dict):
        picked = str(roles.get(member.get("id") or "") or "").strip()
        if picked:
            return picked
    return str(member.get("tagline") or "").strip()


# ---------------------------------------------------------------------------
# 上下文重组
# ---------------------------------------------------------------------------

def transcript(messages: list[dict], me: str) -> list[dict]:
    """把共享历史翻成「以 `me` 为中心」的对话。

    自己说的 → `assistant`（去掉名字前缀，那就是我说的话）
    别人说的 → `user`，前缀 `【名字】`
    人类说的 → `user`，前缀 `【用户】`

    人类那句也必须带标签：改完之后它和同伴的话都是 `user` 角色，
    不带标签就分不清哪句是下指令、哪句是同伴在发言。
    """
    out: list[dict] = []
    for message in messages or []:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "assistant":
            parsed = split_utterance(content)
            if parsed is None:
                # 不是群发言（比如单人会话留下的），原样当自己说的
                out.append({"role": "assistant", "content": content})
            elif parsed[0] == me:
                out.append({"role": "assistant", "content": parsed[1]})
            else:
                out.append({"role": "user", "content": f"【{parsed[0]}】{parsed[1]}"})
        elif role == "user":
            out.append({"role": "user", "content": f"【用户】{content}"})
        else:
            out.append(message)
    return out


def others_spoke(messages: list[dict], me: str) -> bool:
    """历史里有没有「别人」说过话。决定要不要要求它先接话。"""
    for message in messages or []:
        if message.get("role") != "assistant":
            continue
        parsed = split_utterance(message.get("content") or "")
        if parsed and parsed[0] != me:
            return True
    return False


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

def roster(group: dict, members: list[dict], me_id: str) -> str:
    name = (group or {}).get("name") or "群聊"
    lines = [f"## 这个群：{name}"]
    topic = str((group or {}).get("topic") or "").strip()
    if topic:
        lines.append(f"平时聊的是：{topic}")

    others = [m for m in members if m.get("id") != me_id]
    if others:
        lines.append("群里除了你还有这些人：")
        for member in others:
            note = role_note(group, member)
            who = member.get("name") or member.get("id")
            emoji = f" {member['emoji']}" if member.get("emoji") else ""
            lines.append(f"- {who}{emoji}" + (f" —— {note}" if note else ""))

    mine = next((m for m in members if m.get("id") == me_id), None)
    if mine:
        note = role_note(group, mine)
        emoji = f" {mine['emoji']}" if mine.get("emoji") else ""
        lines.append(f"你是「{mine.get('name') or me_id}」{emoji}"
                     + (f" —— {note}" if note else ""))
    return "\n".join(lines)


def turn_block(group: dict, speakers: list[dict], me_id: str,
               spoken: list[str], round_no: int, rounds: int, index: int) -> str:
    rule = (group or {}).get("rule") or "round_robin"
    label = {"round_robin": "轮流说，每人一轮",
             "mention": "只让被点到名的说",
             "host": "由主持人点名"}.get(rule, "轮流说")

    lines = ["## 这一轮"]
    head = f"第 {round_no} 轮" + (f"（共 {rounds} 轮）" if rounds > 1 else "")
    lines.append(f"{head}。规则：{label}。")

    order = []
    for member in speakers:
        who = member.get("name") or member.get("id")
        order.append("**你**" if member.get("id") == me_id else who)
    if order:
        lines.append("发言顺序：" + " → ".join(order))

    if spoken:
        lines.append("这一轮已经说过：" + "、".join(spoken))
    # 「后面还有谁」按**实际顺序**算，不能拿全体成员减去自己 ——
    # 那样排在前面、已经说过的人也会被算成「后面还有」，等于告诉它个假消息。
    after = [m.get("name") or m.get("id") for m in speakers[index + 1:]
             if m.get("id") != me_id]
    if after:
        lines.append("你之后还会说：" + "、".join(dict.fromkeys(after)))
    return "\n".join(lines)


# 群成员这一轮**没有工具**（见 `_group_loop` 的说明）。可系统提示里那套
# 「先看再动 / 开阶段 / 跑测试 / 交结论」是给单干时的自己写的，
# 原样留着会让它一边开团一边想调工具，或者把三句话写成一份交付清单。
# 所以要在最前面把这段按下去 —— 覆盖比重写一套提示词更省，也更不容易失真。
_OVERRIDE = """## 现在这一轮是在开会，不是在写代码（覆盖上面「工作方式」那几条）

- 你这一轮**没有工具**。不要调用工具，也不要写「我先看一下 X」这种话。
- 不用开阶段，不用跑测试，不用交「改了哪些文件」的清单。
- 上面那些「分阶段推进」「写完必须验证」的要求，这一轮一律不适用。
- 你是群里的一位成员，正在跟其他人**讨论**。开口前先把别人说的读一遍。"""


def contract(first_turn: bool, rounds: int) -> str:
    """回应义务。这是让「各说各话」变成「对话」的那一段。"""
    lines = ["## 你要怎么说话"]
    if first_turn:
        lines.append("1. 这一轮你先说。直接给出你的判断，不要客套，也不要等别人。")
    else:
        lines.append(
            "1. **先接别人的话，并且点名**。第一句必须是"
            "「我同意 X 说的……，但是……」「X 那个结论我不认同，因为……」"
            "这种形状。做不到就先别说话。"
        )
        lines.append(
            "2. **不要重新回答用户最初那句话**。那句话已经有人回答过了，"
            "再说一遍就是各说各话 —— 那正是这个群最该避免的事。"
        )
    # 编号接着上面数，别出现「2. 之后直接 4.」—— 模型不一定在意，但人会。
    # 这里用「已经写了几条」推进，而不是硬编码 3。
    n = len(lines) - 1

    def step(text: str) -> None:
        nonlocal n
        n += 1
        lines.append(f"{n}. {text}")

    step("只说你要**补充的、要反对的、要改的**。不复述别人刚说过的话，不做全场总结。")
    step("三句话以内。")
    if rounds > 1:
        step("这一轮不是最后一轮，别人还会接。所以不要把话说死，"
             "把「还没定的事」留给下一轮。")
    step(f"**如果你没有新东西可说** —— 或者你完全同意、只想说「我也是」—— "
         f"就不要发言，整句只输出 `{SKIP}`。硬凑一句比不说更糟。")
    return "\n".join(lines)


def system_for(base: str, member: dict, group: dict, members: list[dict],
               speakers: list[dict], index: int, spoken: list[str],
               round_no: int, rounds: int, first_turn: bool, persona: str) -> str:
    """一位成员这一轮看到的系统提示。"""
    me_id = member.get("id") or ""
    name = member.get("name") or me_id
    host = (group or {}).get("host") or ""
    last_round = round_no >= rounds

    seat = []
    if (group or {}).get("rule") == "host" and me_id == host:
        if not spoken and round_no == 1 and index == 0:
            seat.append("你是主持人。先把话题拆开，说清这轮要解决什么，然后请其他人说。")
        elif last_round:
            seat.append("你是主持人。把大家说的收一收：哪些成了共识、哪些还顶着、"
                        "下一步谁做什么。这时允许你超过三句。")
        else:
            seat.append("你是主持人。别人说话时你不要抢，最后再说。")

    parts = [
        base,
        _OVERRIDE,
        persona,
        roster(group, members, me_id),
        turn_block(group, speakers, me_id, spoken, round_no, rounds, index),
        contract(first_turn, rounds),
        "\n".join(seat) if seat else "",
        f"记住：你是「{name}」，不是用户，也不是别的成员。",
    ]
    return "\n\n".join(p for p in parts if p)
