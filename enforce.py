"""PreToolUse hook: stop Claude reading large files whole, so bulk reading goes through local-helper.

Denies:
  - Read of a large text file with no limit, or a limit above WINDOW lines
  - Read windows that would take one agent past its paging budget for that file
  - Bash/PowerShell commands that dump a large file whole (cat, type, Get-Content, gc, more, less, bat)
Always allows: windowed reads within budget (also enough to Edit a file: Edit works after a partial
Read), re-reads of ranges already read, small files, binaries, exempt paths.
Allows everything if Ollama is down (Claude must never be blocked by a dead helper), or if a file
named ENFORCE_OFF exists next to this script (kill switch). Any internal error also allows.
"""
import json
import os
import re
import shlex
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backend  # noqa: E402
import outline  # noqa: E402

MAX_LINES = 500
MAX_BYTES = 40_000
WINDOW = 300
KILL_SWITCH = os.path.join(HERE, "ENFORCE_OFF")
CONFIG = os.path.join(HERE, "config.json")     # optional, user-local: {"exempt_dirs": ["~/somewhere"]}
HOME = os.path.expanduser("~")
CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"
# Instructions Claude must read whole (skills, plugins, agents, commands) are never policed, and
# neither is Claude Code's own session storage: "projects" holds the spill files it writes when a
# tool result is too big to inline (blocking those stops Claude reading its OWN grep output), its
# transcripts, and the memory directory.
DEFAULT_EXEMPT = [os.path.join(HOME, ".claude", d)
                  for d in ("plugins", "skills", "commands", "agents", "projects")]
EXEMPT_NAMES = {"claude.md", "claude.local.md", "memory.md", "settings.json", "settings.local.json"}
BINARY_EXT = set(outline.BINARY_EXT) | {".ipynb"}     # one list shared with local_map
OUTLINE_MAX_FILE = 20 * 1024 * 1024    # above this, no outline: the deny must arrive well inside the hook timeout
DUMP_CMDS = {"cat", "type", "get-content", "gc", "more", "less", "bat"}
# Anything that narrows the output makes the command a windowed read, which is fine.
# PowerShell's Where-Object/ForEach-Object/Measure-Object are the grep/awk/wc of that shell, and the
# Bash equivalents were already here. Without them the hook refused pipelines that plainly filter
# ("Get-Content app.log | Where-Object { $_ -match ' ERROR ' }"), which cost a benchmark run six
# extra turns working around it.
NARROWING = re.compile(r"\|\s*(head|tail|grep|rg|findstr|wc|select-object|select-string|sls|sort|uniq|awk|sed"
                       r"|where-object|where|foreach-object|measure-object|group-object|compare-object)\b"
                       r"|\|\s*[?%]\s*[{(]"      # the ? and % aliases: `| ? { ... }`, `| % { ... }`
                       r"|-TotalCount\b|-Tail\b|-Head\b|-First\b|-Last\b|\bsed\s+-n\b", re.I)
STATE_DIR = os.path.join(os.environ.get("LOCAL_HELPER_DATA") or HERE, "state")
STATE_MAX_AGE = 2 * 86400
OUTLINE_ITEMS = 50
OUTLINE_MAX_CHARS = 5000


def allow():
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


def _config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def ollama_up():
    """Is the model backend (Ollama or an OpenAI-compatible server) listening? If not, the tools can't
    help, so the hook allows everything. backend.base_url() reads the environment, then config.json
    (install.py records the address there: the hook never sees `claude mcp add -e` variables)."""
    if os.environ.get("LOCAL_HELPER_ASSUME_OLLAMA_UP") == "1":      # tests / CI without a backend
        return True
    try:
        with socket.create_connection(backend.host_port(), timeout=0.3):
            return True
    except OSError:
        return False


def _fold(p):
    p = os.path.normcase(os.path.abspath(p))
    return p.lower() if CASE_INSENSITIVE_FS else p


def exempt_dirs():
    dirs = list(DEFAULT_EXEMPT)
    extra = _config().get("exempt_dirs")
    if isinstance(extra, list):        # a bare string would be iterated per character: '~' exempted all of home
        dirs += [os.path.expanduser(d) for d in extra if isinstance(d, str) and d.strip()]
    return [_fold(d) for d in dirs]


