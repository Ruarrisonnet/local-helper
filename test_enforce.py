"""Pipe synthetic hook payloads through enforce.py and check each allow/deny decision."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "enforce.py")
tmp = tempfile.mkdtemp(prefix="lh_enforce_")
BIG = os.path.join(tmp, "big.py").replace("\\", "/")
SMALL = os.path.join(tmp, "small.py").replace("\\", "/")
with open(BIG, "w") as f:
    for i in range(700):
        f.write(f"def fn_{i}():\n    return {i}\n" if i % 10 == 0 else f"x_{i} = {i}\n")
with open(SMALL, "w") as f:
    f.write("print('hi')\n" * 300)
SKILL_DIR = os.path.join(os.path.expanduser("~"), ".claude", "skills", "_enforce_probe")
os.makedirs(SKILL_DIR, exist_ok=True)
EXEMPT = os.path.join(SKILL_DIR, "ref.md").replace("\\", "/")
with open(EXEMPT, "w") as f:
    f.write("skill reference line\n" * 600)
BIG_BS = BIG.replace("/", "\\\\")


def run(payload):
    r = subprocess.run([sys.executable, HOOK], input=payload if isinstance(payload, str) else json.dumps(payload),
                       capture_output=True, text=True, timeout=20)
    return ("DENY" if '"deny"' in r.stdout else "ALLOW"), r.stdout


def read(path, **kw):
    return {"tool_name": "Read", "tool_input": {"file_path": path, **kw}}


def sh(tool, cmd):
    return {"tool_name": tool, "tool_input": {"command": cmd}}


cases = [
    ("DENY", read(BIG), "Read big, no limit"),
    ("ALLOW", read(BIG, offset=100, limit=20), "Read big, window of 20"),
    ("DENY", read(BIG, limit=2000), "Read big, limit 2000 (dodge)"),
    ("ALLOW", read(SMALL), "Read small file"),
    ("ALLOW", read(EXEMPT), "Read big file under ~/.claude/skills (exempt)"),
    ("ALLOW", read("C:/nope/missing.txt"), "Read missing file"),
    ("DENY", sh("Bash", f"cat {BIG}"), "bash cat big"),
    ("DENY", sh("Bash", f"cd /tmp && cat '{BIG_BS}'"), "bash cat big, backslash path after &&"),
    ("ALLOW", sh("Bash", f"cat {BIG} | head -50"), "bash cat big | head"),
    ("ALLOW", sh("Bash", f"sed -n 1,80p {BIG}"), "bash sed -n window"),
    ("ALLOW", sh("Bash", "ls -la && git status"), "bash unrelated"),
    ("DENY", sh("PowerShell", f"Get-Content {BIG}"), "PS Get-Content big"),
    ("ALLOW", sh("PowerShell", f"Get-Content {BIG} -TotalCount 40"), "PS Get-Content -TotalCount"),
    ("ALLOW", sh("PowerShell", f"type {SMALL}"), "PS type small"),
    ("ALLOW", "not json", "garbage stdin"),
]
fails = 0
for want, payload, name in cases:
    got, _ = run(payload)
    fails += got != want
    print(f"{'PASS' if got == want else 'FAIL'} want {want:5} got {got:5} {name}")

# v1.1: the deny message carries an exact outline of the file
_, out = run(read(BIG))
reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else ""
ok = "outline:" in reason and "L1: def fn_0():" in reason and "L" in reason
fails += not ok
print(f"{'PASS' if ok else 'FAIL'} deny message includes an outline with real line numbers")

open(os.path.join(HERE, "ENFORCE_OFF"), "w").close()
got, _ = run(read(BIG))
os.remove(os.path.join(HERE, "ENFORCE_OFF"))
fails += got != "ALLOW"
print(f"{'PASS' if got == 'ALLOW' else 'FAIL'} kill switch ENFORCE_OFF allows everything")

import shutil
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(SKILL_DIR, ignore_errors=True)
print(f"\n{len(cases) + 2 - fails}/{len(cases) + 2} passed")
sys.exit(1 if fails else 0)
