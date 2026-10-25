"""MCP 接入：自己实现 JSON-RPC。两种传输，一个接口。

为什么不用官方 SDK：它会把 `anyio` / `httpx-sse` / `pydantic` 一整串依赖拖进来，
打包成 exe 之后体积翻倍，而且任何一个版本冲突都会变成「双击没反应」。
而 MCP 的传输其实就三件事——把 JSON 发出去、把 JSON 收回来、用 id 对上号。
两百行能写完，还看得懂。

两种传输（都按 2024-11-05 的协议）：
- **stdio**：本地子进程，按行收发。适合文件系统、git 这类本机工具。
- **streamable http**：往一个 URL POST，响应可能是 `application/json`，
  也可能是 `text/event-stream`（SSE）。适合已经跑在服务器上的服务。

协议流程：
    → initialize
    ← result（含协议版本、服务端能力）
    → notifications/initialized        （通知，不回 id）
    → tools/list
    ← result（工具清单）
    → tools/call  {name, arguments}
    ← result（content 数组）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config
from .net import _ssl_context

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "DreamRole", "version": "1.0.0"}
DEFAULT_TIMEOUT = 30.0
CALL_TIMEOUT = 180.0
TOOL_PREFIX = "mcp__"

# Windows 上不给 CREATE_NO_WINDOW 的话，每连一个 MCP 就弹一个黑框
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _safe(text: str, limit: int = 48) -> str:
    """OpenAI 的工具名只认 [a-zA-Z0-9_-]，其它一律换成下划线。"""
    out = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(text))
    return out[:limit] or "x"


class MCPError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 一个 stdio 客户端
# ---------------------------------------------------------------------------

class StdioClient:
    transport = "stdio"

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str = "") -> None:
        self.name = name
        self.command = command
        self.args = [str(a) for a in (args or [])]
        self.env_extra = {str(k): str(v) for k, v in (env or {}).items()}
        self.cwd = cwd or None

        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.error = ""
        self.server_info: dict = {}
        self.protocol = ""
        self.connected = False
        self.started_at = 0.0

        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._next_id = 0
        self._closed = False

    # -- 生命周期 -----------------------------------------------------------

    def _resolve(self) -> list[str] | str:
        cmd = self.command
        if os.name == "nt" and not Path(cmd).is_file():
            found = shutil.which(cmd) or shutil.which(cmd + ".cmd") or shutil.which(cmd + ".exe")
            if found:
                cmd = found
        return [cmd, *self.args]

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        argv = self._resolve()
        env = os.environ.copy()
        env.update(self.env_extra)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.cwd,
                bufsize=-1,          # 行缓冲：readline() 才能正常工作
                creationflags=_CREATE_NO_WINDOW,
                shell=isinstance(argv, str),
            )
        except (OSError, ValueError) as exc:
            raise MCPError(f"启动 {self.name} 失败：{exc}") from exc

        self._closed = False
        self.started_at = time.time()
        threading.Thread(target=self._read_loop, name=f"mcp-{self.name}-out", daemon=True).start()
        threading.Thread(target=self._err_loop, name=f"mcp-{self.name}-err", daemon=True).start()
        self._handshake()

    def _handshake(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": CLIENT_INFO,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        self.server_info = result.get("serverInfo") or {}
        self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.notify("notifications/initialized")
        self.connected = True
        self.refresh_tools()

    def refresh_tools(self) -> list[dict]:
        result = self.request("tools/list", {}, timeout=DEFAULT_TIMEOUT)
        raw = result.get("tools")
        self.tools = [t for t in raw if isinstance(t, dict) and t.get("name")] if isinstance(raw, list) else []
        return self.tools

    def close(self) -> None:
        self._closed = True
        self.connected = False
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            for slot in self._pending.values():
                slot["error"] = "连接已关闭"
                slot["event"].set()
            self._pending.clear()

    # -- 收发 ---------------------------------------------------------------

    def _read_loop(self) -> None:
        stream = self.proc.stdout if self.proc else None
        if stream is None:
            return
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue  # 有的服务端会往 stdout 打日志，忽略即可
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            self.connected = False
            with self._lock:
                for slot in self._pending.values():
                    slot["error"] = f"{self.name} 的进程已经退出"
                    slot["event"].set()
                self._pending.clear()

    def _err_loop(self) -> None:
        """必须有人读 stderr —— 管道写满了子进程会卡死。"""
        stream = self.proc.stderr if self.proc else None
        if stream is None:
            return
        try:
            while True:
                chunk = stream.readline()
                if not chunk:
                    break
        except (OSError, ValueError):
            pass

    def _dispatch(self, message: dict) -> None:
        msg_id = message.get("id")
        if msg_id is None:
            return  # 服务端主动发的通知，这里不需要
        with self._lock:
            slot = self._pending.pop(msg_id, None)
        if not slot:
            return
        slot["result"] = message.get("result") or {}
        slot["error"] = message.get("error") or {}
        slot["event"].set()

    def _write(self, payload: dict) -> None:
        proc = self.proc
        if not proc or proc.poll() is not None or not proc.stdin:
            raise MCPError(f"{self.name} 没有在运行")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise MCPError(f"往 {self.name} 写数据失败：{exc}") from exc

    def request(self, method: str, params: dict | None = None,
                timeout: float = DEFAULT_TIMEOUT) -> dict:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            slot = {"event": threading.Event(), "result": {}, "error": {}}
            self._pending[req_id] = slot
        self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise MCPError(f"{self.name} 对 {method} 超过 {int(timeout)} 秒没回应")
        if slot["error"]:
            error = slot["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPError(f"{self.name} 报错：{message}")
        return slot["result"] or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- 工具 ---------------------------------------------------------------

    def call_tool(self, tool: str, arguments: dict) -> str:
        result = self.request(
            "tools/call", {"name": tool, "arguments": arguments or {}}, timeout=CALL_TIMEOUT
        )
        return _flatten(result)


# ---------------------------------------------------------------------------
# 一个 HTTP 客户端（Streamable HTTP）
# ---------------------------------------------------------------------------

class HttpClient(StdioClient):
    """同一个接口，传输换成 HTTP。

    复用父类的工具清单 / 状态字段，只把 `start` / `request` / `notify` / `close`
    换掉 —— 这样上层（schema 生成、prompt_block、call）一行都不用改。

    两个坑：
    1. 服务端可以把响应回成 SSE，而且**不主动关连接**。所以不能 `read()`，
       要一行一行读到我们要的那个 id 就立刻收手，否则会一直挂到超时。
    2. 会话 id 在 `Mcp-Session-Id` 响应头里，之后每次请求都要带上，否则服务端
       会当成一次全新的会话，工具清单每次都是空的。
    """

    transport = "http"

    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(name=name, command="", args=[])
        self.url = url
        self.headers_extra = {str(k): str(v) for k, v in (headers or {}).items()}
        self.session_id = ""

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        self.started_at = time.time()
        self._closed = False
        self._handshake()

    def close(self) -> None:
        if self.session_id:
            try:
                self._raw("DELETE", None, timeout=5.0)
            except Exception:  # noqa: BLE001 - 关不掉也无所谓，服务端会自己过期
                pass
        self.session_id = ""
        self.connected = False
        self._closed = True

    # -- 收发 ---------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        headers.update(self.headers_extra)
        return headers

    def _raw(self, method: str, payload: dict | None, timeout: float):
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=data, headers=self._headers(),
                                         method=method)
        return urllib.request.urlopen(request, timeout=timeout,
                                      context=_ssl_context())

    def _exchange(self, payload: dict, timeout: float) -> dict:
        try:
            response = self._raw("POST", payload, timeout)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise MCPError(f"{self.name} 返回 HTTP {exc.code}：{body}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"连不上 {self.name}：{exc}") from exc

        with response:
            sid = response.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            if response.status == 202:
                return {}                       # 通知的回应，没有正文
            kind = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in kind:
                return self._read_sse(response, payload.get("id"), timeout)
            body = response.read().decode("utf-8", "replace").strip()
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise MCPError(f"{self.name} 回的不是 JSON：{body[:200]}") from exc

    def _read_sse(self, response, want_id, timeout: float) -> dict:
        """读到 id 对上的那条就停。

        不能等连接关闭：规范允许服务端一直开着流往下推通知。
        """
        deadline = time.time() + timeout
        data_lines: list[str] = []
        while time.time() < deadline:
            try:
                line = response.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text.startswith("data:"):
                data_lines.append(text[5:].lstrip())
                continue
            if text:
                continue                       # event: / id: / 注释行
            if not data_lines:
                continue
            raw = "\n".join(data_lines)
            data_lines = []
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("id") == want_id:
                return message
        if data_lines:
            try:
                return json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                pass
        raise MCPError(f"{self.name} 的流里没有等到 id={want_id} 的回应")

    def request(self, method: str, params: dict | None = None,
                timeout: float = DEFAULT_TIMEOUT) -> dict:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
        message = self._exchange(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
            timeout,
        )
        if message.get("error"):
            error = message["error"]
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPError(f"{self.name} 报错：{detail}")
        return message.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._exchange({"jsonrpc": "2.0", "method": method, "params": params or {}}, 15.0)


def _flatten(result: dict) -> str:
    """把 MCP 的 content 数组压成一段文本 —— 模型只看得懂文本。"""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(f"[资源 {resource.get('uri') or ''}] {str(resource.get('text') or '')[:2000]}")
        elif kind == "image":
            parts.append(f"[图片 {item.get('mimeType') or ''}]（这里只能转成文字，图片没法给你看）")
        else:
            parts.append(json.dumps(item, ensure_ascii=False)[:2000])
    if result.get("isError"):
        return "（MCP 工具报错）" + ("\n".join(parts) or "没有错误信息")
    return "\n".join(parts).strip() or "（MCP 工具没有返回内容）"


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------

_clients: dict[str, StdioClient] = {}
_map: dict[str, tuple[str, str]] = {}     # 全名 -> (server, tool)
_lock = threading.RLock()


def _entries() -> dict[str, dict]:
    return dict(config.get().get("mcp_servers") or {})


def servers() -> list[dict]:
    out = []
    for name, conf in _entries().items():
        client = _clients.get(name)
        out.append(
            {
                "name": name,
                "transport": str(conf.get("transport") or ("http" if conf.get("url") else "stdio")),
                "command": conf.get("command") or "",
                "args": conf.get("args") or [],
                "url": conf.get("url") or "",
                "enabled": bool(conf.get("enabled", True)),
                "connected": bool(client and client.connected),
                "tools": len(client.tools) if client else 0,
                "error": (client.error if client else "") or "",
                "protocol": client.protocol if client else "",
                "server": (client.server_info or {}).get("name", "") if client else "",
            }
        )
    return out


def add_server(name: str, command: str = "", args: list[str] | None = None,
               env: dict[str, str] | None = None, enabled: bool = True,
               url: str = "", headers: dict[str, str] | None = None) -> dict:
    """加一个 MCP 服务。给了 url 就走 HTTP，否则走本地子进程。"""
    name = _safe(name or "server", 24)
    if url:
        entry = {"transport": "http", "url": url, "headers": headers or {}, "enabled": enabled}
    else:
        entry = {"transport": "stdio", "command": command, "args": args or [],
                 "env": env or {}, "enabled": enabled}
    config.merge_map("mcp_servers", {name: entry})
    return entry


def remove_server(name: str) -> None:
    entries = _entries()
    entries.pop(name, None)
    config.update({"mcp_servers": entries})
    close(name)


def connect(name: str) -> dict:
    conf = _entries().get(name)
    if not conf:
        return {"ok": False, "error": f"没有叫 {name} 的 MCP 服务"}
    close(name)

    if conf.get("url"):
        client: StdioClient = HttpClient(
            name=name,
            url=str(conf.get("url") or ""),
            headers=dict(conf.get("headers") or {}),
        )
    else:
        client = StdioClient(
            name=name,
            command=str(conf.get("command") or ""),
            args=list(conf.get("args") or []),
            env=dict(conf.get("env") or {}),
        )
    try:
        client.start()
    except (MCPError, Exception) as exc:  # noqa: BLE001
        client.error = str(exc)
        with _lock:
            _clients[name] = client
        return {"ok": False, "error": str(exc)}
    with _lock:
        _clients[name] = client
    return {"ok": True, "tools": len(client.tools), "transport": client.transport}


def close(name: str) -> None:
    with _lock:
        client = _clients.pop(name, None)
    if client:
        client.close()


def connect_all() -> None:
    for name, conf in _entries().items():
        if conf.get("enabled", True):
            try:
                connect(name)
            except Exception:  # noqa: BLE001 - 单个服务连不上不能拖垮启动
                continue


def close_all() -> None:
    for name in list(_clients):
        close(name)


def _rebuild_map() -> dict[str, tuple[str, str]]:
    mapping: dict[str, tuple[str, str]] = {}
    with _lock:
        for server, client in _clients.items():
            if not client.connected:
                continue
            for tool in client.tools:
                full = f"{TOOL_PREFIX}{_safe(server, 24)}__{_safe(tool['name'], 40)}"
                mapping[full] = (server, tool["name"])
    return mapping


def schemas() -> list[dict]:
    """已连接服务的工具，转成 OpenAI 的 function schema。"""
    global _map
    mapping = _rebuild_map()
    _map = mapping
    out: list[dict] = []
    with _lock:
        clients = dict(_clients)
    for full, (server, tool_name) in mapping.items():
        client = clients.get(server)
        if not client:
            continue
        tool = next((t for t in client.tools if t["name"] == tool_name), None)
        if not tool:
            continue
        params = tool.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(params, dict) or params.get("type") != "object":
            params = {"type": "object", "properties": {}}
        description = str(tool.get("description") or "")[:800]
        out.append(
            {
                "type": "function",
                "function": {
                    "name": full,
                    "description": f"[MCP:{server}] {description}".strip(),
                    "parameters": params,
                },
            }
        )
    return out


def handles(name: str) -> bool:
    return name.startswith(TOOL_PREFIX)


def call(full_name: str, arguments: dict) -> str:
    target = _map.get(full_name)
    if not target:
        _rebuild_map()
        target = _map.get(full_name)
    if not target:
        return f"没有这个 MCP 工具：{full_name}。可能服务没连上，去「设置 → MCP」看看。"
    server, tool_name = target
    with _lock:
        client = _clients.get(server)
    if not client:
        return f"MCP 服务 {server} 没在运行。"
    try:
        if not client.connected:
            client.start()
        return client.call_tool(tool_name, arguments)
    except MCPError as exc:
        return f"MCP 调用失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return f"MCP 调用出错：{type(exc).__name__}: {exc}"


def prompt_block() -> str:
    """给系统提示的那一段：只告诉模型「有哪些服务、大概能干什么」。"""
    lines: list[str] = []
    with _lock:
        clients = dict(_clients)
    for name, client in clients.items():
        if not client.connected or not client.tools:
            continue
        names = "、".join(t["name"] for t in client.tools[:8])
        more = f" 等 {len(client.tools)} 个" if len(client.tools) > 8 else ""
        lines.append(f"- {name}：{names}{more}")
    if not lines:
        return ""
    return "已接入的外部工具（MCP 服务，函数名以 mcp__ 开头，可以直接调用）：\n" + "\n".join(lines)


def reload_all() -> None:
    close_all()
    connect_all()