def _native(path):
    """Git Bash / Cygwin / WSL spellings of Windows paths (/c/Users/x, /cygdrive/c/x, /mnt/c/x) -> C:/..."""
    if os.name == "nt":
        m = re.match(r"^/(?:cygdrive/|mnt/)?([A-Za-z])(/|$)(.*)", path)
        if m:
            return f"{m.group(1).upper()}:/{m.group(3)}"
    return path


def _utf16(head):
    """UTF-16 text (PowerShell 5.1's `>` writes it) is full of NULs but is text, and Claude Code's Read
    decodes it and returns it whole: BOM, or NULs sitting in every other byte."""
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    odd, even = head[1::2].count(0), head[0::2].count(0)
    half = max(1, len(head) // 2)
    return (odd > 0.4 * half and even < 0.05 * half) or (even > 0.4 * half and odd < 0.05 * half)


def measure(path):
    """(path, lines, bytes) for a file worth policing, else None."""
    try:
        p = os.path.abspath(os.path.expanduser(_native(path.strip().strip('"').strip("'"))))
    except Exception:
        return None
    if not os.path.isfile(p):
        return None
    folded = _fold(p)
    if any(folded == d or folded.startswith(d.rstrip(os.sep) + os.sep) for d in exempt_dirs()):
        return None
    base = os.path.basename(p).lower()                 # .JPG and CLAUDE.md are exempt on Linux too
    if base in EXEMPT_NAMES or os.path.splitext(base)[1] in BINARY_EXT:
        return None
    size = os.path.getsize(p)
    if size < 2_000:
        return None
    lines = 1
    with open(p, "rb") as f:
        head = f.read(8192)
        if b"\0" in head and not _utf16(head):         # binary whatever its extension
            return None
        lines += head.count(b"\n")
        for block in iter(lambda: f.read(1 << 20), b""):   # counted in 1MB blocks: flat memory on huge files
            lines += block.count(b"\n")
    if lines <= MAX_LINES and size <= MAX_BYTES:
        return None
    return p, lines, size


def advice(p, lines, size):
    msg = (f"local-helper enforcement: {p} is {lines:,} lines / {size // 1024}KB, too large to read whole. "
           f"Use the outline below to pick a range, then Read with offset and limit <= {WINDOW} (also enough to "
           "Edit the file). For a question that needs the whole file, use mcp__local-helper__local_summarize "
           "(path, question) or local_extract (path, what). For exact matches, use Grep.")
    if size > OUTLINE_MAX_FILE:
        return msg + "\n\n(no outline: the file is too big to outline inside the hook's time limit; use " \
                     "mcp__local-helper__local_outline, or Grep for what you need)"
    try:
        o = outline.outline_file(p, OUTLINE_ITEMS)
        if len(o) > OUTLINE_MAX_CHARS:
            o = o[:OUTLINE_MAX_CHARS] + "\n... (outline truncated; mcp__local-helper__local_outline gives the full one)"
        msg += "\n\n" + o
    except Exception:
        pass  # the outline is a bonus; the deny must still happen
    return msg


# ---------------------------------------------------------------- paging budget
# Windows of <= WINDOW lines are allowed, but paging a whole big file through them costs the same
# tokens as reading it whole. Track the distinct lines each AGENT has read of each file: subagents
# share the parent's session_id but have their own agent_id and their own context, so v1.2's
# per-session budget let one subagent use up another's. Re-reading a range is free.

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


def _state_path(owner):
    return os.path.join(STATE_DIR, "reads-" + re.sub(r"[^A-Za-z0-9_-]", "", owner)[:120] + ".json")


class _Lock:
    """Exclusive lock file: parallel Read calls (one per tool call in a batch) run this hook at the same
    time, and without it their read-modify-write of the state lost updates and corrupted the JSON."""

    def __init__(self, path):
        self.path = path + ".lock"
        self.fd = None

    def __enter__(self):
        deadline = time.time() + 3
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 10:     # stale: its hook died
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError("state lock busy")
                time.sleep(0.02)

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            os.remove(self.path)
        except OSError:
            pass


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, state):
    tmp = f"{path}.{os.getpid()}.tmp"                  # unique per process: no shared temp file
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)
    now = time.time()
    for name in os.listdir(STATE_DIR):
        fp = os.path.join(STATE_DIR, name)
        try:
            if now - os.path.getmtime(fp) > STATE_MAX_AGE:
                os.remove(fp)
        except OSError:
            pass


