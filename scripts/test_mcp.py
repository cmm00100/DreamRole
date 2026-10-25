"""MCP 传输自检：起一个假 MCP 服务，把两种传输都跑一遍。

做法：本机起一个极小的 HTTP 服务，假装自己是 MCP 服务器，
并且**故意把一半响应回成 SSE**（规范允许，很多服务端也这么干）——
这正是自己写客户端最容易翻车的地方：SSE 流不会自己关，等 `read()` 会挂到超时。

    python scripts/test_mcp.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _console import harden  # noqa: E402

harden()

SANDBOX = Path(tempfile.mkdtemp(prefix="dreamrole-mcp-"))
os.environ["DREAMROLE_DATA_DIR"] = str(SANDBOX / "data")

from app import config, mcp  # noqa: E402
from app.paths import ensure_dirs  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  [ok]   " if ok else "  [FAIL] ") + name + ("" if ok else "  " + str(detail)))


# ---------------------------------------------------------------------------
# 假 MCP 服务
# ---------------------------------------------------------------------------

STATE = {"session": "sess-abc", "calls": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D401 - 不要日志
        pass

    def _sse(self, payload: dict) -> None:
        body = ("event: message\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Mcp-Session-Id", STATE["session"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", STATE["session"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)

        method = message.get("method")
        msg_id = message.get("id")

        # 通知（没有 id）：回 202，没有正文
        if msg_id is None:
            self.send_response(202)
            self.send_header("Mcp-Session-Id", STATE["session"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "initialize":
            return self._json({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            }})

        if method == "tools/list":
            # 故意用 SSE 回
            return self._sse({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "echo", "description": "把话原样说回来",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}},
            ]}})

        if method == "tools/call":
            STATE["calls"] += 1
            args = (message.get("params") or {}).get("arguments") or {}
            # 也用 SSE 回，验证流里有正确 id 就收手
            return self._sse({"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": "回声：" + str(args.get("text"))}],
            }})

        self._json({"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": "没有这个方法"}})


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/mcp"


# ---------------------------------------------------------------------------

def main() -> int:
    ensure_dirs()
    config.load()
    config.update({"mcp_servers": {}})

    server, url = serve()
    print(f"假 MCP 服务：{url}\n")

    print("[1] HTTP 传输（响应是 JSON + SSE 混合）")
    entry = mcp.add_server("fake", url=url)
    check("配置里记成 http", entry.get("transport") == "http", entry)

    result = mcp.connect("fake")
    check("连上了", result.get("ok") is True, result)
    check("握手拿到服务端名字", any(item["server"] == "fake-mcp" for item in mcp.servers()),
          mcp.servers())
    check("工具清单拿回来了", result.get("tools") == 1, result)

    schemas = mcp.schemas()
    check("工具转成了 schema", any(s["function"]["name"] == "mcp__fake__echo" for s in schemas),
          [s["function"]["name"] for s in schemas])
    tool_schema = next((s for s in schemas if s["function"]["name"] == "mcp__fake__echo"), None)
    check("schema 带着正确的参数", bool(tool_schema)
          and "text" in tool_schema["function"]["parameters"].get("properties", {}),
          tool_schema)

    out = mcp.call("mcp__fake__echo", {"text": "你好"})
    check("SSE 响应能解析", out == "回声：你好", out)
    check("服务端真的被调到了", STATE["calls"] == 1, STATE["calls"])

    block = mcp.prompt_block()
    check("系统提示里有这个服务", "fake" in block and "echo" in block, block)

    mcp.close("fake")
    check("断开后状态是未连接", any(item["name"] == "fake" and not item["connected"]
                              for item in mcp.servers()))

    print("\n[2] 连不上的时候要说人话")
    mcp.add_server("dead", url="http://127.0.0.1:1/mcp")
    result = mcp.connect("dead")
    check("返回失败而不是抛异常", result.get("ok") is False, result)
    check("错误信息里有名字", "dead" in str(result.get("error")), result.get("error"))
    check("工具清单是空的", mcp.schemas() == [] or all(
        "dead" not in s["function"]["name"] for s in mcp.schemas()))

    print("\n[3] 名字里的怪字符被洗掉")
    mcp.add_server("my server/测试", url=url)
    names = [item["name"] for item in mcp.servers()]
    check("服务名只剩安全字符", all(" " not in name and "/" not in name for name in names), names)

    print("\n[4] stdio 传输（本地子进程）")
    mcp.add_server("local", command=sys.executable,
                   args=[str(ROOT / "scripts" / "fake_mcp_stdio.py")])
    result = mcp.connect("local")
    check("本地进程连上了", result.get("ok") is True, result)
    check("走的是 stdio", result.get("transport") == "stdio", result)
    mcp.schemas()
    out = mcp.call("mcp__local__echo", {"text": "进程间"})
    check("stdio 工具能调", out == "回声：进程间", out)
    mcp.close("local")
    check("关掉之后进程不在了", all(
        not (item["name"] == "local" and item["connected"]) for item in mcp.servers()))

    print("\n[5] 配置里缺 url 也缺 command")
    conf = mcp.add_server("broken", command="")
    check("保存下来了（但不该连上）", conf.get("transport") == "stdio", conf)
    result = mcp.connect("broken")
    check("连接失败给的是文字", result.get("ok") is False, result)

    server.shutdown()
    print("\n" + "=" * 50)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for name in FAIL:
        print("  失败：" + name)
    if not FAIL:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
