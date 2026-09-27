"""Does local-helper actually save Claude tokens? Run real headless Claude Code sessions on the same
tasks with and without it, and compare what entered the context, what it cost, and whether the answer
was right.

    python bench.py --dry-run           show the tasks and the commands, spend nothing
    python bench.py --reps 3            every task, every arm, 3 times (spends your Claude usage)
    python bench.py --tasks prose,noisy --reps 5
    python bench.py --arms baseline,tools,helper    add the MCP-without-hook arm
    python bench.py --arms directed --tasks prose   helper + a CLAUDE.md telling Claude to use the tools
    python bench.py --resume ...        keep the real runs already in --out and do only the rest
    python bench.py --overwrite ...     start over; without this or --resume an existing --out is refused
    python bench.py --self-check        check the parser, graders and loop logic; spends nothing

It stops at your account's usage limit instead of recording failed sessions as results, and prints the
--resume command to continue once the limit resets. --resume refuses rows made by different code (each
row records hashes of bench.py, server.py, enforce.py and backend.py) unless --force-resume is given.

Each run happens in a fresh copy of a fixture project, isolated from your setup: --setting-sources
project (no user settings, hooks or plugins), --strict-mcp-config, the user's global CLAUDE.md
excluded, and an explicit environment allowlist. Both arms get the same tools (Read, Grep, Glob,
Bash) and the same shell permission rules; the helper arm additionally gets local-helper's MCP
server and its hook. Answers are graded by regex against truth computed when the fixture is built.

Metrics per run (from the stream, not guessed):
  fresh   tokens that entered the context for the first time (input + cache writes). The honest
          "how much text did this session ingest" number.
  peak    the context size of the last assistant message: how full the window got.
  cost    USD, as reported by the CLI.
  Earlier versions summed cache_read across turns; that counts the same cached prefix once per turn,
  so it tracked turn count rather than text. It is gone.

Results and every raw transcript go to bench_results.json / bench_runs/.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import urllib.request
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backend  # noqa: E402
import bench_fixture  # noqa: E402
import install  # noqa: E402  (for the exact hook command the installer writes)

MODEL = "claude-sonnet-5"                # pinned: "sonnet" is a floating alias
CLAUDE = (shutil.which("claude")         # the real path (claude.cmd on Windows): no shell needed
          or shutil.which("claude", path=os.path.join(os.path.expanduser("~"), ".local", "bin"))
          or "claude")
MAX_BUDGET = "2.00"                      # per run, a safety stop; loose enough not to cut a run short
RUN_TIMEOUT = 3600                       # seconds per session; a variable so self_check can shorten it
# What the child's server.py actually uses: child_env strips every LOCAL_HELPER_* but DATA, and
# preflight refuses to run while any is set, so the defaults are the effective values. Recorded per row.
BIG_MODEL = "qwen2.5-coder:7b-instruct-q3_K_M"
EMBED_MODEL = "nomic-embed-text"
CODE_FILES = ("bench.py", "server.py", "enforce.py", "backend.py")
BASE_TOOLS = ["Read", "Grep", "Glob", "Bash", "PowerShell"]   # PowerShell: Claude reaches for it on Windows
HELPER_TOOLS = ["mcp__local-helper__local_map", "mcp__local-helper__local_outline",
                "mcp__local-helper__local_run", "mcp__local-helper__local_summarize",
                "mcp__local-helper__local_extract", "mcp__local-helper__local_find",
                "mcp__local-helper__local_classify", "mcp__local-helper__local_draft",
                "mcp__local-helper__local_stats"]
# Identical in both arms: --allowedTools names the tool, not the command, and Bash still asks per
# command without these. An allowlist (not bypassPermissions) so the published setup is auditable.
SHELL_ALLOW = ["Bash(python:*)", "Bash(python3:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(wc:*)",
               "Bash(head:*)", "Bash(tail:*)", "Bash(sed:*)", "Bash(awk:*)", "Bash(sort:*)",
               "Bash(uniq:*)", "Bash(cat:*)", "Bash(ls:*)", "Bash(find:*)", "Bash(type:*)",
               "PowerShell(python:*)", "PowerShell(Select-String:*)", "PowerShell(Get-Content:*)",
               "PowerShell(Measure-Object:*)", "PowerShell(Get-ChildItem:*)", "PowerShell(Select-Object:*)"]
ENV_KEEP = ["PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
            "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA",
            "PROGRAMFILES(X86)", "USERNAME", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "TERM"]

# The "directed" arm: how people actually run local-helper - with a project rule telling Claude to use
# it (the author's own global rule said much the same). Unprompted, Claude called a local-helper tool
# 3 times in 84 sessions, so without this arm nothing measures the model tools at all.
DIRECTED_RULE = (
    "# Project instructions\n\n"
    "This project has the local-helper MCP tools. Before reading any file over ~500 lines, or any log or "
    "command output over ~20KB, in full: use mcp__local-helper__local_outline to navigate it, "
    "mcp__local-helper__local_summarize or mcp__local-helper__local_extract when the question needs the "
    "whole text read and understood, and mcp__local-helper__local_run for noisy commands.\n")

ONE_LINE = "Answer on one line, exactly in this form: "


def num(out, key):
    """The number the model reported for KEY=, or None."""
    m = re.search(r"%s\s*=\s*\**\s*`?(\d+)" % key, out, re.I)
    return int(m.group(1)) if m else None


TASKS = {
    # --- a probe, not a task: what does merely loading local-helper cost per session? ---
    "overhead": {
        "kind": "probe",
        "prompt": "Reply with exactly: OK",
        "grade": lambda out, fx: bool(re.search(r"\bOK\b", out)),
    },
    # --- control: a couple of greps answer these, so no saving is expected ---
    "symbol": {
        "kind": "greppable",
        "prompt": "In this repository, which function in server.py decides whether to use the big or the "
                  "small model, and what is the default minimum free RAM in GB it requires for the big "
                  "model? " + ONE_LINE + "FUNCTION=<name> GB=<number>.",
        "grade": lambda out, fx: bool(re.search(r"FUNCTION\s*=\s*\**\s*`?pick_model\b", out))
                                 and bool(re.search(r"GB\s*=\s*\**\s*`?1\.5\b", out)),
    },
    "log_count": {
        "kind": "greppable",
        "prompt": "app.log in this directory is a large service log. One ERROR message is repeated many "
                  "times, far more than any other. What is it and exactly how many times does it occur? "
                  + ONE_LINE + "ERROR=<message> COUNT=<number>.",
        "grade": lambda out, fx: bool(re.search(r"ERROR\s*=[^\n]*checksum mismatch", out))
                                 and num(out, "COUNT") == fx["log_repeated"],
    },
    # --- bulk: the answer needs a lot of text understood, and grep cannot express the question ---
    "prose": {
        "kind": "bulk",
        "prompt": "incidents.md in this directory contains post-incident write-ups, one per '## INC-' "
                  "heading. In how many of them was the root cause that the service ran out of database "
                  "connections, so requests piled up waiting for a free connection to the primary "
                  "database? The write-ups are free prose, no two describe it the same way, and some "
                  "name causes that were suspected and then ruled out - count only the actual root cause. "
                  + ONE_LINE + "COUNT=<number>.",
        "grade": lambda out, fx: num(out, "COUNT") == fx["inc_db_pool"],
    },
    "log_semantic": {
        "kind": "bulk",
        "prompt": "app.log in this directory is a large service log. Ignoring INFO lines, how many of its "
                  "ERROR lines describe the service giving up because it had waited too long for "
                  "something? The ERROR lines are free text and almost no two are worded the same. "
                  + ONE_LINE + "COUNT=<number>.",
        "grade": lambda out, fx: num(out, "COUNT") == fx["log_waited"],
    },
    "noisy": {
        "kind": "bulk",
        "prompt": "Run `python noisy_tests.py` in this directory. It is very chatty. How many cases "
                  "failed, and what is the number of the first case that failed? Failures are worded "
                  "inconsistently and there is no single marker word to search for. "
                  + ONE_LINE + "FAILED=<n> FIRST=<n>.",
        "grade": lambda out, fx: num(out, "FAILED") == fx["noisy_failed"]
                                 and num(out, "FIRST") == fx["noisy_first"],
    },
}
# how close was it, for tasks whose answer is a count (reported alongside exact correctness)
COUNT_TRUTH = {"prose": "inc_db_pool", "log_semantic": "log_waited", "log_count": "log_repeated"}


def fixture(tmp, task):
    """A fresh project per run: this repo's v1.3 source plus whatever corpus the task needs."""
    d = os.path.join(tmp, "project")
    os.makedirs(d)
    # README.md is deliberately NOT copied: v1.3's README quotes both halves of the `symbol` answer
    # ("L111: def pick_model", "LOCAL_HELPER_MIN_FREE_GB | 1.5"), which made that task greppable
    # against a 9KB file instead of the 1,477-line server.py.
    for f in ("server.py", "outline.py", "enforce.py", "install.py", "test_units.py"):
        data = subprocess.run(["git", "-C", HERE, "show", f"v1.3:{f}"], capture_output=True,
                              check=True).stdout
        open(os.path.join(d, f), "wb").write(data)
    fx = {}
    if task in ("log_count", "log_semantic"):
        t = bench_fixture.make_log(os.path.join(d, "app.log"))
        fx["log_waited"], fx["log_repeated"] = t["waited"], t["top_repeated"][1]
    if task == "prose":
        # 260 write-ups (~2,300 lines / 120KB): too big for the hook's whole-file read AND too big
        # for its paging budget, so the helper arm has to use a tool or Bash rather than page it.
        counts, _labels = bench_fixture.make_incidents(os.path.join(d, "incidents.md"), n_incidents=260)
        fx["inc_db_pool"] = counts["db_pool"]
    if task == "noisy":
        fx["noisy_failed"], fx["noisy_first"] = bench_fixture.make_noisy_tests(
            os.path.join(d, "noisy_tests.py"))
    if task == "symbol":
        check_no_leak(d, ("pick_model", "1.5"), "server.py")
    return d, fx


