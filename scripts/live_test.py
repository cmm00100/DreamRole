"""连真模型跑一遍：这条链路在假模型上通过，不代表在真模型上也没问题。

真模型会暴露假模型掩盖的东西：
- 它可能一次吐出 5 个工具调用；
- 它可能把 arguments 写成带换行的 JSON；
- 它可能先解释一大段再动手；
- 它可能压根不调工具，直接口头答应。
这些都得在真实 token 流下看一遍。

    python scripts/live_test.py            # 全跑（花几十个 token）
    python scripts/live_test.py --quick    # 只测连通
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _console import harden  # noqa: E402

harden()

SANDBOX = Path(tempfile.mkdtemp(prefix="dreamrole-live-"))
os.environ["DREAMROLE_DATA_DIR"] = str(SANDBOX / "data")

# 把真实的 settings.json 复制进沙盒：这样测试用的是你已经在界面里配好的那套
# provider / key，脚本里不出现任何密钥。
_real_settings = ROOT / ".data" / "settings.json"
if _real_settings.is_file():
    (SANDBOX / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy2(_real_settings, SANDBOX / "data" / "settings.json")

from app import config, graph, promptopt  # noqa: E402
from app import session as sessions  # noqa: E402
from app.llm import ChatClient, LLMError  # noqa: E402
from app.paths import ensure_dirs  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  [ok]   " if ok else "  [FAIL] ") + name + ("" if detail else ""), flush=True)
    if not ok and detail:
        print("          " + str(detail)[:400])


def seed_project() -> Path:
    project = SANDBOX / "demo"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text(
        "# 计算器\n\n一个用来练手的小项目。\n\n## 怎么跑\n\n```\npython src/calc.py\n```\n",
        encoding="utf-8",
    )
    (project / "src" / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n\n\n"
        "def div(a, b):\n"
        "    return a / b\n\n\n"
        "if __name__ == '__main__':\n"
        "    print(add(1, 2))\n",
        encoding="utf-8",
    )
    return project


def run_session(project: Path, mode: str, permission: str, text: str,
                auto: str = "allow", timeout: float = 180.0):
    """跑一轮，自动回答所有确认请求。返回 (session, 耗时)。"""
    sess = sessions.create(str(project), mode=mode, permission=permission)
    started = time.time()
    thread = threading.Thread(target=lambda: sess.run_turn(text), daemon=True)
    thread.start()

    answers = 0
    while time.time() - started < timeout:
        with sess._cond:
            pending = list(sess._pending.values())
        if pending:
            request = pending[0]
            decision = "continue" if request["kind"] == "stage" else auto
            sess.answer(request["id"], decision)
            answers += 1
            time.sleep(0.15)
            continue
        if not thread.is_alive():
            break
        time.sleep(0.2)
    thread.join(timeout=5)
    return sess, time.time() - started, answers


def main() -> int:
    quick = "--quick" in sys.argv
    ensure_dirs()
    config.load()

    print("=" * 62)
    print("真模型联调")
    print("=" * 62)
    conn = config.provider_conn()
    print(f"provider={conn['provider']}  model={conn['model']}")

    print("\n[1] 连通性")
    try:
        started = time.time()
        reply = ChatClient().complete(
            [{"role": "user", "content": "只回三个字：连接成功"}], temperature=0.0, max_tokens=32
        )
        elapsed = time.time() - started
        check("模型能回话", bool(reply), reply)
        check(f"首字延迟可接受（{elapsed:.1f}s）", elapsed < 20, f"{elapsed:.1f}s")
        print(f"          回复：{reply[:40]}")
    except LLMError as exc:
        check("模型能回话", False, exc)
        print("\n模型都不通，后面的不用测了。")
        return 1

    if quick:
        print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
        shutil.rmtree(SANDBOX, ignore_errors=True)
        return 0 if not FAIL else 1

    project = seed_project()

    print("\n[2] 真模型 · 读工具（执行模式，全权）")
    sess, seconds, answers = run_session(
        project, "craft", "full",
        "看一下 src/calc.py 里 div 函数，用一句话告诉我它有没有问题。只读，不要改任何文件。",
    )
    kinds = [e.type.value for e in sess.events]
    actions = [e for e in sess.events if e.type.value == "action"]
    answer = next((e.content for e in sess.events if e.type.value == "answer"), "")
    check("跑完了", not sess.running, "还在跑")
    check("模型真的调了工具", len(actions) > 0, f"{len(actions)} 个动作")
    check("有最终答复", bool(answer.strip()), answer[:80])
    check("答复里提到除零问题", "除" in answer or "0" in answer or "zero" in answer.lower(), answer[:120])
    check("没有越权改文件", not (project / "src" / "calc.py").read_text(encoding="utf-8").count("try"), "被改了")
    check(f"{seconds:.0f}s 内完成", seconds < 150, f"{seconds:.0f}s")
    print(f"          用了 {sess.usage['prompt']} 输入 / {sess.usage['completion']} 输出 token，"
          f"{sess.usage['calls']} 次模型调用")

    print("\n[3] 真模型 · 写工具 + 闸门（标准权限，自动点允许）")
    sess2, seconds2, answers2 = run_session(
        project, "craft", "standard",
        "给 src/calc.py 的 div 函数加一个判断：除数是 0 时抛出 ValueError('除数不能为 0')。"
        "改完读回来确认一下。",
        auto="always",
    )
    source = (project / "src" / "calc.py").read_text(encoding="utf-8")
    check("闸门被触发过", answers2 > 0, f"{answers2} 次")
    check("文件真的被改了", "ValueError" in source, source[:200])
    check("改动记录留了快照", bool(config.get()) and any(
        "calc.py" in path for path in __import__("app.store", fromlist=["changes"]).changes()
    ))
    check("改完没把函数名写坏", "def div" in source and "def add" in source, source[:160])

    print("\n[4] 真模型 · 规划模式不能写")
    before = (project / "src" / "calc.py").read_text(encoding="utf-8")
    sess3, _, _ = run_session(
        project, "plan", "full",
        "把 div 改成返回字符串。直接改，别解释。",
    )
    after = (project / "src" / "calc.py").read_text(encoding="utf-8")
    check("规划模式真的没动文件", before == after, "文件被改了")
    check("规划模式仍然给了方案", any(
        e.type.value == "answer" and len(e.content) > 20 for e in sess3.events
    ))

    print("\n[5] 真模型 · 教学模式断点")
    sess4, seconds4, answers4 = run_session(
        project, "teach", "standard",
        "带我读一遍这个项目，分两步：先看目录，再看 calc.py。",
        auto="continue",
    )
    stages = graph.build([e.to_dict() for e in sess4.events])["stages"]
    check("产生了多个阶段", len(stages) >= 2, f"{len(stages)} 个")
    check("阶段交接处停过", any(
        (e.payload or {}).get("breakpoint") for e in sess4.events
    ), "没停")

    print("\n[6] 真模型 · 提示词优化")
    result = promptopt.optimize("帮我改一下这个README", "structure")
    check("优化成功", bool(result.get("ok")), result.get("error"))
    optimized = result.get("optimized") or ""
    check("结果里没有模型的前言", "以下是" not in optimized[:20], optimized[:60])
    check("结果不是原样返回", optimized.strip() != "帮我改一下这个README", optimized[:60])
    check("长度合理", len(optimized) > 40, f"{len(optimized)} 字")
    print(f"          {len('帮我改一下这个README')} 字 → {len(optimized)} 字，"
          f"警告 {len(result.get('warnings') or [])} 条")
    if result.get("warnings"):
        for warning in result["warnings"]:
            print("          · " + warning)

    print("\n[7] 真模型 · 评审")
    judge = promptopt.compare("帮我改一下这个README", optimized)
    check("评审成功", bool(judge.get("ok")), judge.get("reason"))
    if judge.get("ok"):
        print(f"          判定：{judge['winner_label']}  "
              f"原话 {judge['scores']['original']} / 改写 {judge['scores']['optimized']}")
        for item in (judge.get("overfit") or [])[:2]:
            print("          · 它自己加的：" + item)

    print("\n[8] 真模型 · 多轮上下文")
    sess5 = sessions.create(str(project), mode="craft", permission="full")
    thread = threading.Thread(target=lambda: sess5.run_turn("记住这个数：四十二。只回「记住了」。"), daemon=True)
    thread.start()
    thread.join(timeout=120)
    while sess5.running:
        time.sleep(0.2)
    thread2 = threading.Thread(target=lambda: sess5.run_turn("刚才那个数是多少？只回数字。"), daemon=True)
    thread2.start()
    deadline = time.time() + 120
    while sess5.running and time.time() < deadline:
        with sess5._cond:
            pending = list(sess5._pending.values())
        if pending:
            sess5.answer(pending[0]["id"], "allow")
        time.sleep(0.2)
    thread2.join(timeout=5)
    last_answer = ""
    for event in reversed(sess5.events):
        if event.type.value == "answer":
            last_answer = event.content
            break
    check("第二轮记得第一轮的内容", "42" in last_answer or "四十二" in last_answer, last_answer[:80])

    print("\n" + "=" * 62)
    total = sess.usage["total"] + sess2.usage["total"] + sess3.usage["total"] + \
        sess4.usage["total"] + sess5.usage["total"]
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项   本轮共耗 {total} token")
    for name in FAIL:
        print("  失败：" + name)
    if not FAIL:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    else:
        print(f"\n现场保留在：{SANDBOX}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
