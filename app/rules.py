"""安全规则：在权限判定**之前**先过一遍的硬规则。

和四档权限的分工：
- 权限档位回答「这一类动作现在要不要问你」（读 / 写 / 危险 × 四档矩阵）；
- 规则引擎回答「这个具体动作，在任何档位下都不许做」。

为什么两套而不是一套：权限是**用户可以放松的**（他能自己选「全权」），
而「往 C:\\Windows 里写文件」「把 ~/.ssh 读出来发走」这类事不该因为用户点了
「全权」就变成允许。档位是偏好，规则是底线。

规则也是数据，不是分支：内置的写在 `BUILTIN`，用户加的写在 settings.rules，
两者形状一样，一起喂给 `check()`。加一条规则不需要改任何 if。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from . import config

# action: deny = 直接拒绝；gate = 不管什么档位都要停下来问
BUILTIN: list[dict] = [
    {
        "id": "no-system-write",
        "kind": "path",
        "action": "deny",
        "tools": ["write_file", "edit_file", "delete_file", "move_file", "copy_file",
                  "mkdir", "apply_patch", "remove_junk"],
        "patterns": [
            "C:/Windows/**", "C:/Program Files/**", "C:/Program Files (x86)/**",
            "C:/ProgramData/**", "/etc/**", "/usr/**", "/bin/**", "/sbin/**",
            "/System/**", "/Library/**", "/boot/**",
        ],
        "why": "这是系统目录，写坏了整台机器都可能起不来。要装东西请用包管理器。",
    },
    {
        "id": "no-secret-dirs",
        "kind": "path",
        "action": "deny",
        "tools": ["read_file", "write_file", "edit_file", "delete_file", "copy_file",
                  "move_file", "list_dir", "grep"],
        "patterns": [
            "**/.ssh/**", "**/.gnupg/**", "**/.aws/credentials", "**/.kube/config",
            "**/AppData/Roaming/gcloud/**", "**/.config/gcloud/**",
        ],
        "why": "这里放的是私钥和云凭据。读出来之后很容易被顺手写进对话或日志里。",
    },
    {
        "id": "no-destructive-command",
        "kind": "command",
        "action": "deny",
        "patterns": [
            r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME|[A-Za-z]:\\)\s*$",
            r"\bformat\s+[a-zA-Z]:",
            r"\bdiskpart\b",
            r"\breg\s+delete\s+HKLM",
            r"\bshutdown\b", r"\breboot\b",
            r"\bmkfs\.",
            r":\(\)\s*\{\s*:\|\:&\s*\};:",          # fork bomb
            r"\bdd\s+if=.+\s+of=/dev/[sh]d",
        ],
        "why": "这条命令会把系统或数据直接抹掉，不是「改错一个文件」那种级别。",
    },
    {
        "id": "gate-shell-pipe",
        "kind": "command",
        "action": "gate",
        "patterns": [
            r"\b(curl|wget|iwr|Invoke-WebRequest)\b[^\n|]*\|\s*(sh|bash|zsh|powershell|pwsh|iex)\b",
            r"\bpip\s+install\s+(-U\s+)?--?g",
            r"\bnpm\s+install\s+-g\b",
        ],
        "why": "把网上抓下来的东西直接交给 shell 执行，等于把控制权交出去。",
    },
    {
        "id": "no-metadata-endpoint",
        "kind": "net",
        "action": "deny",
        "patterns": ["169.254.169.254", "metadata.google.internal", "100.100.100.200"],
        "why": "这是云主机的元数据接口，里面通常挂着临时凭据。",
    },
]

# 规则要看的参数名。一个工具可能用好几个名字指向同一件事（path / target / source）
PATH_KEYS = ("path", "target", "source", "dest", "from", "to", "dir", "parent", "file")


def user_rules() -> list[dict]:
    """用户自己加的规则。形状和内置的一样，坏掉的直接跳过而不是整个引擎挂掉。"""
    raw = config.get().get("rules") or []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        action = str(item.get("action") or "")
        patterns = item.get("patterns") or ([item["pattern"]] if item.get("pattern") else [])
        if kind not in ("path", "command", "net") or action not in ("deny", "gate"):
            continue
        patterns = [str(p) for p in patterns if str(p).strip()]
        if not patterns:
            continue
        out.append({
            "id": str(item.get("id") or "user-rule"),
            "kind": kind,
            "action": action,
            "tools": [str(t) for t in (item.get("tools") or [])] or None,
            "patterns": patterns,
            "why": str(item.get("why") or "你加的规则"),
            "user": True,
        })
    return out


def all_rules() -> list[dict]:
    return user_rules() + BUILTIN


def _norm(path: str) -> str:
    """统一成 posix 风格、展开 ~、去掉尾斜杠。规则里写的都是这种形态。"""
    text = str(path or "").strip().strip('"').strip("'")
    if not text:
        return ""
    try:
        text = str(Path(text).expanduser())
    except (OSError, ValueError):
        pass
    return text.replace("\\", "/").rstrip("/")


def _match_path(pattern: str, target: str) -> bool:
    """`**` 要能跨目录层级匹配，`fnmatch` 的 `*` 不跨分隔符，所以自己展开。"""
    pattern = _norm(pattern)
    target = _norm(target)
    if not pattern or not target:
        return False
    if pattern.startswith("**/"):
        tail = pattern[3:]
        parts = target.split("/")
        return any(fnmatch.fnmatchcase("/".join(parts[index:]), tail)
                   for index in range(len(parts)))
    if "/**/" in pattern:
        head, _, tail = pattern.partition("/**/")
        return target.startswith(head + "/") and fnmatch.fnmatchcase(
            target.rsplit("/", 1)[-1], tail)
    if pattern.endswith("/**"):
        return target == pattern[:-3] or target.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(target, pattern)


def _resolved_paths(args: dict, workspace: str) -> list[str]:
    out = []
    for key in PATH_KEYS:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        target = Path(text).expanduser()
        if not target.is_absolute() and workspace:
            target = Path(workspace) / text
        try:
            out.append(str(target.resolve()))
        except (OSError, ValueError):
            out.append(str(target))
    return out


def check(tool: str, args: dict, workspace: str = "") -> tuple[str, str, str]:
    """返回 (动作, 给用户看的原因, 规则 id)。动作为空串表示没有规则命中。"""
    args = args or {}
    paths = _resolved_paths(args, workspace)
    command = str(args.get("command") or "")
    url = str(args.get("url") or args.get("href") or "")

    for rule in all_rules():
        if rule.get("tools") and tool not in rule["tools"]:
            continue
        kind = rule["kind"]
        if kind == "path":
            for pattern in rule["patterns"]:
                for target in paths:
                    if _match_path(pattern, target):
                        return rule["action"], f"{rule['why']}（命中规则 {rule['id']}：{target}）", rule["id"]
        elif kind == "command":
            if tool not in ("run_command", "exec_cmd"):
                continue
            for pattern in rule["patterns"]:
                if re.search(pattern, command, re.IGNORECASE):
                    return rule["action"], f"{rule['why']}（命中规则 {rule['id']}）", rule["id"]
        elif kind == "net":
            if not url:
                continue
            for pattern in rule["patterns"]:
                if pattern.lower() in url.lower():
                    return rule["action"], f"{rule['why']}（命中规则 {rule['id']}）", rule["id"]
    return "", "", ""


def describe() -> dict:
    rules = all_rules()
    return {
        "rules": [
            {
                "id": rule["id"],
                "kind": rule["kind"],
                "action": rule["action"],
                "tools": rule.get("tools") or [],
                "patterns": rule["patterns"],
                "why": rule["why"],
                "user": bool(rule.get("user")),
            }
            for rule in rules
        ],
        "counts": {
            "total": len(rules),
            "deny": sum(1 for rule in rules if rule["action"] == "deny"),
            "gate": sum(1 for rule in rules if rule["action"] == "gate"),
        },
    }