def check_no_leak(d, needles, expected_file):
    """A graded answer must not be sitting in some small file the task never meant to test."""
    for needle in needles:
        holders = []
        for root, _, files in os.walk(d):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if needle in open(p, encoding="utf-8", errors="ignore").read():
                        holders.append(os.path.relpath(p, d))
                except OSError:
                    pass
        extra = [h for h in holders if h != expected_file]
        if extra:
            raise RuntimeError(f"answer {needle!r} leaks into {extra}; the task would not test "
                               f"{expected_file}")


def argv_for(arm, tmp):
    home_claude = os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md").replace("\\", "/")
    settings = {"claudeMdExcludes": [home_claude], "permissions": {"allow": list(SHELL_ALLOW)}}
    mcp = {"mcpServers": {}}
    tools = list(BASE_TOOLS)
    if arm in ("helper", "tools", "directed"):
        data = os.path.join(tmp, "helper-data")
        os.makedirs(data, exist_ok=True)
        mcp["mcpServers"]["local-helper"] = {"type": "stdio", "command": sys.executable,
                                             "args": [os.path.join(HERE, "server.py")],
                                             "env": {"LOCAL_HELPER_DATA": data}}
        tools += HELPER_TOOLS
    if arm in ("helper", "directed"):        # "tools" is the same minus enforcement
        settings["hooks"] = {"PreToolUse": [{"matcher": install.MATCHER, "hooks": [install.hook_command()]}]}
    sfile = os.path.join(tmp, f"settings-{arm}.json")
    mfile = os.path.join(tmp, f"mcp-{arm}.json")
    json.dump(settings, open(sfile, "w"))
    json.dump(mcp, open(mfile, "w"))
    return [CLAUDE, "-p", "--model", MODEL, "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", mfile,
            "--settings", sfile, "--max-budget-usd", MAX_BUDGET, "--no-session-persistence",
            "--allowedTools", *tools]


