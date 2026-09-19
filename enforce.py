"""PreToolUse hook: stop Claude reading large files whole, so bulk reading goes through local-helper.

Denies:
  - Read of a large text file with no limit, or a limit above WINDOW lines
  - Bash/PowerShell commands that dump a large file whole (cat, type, Get-Content, gc, more, less, bat)
Always allows: windowed reads (offset/limit <= WINDOW), which is also enough to Edit a file
(measured 2026-09-19: Edit works after a partial Read), small files, binaries, exempt paths.
Allows everything if Ollama is down (Claude must never be blocked by a dead helper), or if
~/local-helper/ENFORCE_OFF exists (kill switch).
"""
import json
import os
import re
import shlex
import socket
import sys

MAX_LINES = 500
MAX_BYTES = 40_000
WINDOW = 300
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
EXEMPT_DIRS = [os.path.normcase(os.path.join(HOME, ".claude", d)) for d in
               ("plugins", "skills", "atelier", "commands", "agents")]
EXEMPT_NAMES = {"claude.md", "memory.md", "settings.json", "settings.local.json"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".ipynb", ".zip",
              ".exe", ".dll", ".so", ".bin", ".pt", ".gguf", ".safetensors", ".mp3", ".mp4", ".wav"}
DUMP_CMDS = {"cat", "type", "get-content", "gc", "more", "less", "bat"}
# Anything that narrows the output makes the command a windowed read, which is fine.
NARROWING = re.compile(r"\|\s*(head|tail|grep|rg|findstr|wc|select-object|select-string|sls|sort|uniq|awk|sed)\b"
                       r"|-TotalCount\b|-Tail\b|-Head\b|-First\b|-Last\b|\bsed\s+-n\b", re.I)


def allow():
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


def ollama_up():
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.3):
            return True
    except OSError:
        return False


def measure(path):
    """(lines, bytes) for a file worth policing, else None."""
    try:
        p = os.path.abspath(os.path.expanduser(path.strip().strip('"').strip("'")))
    except Exception:
        return None
    if not os.path.isfile(p):
        return None
    nc = os.path.normcase(p)
    if any(nc.startswith(d + os.sep) for d in EXEMPT_DIRS):
        return None
    if os.path.basename(nc) in EXEMPT_NAMES or os.path.splitext(nc)[1] in BINARY_EXT:
        return None
    size = os.path.getsize(p)
    if size < 2_000:
        return None
    with open(p, "rb") as f:
        lines = f.read().count(b"\n") + 1
    if lines <= MAX_LINES and size <= MAX_BYTES:
        return None
    return p, lines, size


def advice(p, lines, size):
    return (f"local-helper enforcement: {p} is {lines:,} lines / {size // 1024}KB, too large to read whole. "
            "Instead use one of: "
            f"(1) mcp__local-helper__local_summarize(path=..., question=...) or local_extract(path=..., what=...), "
            "then Read only the cited lines; "
            f"(2) Read with offset and limit <= {WINDOW} for the exact range you need (this is also enough to Edit the file); "
            "(3) Grep for a precise pattern. "
            "If the local-helper tools are not available in this session, use (2) or (3).")


def check_read(ti):
    m = measure(ti.get("file_path", ""))
    if not m:
        allow()
    limit = ti.get("limit")
    if limit is not None and int(limit) <= WINDOW:
        allow()
    deny(advice(*m))


def check_shell(cmd):
    if not re.search(r"\b(cat|type|get-content|gc|more|less|bat)\b", cmd, re.I):
        allow()
    if NARROWING.search(cmd):
        allow()
    # Check each simple command separately: `cd x && cat big.log` etc.
    for part in re.split(r"&&|\|\||;|\|", cmd):
        try:
            toks = shlex.split(part.replace("\\", "/"), posix=True)
        except ValueError:
            toks = part.split()
        for i, t in enumerate(toks):
            if t.lower() in DUMP_CMDS:
                for arg in toks[i + 1:]:
                    if arg.startswith("-"):
                        continue
                    m = measure(arg)
                    if m:
                        deny(advice(*m))
    allow()


def main():
    if os.path.exists(os.path.join(HERE, "ENFORCE_OFF")):
        allow()
    try:
        data = json.load(sys.stdin)
    except Exception:
        allow()
    tool, ti = data.get("tool_name", ""), data.get("tool_input") or {}
    if not ollama_up():
        allow()
    if tool == "Read":
        check_read(ti)
    elif tool in ("Bash", "PowerShell"):
        check_shell(ti.get("command", ""))
    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # a bug in the enforcer must never block Claude
