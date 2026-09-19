"""Drive server.py over real MCP stdio and check answers against known ground truth.

Ground truth comes from a real file (Prometheus core/llm.py), computed here with regex,
so the test catches the local model hallucinating rather than just "it returned text".
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.expanduser(r"~\prometheus\core\llm.py")

truth_fns = re.findall(r"^def (\w+)", open(TARGET, encoding="utf-8").read(), re.M)

msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    "this is not json",
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "local_summarize", "arguments": {
        "path": TARGET, "max_words": 120,
        "question": "How does this code detect that the Claude account usage limit has been hit? Name the function and its line."}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "local_extract", "arguments": {
        "path": TARGET, "what": "top-level function definitions (lines starting with 'def ' at column 0); give the function name"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "local_classify", "arguments": {
        "items": ["tests/test_login.py", "README.md", "src/auth.py", "package-lock.json", "docs/setup.md"],
        "labels": ["code", "test", "docs", "generated"]}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "local_summarize", "arguments": {"path": "C:/nope.txt"}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "local_stats", "arguments": {}}},
]
stdin = "\n".join(m if isinstance(m, str) else json.dumps(m) for m in msgs) + "\n"

t0 = time.time()
out = subprocess.run([sys.executable, os.path.join(HERE, "server.py")], input=stdin,
                     capture_output=True, text=True, timeout=900)
print(f"server ran {time.time() - t0:.0f}s, exit {out.returncode}")
if out.stderr.strip():
    print("STDERR:", out.stderr)

res = {}
for line in out.stdout.splitlines():
    m = json.loads(line)
    res[m.get("id")] = m

checks = []
checks.append(("parse error answered, server survived", None in res and 7 in res))
checks.append(("5 tools listed", len(res[2]["result"]["tools"]) == 5))

s = res[3]["result"]["content"][0]["text"]
print("\n--- summarize:\n" + s)
checks.append(("summarize names _is_provider_refusal", "_is_provider_refusal" in s))

e = res[4]["result"]["content"][0]["text"]
print("\n--- extract:\n" + e)
found = {f for f in truth_fns if re.search(rf"\b{re.escape(f)}\b", e)}
print(f"recall {len(found)}/{len(truth_fns)}; missed: {sorted(set(truth_fns) - found)}")
# Regression floor. Measured 2026-09-19: 15/28 on both 3B and 7B turned out to be grounding dropping bare
# names, not the model; see ground(). After the fix: 23/28 (82%) on the 7B. Precision must hold regardless.
checks.append((f"extract recall >= 75% ({len(found)}/{len(truth_fns)})", len(found) >= 0.75 * len(truth_fns)))
src = open(TARGET, encoding="utf-8").read().split("\n")
cited = [(int(n), t) for n, t in re.findall(r"^L(\d+): (.*)$", e, re.M)]
checks.append((f"every cited line number is real ({len(cited)} cited)",
               all(src[n - 1].strip().startswith(t[:40].strip()) for n, t in cited)))

c = res[5]["result"]["content"][0]["text"]
print("\n--- classify:\n" + c)
want = {0: "test", 1: "docs", 2: "code", 3: "generated", 4: "docs"}
got = dict((int(a), b.strip().lower()) for a, b in re.findall(r"^(\d+):\s*(\w+)", c, re.M))
right = sum(got.get(k) == v for k, v in want.items())
checks.append((f"classify {right}/5 correct", right >= 4))

checks.append(("missing file -> clean isError", res[6]["result"].get("isError") is True))
print("\n--- stats:\n" + res[7]["result"]["content"][0]["text"])

print()
for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)
sys.exit(0 if all(ok for _, ok in checks) else 1)