def child_env(data_dir):
    """An explicit allowlist, so the developer's own LOCAL_HELPER_*/CLAUDE_* cannot change a run."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP}
    env["LOCAL_HELPER_DATA"] = data_dir
    return env


def parse_stream(text):
    """Read the NDJSON stream: real tool calls, real per-message usage, MCP status."""
    got = {"tools": [], "mcp": None, "offered": 0, "fresh": 0, "peak": 0, "denied": [],
           "result": "", "cost": None, "turns": None, "is_error": False, "stop": None,
           "result_fresh": None, "prefix_cached": None}
    usage_by_id = {}
    for i, line in enumerate(text.splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            got["mcp"] = [s.get("status") for s in ev.get("mcp_servers", [])
                          if s.get("name") == "local-helper"]
            got["offered"] = len([t for t in ev.get("tools", []) if "local-helper" in t])
        msg = ev.get("message")
        msg = msg if isinstance(msg, dict) else {}    # some events carry a bare string here
        if ev.get("type") == "assistant" and isinstance(msg.get("usage"), dict):
            # One API response arrives as one event PER CONTENT BLOCK (thinking, text, tool_use), each
            # carrying the same usage. v1.4 summed every event, so a response counted once or twice
            # depending on whether its thinking block came separately - which inflated `fresh` by up to
            # 2x, unevenly between arms. Count each response once, keyed by its API message id.
            # No id: key by line, never id(ev) - a freed event's address is reused by a later one,
            # which silently dropped a response.
            usage_by_id[msg.get("id") or f"noid-{i}"] = msg["usage"]
        content = msg.get("content")          # a string on some messages, a block list on others
        for blk in content if isinstance(content, list) else []:
            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                got["tools"].append(blk.get("name"))
        if ev.get("type") == "result":
            got["result"] = str(ev.get("result", ""))
            got["cost"] = ev.get("total_cost_usd")
            got["turns"] = ev.get("num_turns")
            got["is_error"] = bool(ev.get("is_error"))
            got["stop"] = ev.get("terminal_reason") or ev.get("stop_reason")
            got["denied"] = [d.get("tool_name") for d in ev.get("permission_denials") or []]
            ru = ev.get("usage") or {}       # the CLI's own session total: a cross-check on `fresh`
            if ru:
                got["result_fresh"] = (ru.get("input_tokens") or 0) + (ru.get("cache_creation_input_tokens") or 0)
    for u in usage_by_id.values():
        fresh = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        got["fresh"] += fresh
        got["peak"] = max(got["peak"], fresh + (u.get("cache_read_input_tokens") or 0))
    if usage_by_id:
        # Was the system-prompt prefix already cached when the session started? A cold prefix is
        # written in full (~17k tokens) and swamps what the task itself cost, so arms must be compared
        # warm against warm (summarise drops cold rows from its medians). Both arms share the cached
        # system prefix, so in practice the cold session is the first of a batch, or one that follows
        # a gap longer than the cache lifetime - whichever arm happens to run then.
        got["prefix_cached"] = (next(iter(usage_by_id.values())).get("cache_read_input_tokens") or 0) > 0
    return got


def run_one(task, arm, rep, runs_dir):
    tmp = tempfile.mkdtemp(prefix=f"lh_bench_{task}_{arm}_")
    try:
        workdir, fx = fixture(tmp, task)
        if arm == "directed":
            open(os.path.join(workdir, "CLAUDE.md"), "w", encoding="utf-8").write(DIRECTED_RULE)
        argv = argv_for(arm, tmp)
        data_dir = os.path.join(tmp, "helper-data")
        os.makedirs(data_dir, exist_ok=True)
        t0 = time.time()
        timed_out = False
        try:
            r = subprocess.run(argv, input=TASKS[task]["prompt"], capture_output=True, text=True,
                               encoding="utf-8", cwd=workdir, timeout=RUN_TIMEOUT, env=child_env(data_dir))
            stdout, stderr = r.stdout, r.stderr
        except subprocess.TimeoutExpired as e:
            # One hung session must not crash the batch and lose the rows before it. Keep what it
            # wrote; on POSIX the partial output is bytes even with text=True.
            timed_out = True
            stdout, stderr = (x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")
                              for x in (e.stdout, e.stderr))
        secs = round(time.time() - t0, 1)
        raw = os.path.join(runs_dir, f"{task}-{arm}-rep{rep}.ndjson")
        open(raw, "w", encoding="utf-8").write(stdout)
        g = parse_stream(stdout)
        out = g["result"] or (stdout + stderr)[-500:]
        helper_calls = [t for t in g["tools"] if t and t.startswith("mcp__local-helper__")]
        row = {"task": task, "kind": TASKS[task].get("kind", ""), "arm": arm, "rep": rep,
               "ok": bool(TASKS[task]["grade"](out, fx)) and not g["is_error"],
               "fresh": g["fresh"], "peak": g["peak"], "cost_usd": g["cost"], "turns": g["turns"],
               "secs": secs, "is_error": g["is_error"], "stop": g["stop"],
               "tools_used": sorted(set(g["tools"])), "helper_calls": len(helper_calls),
               "helper_tools": sorted(set(helper_calls)), "mcp_status": g["mcp"],
               "helper_tools_offered": g["offered"], "denied": g["denied"], "prefix_cached": g["prefix_cached"],
               "answer": out.strip()[-300:], "raw": os.path.relpath(raw, HERE).replace(os.sep, "/"),
               # the model's own final text, kept apart from `answer`'s stdout fallback: hit_limit
               # must not fire on some log line the CLI printed
               "result": g["result"].strip()[-300:], "result_fresh": g["result_fresh"],
               "backend": backend.kind(), "backend_version": BACKEND_VERSION, "model": MODEL,
               "big_model": BIG_MODEL}
        t = COUNT_TRUTH.get(task)
        if t and t in fx:
            row["truth"], row["said"] = fx[t], num(out, "COUNT")
        notes = []
        if timed_out:
            row["ok"] = False
            notes.append("timeout")
        if arm in ("helper", "tools", "directed") and g["mcp"] != ["connected"]:
            row["ok"] = False
            notes.append(f"local-helper MCP did not connect: {g['mcp']}")
        if g["result_fresh"] is not None and g["result_fresh"] != g["fresh"]:
            # the CLI's own session total disagrees with the per-message dedup: the stream changed shape
            notes.append(f"fresh {g['fresh']} != CLI total {g['result_fresh']}")
        if notes:
            row["note"] = "; ".join(notes)
        return row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stray_env(environ):
    """LOCAL_HELPER_* settings the child would never see (child_env drops them) but this process would
    check and record - so preflight and the rows would describe a setup the runs did not use."""
    return sorted(k for k in environ if k.upper().startswith("LOCAL_HELPER_") and k.upper() != "LOCAL_HELPER_DATA")


def preflight(arms):
    """The helper arms must really have local-helper working, or the comparison is meaningless."""
    if not os.path.isfile(CLAUDE) and not shutil.which(CLAUDE):
        sys.exit(f"refusing to benchmark: the claude CLI was not found ({CLAUDE}).")
    stray = stray_env(os.environ)
    if stray:
        sys.exit(f"refusing to benchmark: {', '.join(stray)} is set here, but the benchmarked sessions "
                 "run with the defaults (their environment is an allowlist), so the checks below and the "
                 "recorded setup would not match the runs. Unset it; use config.json for backend/url.")
    if not any(a in arms for a in ("helper", "tools", "directed")):
        return
    if os.path.exists(os.path.join(HERE, "ENFORCE_OFF")):
        sys.exit("refusing to benchmark: ENFORCE_OFF exists, so the hook would be disabled in the "
                 "'helper' and 'directed' runs. Delete it first.")
    import socket
    try:
        with socket.create_connection(backend.host_port(), timeout=2):
            pass
    except OSError:
        sys.exit(f"refusing to benchmark: the {backend.kind()} backend is not reachable at "
                 f"{backend.base_url()}, so the model tools would fail in the 'helper' runs.")
    have = backend.list_models() or set()
    missing = [m for m in (BIG_MODEL, EMBED_MODEL) if not backend.has_model(have, m)]
    if missing:
        sys.exit(f"refusing to benchmark: missing model(s) {', '.join(missing)}.")
    mcp_probe()


def mcp_probe():
    """Start server.py over stdio and make it list its tools: a silent crash would fake a clean run."""
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    req = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
           '"capabilities":{},"clientInfo":{"name":"bench","version":"1"}}}\n'
           '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
           '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n')
    try:
        out, err = p.communicate(req, timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        sys.exit("refusing to benchmark: server.py did not answer MCP within 60s.")
    names = re.findall(r'"name"\s*:\s*"(local_\w+)"', out)
    if len(set(names)) < 9:
        sys.exit(f"refusing to benchmark: server.py listed {len(set(names))} tools, expected 9. "
                 f"stderr: {err[-300:]}")


BACKEND_VERSION = None


def backend_version():
    """Recorded with every row: an Ollama auto-update landed in the middle of v1.5's benchmark, and the
    local model's speed is part of what the directed arm measures."""
    if backend.kind() != "ollama":
        return None
    try:
        with urllib.request.urlopen(backend.base_url().rstrip("/") + "/api/version", timeout=5) as r:
            return json.load(r).get("version")
    except (OSError, ValueError):
        return None


