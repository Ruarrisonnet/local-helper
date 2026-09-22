"""Does local-helper actually save Claude tokens? Run real headless Claude Code sessions on the same
tasks with and without it, and compare what entered the context, what it cost, and whether the answer
was right.

    python bench.py --dry-run           show the tasks and the commands, spend nothing
    python bench.py --reps 3            every task, every arm, 3 times (spends your Claude usage)
    python bench.py --tasks prose,noisy --reps 5
    python bench.py --arms baseline,tools,helper    add the MCP-without-hook arm

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
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
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
    if arm in ("helper", "tools"):
        data = os.path.join(tmp, "helper-data")
        os.makedirs(data, exist_ok=True)
        mcp["mcpServers"]["local-helper"] = {"type": "stdio", "command": sys.executable,
                                             "args": [os.path.join(HERE, "server.py")],
                                             "env": {"LOCAL_HELPER_DATA": data}}
        tools += HELPER_TOOLS
    if arm == "helper":                      # "tools" is the same minus enforcement
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
           "result": "", "cost": None, "turns": None, "is_error": False, "stop": None}
    for line in text.splitlines():
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
            u = msg["usage"]
            fresh = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            got["fresh"] += fresh
            got["peak"] = max(got["peak"], fresh + (u.get("cache_read_input_tokens") or 0))
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
    return got


def run_one(task, arm, rep, runs_dir):
    tmp = tempfile.mkdtemp(prefix=f"lh_bench_{task}_{arm}_")
    try:
        workdir, fx = fixture(tmp, task)
        argv = argv_for(arm, tmp)
        data_dir = os.path.join(tmp, "helper-data")
        os.makedirs(data_dir, exist_ok=True)
        t0 = time.time()
        r = subprocess.run(argv, input=TASKS[task]["prompt"], capture_output=True, text=True,
                           encoding="utf-8", cwd=workdir, timeout=3600, env=child_env(data_dir))
        secs = round(time.time() - t0, 1)
        raw = os.path.join(runs_dir, f"{task}-{arm}-rep{rep}.ndjson")
        open(raw, "w", encoding="utf-8").write(r.stdout)
        g = parse_stream(r.stdout)
        out = g["result"] or (r.stdout + r.stderr)[-500:]
        helper_calls = [t for t in g["tools"] if t and t.startswith("mcp__local-helper__")]
        row = {"task": task, "kind": TASKS[task].get("kind", ""), "arm": arm, "rep": rep,
               "ok": bool(TASKS[task]["grade"](out, fx)) and not g["is_error"],
               "fresh": g["fresh"], "peak": g["peak"], "cost_usd": g["cost"], "turns": g["turns"],
               "secs": secs, "is_error": g["is_error"], "stop": g["stop"],
               "tools_used": sorted(set(g["tools"])), "helper_calls": len(helper_calls),
               "helper_tools": sorted(set(helper_calls)), "mcp_status": g["mcp"],
               "helper_tools_offered": g["offered"], "denied": g["denied"],
               "answer": out.strip()[-300:], "raw": os.path.basename(raw),
               "backend": backend.kind(), "model": MODEL,
               "big_model": os.environ.get("LOCAL_HELPER_BIG", "qwen2.5-coder:7b-instruct-q3_K_M")}
        t = COUNT_TRUTH.get(task)
        if t and t in fx:
            row["truth"], row["said"] = fx[t], num(out, "COUNT")
        if arm in ("helper", "tools") and g["mcp"] != ["connected"]:
            row["ok"] = False
            row["note"] = f"local-helper MCP did not connect: {g['mcp']}"
        return row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def preflight(arms):
    """The helper arms must really have local-helper working, or the comparison is meaningless."""
    if not os.path.isfile(CLAUDE) and not shutil.which(CLAUDE):
        sys.exit(f"refusing to benchmark: the claude CLI was not found ({CLAUDE}).")
    if not any(a in arms for a in ("helper", "tools")):
        return
    if os.path.exists(os.path.join(HERE, "ENFORCE_OFF")):
        sys.exit("refusing to benchmark: ENFORCE_OFF exists, so the hook would be disabled in the "
                 "'helper' runs. Delete it first.")
    import socket
    try:
        with socket.create_connection(backend.host_port(), timeout=2):
            pass
    except OSError:
        sys.exit(f"refusing to benchmark: the {backend.kind()} backend is not reachable at "
                 f"{backend.base_url()}, so the model tools would fail in the 'helper' runs.")
    have = backend.list_models() or set()
    missing = [m for m in (os.environ.get("LOCAL_HELPER_BIG", "qwen2.5-coder:7b-instruct-q3_K_M"),
                           os.environ.get("LOCAL_HELPER_EMBED", "nomic-embed-text"))
               if not backend.has_model(have, m)]
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
    print("bench self-check ok")


def med(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return statistics.median(vals) if vals else 0


def summarise(rows, tasks, arms):
    print("\n" + "=" * 100)
    print("per task (median over reps; token/cost medians use CORRECT runs only, n shown)")
    print(f"{'task':13} {'kind':10} {'arm':9} {'correct':>8} {'fresh':>9} {'peak':>9} {'cost$':>8} "
          f"{'turns':>6} {'secs':>6} {'helper calls':>13}")
    for t in tasks:
        for a in arms:
            rs = [r for r in rows if r["task"] == t and r["arm"] == a]
            if not rs:
                continue
            good = [r for r in rs if r["ok"]] or rs
            print(f"{t:13} {TASKS[t].get('kind',''):10} {a:9} {sum(r['ok'] for r in rs):>3}/{len(rs):<4} "
                  f"{med(good,'fresh'):>9,.0f} {med(good,'peak'):>9,.0f} {med(good,'cost_usd'):>8.3f} "
                  f"{med(good,'turns'):>6.0f} {med(good,'secs'):>6.0f} "
                  f"{sum(r['helper_calls'] for r in rs):>13}")
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
    print("\ndelta vs baseline (median of correct runs; negative = local-helper used less)")
    for t in tasks:
        base = [r for r in rows if r["task"] == t and r["arm"] == "baseline" and r["ok"]]
        if not base:
            continue
        for a in [x for x in arms if x != "baseline"]:
            alt = [r for r in rows if r["task"] == t and r["arm"] == a and r["ok"]]
            if not alt:
                print(f"  {t:13} {a:9} no correct runs to compare")
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
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    tasks = [t for t in a.tasks.split(",") if t in TASKS]
    arms = [x for x in a.arms.split(",") if x in ("baseline", "tools", "helper")]
    if a.dry_run:
        for t in tasks:
            print(f"--- {t} ({TASKS[t].get('kind','')})\n{TASKS[t]['prompt']}\n")
        d = tempfile.mkdtemp(prefix="lh_bench_dry_")
        print(" ".join(argv_for("helper", d)))
        shutil.rmtree(d, ignore_errors=True)
        print(f"\n{len(tasks)} tasks x {len(arms)} arms x {a.reps} reps = "
              f"{len(tasks)*len(arms)*a.reps} real sessions")
        return
    preflight(arms)
    runs_dir = os.path.join(HERE, "bench_runs")
    os.makedirs(runs_dir, exist_ok=True)
    rows = []
    for rep in range(a.reps):
        for t in tasks:
            order = list(arms)
            random.Random(rep * 31 + hash(t) % 97).shuffle(order)   # never always baseline-first
            for arm in order:
                row = run_one(t, arm, rep, runs_dir)
                rows.append(row)
                print(f"{t:13} {arm:9} rep{rep} ok={row['ok']!s:5} fresh={row['fresh']:>8,} "
                      f"peak={row['peak']:>8,} cost=${row['cost_usd'] or 0:.3f} "
                      f"turns={row['turns']} {row['secs']}s helper_calls={row['helper_calls']}"
                      f"{' ' + row.get('note', '')}", flush=True)
                json.dump(rows, open(a.out, "w"), indent=1)
    summarise(rows, tasks, arms)


if __name__ == "__main__":
    main()
