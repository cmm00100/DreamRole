"""开发用的假模型：不花 token 也能把整条链路跑一遍。

只在 `DREAMROLE_FAKE_LLM=1` 时由 desktop.py 装上。它按「这是第几轮」决定做什么，
所以每次跑出来的过程图都一样，方便对照界面有没有画错。

    DREAMROLE_FAKE_LLM=1 python desktop.py --no-window
"""
from __future__ import annotations

import json

SCRIPT = [
    {
        "content": "先别急着改。我先看一眼这个目录里有什么，再决定从哪儿下手。",
        "calls": [
            ("set_stage", {"title": "先看清这里"}),
            ("list_dir", {"path": "."}),
        ],
    },
    {
        "content": "目录结构清楚了。接着读一下说明文件，确认这个项目自己是怎么描述自己的。",
        "calls": [("read_file", {"path": "README.md"})],
    },
    {
        "content": "看到这儿可以先动手了。第一件事是把入口文件补一个说明注释。",
        "calls": [("set_stage", {"title": "动手改一处"})],
    },
    {
        "content": "改完立刻读回来确认，别凭记忆认为改对了。",
        "calls": [
            ("write_file", {"path": "dreamrole-demo.txt", "content": "DreamRole 的假模型写的文件。\n这一行是用来验证 diff 和回滚的。\n"}),
            ("read_file", {"path": "dreamrole-demo.txt"}),
        ],
    },
    {
        "content": "最后一件事：把这个文件删掉，看看删除是不是走了回收站。",
        "calls": [("delete_file", {"path": "dreamrole-demo.txt", "reason": "演示文件，用完就删，验证删除是否走回收站"})],
    },
    {
        "content": (
            "做完了。这次一共三步：\n\n"
            "1. **看清这里** —— 列目录、读说明，没动任何文件；\n"
            "2. **动手改一处** —— 写了一个演示文件并读回来验证；\n"
            "3. **收尾** —— 把演示文件删掉，走的是回收站，能还原。\n\n"
            "下一步如果你要真改这个项目，我建议先从入口文件开始。"
        ),
        "calls": [],
    },
]


class FakeClient:
    """接口和真 ChatClient 一样，只是不走网络。"""

    # 同一个会话里再跑一轮时，messages 里已经躺着一堆上一轮的 assistant，
    # 直接按条数算就会落在剧本的最后一格（纯说话），一个工具都不调。
    # 要在第二轮里接着演，就把这里设成「要跳过的格子数」。
    offset = 0

    def __init__(self, conn=None):
        self.conn = conn or {}
        self.model = "fake-model"

    def _step_index(self, messages):
        seen = sum(1 for m in messages if m.get("role") == "assistant")
        return max(0, seen - FakeClient.offset)

    def chat(self, messages, tools=None, on_delta=None, is_cancelled=None,
             temperature=0.3, max_tokens=None, **kwargs):
        from app.llm import Cancelled

        if is_cancelled and is_cancelled():
            raise Cancelled()

        index = min(self._step_index(messages), len(SCRIPT) - 1)
        step = SCRIPT[index]
        content = step["content"]

        if on_delta and content:
            # 故意切碎，模拟真实流式的节奏
            for cursor in range(0, len(content), 6):
                on_delta(content[cursor:cursor + 6])

        calls = [
            {"id": f"fake_{index}_{position}", "name": name,
             "arguments": json.dumps(args, ensure_ascii=False)}
            for position, (name, args) in enumerate(step["calls"])
        ]
        return {
            "content": content,
            "tool_calls": calls,
            "usage": {"prompt": 120, "completion": 80, "total": 200, "seconds": 0.2},
            "reasoning": "",
        }

    def complete(self, messages, temperature=0.2, max_tokens=None) -> str:
        prompt = str(messages[-1].get("content") or "")
        if "JSON" in prompt or "json" in prompt:
            return json.dumps(
                {
                    "winner": "optimized",
                    "score_original": 5,
                    "score_optimized": 8,
                    "learnable": ["把验收标准写出来了", "明确说了不要动无关代码"],
                    "overfit": ["补了一个原文没提的文件名"],
                    "conflicts": [],
                },
                ensure_ascii=False,
            )
        return (
            "## 任务\n把 README 重写一遍，让第一次打开这个项目的人能在三分钟内跑起来。\n\n"
            "## 约束\n- 不要改动代码，只改 README.md\n- 保留原有的项目名和命令\n\n"
            "## 输出\n一个 README.md，包含「这是什么 / 怎么跑 / 目录说明」三节。\n\n"
            "## 验收\n按 README 的步骤真的能跑起来。"
        )


