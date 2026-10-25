"""role2 链路冒烟：真的拉一次 DeepSeek Harness，跑一轮，检查事件翻译对不对。

这个脚本**会花真 token**（一次几 k），所以不进日常回归。什么时候跑：
改过 `app/dsh_engine.py`、DSH 升级过、或者界面上 role2 看起来不对的时候。

    python scripts/role2_check.py            # 跑一轮最小任务
    python scripts/role2_check.py --offline  # 只查「找不找得到 DSH / key 有没有」，不花 token

它验证四件事：
1. 找得到 DSH（exe + cli.js），且有可用的 key；
2. 真起进程、真收到 sessionId（多轮续命全靠它）；
3. 事件被翻成 DreamRole 的六种：action → result → answer；
4. 工具卡上的路径/参数是真的（不是空壳）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 沙盒里的 settings 是空的，但 key 是这台机器上已经有的东西 —— 借一下。
# （正式运行时 key 从 config 里读；这里只是让脚本不用再问一次。）
_REAL_SETTINGS = Path(__file__).resolve().parent.parent / ".data" / "settings.json"
if _REAL_SETTINGS.exists() and not os.environ.get("DEEPSEEK_API_KEY"):
    try:
        _data = json.loads(_REAL_SETTINGS.read_text(encoding="utf-8"))
        _key = ((_data.get("providers") or {}).get("deepseek") or {}).get("api_key") or ""
        if _key:
            os.environ["DEEPSEEK_API_KEY"] = _key
    except (OSError, ValueError):
        pass

SANDBOX = Path(tempfile.mkdtemp(prefix="dreamrole-role2-"))
os.environ["DREAMROLE_DATA_DIR"] = str(SANDBOX / "data")

from app import config, dsh_engine, engines, session as sessions   # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {detail}")


def main() -> int:
    offline = "--offline" in sys.argv
    config.load()

    print("· 定位 DSH")
    found = dsh_engine.locate()
    check("找得到 DeepSeek Harness", found["ok"], found.get("note"))
    check("cli.js 也在", bool(found.get("cli")), found.get("cli"))
    check("有可用的 DeepSeek key", bool(dsh_engine._api_key()))
    print("  exe:", found.get("exe"))
    print("  home:", found.get("home"))

    rows = engines.list_roles()
    check("两套底层都登记了", {r["id"] for r in rows} == {"role1", "role2"},
          [r["id"] for r in rows])
    check("role2 自报可用", next(r for r in rows if r["id"] == "role2")["ready"])
    check("认不出的引擎名退回 role1", engines.normalize("role9") == "role1")

    if offline:
        print(f"\n通过 {PASS} · 失败 {FAIL}（offline：没跑真模型）")
        return 1 if FAIL else 0

    print("\n· 真跑一轮（会花 token）")
    work = SANDBOX / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "note.txt").write_text("hello from role2\n", encoding="utf-8")

    sess = sessions.create(project=str(work), engine="role2")
    check("会话绑在 role2 上", sess.engine == "role2", sess.engine)

    sess.run_turn("读一下 note.txt，把内容原样告诉我，不要做别的")

    kinds = [e.type.value for e in sess.events]
    check("记下了 DSH 的会话 id（多轮续命要用）", bool(sess.dsh_session), sess.dsh_session)
    check("有 user 事件", kinds.count("user") == 1, kinds)
    check("有工具调用（action）", "action" in kinds, kinds)
    check("有工具返回（result）", "result" in kinds, kinds)
    check("有最终答复（answer）", "answer" in kinds, kinds)

    actions = [e for e in sess.events if e.type.value == "action"]
    if actions:
        first = actions[0]
        check("工具名是真的（read）", (first.tool or "") == "read", first.tool)
        check("参数摘要里有那个文件名", "note.txt" in (first.content or ""), first.content)

    results = [e for e in sess.events if e.type.value == "result"]
    check("result 挂在对应那条 action 后面",
          bool(results) and any(r.parent_id for r in results),
          [r.parent_id for r in results])

    answers = [e for e in sess.events if e.type.value == "answer"]
    joined = "\n".join(a.content for a in answers)
    check("答复里出现了文件内容", "hello from role2" in joined, joined[:160])
    check("用量记上了（不然控制台会一直显示 0）", sess.usage["total"] > 0, sess.usage)
    check("没有 error 性质的 notice",
          not any(e.type.value == "notice" and e.payload.get("error") for e in sess.events),
          [e.content for e in sess.events if e.type.value == "notice"][:2])

    print("\n事件流：")
    for e in sess.events:
        print(f"  {e.type.value:9s} {str(e.tool or ''):6s} {str(e.content)[:70]!r}")

    print(f"\n通过 {PASS} · 失败 {FAIL}")
    print(f"沙盒：{SANDBOX}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
