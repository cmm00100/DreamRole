"""一个最小的 MCP 服务（stdio），只用来验证客户端能不能正确说话。

它做的事：从标准输入按行读 JSON-RPC，往标准输出按行写回应。
工具只有一个 `echo`。真接东西的时候当然不用它。

    python scripts/fake_mcp_stdio.py
"""
from __future__ import annotations

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "把话原样说回来",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def reply(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = message.get("id")
        method = message.get("method")
        if msg_id is None:
            continue                       # 通知不用回

        if method == "initialize":
            reply({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-stdio", "version": "0.1"},
            }})
        elif method == "tools/list":
            reply({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            args = (message.get("params") or {}).get("arguments") or {}
            reply({"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": "回声：" + str(args.get("text"))}],
            }})
        else:
            reply({"jsonrpc": "2.0", "id": msg_id,
                   "error": {"code": -32601, "message": "没有这个方法"}})


if __name__ == "__main__":
    main()
