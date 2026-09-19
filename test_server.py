"""Drive server.py over real MCP stdio and check answers against known ground truth.

Ground truth comes from a real file (Prometheus core/llm.py), computed here with regex,
so the test catches the local model hallucinating rather than just "it returned text".
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.expanduser(r"~\prometheus\core\llm.py")
NONCE = uuid.uuid4().hex[:8]
# Fresh throwaway cache per run. (v1.1 finding: putting a nonce INTO the question to dodge the cache
# corrupted the query itself -- the model looked for "(probe3)" and extract returned 0 lines.)
os.environ["LOCAL_HELPER_CACHE_DB"] = os.path.join(tempfile.gettempdir(), f"lh_cache_{NONCE}.db")

src_text = open(TARGET, encoding="utf-8").read()
src = src_text.split("\n")
truth_fns = re.findall(r"^def (\w+)", src_text, re.M)

# A fake noisy test run: 3000 ok lines, a WARNING every 75 iterations with varying numbers (40 total),
# one distinct failure at the end, exit code 3. Written to a temp script so quoting is not an issue.
noisy = os.path.join(tempfile.gettempdir(), f"lh_noisy_{NONCE}.py")
with open(noisy, "w") as f:
    f.write("import sys\n"
            "for i in range(3000):\n"
            "    print(f'test_case_{i} ... ok')\n"
            "    if i % 75 == 0: print(f'WARNING: slow fixture took {i}ms on worker {i%7}')\n"
            "print('FAILED test_payment_refund - AssertionError: expected 200 got 500')\n"
            "sys.exit(3)\n")
PY = sys.executable.replace("\\", "/")
NOISY = noisy.replace("\\", "/")

summ_q = "How does this code detect that the Claude account usage limit has been hit? Name the function."
msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    "this is not json",
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "local_summarize", "arguments": {
        "path": TARGET, "max_words": 120, "question": summ_q}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "local_extract", "arguments": {
        "path": TARGET, "what": f"top-level function definitions (lines starting with 'def ' at column 0); give the function name"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "local_classify", "arguments": {
        "items": ["tests/test_login.py", "README.md", "src/auth.py", "package-lock.json", "docs/setup.md"],
        "labels": ["code", "test", "docs", "generated"]}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "local_summarize", "arguments": {"path": "C:/nope.txt"}}},
    # v1.1
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "local_outline", "arguments": {"path": TARGET}}},
    {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "local_summarize", "arguments": {
        "path": TARGET, "max_words": 120, "question": summ_q}}},            # same as id 3 -> must hit cache
    {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "local_run", "arguments": {
        "command": "echo hello-from-run && exit 0", "shell": "bash"}}},
    {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "local_run", "arguments": {
        "command": f"'{PY}' '{NOISY}'", "shell": "bash"}}},
    {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "local_run", "arguments": {
        "command": "Start-Sleep -Seconds 30", "shell": "powershell", "timeout": 3}}},
    {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "local_run", "arguments": {
        "command": f"'{PY}' '{NOISY}'", "shell": "bash", "question": "Which test failed and why?"}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "local_stats", "arguments": {}}},
]
stdin = "\n".join(m if isinstance(m, str) else json.dumps(m) for m in msgs) + "\n"

t0 = time.time()
out = subprocess.run([sys.executable, os.path.join(HERE, "server.py")], input=stdin,
                     capture_output=True, text=True, timeout=1200)
print(f"server ran {time.time() - t0:.0f}s, exit {out.returncode}")
if out.stderr.strip():
    print("STDERR:", out.stderr)
os.remove(noisy)
try:
    os.remove(os.environ["LOCAL_HELPER_CACHE_DB"])
except OSError:
    pass

res = {}
for line in out.stdout.splitlines():
    m = json.loads(line)
    res[m.get("id")] = m


def text(i):
    return res[i]["result"]["content"][0]["text"]


checks = []
init = res[1]["result"]
checks.append(("initialize: version 1.1.0 + instructions", init["serverInfo"]["version"] == "1.1.0"
               and "local_outline" in init.get("instructions", "")))
checks.append(("parse error answered, server survived", None in res and 7 in res))
tools = {t["name"]: t for t in res[2]["result"]["tools"]}
checks.append((f"7 tools listed ({len(tools)})", len(tools) == 7))
checks.append(("annotations: outline read-only, run destructive",
               tools["local_outline"]["annotations"]["readOnlyHint"] is True
               and tools["local_run"]["annotations"]["destructiveHint"] is True))

s = text(3)
print("\n--- summarize:\n" + s)
checks.append(("summarize names _is_provider_refusal", "_is_provider_refusal" in s))

e = text(4)
found = {f for f in truth_fns if re.search(rf"\b{re.escape(f)}\b", e)}
print(f"\n--- extract: recall {len(found)}/{len(truth_fns)}; missed: {sorted(set(truth_fns) - found)}")
# Regression floor. Measured 2026-09-19: 15/28 on both 3B and 7B turned out to be grounding dropping bare
# names, not the model; see ground(). After the fix: 23-25/28 on the 7B. Precision must hold regardless.
checks.append((f"extract recall >= 75% ({len(found)}/{len(truth_fns)})", len(found) >= 0.75 * len(truth_fns)))
cited = [(int(n), t) for n, t in re.findall(r"^L(\d+): (.*)$", e, re.M)]
checks.append((f"every extract line number is real ({len(cited)} cited)",
               all(src[n - 1].strip().startswith(t[:40].strip()) for n, t in cited)))
# Real is not the same as relevant: a cited line must be a def line or a continuation of one (multi-line signature).
def_lines = [i + 1 for i, ln in enumerate(src) if ln.startswith("def ")]
on_target = [n for n, _ in cited if any(0 <= n - d <= 3 for d in def_lines)]
checks.append((f"extract lines on target ({len(on_target)}/{len(cited)})", cited and len(on_target) >= 0.9 * len(cited)))

c = text(5)
want = {0: "test", 1: "docs", 2: "code", 3: "generated", 4: "docs"}
got = dict((int(a), b.strip().lower()) for a, b in re.findall(r"^(\d+):\s*(\w+)", c, re.M))
right = sum(got.get(k) == v for k, v in want.items())
checks.append((f"classify {right}/5 correct", right >= 4))
checks.append(("missing file -> clean isError", res[6]["result"].get("isError") is True))

o = text(8)
o_found = {f for f in truth_fns if re.search(rf"^L\d+: def {re.escape(f)}\b", o, re.M)}
o_cited = [(int(n), t) for n, t in re.findall(r"^L(\d+): (.*)$", o, re.M)]
checks.append((f"outline finds every top-level def ({len(o_found)}/{len(truth_fns)})", len(o_found) == len(truth_fns)))
checks.append(("outline line numbers all real", all(src[n - 1].rstrip().startswith(t[:40].rstrip().rstrip('.'))
                                                  for n, t in o_cited)))

s2 = text(9)
checks.append(("repeat summarize served from cache", "CACHED" in s2.split("\n")[0]))
checks.append(("cached answer identical to original", s2.split("\n", 1)[1] == s.split("\n", 1)[1]))

r1 = text(10)
checks.append(("run: short output verbatim + exit 0", "hello-from-run" in r1 and "exit 0" in r1 and "verbatim" in r1))

r2 = text(11)
print("\n--- local_run digest (noisy test run):\n" + r2[:1800])
log_m = re.search(r"full output saved: (.+?\.log)", r2)
checks.append(("run: exit code 3 propagated", "exit 3" in r2.split("\n")[0]))
checks.append(("run: failing test surfaced", "FAILED test_payment_refund" in r2))
checks.append(("run: 40 WARNING lines (i%75==0 over 3000) grouped to one shape with count", re.search(r"WARNING: slow fixture.*x40", r2) is not None))
checks.append(("run: full log saved with all 3000+ lines", bool(log_m) and os.path.exists(log_m.group(1))
               and open(log_m.group(1), encoding="utf-8").read().count("\n") >= 3000))
checks.append(("run: digest far smaller than output", len(r2) < 6000))

r3 = text(12)
checks.append(("run: timeout kills and reports", "TIMED OUT" in r3))

r4 = text(13)
print("\n--- local_run with question:\n" + r4[-700:])
checks.append(("run+question: model names the failing test", "test_payment_refund" in r4.split("-- answer from", 1)[-1]))

print("\n--- stats:\n" + text(7))
print()
for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)
sys.exit(0 if all(ok for _, ok in checks) else 1)