# The CLI's own wordings only. "rate limit" / "limit exceeded" also appear in ordinary answers about
# the fixture logs, and a false hit stops the whole run.
LIMIT_TEXT = re.compile(r"(session|usage) limit|hit your (\w+ )?limit|limit.{0,40}resets|rate_limit_error", re.I)


def hit_limit(row):
    """The account ran out, not the task. Seen both ways: a zero-token api_error before the session
    starts, and a session that did real work and then answered "You've hit your session limit".
    Only the final result text is checked; rows from before `result` existed fall back to `answer`."""
    return bool(LIMIT_TEXT.search(row.get("result", row.get("answer")) or "")) or (
        row.get("fresh", 0) == 0 and row.get("stop") == "api_error")


def code_version():
    """Recorded per row, so a result can be tied to the code that produced it, and --resume can refuse
    to mix rows from different code into one table."""
    files = {}
    for f in CODE_FILES:
        with open(os.path.join(HERE, f), "rb") as fh:
            files[f] = hashlib.sha256(fh.read()).hexdigest()[:12]
    try:
        git = subprocess.run(["git", "-C", HERE, "describe", "--always", "--dirty"], capture_output=True,
                             text=True, timeout=30).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        git = None                           # no git, or not a checkout: the hashes still identify the code
    return files, git


