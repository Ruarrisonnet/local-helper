"""Pipe synthetic hook payloads through enforce.py and check each allow/deny decision.

Self-contained: runs a COPY of enforce.py + outline.py in a temp dir, so your live kill switch,
config and paging state are never touched. Needs no Ollama (LOCAL_HELPER_ASSUME_OLLAMA_UP=1).

    python test_enforce.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = tempfile.mkdtemp(prefix="lh_enforce_")
for f in ("enforce.py", "outline.py", "backend.py"):
    shutil.copy(os.path.join(HERE, f), WORK)
HOOK = os.path.join(WORK, "enforce.py")
ENV = dict(os.environ, LOCAL_HELPER_ASSUME_OLLAMA_UP="1")

files = os.path.join(WORK, "files")
os.makedirs(files)
BIG = os.path.join(files, "big.py")                       # 770 lines -> paging budget max(600, 192) = 600
with open(BIG, "w") as f:
    for i in range(700):
        f.write(f"def fn_{i}():\n    return {i}\n" if i % 10 == 0 else f"x_{i} = {i}\n")
SMALL = os.path.join(files, "small.py")
with open(SMALL, "w") as f:
    f.write("print('hi')\n" * 300)
UNI_DIR = os.path.join(files, "Müller")
os.makedirs(UNI_DIR)
UNI_BIG = os.path.join(UNI_DIR, "grosse_datei.py")
shutil.copy(BIG, UNI_BIG)
PHOTO = os.path.join(files, "IMG_0001.JPG")               # uppercase binary extension: exempt everywhere
with open(PHOTO, "wb") as f:
    f.write(b"\xff\xd8" + b"\n" * 5000)
CFG_DIR = os.path.join(files, "configured_exempt")
os.makedirs(CFG_DIR)
CFG_BIG = os.path.join(CFG_DIR, "ref.md")
with open(CFG_BIG, "w") as f:
    f.write("reference line\n" * 900)
with open(os.path.join(WORK, "config.json"), "w") as f:
    json.dump({"exempt_dirs": [CFG_DIR]}, f)
BIG_BS = BIG.replace("\\", "/").replace("/", "\\\\")


def run(payload, env=ENV):
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    r = subprocess.run([sys.executable, HOOK], input=data.encode("utf-8"), capture_output=True, env=env, timeout=30)
    out = r.stdout.decode("utf-8", "replace")
    return ("DENY" if '"deny"' in out else "ALLOW"), out, r.returncode


def read(path, **kw):
    return {"tool_name": "Read", "tool_input": {"file_path": path, **kw}}


def sh(tool, cmd):
    return {"tool_name": tool, "tool_input": {"command": cmd}}


results = []


def expect(want, payload, name, env=ENV):
    got, out, code = run(payload, env)
    results.append((got == want and code == 0, f"want {want:5} got {got:5} {name}"))
    return out


fwd = BIG.replace("\\", "/")
expect("DENY", read(BIG), "Read big, no limit")
expect("ALLOW", read(BIG, offset=100, limit=20), "Read big, window of 20 (no session: not counted)")
expect("DENY", read(BIG, limit=2000), "Read big, limit 2000 (dodge)")
expect("ALLOW", read(SMALL), "Read small file")
expect("ALLOW", read(os.path.join(files, "missing.txt")), "Read missing file")
expect("ALLOW", read(PHOTO), "Read IMG_0001.JPG (uppercase binary extension)")
expect("ALLOW", read(CFG_BIG), "Read big file in a config.json exempt_dirs folder")
# Claude Code spills an oversized tool result to ~/.claude/projects/<slug>/<id>/tool-results/*.txt
# and then reads it back. Blocking that stops Claude reading its own grep output (seen for real in
# a benchmark run), so everything under ~/.claude/projects is exempt.
SPILL = os.path.join(WORK, ".claude", "projects", "slug", "sess", "tool-results")
os.makedirs(SPILL)
SPILL_BIG = os.path.join(SPILL, "toolu_01abc.txt")
shutil.copy(BIG, SPILL_BIG)
expect("ALLOW", read(SPILL_BIG), "Read a big Claude Code tool-result spill file",
       env=dict(ENV, HOME=WORK, USERPROFILE=WORK))
expect("ALLOW", read(os.path.join(WORK, ".claude", "projects", "slug", "sess", "tool-results", "toolu_01abc.txt"),
                    limit=2000), "Read spill file with a big limit",
       env=dict(ENV, HOME=WORK, USERPROFILE=WORK))
expect("DENY", read(UNI_BIG), "Read big file under a non-ASCII path, sent as raw UTF-8")
expect("DENY", sh("Bash", f"cat {fwd}"), "bash cat big")
expect("DENY", sh("Bash", f"cd /tmp && cat '{BIG_BS}'"), "bash cat big, backslash path after &&")
expect("ALLOW", sh("Bash", f"cat {fwd} | head -50"), "bash cat big | head")
expect("DENY", sh("Bash", f"cat {fwd}; sed -n 1,80p {fwd}"), "a narrowed command elsewhere doesn't excuse a bare cat")
# Claude Code's standard commit form: the heredoc body is a message, not commands (regression hunt: HIGH)
commit = f"git commit -m \"$(cat <<'EOF'\nFix type errors\ntype {fwd} was wrong\ncat {fwd} too\n\nCo-Authored-By: x\nEOF\n)\""
expect("ALLOW", sh("Bash", commit), "heredoc commit message mentioning 'type <big file>' is not a dump")
expect("ALLOW", sh("Bash", f"gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\ncat {fwd}\nEOF\n)\""),
       "heredoc PR body is not a dump")
expect("ALLOW", sh("Bash", f"cat {fwd} \\\n  | head -5"), "line continuation: cat big \\ | head")
expect("ALLOW", sh("Bash", f"cat {fwd} |\n  head -5"), "trailing pipe continues onto the next line")
expect("DENY", sh("Bash", f"echo x | cat {fwd}"), "a later pipeline stage that dumps the file")
expect("ALLOW", sh("Bash", f"grep -n type {fwd}"), "'type' as an argument is not a dump command")
UTF16 = os.path.join(files, "ps_output.txt")                  # PowerShell 5.1 '>' writes UTF-16LE with a BOM
with open(UTF16, "wb") as f:
    f.write("﻿".encode("utf-16-le") + ("log line of text\r\n" * 900).encode("utf-16-le"))
expect("DENY", read(UTF16), "large UTF-16 text file is text, not binary")
if os.name == "nt":
    gitbash = "/" + fwd[0].lower() + fwd[2:]                  # C:/x/big.py -> /c/x/big.py
    expect("DENY", sh("Bash", f"cat {gitbash}"), "bash cat big via a Git Bash /c/... path")
NUL = os.path.join(files, "data.dat")
with open(NUL, "wb") as f:
    f.write(b"\0\1\2" + b"x\n" * 5000)
expect("ALLOW", read(NUL), "file with NUL bytes treated as binary")
HUGE = os.path.join(files, "huge.log")
with open(HUGE, "wb") as f:
    f.write((b"2026-01-01 INFO line of a very large log file\n") * 500_000)   # ~23MB
import time as _t
_t0 = _t.time()
huge_out = expect("DENY", read(HUGE), "a 23MB file is denied")
results.append((_t.time() - _t0 < 8 and "no outline" in huge_out, f"...quickly and without an outline ({_t.time() - _t0:.1f}s)"))
expect("ALLOW", sh("Bash", "ls -la && git status"), "bash unrelated")
expect("DENY", sh("PowerShell", f"Get-Content {fwd}"), "PS Get-Content big")
expect("ALLOW", sh("PowerShell", f"Get-Content {fwd} -TotalCount 40"), "PS Get-Content -TotalCount")
# The PowerShell equivalents of grep/awk/wc filter just as much as the Bash ones, which were already
# allowed. A real benchmark run lost six turns to the first of these being refused.
expect("ALLOW", sh("PowerShell", f"$e = Get-Content {fwd} | Where-Object {{ $_ -match ' ERROR ' }}"),
       "PS Get-Content | Where-Object, assigned to a variable")
expect("ALLOW", sh("PowerShell", f"Get-Content {fwd} | ? {{ $_ -match 'x' }}"), "PS Get-Content | ? {} alias")
expect("ALLOW", sh("PowerShell", f"Get-Content {fwd} | Measure-Object -Line"), "PS Get-Content | Measure-Object")
expect("ALLOW", sh("PowerShell", f"Get-Content {fwd} | % {{ $_.Trim() }} | Group-Object"), "PS Get-Content | % {} | Group-Object")
expect("DENY", sh("PowerShell", f"Get-Content {fwd} | Out-String"), "PS Get-Content | Out-String still dumps")
expect("ALLOW", sh("PowerShell", f"type {SMALL}"), "PS type small")
expect("ALLOW", "not json", "garbage stdin")
expect("ALLOW", json.dumps([1, 2]), "valid JSON that is not an object")

# paging budget: per (session, agent)
paging = [
    ("ALLOW", "S", None, 1, 300, "main agent: page 1-300 (300 used)"),
    ("ALLOW", "S", None, 1, 300, "main agent: re-read 1-300 is free"),
    ("ALLOW", "S", None, 301, 300, "main agent: page 301-600 (600 = budget)"),
    ("DENY", "S", None, 601, 100, "main agent: page 601-700 (700 > 600) denied"),
    ("ALLOW", "S", None, 100, 50, "main agent: re-read after the deny is free"),
    ("ALLOW", "S", "agent-1", 601, 100, "subagent in the same session has its own budget"),
    ("ALLOW", "S", None, 5000, 100, "read past EOF allowed and not counted"),
    ("ALLOW", "T", None, 601, 100, "another session has its own budget"),
]
for want, sess, agent, off, lim, name in paging:
    p = {**read(BIG, offset=off, limit=lim), "session_id": sess}
    if agent:
        p["agent_id"] = agent
    expect(want, p, name)
raw = run({**read(BIG, offset=601, limit=100), "session_id": "S"})[1]
deny_msg = json.loads(raw)["hookSpecificOutput"]["permissionDecisionReason"] if raw else ""
results.append(("paging budget" in deny_msg and "Re-reading ranges" in deny_msg and os.path.join(WORK, "ENFORCE_OFF") in deny_msg,
                "paging deny explains the budget, free re-reads, and the real kill-switch path"))

# 8 hooks at once on one agent's state: no lost updates, no corrupt JSON
with ThreadPoolExecutor(8) as pool:
    list(pool.map(lambda k: run({**read(BIG, offset=1 + k * 10, limit=10), "session_id": "R"}), range(8)))
state_file = [f for f in os.listdir(os.path.join(WORK, "state")) if f.startswith("reads-R-")]
try:
    ranges = json.load(open(os.path.join(WORK, "state", state_file[0])))
    covered = sum(b - a + 1 for rs in ranges.values() for a, b in rs)
    ok = covered == 80
except Exception as e:
    ok, covered = False, repr(e)
results.append((ok, f"8 parallel hooks: state intact, all 80 lines recorded (got {covered})"))

_, out, _ = run(read(BIG))
reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else ""
results.append(("outline:" in reason and "L1: def fn_0():" in reason, "deny message includes an outline with real line numbers"))

# The exact command install.py writes, run from a project dir that contains a hostile json.py: in
# isolated mode (-I) the project's json.py must never be imported, and the deny must still happen.
sys.path.insert(0, HERE)
import install  # noqa: E402
hook = install.hook_command()
proj = os.path.join(WORK, "hostile_project")
os.makedirs(proj)
marker = os.path.join(WORK, "IMPORTED.txt")
for mod in ("json", "re", "types", "enum", "socket"):
    with open(os.path.join(proj, mod + ".py"), "w") as f:
        f.write(f"open({marker!r}, 'a').write('{mod} ')\n")
guard = [a.replace(repr(install.HOOK), repr(HOOK)) for a in hook["args"]]
r = subprocess.run([hook["command"]] + guard, input=json.dumps(read(BIG)).encode(), capture_output=True,
                   cwd=proj, env=ENV, timeout=30)
results.append(('"deny"' in r.stdout.decode() and not os.path.exists(marker),
                "installed hook command, run from a project with a hostile json.py: still denies, imports nothing"))
gone = [a.replace(repr(install.HOOK), repr(os.path.join(WORK, "moved", "enforce.py"))) for a in hook["args"]]
r = subprocess.run([hook["command"]] + gone, input=json.dumps(read(BIG)).encode(), capture_output=True, env=ENV, timeout=30)
results.append((r.returncode == 0 and not r.stdout.strip(), "installed hook command with enforce.py moved away: exit 0, allows"))

with open(os.path.join(WORK, "config.json"), "w") as f:
    json.dump({"exempt_dirs": "~"}, f)                        # a string, not a list: must be ignored
# HOME points at WORK so that, if the string were iterated, '~' really would cover BIG on every OS.
expect("DENY", read(BIG), "config exempt_dirs given as a string does not exempt anything",
       env=dict(ENV, HOME=WORK, USERPROFILE=WORK))
root = os.path.abspath(os.sep) if os.name != "nt" else os.path.splitdrive(BIG)[0] + os.sep
with open(os.path.join(WORK, "config.json"), "w") as f:
    json.dump({"exempt_dirs": [root]}, f)
expect("ALLOW", read(BIG), f"a filesystem root ({root}) in exempt_dirs covers everything under it")
os.remove(os.path.join(WORK, "config.json"))

down = {k: v for k, v in ENV.items() if k != "LOCAL_HELPER_ASSUME_OLLAMA_UP"}
down["LOCAL_HELPER_OLLAMA"] = "http://127.0.0.1:9"     # nothing listens there
expect("ALLOW", read(BIG), "Ollama down -> allow everything", env=down)

open(os.path.join(WORK, "ENFORCE_OFF"), "w").close()
expect("ALLOW", read(BIG), "kill switch ENFORCE_OFF allows everything")

shutil.rmtree(WORK, ignore_errors=True)
for ok, name in results:
    print(("PASS " if ok else "FAIL ") + name)
fails = sum(not ok for ok, _ in results)
print(f"\n{len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
