"""事件模型：整个 agent 的真相源。

原则：只追加，不修改。任何一个瞬间的图，都是从事件流投影出来的。

和「我的agent」的版本相比，这里把事件类型收敛成 6 种，并新增 `notice`
（系统提示类，例如上下文已压缩、工具被禁用），它的存在是为了让「运行中发生了什么」
也能进入事件流 —— 否则这些信息只能靠日志，回放时就丢了。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum


class EventType(str, Enum):
    USER = "user"        # 用户说的话
    REASONING = "reasoning"  # 模型的思维链（深度思考打开时才有）
    THINK = "think"      # agent 的推理 / 讲解
    ACTION = "action"    # 一次工具调用
    RESULT = "result"    # 工具的返回值
    ANSWER = "answer"    # 面向用户的最终答复
    NOTICE = "notice"    # 系统提示：压缩、降级、跳过、出错


@dataclass
class Event:
    type: EventType
    content: str
    parent_id: str | None = None      # 接在谁后面 —— 这一个字段决定了图的形状
    tool: str | None = None           # ACTION 事件才有
    step_id: int | None = None        # 属于第几轮对话
    stage: int | None = None          # 属于第几个阶段 —— 阶段就是图上的一列
    stage_title: str | None = None
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["type"] = self.type.value
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> "Event":
        data = dict(raw)
        data["type"] = EventType(data["type"])
        known = {f for f in cls.__dataclass_fields__}  # 丢弃未来字段，向前兼容
        return cls(**{k: v for k, v in data.items() if k in known})