def install() -> None:
    import app.llm as llm
    import app.session as session

    llm.ChatClient = FakeClient
    session.ChatClient = FakeClient
    print("[fake] 已启用假模型，不会真的调用任何 API")


# ---------------------------------------------------------------------------
# 再来一轮：给「产物」这类要看得见文件的功能准备点真东西
# ---------------------------------------------------------------------------

# 上面 SCRIPT 的收尾会把演示文件删掉（那是为了验证回收站）。截图和前端自检
# 需要的是「盘上还留着」的文件，所以另起一套只写不删的剧本。
KEEP_SCRIPT = [
    {"content": "补一份说明，再给入口加一段注释。",
     "calls": [
         ("write_file", {"path": "NOTES.md",
                         "content": "# 随手记\n\n这个文件是这一轮新建的，用来验证产物面板。\n"}),
         ("edit_file", {"path": "README.md", "old_string": "## 怎么跑",
                        "new_string": "## 怎么跑（先装依赖）\n\n```\npip install -r requirements.txt\n```\n\n## 怎么跑\n"}),
     ]},
    {"content": "写完了。这一轮留下了一份新文档，并给 README 补了安装步骤。", "calls": []},
]


def run_keep_turn(sess, prompt: str = "把说明补全一点") -> bool:
    """在已有会话上再跑一轮，留下两个不会被删掉的产物。返回是否真的跑起来了。

    两个坑，都是踩过的：

    - 假模型按「messages 里有几条 assistant」决定演哪一步，而第二轮是接着第一轮
      的消息往下数的。不告诉它跳过前面那些，第二轮一上来就落在剧本最后一格
      （纯说话），结果一个文件都不写。
    - 上一轮可能还没跑完（剧本长、断点多）。`run_turn` 撞上「上一轮还没结束」
      会直接抛，而它跑在线程里 —— 异常被吞掉，表现就是「什么都没发生」。
      所以先等它真的停下来；顺带把权限临时放宽到 full，别让写入卡在审批上。
    """
    import threading
    import time

    if sess is None:
        return False
    if not sess.wait_idle(20):
        print("[fake] 上一轮还没结束，跳过追加的一轮")
        return False

    keep_script = SCRIPT[:]
    keep_offset = FakeClient.offset
    keep_permission = getattr(sess, "permission", None)
    keep_mode = getattr(sess, "mode", None)
    SCRIPT[:] = KEEP_SCRIPT
    FakeClient.offset = sum(
        1 for item in getattr(sess, "messages", []) if item.get("role") == "assistant"
    )
    # 教学模式要求「先开阶段再动手」，而 KEEP_SCRIPT 故意不碰阶段（一开阶段，
    # 会话就会一直催「接着往下带」，截图里会多出一串重复的收尾话）。
    # 所以这一轮临时换成施工档 + 全权限：它要验的是「文件有没有留下」，不是审批。
    if keep_permission is not None:
        sess.permission = "full"
    if keep_mode is not None:
        sess.mode = "craft"
    errors: list[str] = []

    def drive() -> None:
        try:
            sess.run_turn(prompt)
        except BaseException as exc:  # noqa: BLE001 - 脚本里要看得见，别被线程吞掉
            import traceback
            errors.append("".join(traceback.format_exception(exc)))

    try:
        thread = threading.Thread(target=drive, daemon=True)
        thread.start()
        deadline = time.time() + 60
        while time.time() < deadline:
            if not thread.is_alive():
                break
            time.sleep(0.15)
        thread.join(timeout=5)
        if errors:
            print("[fake] 追加的一轮出错了：\n" + errors[0][-1200:])
        return not thread.is_alive() and not errors
    finally:
        SCRIPT[:] = keep_script
        FakeClient.offset = keep_offset
        if keep_permission is not None:
            sess.permission = keep_permission
        if keep_mode is not None:
            sess.mode = keep_mode