def load_resume(path, code, force):
    """The real rows already in PATH, refusing ones made by other code unless FORCE."""
    with open(path, encoding="utf-8") as f:
        rows = [r for r in json.load(f) if not hit_limit(r)]
    stale = [r for r in rows if r.get("code") != code]
    if stale and not force:
        sys.exit(f"refusing to resume: {len(stale)} of {len(rows)} rows in {path} were made by different "
                 f"code (bench/server/enforce/backend hashes differ, or were not recorded), so one table "
                 f"would mix two versions. Start a new --out, or pass --force-resume to mix them anyway.")
    return rows


def save_rows(rows, path):
    """Write-then-rename: a crash mid-dump must not leave a truncated file that loses every run."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
    os.replace(tmp, path)


def arm_order(arms, tasks, t, rep):
    """Rotate, don't shuffle: over len(arms) reps every arm goes first exactly once per task, so a warm
    cache or a drifting backend cannot favour one arm. Offset by task so reps do not all start alike."""
    k = (rep + tasks.index(t)) % len(arms)
    return arms[k:] + arms[:k]


def self_check():
    """Parsing the stream is the whole measurement: check it on the shapes the CLI really emits."""
    stream = "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "local-helper", "status": "connected"}],
         "tools": ["Read", "mcp__local-helper__local_outline", "mcp__local-helper__local_run"]},
        {"type": "user", "message": {"content": "a plain string, not a block list"}},   # used to crash
        {"type": "user", "message": "a bare string where a dict is expected"},          # so did this
        {"type": "assistant", "message": {"usage": {"input_tokens": 4, "cache_creation_input_tokens": 100,
                                                    "cache_read_input_tokens": 1000},
                                          "content": [{"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 50,
                                                    "cache_read_input_tokens": 3000},
                                          "content": [{"type": "tool_use",
                                                       "name": "mcp__local-helper__local_outline", "input": {}}]}},
        {"type": "result", "result": "COUNT=28", "total_cost_usd": 0.5, "num_turns": 3,
         "is_error": False, "permission_denials": [{"tool_name": "Bash"}]},
    ])
    g = parse_stream(stream + "\nnot json at all\n")
    assert g["fresh"] == 156, g["fresh"]                       # 4+100 + 2+50, no cache reads summed
    # One API response arrives as one event per content block, all carrying the same usage and the
    # same message id. v1.4 summed them, inflating `fresh` by up to 2.69x. Count each id once.
    one_response = "\n".join(json.dumps({"type": "assistant", "message": {
        "id": "msg_same", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 5000,
                                    "cache_read_input_tokens": 12000},
        "content": [{"type": kind}]}}) for kind in ("thinking", "text", "tool_use"))
    dup = parse_stream(one_response + "\n" + json.dumps(
        {"type": "result", "result": "", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 5000}}))
    assert dup["fresh"] == 5002, f"one response counted {dup['fresh'] / 5002:.0f} times"
    assert dup["fresh"] == dup["result_fresh"], "must agree with the CLI's own session total"
    assert g["peak"] == 3052, g["peak"]                        # last message only: 2+50+3000
    assert g["mcp"] == ["connected"], g["mcp"]
    assert g["offered"] == 2, g["offered"]
    assert g["tools"] == ["Read", "mcp__local-helper__local_outline"], g["tools"]
    assert g["denied"] == ["Bash"] and g["turns"] == 3 and g["cost"] == 0.5, g
    assert num("COUNT = **28** of them", "COUNT") == 28
    assert num("FAILED=7 FIRST=140", "FIRST") == 140
    assert num("no number here", "COUNT") is None
    assert TASKS["prose"]["grade"]("COUNT=28", {"inc_db_pool": 28})
    assert not TASKS["prose"]["grade"]("COUNT=27", {"inc_db_pool": 28})
    assert not TASKS["log_count"]["grade"]("ERROR=something else COUNT=57", {"log_repeated": 57})
    assert TASKS["log_count"]["grade"]("ERROR=config checksum mismatch on shard 4 COUNT=57",
                                       {"log_repeated": 57})
    # Two responses without an id, a line apart: keyed by id(ev), the second reused the first's freed
    # address and overwrote it, so only one was counted.
    noid = "\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 10}}},
        {"type": "user", "message": {"content": "filler"}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 10}}}])
    assert parse_stream(noid)["fresh"] == 22, parse_stream(noid)["fresh"]
    # both shapes the usage limit has actually taken in a run, and one that is merely a wrong answer
    assert hit_limit({"fresh": 9462, "result": "You've hit your session limit - resets 12:30am", "stop": "x"})
    assert hit_limit({"fresh": 0, "result": "", "stop": "api_error"})
    assert hit_limit({"fresh": 5, "answer": "Claude AI usage limit reached|1759012345"})   # old row, no result
    assert not hit_limit({"fresh": 9462, "result": "COUNT=299", "stop": "end_turn"})
    # ordinary answers about the fixture logs, and a CLI log line that only reached the stdout fallback
    assert not hit_limit({"fresh": 9462, "result": "ERROR=upstream rate limit exceeded COUNT=41"})
    assert not hit_limit({"fresh": 9462, "result": "", "answer": "warn: usage limit 80% used", "stop": "end_turn"})
    # rotation: over len(arms) reps, each arm goes first exactly once for every task
    arms3, tasks2 = ["baseline", "helper", "directed"], ["prose", "noisy"]
    for t in tasks2:
        firsts = [arm_order(arms3, tasks2, t, rep)[0] for rep in range(len(arms3))]
        assert sorted(firsts) == sorted(arms3), (t, firsts)
    # exact orders, so the check is deterministic: the seeded shuffle it replaced passed the balance check
    # above for some hash seeds (13 of 200), so on its own that check could not reliably catch a revert
    assert [arm_order(arms3, tasks2, "prose", r) for r in range(3)] == [
        ["baseline", "helper", "directed"], ["helper", "directed", "baseline"], ["directed", "baseline", "helper"]]
    assert arm_order(arms3, tasks2, "noisy", 0) == ["helper", "directed", "baseline"]
    assert stray_env({"LOCAL_HELPER_DATA": "d", "LOCAL_HELPER_BIG": "m", "PATH": "p"}) == ["LOCAL_HELPER_BIG"]
    tmp = tempfile.mkdtemp(prefix="lh_bench_check_")
    try:
        _check_rows_and_files(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("bench self-check ok")


def _check_rows_and_files(tmp):
    """The self-check parts that need files: summarise, --resume, --out, and run_one's error paths."""
    import contextlib
    import io
    cold_raw = os.path.join(tmp, "cold.ndjson")      # an old row's transcript: prefix NOT cached
    with open(cold_raw, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "message": {"id": "m", "usage": {
            "input_tokens": 1, "cache_creation_input_tokens": 699, "cache_read_input_tokens": 0}}}))
    base = {"task": "overhead", "arm": "baseline", "ok": True, "helper_calls": 0, "peak": 1,
            "cost_usd": 0.1, "turns": 1, "secs": 1}
    rows = [dict(base, rep=0, fresh=100, prefix_cached=True),
            dict(base, rep=1, fresh=900, prefix_cached=False),
            dict(base, rep=2, fresh=700, raw=cold_raw),                 # no field: must be re-read
            dict(base, task="symbol", rep=0, fresh=5, note="OTHER TASK")]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        summarise(rows, ["overhead"], ["baseline"])
    out = buf.getvalue()
    # 100 only if both cold rows are dropped (with either kept the median is 400 or 700)
    assert re.search(r"overhead .* 3/3 +100 ", out), out
    assert "(2 cold-prefix excluded)" in out and "OTHER TASK" not in out, out
    # --resume: rows from other code are refused unless forced
    code, _git = code_version()
    assert sorted(code) == sorted(CODE_FILES) and all(len(h) == 12 for h in code.values()), code
    res = os.path.join(tmp, "res.json")
    save_rows([dict(base, rep=0, fresh=1, code=dict(code, **{"server.py": "000000000000"}))], res)
    assert not os.path.exists(res + ".tmp")
    try:
        load_resume(res, code, force=False)
        raise AssertionError("resumed rows made by different code")
    except SystemExit as e:
        assert "different code" in str(e), e
    assert len(load_resume(res, code, force=True)) == 1
    # an existing --out is not overwritten without --resume or --overwrite. preflight is stubbed so that
    # if this guard ever breaks, the check fails here instead of starting real, paid sessions.
    global preflight
    argv0, real_preflight = sys.argv, preflight
    sys.argv = ["bench.py", "--out", res]
    preflight = lambda arms: sys.exit("reached preflight")                # noqa: E731
    try:
        main()
        raise AssertionError("main() would overwrite an existing --out")
    except SystemExit as e:
        assert "already exists" in str(e), e
    finally:
        sys.argv, preflight = argv0, real_preflight
    # run_one: a hung session becomes a row (with its partial transcript), and a CLI total that
    # disagrees with the dedup is noted. A python one-liner stands in for the claude CLI.
    # fixture() is stubbed too: the real one runs `git show v1.3:...`, which a shallow CI checkout, a ZIP
    # download or any copy without tags cannot do - and these checks are about run_one, not the fixture.
    global argv_for, RUN_TIMEOUT, fixture
    real_argv_for, real_timeout, real_fixture = argv_for, RUN_TIMEOUT, fixture

    def empty_fixture(tmp_dir, task):
        d = os.path.join(tmp_dir, "project")
        os.makedirs(d)
        return d, {}
    fixture = empty_fixture
    init = json.dumps({"type": "system", "subtype": "init"})
    hang = f"print({init!r}, flush=True); import time; time.sleep(60)"
    lines = [json.dumps({"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": 4,
                                                                              "cache_creation_input_tokens": 100}}}),
             json.dumps({"type": "result", "result": "OK", "usage": {"input_tokens": 999}})]
    mismatch = f"print({chr(10).join(lines)!r})"
    try:
        RUN_TIMEOUT = 5
        argv_for = lambda arm, t: [sys.executable, "-c", hang]            # noqa: E731
        row = run_one("overhead", "baseline", 0, tmp)
        assert row["ok"] is False and row["note"] == "timeout", row
        with open(os.path.join(tmp, "overhead-baseline-rep0.ndjson"), encoding="utf-8") as f:
            assert '"init"' in f.read(), "partial output was not kept"
        argv_for = lambda arm, t: [sys.executable, "-c", mismatch]        # noqa: E731
        row = run_one("overhead", "baseline", 1, tmp)
        assert row["fresh"] == 104 and row["result_fresh"] == 999 and "CLI total 999" in row["note"], row
    finally:
        argv_for, RUN_TIMEOUT, fixture = real_argv_for, real_timeout, real_fixture


