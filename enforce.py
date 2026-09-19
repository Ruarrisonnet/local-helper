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
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import outline  # noqa: E402

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


OUTLINE_ITEMS = 50
OUTLINE_MAX_CHARS = 5000


def advice(p, lines, size):
    msg = (f"local-helper enforcement: {p} is {lines:,} lines / {size // 1024}KB, too large to read whole. "
           f"Use the outline below to pick a range, then Read with offset and limit <= {WINDOW} (also enough to "
           "Edit the file). For a question that needs the whole file, use mcp__local-helper__local_summarize "
           "(path, question) or local_extract (path, what). For exact matches, use Grep.")
    try:
        o = outline.outline_file(p, OUTLINE_ITEMS)
        if len(o) > OUTLINE_MAX_CHARS:
            o = o[:OUTLINE_MAX_CHARS] + "\n... (outline truncated; mcp__local-helper__local_outline gives the full one)"
        msg += "\n\n" + o
    except Exception:
        pass  # the outline is a bonus; the deny must still happen
    return msg


# ---------------------------------------------------------------- paging budget (v1.2)
# Windows of <= WINDOW lines are allowed, but paging a whole big file through them costs the same
# tokens as reading it whole. Track the distinct lines read per file per session; re-reading a range
# is free (so re-checking code after an Edit costs nothing), new lines count against the budget.
STATE_DIR = os.path.join(HERE, "state")
STATE_MAX_AGE = 2 * 86400


def budget_for(lines):
    return max(600, lines // 4)


def _merge(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _covered(ranges):
    return sum(b - a + 1 for a, b in ranges)


def _state_path(session):
    return os.path.join(STATE_DIR, "reads-" + re.sub(r"[^A-Za-z0-9_-]", "", session)[:80] + ".json")


def _load_state(session):
    try:
        with open(_state_path(session), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(session, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_path(session) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, _state_path(session))
    now = time.time()
    for name in os.listdir(STATE_DIR):
        fp = os.path.join(STATE_DIR, name)
        try:
            if now - os.path.getmtime(fp) > STATE_MAX_AGE:
                os.remove(fp)
        except OSError:
            pass


def check_read(ti, session):
    m = measure(ti.get("file_path", ""))
    if not m:
        allow()
    p, lines, size = m
    limit = ti.get("limit")
    if limit is None or int(limit) > WINDOW:
        deny(advice(*m))
    if not session:
        allow()
    start = max(1, int(ti.get("offset") or 1))
    end = min(lines, start + int(limit) - 1)
    state = _load_state(session)
    key = os.path.normcase(p)
    before = state.get(key, [])
    after = _merge(before + [[start, end]])
    used, budget = _covered(after), budget_for(lines)
    if used > budget and _covered(before) < used:
        deny(f"local-helper enforcement: you have already read {_covered(before):,} distinct lines of {p} "
             f"({lines:,} lines) in windows this session; lines {start}-{end} would take it to {used:,}, over "
             f"the paging budget of {budget:,}. Paging through a file costs as many tokens as reading it whole. "
             "For a question about the whole file use mcp__local-helper__local_summarize (path, question) or "
             "local_extract (path, what); for exact matches use Grep. Re-reading ranges you have already "
             "read is always allowed. If you really need more of this file, tell the user; they can "
             "turn enforcement off by creating ~/local-helper/ENFORCE_OFF.")
    state[key] = after
    _save_state(session, state)
    allow()


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
        check_read(ti, data.get("session_id") or "")
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