def check_read(ti, owner):
    m = measure(ti.get("file_path", ""))
    if not m:
        allow()
    p, lines, size = m
    limit = ti.get("limit")
    if limit is None or int(limit) > WINDOW:
        deny(advice(*m))
    start = max(1, int(ti.get("offset") or 1))
    if not owner or start > lines:                     # past EOF reads nothing: nothing to count
        allow()
    end = min(lines, start + int(limit) - 1)
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _state_path(owner)
    with _Lock(path):
        state = _load_state(path)
        key = _fold(p)
        before = [r for r in state.get(key, []) if isinstance(r, list) and len(r) == 2 and r[0] <= r[1]]
        after = _merge(before + [[start, end]])
        used, budget = _covered(after), budget_for(lines)
        if used > budget and _covered(before) < used:
            deny(f"local-helper enforcement: you have already read {_covered(before):,} distinct lines of {p} "
                 f"({lines:,} lines) in windows; lines {start}-{end} would take it to {used:,}, over the paging "
                 f"budget of {budget:,}. Paging through a file costs as many tokens as reading it whole. For a "
                 "question about the whole file use mcp__local-helper__local_summarize (path, question) or "
                 "local_extract (path, what); for exact matches use Grep. Re-reading ranges you have already "
                 "read is always allowed. If you really need more of this file, tell the user; they can turn "
                 f"enforcement off by creating the file {KILL_SWITCH}")
        state[key] = after
        _save_state(path, state)
    allow()


# Heredoc bodies are data, not commands. Claude Code writes commit and PR messages as
# git commit -m "$(cat <<'EOF' ... EOF)", and a message line like "Fix type errors in server.py" was
# being read as the command `type server.py` and denied.
_HEREDOC = re.compile(r"(<<-?\s*(['\"]?)(\w+)\2[^\n]*)\n.*?\n[ \t]*\3[ \t]*(?=\n|\)|$)", re.S)
# A line ending in \ or | (or the next starting with |) continues the same command.
_CONTINUATION = re.compile(r"[\\`]\r?\n|(?<=\|)[ \t]*\r?\n|\r?\n(?=[ \t]*\|(?!\|))")
_LEADING = re.compile(r"^(?:[A-Za-z_]\w*=\S*\s+|(?:sudo|command|exec|nohup|time|builtin)\s+)*")


def check_shell(cmd):
    if not re.search(r"\b(cat|type|get-content|gc|more|less|bat)\b", cmd, re.I):
        allow()
    cmd = _CONTINUATION.sub(" ", _HEREDOC.sub(r"\1", cmd))
    # Each command (split on ; && || newline) is judged on its own pipeline: a `| head` on one command
    # doesn't excuse a bare `cat big.log` elsewhere in the same line.
    for segment in re.split(r"&&|\|\||;|\n", cmd):
        if NARROWING.search(segment):
            continue
        for stage in segment.split("|"):              # every stage: `x | cat big.log` dumps the file too
            try:
                toks = shlex.split(_LEADING.sub("", stage.strip()).replace("\\", "/"), posix=True)
            except ValueError:
                toks = stage.split()
            # Only the PROGRAM position counts: "cat"/"type" as an argument or a word in a message
            # is not a dump command.
            if not toks or toks[0].lower() not in DUMP_CMDS:
                continue
            for arg in toks[1:]:
                if arg.startswith("-"):
                    continue
                m = measure(arg)
                if m:
                    deny(advice(*m))
    allow()


def main():
    if os.path.exists(KILL_SWITCH):
        allow()
    try:
        # bytes -> UTF-8 ourselves: sys.stdin is cp1252 on Windows, which mangled non-ASCII paths and
        # made the hook silently allow full reads for users like C:\Users\Müller (v1.2).
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except Exception:
        allow()
    if not isinstance(data, dict):
        allow()
    tool, ti = data.get("tool_name", ""), data.get("tool_input") or {}
    if not isinstance(ti, dict) or not ollama_up():
        allow()
    if tool == "Read":
        session = data.get("session_id") or ""
        check_read(ti, f"{session}-{data.get('agent_id') or 'main'}" if session else "")
    elif tool in ("Bash", "PowerShell"):
        check_shell(str(ti.get("command", "")))
    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # a bug in the enforcer must never block Claude
