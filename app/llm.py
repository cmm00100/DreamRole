"""模型接入层：OpenAI 兼容协议 + 超时 + 重试 + 流式 + 用量统计。

这一层刻意把「不同厂商的差异」压到零：只要是 OpenAI 兼容端点，其余全部走同一条路。
唯一需要特殊处理的是**流式下的 tool_calls**：它会按 index 分片到达，必须自己拼。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from openai import OpenAI

from . import config, dlp

DEFAULT_TIMEOUT = 120.0
MAX_RETRIES = 3


class LLMError(RuntimeError):
    """能解释清楚的模型调用失败。"""


class Cancelled(RuntimeError):
    """用户主动中止。"""


def estimate_tokens(text: str) -> int:
    """粗估 token：中日韩字符按 1 个算，其它按 4 字符 1 个。

    不引 tiktoken —— 它带一个几 MB 的 BPE 表，而我们只需要「够用来决定要不要压缩」。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    return cjk + (len(text) - cjk) // 4 + 1


def estimate_messages(messages: Iterable[dict]) -> int:
    total = 0
    for msg in messages:
        total += 4
        total += estimate_tokens(str(msg.get("content") or ""))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            total += estimate_tokens(str(fn.get("arguments") or "")) + 8
    return total


class ChatClient:
    """一次构造，复用连接。"""

    def __init__(self, conn: dict[str, str] | None = None) -> None:
        conn = conn or config.provider_conn()
        if not conn.get("base_url"):
            raise LLMError("没有配置 base_url，去「设置」里选一家模型服务商")
        if not conn.get("api_key") and "127.0.0.1" not in conn["base_url"] and "localhost" not in conn["base_url"]:
            raise LLMError("还没有 API key，去「设置」里填一下")
        self.conn = conn
        self.model = conn["model"]
        self._client = OpenAI(
            api_key=conn.get("api_key") or "not-needed",
            base_url=conn["base_url"],
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )

    # -- 内部 ---------------------------------------------------------------

    def _completion(self, messages, tools, stream, temperature, max_tokens,
                    model=None, extra=None):
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if extra:
            # 以「思考等级」为代表的可选参数走这里。兼容端点对这些参数的态度很分裂：
            # 有的收下不理，有的直接 400。所以下面统一兜一层「失败了就退回不带」。
            kwargs.update(extra)
        if stream:
            kwargs["stream"] = True
            # 有些兼容端点不认这个参数，失败就退回去不带它
            try:
                return self._client.chat.completions.create(
                    **kwargs, stream_options={"include_usage": True}
                )
            except Exception:
                return self._client.chat.completions.create(**kwargs)
        return self._client.chat.completions.create(**kwargs)

    # -- 对外 ---------------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        model: str | None = None,
        extra: dict | None = None,
        on_reason: Callable[[str], None] | None = None,
    ) -> dict:
        """返回 {content, tool_calls, usage, reasoning, degraded}。

        统一成流式：即使不需要逐字回调，走流式也能让「第一批 token 到达时间」可观测，
        而且和带 tool_calls 的场景共用同一条解析路径，少一套分支。

        `degraded` 记的是「这一轮哪些可选参数被服务端拒了、我们退回了什么」。
        不抛异常：用户要的是回答，不是一个 400。
        """
        started = time.monotonic()
        degraded: list[str] = []
        # 出站前打码：凭据只在「本机」和「模型」之间流动，不该落在第三方的日志里。
        # 打码而不是拦截 —— 拦截会让用户莫名其妙发不出去，还不知道是哪句踩的。
        messages, redactions = dlp.scan(messages)
        try:
            stream = self._completion(messages, tools, True, temperature, max_tokens,
                                      model=model, extra=extra)
        except Exception as exc:  # noqa: BLE001 - SDK 异常类型很多，统一翻译
            if extra:
                # 十有八九是服务端不认那个可选参数（思考等级最常见）。
                # 退回不带它再试一次，代价只是这次没思考，比整轮失败划算。
                degraded.append(f"服务端不接受 {', '.join(extra)}：{exc}")
                try:
                    stream = self._completion(messages, tools, True, temperature,
                                              max_tokens, model=model)
                except Exception as again:  # noqa: BLE001
                    raise LLMError(f"请求模型失败：{again}") from again
            else:
                raise LLMError(f"请求模型失败：{exc}") from exc

        content: list[str] = []
        reasoning: list[str] = []
        slots: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] = {}

        try:
            for chunk in stream:
                if is_cancelled and is_cancelled():
                    try:
                        stream.close()
                    except Exception:
                        pass
                    raise Cancelled()

                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    usage = {
                        "prompt": getattr(u, "prompt_tokens", 0) or 0,
                        "completion": getattr(u, "completion_tokens", 0) or 0,
                        "total": getattr(u, "total_tokens", 0) or 0,
                    }

                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                piece = getattr(delta, "content", None)
                if piece:
                    content.append(piece)
                    if on_delta:
                        on_delta(piece)

                # DeepSeek-R1 / reasoner 类模型把思维链单独放在这个字段
                thought = getattr(delta, "reasoning_content", None)
                if not thought:
                    # 有的端点在 `reasoning` 或 `model_extra` 里给，形状不统一
                    alt = getattr(delta, "reasoning", None)
                    if isinstance(alt, str):
                        thought = alt
                    elif isinstance(alt, dict):
                        thought = alt.get("content") or ""
                if thought:
                    reasoning.append(thought)
                    if on_reason:
                        on_reason(thought)

                for tc in getattr(delta, "tool_calls", None) or []:
                    slot = slots.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"读取模型流失败：{exc}") from exc

        tool_calls = [
            {"id": slot["id"] or f"call_{idx}", "name": slot["name"], "arguments": slot["arguments"]}
            for idx, slot in sorted(slots.items())
            if slot["name"]
        ]

        if not usage:
            # 端点不回 usage 时自己估。total 必须一起算出来 —— 只填 prompt/completion
            # 会让「用量」页里 total 恒为 0，看着像这一轮没花 token。
            prompt_tokens = estimate_messages(messages)
            completion_tokens = estimate_tokens("".join(content))
            usage = {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": prompt_tokens + completion_tokens,
                "estimated": True,
            }
        usage["seconds"] = round(time.monotonic() - started, 2)

        return {
            "content": "".join(content),
            "tool_calls": tool_calls,
            "usage": usage,
            "reasoning": "".join(reasoning),
            "degraded": degraded,
            "redactions": redactions,
        }

    def complete(self, messages: list[dict], temperature: float = 0.2, max_tokens: int | None = None) -> str:
        """不带工具的纯文本补全，给提示词优化 / 摘要 / 子代理用。"""
        result = self.chat(
            messages, tools=None, temperature=temperature, max_tokens=max_tokens
        )
        return result["content"].strip()