def med(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return statistics.median(vals) if vals else float("nan")     # nan, not 0: "no data" is not "free"


def warm(rows):
    """Rows whose system prefix was already cached. A cold one pays ~17k tokens of cache writes that
    have nothing to do with the task, so it stays out of token/cost medians (None = unknown, kept)."""
    return [r for r in rows if r.get("prefix_cached") is not False]


def summarise(rows, tasks, arms):
    # --resume keeps every row in --out; only the requested tasks and arms belong in this table
    rows = [r for r in rows if r["task"] in tasks and r["arm"] in arms]
    for r in rows:
        if "prefix_cached" not in r:         # rows from before the field existed: re-read the transcript
            p = os.path.join(HERE, r.get("raw") or "")
            if r.get("raw") and os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    r["prefix_cached"] = parse_stream(f.read())["prefix_cached"]
    print("\n" + "=" * 100)
    print("per task (median over reps; token/cost medians use CORRECT, WARM-prefix runs only)")
    print(f"{'task':13} {'kind':10} {'arm':9} {'correct':>8} {'fresh':>9} {'peak':>9} {'cost$':>8} "
          f"{'turns':>6} {'secs':>6} {'helper calls':>13}")
    for t in tasks:
        for a in arms:
            rs = [r for r in rows if r["task"] == t and r["arm"] == a]
            if not rs:
                continue
            good = [r for r in rs if r["ok"]] or rs
            hot = warm(good)
            cold = len(good) - len(hot)
            print(f"{t:13} {TASKS[t].get('kind',''):10} {a:9} {sum(r['ok'] for r in rs):>3}/{len(rs):<4} "
                  f"{med(hot,'fresh'):>9,.0f} {med(hot,'peak'):>9,.0f} {med(hot,'cost_usd'):>8.3f} "
                  f"{med(good,'turns'):>6.0f} {med(good,'secs'):>6.0f} "
                  f"{sum(r['helper_calls'] for r in rs):>13}" + (f"  ({cold} cold-prefix excluded)" if cold else ""))
    counted = [r for r in rows if r.get("truth") is not None]
    if counted:
        print("\nhow wrong were the counts (median |said - truth|; a cheap wrong answer is not a win)")
        for t in sorted({r["task"] for r in counted}):
            for a in arms:
                rs = [r for r in counted if r["task"] == t and r["arm"] == a]
                errs = [abs(r["said"] - r["truth"]) for r in rs if r.get("said") is not None]
                miss = sum(1 for r in rs if r.get("said") is None)
                if rs:
                    print(f"  {t:13} {a:9} truth={rs[0]['truth']:<5} "
                          f"median error {statistics.median(errs) if errs else float('nan'):>6.1f}  "
                          f"said {[r.get('said') for r in rs]}" + (f"  ({miss} gave no number)" if miss else ""))
    print("\ndelta vs baseline (median of correct warm-prefix runs; negative = local-helper used less)")
    for t in tasks:
        base = warm([r for r in rows if r["task"] == t and r["arm"] == "baseline" and r["ok"]])
        if not base:
            continue
        for a in [x for x in arms if x != "baseline"]:
            alt = warm([r for r in rows if r["task"] == t and r["arm"] == a and r["ok"]])
            if not alt:
                print(f"  {t:13} {a:9} no correct warm runs to compare")
                continue
            bf, af = med(base, "fresh"), med(alt, "fresh")
            bc, ac = med(base, "cost_usd"), med(alt, "cost_usd")
            pf = (af - bf) / bf * 100 if bf else 0
            pc = (ac - bc) / bc * 100 if bc else 0
            print(f"  {t:13} {a:9} fresh {af-bf:>+9,.0f} ({pf:>+6.1f}%)   cost {ac-bc:>+7.3f} ({pc:>+6.1f}%)")
    spread = [r for r in rows if r["ok"]]
    if spread:
        print(f"\nruns: {len(rows)} total, {len(spread)} correct, "
              f"${sum(r['cost_usd'] or 0 for r in rows):.2f} spent")
    bad = [r for r in rows if r.get("note")]
    for r in bad:
        print(f"  !! {r['task']}/{r['arm']}/rep{r['rep']}: {r['note']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--arms", default="baseline,helper")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-check", action="store_true", help="check the stream parser and graders, spend nothing")
    ap.add_argument("--out", default=os.path.join(HERE, "bench_results.json"))
    ap.add_argument("--resume", action="store_true", help="keep the real runs in --out and do only the rest")
    ap.add_argument("--force-resume", action="store_true",
                    help="with --resume, keep rows made by different code (they will be mixed in one table)")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing --out instead of refusing")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    tasks = [t for t in a.tasks.split(",") if t in TASKS]
    arms = [x for x in a.arms.split(",") if x in ("baseline", "tools", "helper", "directed")]
    if a.dry_run:
        for t in tasks:
            print(f"--- {t} ({TASKS[t].get('kind','')})\n{TASKS[t]['prompt']}\n")
        d = tempfile.mkdtemp(prefix="lh_bench_dry_")
        print(" ".join(argv_for("helper", d)))
        shutil.rmtree(d, ignore_errors=True)
        print(f"\n{len(tasks)} tasks x {len(arms)} arms x {a.reps} reps = "
              f"{len(tasks)*len(arms)*a.reps} real sessions")
        return
    if os.path.exists(a.out) and not (a.resume or a.overwrite):
        # a forgotten --resume used to wipe every paid run in the file on the first save
        sys.exit(f"refusing to start: {a.out} already exists. Pass --resume to continue it, --overwrite "
                 f"to replace it, or a new --out.")
    code, git = code_version()
    rows = []
    if a.resume and os.path.exists(a.out):
        rows = load_resume(a.out, code, a.force_resume)
        print(f"resuming: {len(rows)} runs already done in {a.out}")
    preflight(arms)
    global BACKEND_VERSION
    BACKEND_VERSION = backend_version()
    # one folder per invocation: rep-numbered names collided across batches, and a top-up run
    # silently overwrote the first batch's transcripts
    batch = time.strftime("%Y%m%d-%H%M%S")
    runs_dir = os.path.join(HERE, "bench_runs", batch)
    os.makedirs(runs_dir, exist_ok=True)
    done = {(r["task"], r["arm"], r["rep"]) for r in rows}
    for rep in range(a.reps):
        for t in tasks:
            for arm in arm_order(arms, tasks, t, rep):
                if (t, arm, rep) in done:
                    continue
                row = run_one(t, arm, rep, runs_dir)
                row["batch"], row["code"], row["git"] = batch, code, git
                print(f"{t:13} {arm:9} rep{rep} ok={row['ok']!s:5} fresh={row['fresh']:>8,} "
                      f"peak={row['peak']:>8,} cost=${row['cost_usd'] or 0:.3f} "
                      f"turns={row['turns']} {row['secs']}s helper_calls={row['helper_calls']}"
                      f"{' ' + row.get('note', '')}", flush=True)
                if hit_limit(row):
                    # Every later run would fail the same way and look like data. v1.5's first run
                    # burned 21 sessions like this. Stop, keep what is real, say how to continue.
                    print(f"\nSTOPPED: the account's usage limit was hit ({row['answer'][-120:]!r}).\n"
                          f"{len(rows)} real runs are saved in {a.out}. When the limit resets, continue with:\n"
                          f"  python bench.py --resume --reps {a.reps} --tasks {','.join(tasks)} "
                          f"--arms {','.join(arms)} --out {a.out}", flush=True)
                    break
                rows.append(row)
                save_rows(rows, a.out)
            else:
                continue
            break
        else:
            continue
        break
    summarise(rows, tasks, arms)


if __name__ == "__main__":
    main()
