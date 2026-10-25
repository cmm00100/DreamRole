"""定时任务。

刻意做得比 WorkBuddy 简单得多，但保留了它真正解决问题的两个点：

1. **`next_run` 是算出来存下来的**，不是每次醒来现场推。这样重启之后任务不会
   把错过的所有班次一次性补跑（那是最容易把机器打爆的行为）。
2. **同一个任务不并发**。上一次还没跑完，这一次就记一条「跳过」。
   定时任务最怕的不是漏跑，是同时跑三份改同一个文件。

错过的时间点直接丢弃，只跑「从现在起最近的那一次」。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .paths import STATE, ensure_dirs

JOB_FILE = STATE / "schedules.json"
# 每次真的开了一轮就记一行。它是「这个任务到底有没有在干活」的唯一证据 ——
# 任务卡上的「已跑 12 次」是个计数，说不清哪一次、跑出了哪条会话、为什么跳过。
RUN_FILE = STATE / "schedule_runs.json"
MAX_RUNS = 300
TICK_SECONDS = 20

_lock = threading.RLock()
_jobs: list[dict] | None = None
_runs: list[dict] | None = None
_thread: threading.Thread | None = None
_stop = threading.Event()
_running: set[str] = set()
_on_fire: list = []


# ---------------------------------------------------------------------------
# 存取
# ---------------------------------------------------------------------------

def _load() -> list[dict]:
    global _jobs
    with _lock:
        if _jobs is None:
            _jobs = []
            if JOB_FILE.is_file():
                try:
                    data = json.loads(JOB_FILE.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        _jobs = [j for j in data if isinstance(j, dict) and j.get("id")]
                except (json.JSONDecodeError, OSError):
                    _jobs = []
        return _jobs


def _save() -> None:
    with _lock:
        ensure_dirs()
        tmp = JOB_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_load(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(JOB_FILE)


def jobs() -> list[dict]:
    with _lock:
        return [dict(j) for j in _load()]


# ---------------------------------------------------------------------------
# 运行记录
#
# 只在「真的开了一轮」时写一行 —— 跳过、失败也写，因为「昨晚为什么没跑」
# 比「跑了几次」有用得多。反过来，只是把计数 +1 而不留痕，用户就只能看着
# 一个数字猜。
# ---------------------------------------------------------------------------

def _load_runs() -> list[dict]:
    global _runs
    with _lock:
        if _runs is None:
            rows: list[dict] = []
            if RUN_FILE.is_file():
                try:
                    data = json.loads(RUN_FILE.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        rows = [r for r in data if isinstance(r, dict) and r.get("id")]
                except (json.JSONDecodeError, OSError):
                    rows = []
            _runs = rows
        return _runs


def _save_runs() -> None:
    with _lock:
        ensure_dirs()
        tmp = RUN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_load_runs(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(RUN_FILE)


def record_run(job: dict, why: str, state: str, cid: str = "", note: str = "") -> dict:
    """记一次。`state` 只取三种：started / skipped / failed —— 界面按它上色。"""
    row = {
        "id": uuid.uuid4().hex[:8],
        "job": str(job.get("id") or ""),
        "title": str(job.get("title") or "未命名")[:60],
        "project": str(job.get("project") or ""),
        "at": time.time(),
        "why": str(why or "定时")[:10],
        "state": state if state in ("started", "skipped", "failed") else "started",
        "cid": str(cid or ""),
        "note": str(note or "")[:200],
    }
    with _lock:
        items = _load_runs()
        items.append(row)
        if len(items) > MAX_RUNS:
            del items[:len(items) - MAX_RUNS]
        _save_runs()
    return dict(row)


def runs(limit: int = 80) -> list[dict]:
    """新的在前。"""
    with _lock:
        rows = sorted((dict(r) for r in _load_runs()),
                      key=lambda r: r.get("at") or 0, reverse=True)
    return rows[:max(1, min(MAX_RUNS, int(limit or 80)))]


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def _parse_time(text: str) -> tuple[int, int] | None:
    raw = (text or "").strip().replace("：", ":")
    if ":" not in raw:
        return None
    hour, _, minute = raw.partition(":")
    try:
        h, m = int(hour), int(minute)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def next_run(job: dict, now: float | None = None) -> float:
    """算下一次该跑的时间戳。算不出来就返回 0（等于禁用）。"""
    now = now or time.time()
    base = datetime.fromtimestamp(now)
    freq = job.get("freq")

    
    if freq == "manual":
        # 手动 = 不排班：存着这句话，什么时候点「试运行」什么时候跑。
        # 它还是走同一条 _fire 通道，所以产出照样落成一条会话 —— 不是另开一套。
        return 0

    if freq == "interval":
        minutes = max(5, int(job.get("minutes") or 60))
        return now + minutes * 60

    hm = _parse_time(str(job.get("time") or ""))
    if not hm:
        return 0
    hour, minute = hm
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if freq == "daily":
        if target <= base:
            target += timedelta(days=1)
        return target.timestamp()

    if freq == "weekly":
        weekday = int(job.get("weekday") or 1)  # 1=周一
        wanted = weekday - 1                     # datetime: 0=周一
        delta = (wanted - base.weekday()) % 7
        target = target + timedelta(days=delta)
        if target <= base:
            target += timedelta(days=7)
        return target.timestamp()

    return 0


def _describe(job: dict) -> str:
    freq = job.get("freq")
    if freq == "daily":
        return f"每天 {job.get('time')}"
    if freq == "weekly":
        names = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = int(job.get("weekday") or 1)
        label = names[weekday - 1] if 1 <= weekday <= 7 else "一"
        return f"每周{label} {job.get('time')}"
    if freq == "interval":
        return f"每 {int(job.get('minutes') or 60)} 分钟"
    if freq == "manual":
        return "手动触发"
    return "未设定"


# ---------------------------------------------------------------------------
# 增删改
# ---------------------------------------------------------------------------

def add(job: dict) -> dict:
    record = {
        "id": uuid.uuid4().hex[:8],
        "title": str(job.get("title") or "定时任务")[:60],
        "prompt": str(job.get("prompt") or "").strip(),
        "project": str(job.get("project") or ""),
        "freq": job.get("freq") if job.get("freq") in ("daily", "weekly", "interval", "manual") else "daily",
        "time": str(job.get("time") or "09:00"),
        "weekday": int(job.get("weekday") or 1),
        "minutes": max(5, int(job.get("minutes") or 60)),
        "enabled": bool(job.get("enabled", True)),
        # 无人值守跑的那一轮，权限档位、内核、点名要用的技能。
        # 没有它们的话，「到点自己跑」用的永远是默认档 —— 一个要改文件的任务
        # 会在半夜卡在审批上等到天亮，而用户以为自己设好了。
        "permission": str(job.get("permission") or "standard"),
        "engine": str(job.get("engine") or ""),
        "skills": [str(n) for n in (job.get("skills") or []) if str(n).strip()][:12],
        "created": time.time(),
        "last_run": 0,
        "next_run": 0,
        "runs": 0,
        "last_cid": "",
        "note": "",
    }
    record["next_run"] = next_run(record) if record["enabled"] else 0
    with _lock:
        _load().append(record)
        _save()
    return record


def update(job_id: str, patch: dict) -> dict | None:
    with _lock:
        for job in _load():
            if job["id"] != job_id:
                continue
            for key, value in patch.items():
                if key in ("title", "prompt", "project", "freq", "time", "weekday", "minutes",
                           "enabled", "permission", "engine", "skills"):
                    job[key] = value
            job["next_run"] = next_run(job) if job.get("enabled") else 0
            _save()
            return dict(job)
    return None


def remove(job_id: str) -> bool:
    with _lock:
        items = _load()
        before = len(items)
        items[:] = [j for j in items if j["id"] != job_id]
        if len(items) == before:
            return False
        _save()
        return True


def describe_all() -> list[dict]:
    out = []
    for job in jobs():
        item = dict(job)
        item["freq_label"] = _describe(job)
        item["next_label"] = (
            datetime.fromtimestamp(job["next_run"]).strftime("%m-%d %H:%M")
            if job.get("next_run")
            else "—"
        )
        item["running"] = job["id"] in _running
        out.append(item)
    return sorted(out, key=lambda j: (not j.get("enabled"), j.get("next_run") or 0))


# ---------------------------------------------------------------------------
# 给模型用的入口
# ---------------------------------------------------------------------------

def create_from_tool(args: dict, project: str) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "prompt 是空的，不知道到点要做什么。"
    freq = str(args.get("freq") or "daily")
    if freq in ("daily", "weekly") and not _parse_time(str(args.get("time") or "")):
        return "时间要写成 24 小时制的 HH:MM，比如 09:15 或 21:30。"
    if freq == "weekly":
        weekday = args.get("weekday")
        if not isinstance(weekday, int) or not (1 <= weekday <= 7):
            return "weekly 需要 weekday，1=周一 … 7=周日。"
    if freq == "interval" and int(args.get("minutes") or 0) < 5:
        return "间隔不能小于 5 分钟。"

    job = add(
        {
            "title": args.get("title") or prompt[:20],
            "prompt": prompt,
            "project": project,
            "freq": freq,
            "time": args.get("time") or "09:00",
            "weekday": args.get("weekday") or 1,
            "minutes": args.get("minutes") or 60,
        }
    )
    when = datetime.fromtimestamp(job["next_run"]).strftime("%Y-%m-%d %H:%M")
    return f"已创建定时任务「{job['title']}」（{_describe(job)}），下次运行：{when}。"


# ---------------------------------------------------------------------------
# 调度循环
# ---------------------------------------------------------------------------

def on_fire(callback) -> None:
    """注册「任务触发」回调，服务层用它推 SSE。"""
    if callback not in _on_fire:
        _on_fire.append(callback)


def fire(job_id: str, why: str = "手动") -> bool:
    """手动跑一次（「试运行」按钮）。

    走的是和到点自动跑完全一样的那条路：同一个 _fire、同一把「不并发」的锁、
    同样会开一条会话。所以试运行看到的，就是明天凌晨它自己跑出来的那个样子 ——
    试运行如果另开一条捷径，它试的就不是那件事了。
    """
    job = next((item for item in jobs() if item.get("id") == job_id), None)
    if job is None:
        return False
    # 手动任务 next_run 是 0（不排班），触发完不要把下一次排上
    if job.get("freq") == "manual":
        _fire(job_id, why)
        with _lock:
            item = next((one for one in _load() if one.get("id") == job_id), None)
            if item is not None and item.get("freq") == "manual":
                item["next_run"] = 0
                _save()
        return True
    _fire(job_id, why)
    return True


def _fire(job_id: str, why: str = "定时") -> None:
    """触发一个任务。

    参数是 **id** 而不是 job 字典：`jobs()` 返回的是副本（它每次都 `dict(j)` 复制），
    以前这里直接改那份副本，`_save()` 写的却是缓存里的原件 —— 于是
    `next_run` 永远停在过去，调度器每 20 秒把它再触发一次，一天能开出几千条会话。
    改 id 传进来、在锁里取到真身，就不会再出现「改的和存的不是同一份」。
    """
    from . import session as session_mod

    with _lock:
        job = next((item for item in _load() if item.get("id") == job_id), None)
    if job is None:
        return

    if job_id in _running:
        with _lock:
            job["note"] = "上一次还没跑完，这次跳过"
            job["last_run"] = time.time()
            job["next_run"] = next_run(job)
            _save()
        record_run(job, why, "skipped", note="上一次还没跑完")
        return

    _running.add(job_id)
    try:
        # 没有工作区就跳过，**不要**退回用户主目录 —— 那等于让它在家里乱写。
        project = str(job.get("project") or "").strip()
        if not project:
            with _lock:
                job["note"] = "没有工作区，这次跳过（去「自动化」里给它选一个目录）"
                job["last_run"] = time.time()
                job["next_run"] = next_run(job)
                _save()
            record_run(job, why, "skipped", note="没有工作区")
            return
        Path(project).mkdir(parents=True, exist_ok=True)
        sess = session_mod.create(
            project=project,
            permission=str(job.get("permission") or "") or None,
            engine=str(job.get("engine") or "") or None,
        )
        with _lock:
            job["last_run"] = time.time()
            job["next_run"] = next_run(job)
            job["runs"] = int(job.get("runs") or 0) + 1
            job["last_cid"] = sess.cid
            job["note"] = ""
            _save()
        record_run(job, why, "started", cid=sess.cid)
        info = {"job": job_id, "title": job.get("title"), "cid": sess.cid}
        for callback in list(_on_fire):
            try:
                callback(info)
            except Exception:  # noqa: BLE001
                continue
        sess.start(job.get("prompt") or "", list(job.get("skills") or []))
    except Exception as exc:  # noqa: BLE001 - 一个任务炸了不能停掉调度器
        with _lock:
            job["note"] = f"启动失败：{type(exc).__name__}: {exc}"
            _save()
        record_run(job, why, "failed", note=job.get("note") or "")
    finally:
        _running.discard(job_id)


def _tick() -> None:
    now = time.time()
    for job in jobs():
        if not job.get("enabled"):
            continue
        due = float(job.get("next_run") or 0)
        if due and due <= now:
            _fire(job["id"])


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    # 启动时把 next_run 重新算一遍：机器关了两天再开，不该补跑两天的任务
    with _lock:
        dirty = False
        for job in _load():
            if not job.get("enabled"):
                job["next_run"] = 0
                continue
            due = float(job.get("next_run") or 0)
            if not due or due <= time.time():
                job["next_run"] = next_run(job)
                dirty = True
        if dirty:
            _save()

    def loop() -> None:
        while not _stop.is_set():
            try:
                _tick()
            except Exception:  # noqa: BLE001
                pass
            _stop.wait(TICK_SECONDS)

    _thread = threading.Thread(target=loop, name="scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
