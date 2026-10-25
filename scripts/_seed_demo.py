"""截图与自检共用的演示数据。

为什么单独一个文件：`shoot.py` 要截图、`ui_check.py` 要断言，两边都需要
「三个智能体 + 一个群 + 一段真对话」。各写一份的下场是慢慢长歪 ——
截图里有个群、自检里没有，或者反过来，而两边都「看着对」。

**只造真的能跑出来的东西**：群里的对话是拿假模型真跑一轮得来的，
不是往轨迹里塞几条假事件。塞假事件最省事，但那样验的是「渲染函数能画字符串」，
不是「这条链路通」。
"""
from __future__ import annotations

import threading
import time


def seed_assistants(project: str = "", with_chat: bool = True) -> dict:
    """造三个智能体 + 一个群，并在群里真跑一轮。

    返回那个群的记录（`id` / `name`），给需要深链到某个频道的脚本用。
    """
    from app import assistants as A
    from app import session as sessions

    made = [
        A.create("私人秘书", persona=A.STARTERS[0]["persona"],
                 emoji=A.STARTERS[0]["emoji"], tagline=A.STARTERS[0]["tagline"]),
        A.create("研究搭档", persona=A.STARTERS[1]["persona"],
                 emoji=A.STARTERS[1]["emoji"], tagline=A.STARTERS[1]["tagline"]),
        A.create("代码搭子", persona=A.STARTERS[2]["persona"],
                 emoji=A.STARTERS[2]["emoji"], tagline=A.STARTERS[2]["tagline"]),
    ]
    # 让它看起来像「用过一阵子」：两个助理各自攒了几个文件
    for index, item in enumerate(made[:2]):
        layout = A.ensure_dirs(item["id"])
        (layout["workspace"] / "会议纪要.md").write_text("# 周一\n", encoding="utf-8")
        (layout["exports"] / "周报.md").write_text("# 本周\n", encoding="utf-8")
        A.touch(item["id"], index + 3)

    members = [one["id"] for one in made]
    group = A.create_group("周会", members, rule="host", host=members[0],
                           topic="每周一对齐这周要推进的三件事")
    if not with_chat or not project:
        return group

    # 工作区目录得先真的存在。会话那条「没工作区就不许跑」的不变量会把
    # 这一轮直接挡掉 —— 而它是在子线程里挡的，外面只看到「群里没有消息」。
    from pathlib import Path

    Path(project).mkdir(parents=True, exist_ok=True)

    # 真跑一轮：团队页中列画的就是「这个群最近一条会话的事件」，
    # 没有真会话，截出来永远是一屏「这个群还没说过话」。
    sess = sessions.create(project, mode="craft", permission="full",
                           group=group["id"], members=members)
    thread = threading.Thread(target=lambda: sess.run_turn("这周要推进哪三件事？"), daemon=True)
    thread.start()
    thread.join(timeout=120)
    while sess.running:
        threading.Event().wait(0.1)

    from app import store

    # 转折来了却一条事件都没有，多半是这一轮被权限/工作区挡在门口 ——
    # 而它是在子线程里挡的，外面只看到「群里没有消息」。这里明说一句，
    # 免得下次又对着一张空白的截图猜半天。
    events = store.load_trace(sess.cid) if hasattr(store, "load_trace") else []
    if not events:
        print("[seed] 注意：群聊那一轮没有留下事件，团队页会是空的")

    # 把时间往回拨。`save_trace` 写的是「现在」，于是这条群聊会比演示会话还新，
    # 页面默认打开它 —— 「这个对话的产物」于是验的变成一条群聊（当然没有产物）。
    # 这是踩过的坑：加种子数据把别人的断言弄红。
    from app import store

    when = time.time() - 3600 * 5
    store.patch_meta(sess.cid, created=when, updated=when)
    return group
