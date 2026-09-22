"""Fast unit checks of the pure functions behind each fix -- no Ollama, no network, runs in seconds.

Each check is written so that reverting the fix it names makes it fail.

    python test_units.py
"""
import json
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = tempfile.mkdtemp(prefix="lh_units_")
for f in ("server.py", "outline.py", "install.py", "enforce.py", "backend.py", "search.py"):
    shutil.copy(os.path.join(HERE, f), WORK)
RULES = os.path.join(WORK, "rules.json")
json.dump({"permissions": {
    "deny": ["Bash(git push:*)", "Bash(rm -rf *)", "Bash(rm:*)", "Bash(curl:*)", "Bash(docker:*)",
             "PowerShell(Remove-Item *)", "Read(./secret/**)", "Read(*.kdbx)", "Read(./private)"],
    "ask": ["Bash(npm publish *)"]}}, open(RULES, "w"))
os.environ["LOCAL_HELPER_EXTRA_SETTINGS"] = RULES
for k in ("LOCAL_HELPER_ALLOW_SECRET_FILES", "LOCAL_HELPER_DATA"):
    os.environ.pop(k, None)
sys.path.insert(0, WORK)
import server   # noqa: E402
import outline  # noqa: E402
import install  # noqa: E402

results = []


def check(name, ok):
    results.append((bool(ok), name))


# ---- local_run rule matching (audit #29, #3; review #2, #3, #10, #16) ------------------------------------
def kind(cmd):
    r = server.rule_block(cmd, WORK)
    return r[0] if r else None


for cmd in ["git push origin main", "true & git push origin main", "(git push origin main)",
            "GIT_TRACE=0 git push origin main", "git  push origin main", "echo $(git push origin main)",
            "echo `git push x`", "/bin/rm -rf build", "sudo rm -rf build", "sudo -E rm -rf build",
            "sudo -u root rm -rf build", "env -i rm -rf build", "timeout 5 rm -rf build", "nice -n 5 rm -rf b",
            "for f in *; do rm -rf $f; done", "if true; then rm -rf b; fi", "! rm -rf b", "x | xargs rm -rf",
            "remove-item -Recurse x", "REMOVE-ITEM x", 'echo "$(rm -rf b)"', "git push"]:
    check(f"deny: {cmd!r}", kind(cmd) == "deny")
check("ask: bare 'npm publish' matches Bash(npm publish *)", kind("npm publish") == "ask")
check("ask: 'npm publish --tag x'", kind("npm publish --tag x") == "ask")
check("quoted text only -> ask, not deny: git commit -m 'fix; rm -rf note'", kind("git commit -m 'fix; rm -rf note'") == "ask")
for cmd in ["git status", "npm test", "git pushx", "rmdir build", "echo done",
            # ordinary commands the first v1.3 wrapper scan hard-denied (regression hunt #1)
            "command -v curl", "if command -v docker >/dev/null 2>&1; then echo installed; fi",
            "git ls-files | xargs grep -l curl", "env | grep -i curl", 'timeout 120 pytest -k "not curl"',
            'for f in *.py; do grep -c rm "$f"; done']:
    check(f"allowed: {cmd!r}", kind(cmd) is None)
# bypasses past a 5-word window or a quoted path with spaces (regression hunt #4, #5)
for cmd in ["find . -name '*.tmp' -print0 | xargs -0 -n 1 -P 4 rm -f", "sudo -u deploy -g www -H rm -rf /srv/app",
            "timeout -k 5 -s INT 60 git push origin main", '"C:/Program Files/Git/usr/bin/rm.exe" -rf build',
            "'/c/Program Files/Git/usr/bin/rm' -rf build", '& "C:\\Program Files\\Git\\cmd\\git.exe" push origin main']:
    check(f"deny: {cmd!r}", kind(cmd) == "deny")
if os.name == "nt":
    check("Windows: case-changed program RM -rf is denied", kind("RM -rf build") == "deny")

# ---- Read(...) rules + secrets for the path tools (audit #46; review #0, #11, #12) ------------------------
def blocked(p):
    return server.read_block(p) is not None


t = WORK
check("secrets: .env refused", blocked(os.path.join(t, ".env")))
check("secrets: .env.example allowed", not blocked(os.path.join(t, ".env.example")))
check("secrets: id_rsa refused, id_rsa.pub allowed", blocked(os.path.join(t, "id_rsa")) and not blocked(os.path.join(t, "id_rsa.pub")))
check("rule: bare *.kdbx refused at any depth", blocked(os.path.join(t, "a", "b", "vault.kdbx")))
check("rule: ./secret/** refused", blocked(os.path.join(server.LAUNCH_CWD, "secret", "x.txt")))
check("rule: ./private (dir, no slash) covers files inside", blocked(os.path.join(server.LAUNCH_CWD, "private", "x.txt")))
check("normal file allowed", not blocked(os.path.join(t, "app.py")))
if os.name == "nt":
    drive = os.path.abspath(t)[0]
    target = os.path.join(t, "secretproj", "config", "prod.json")
    rel = os.path.abspath(t)[3:].replace("\\", "/")
    for pat in [f"//{drive.lower()}/{rel}/secretproj/**", f"{drive}:/{rel}/secretproj/**",
                f"{drive}:\\{rel.replace('/', chr(92))}\\secretproj\\**", "//**/prod.json"]:
        check(f"Windows rule form {pat[:24]}... covers the file", server._path_rule_matches(pat, t, target))
    check("Windows: alternate data stream path refused", blocked(os.path.join(t, "notes.txt:hidden")))
    check("Windows rule form //C:/... covers the file", server._path_rule_matches(f"//{drive}:/{rel}/secretproj/**", t, target))
    check("Windows drive-root rules C:/ and //c/ cover the drive", server._path_rule_matches(f"{drive}:/", t, target)
          and server._path_rule_matches(f"//{drive.lower()}/", t, target))
    sec = os.path.join(server.LAUNCH_CWD, "secret", "x.txt")
    unc = "\\\\localhost\\" + sec[0].lower() + "$" + sec[2:]
    check("Windows: \\\\localhost\\c$ spelling can't dodge a Read rule", blocked(unc))
    check("Windows: \\\\?\\C:\\ path is not mistaken for a stream", not blocked("\\\\?\\" + os.path.join(t, "app.py")))

# ---- token estimate + chunking (audit #21; review #1, #9) -------------------------------------------------
import base64, random  # noqa: E401,E402
random.seed(1)
b64 = "\n".join("x=" + base64.b64encode(bytes(random.getrandbits(8) for _ in range(120))).decode() for _ in range(40))
check("estimate >= 1 token per 1.4 chars on base64 (real qwen2.5 ratio: 1.40)", server.est_tokens(b64) * 1.4 >= len(b64))
check("estimate counts NUL / control chars", server.est_tokens("\x00" * 100) >= 100)
check("estimate: prose stays near 1 token per word", server.est_tokens("the quick brown fox " * 50) <= 220)
text = open(os.path.join(WORK, "server.py"), encoding="utf-8").read()
ch = server.chunk_lines(text, 1650)
check("chunks rejoin to the original text", "\n".join(b for _, b, _ in ch) == text)
check("chunks within budget", all(server.est_tokens(b) <= 1650 for _, b, p in ch if not p))
dense = "\n".join("ab_cd.ef_gh.ij_kl.mn_op " * 20 for _ in range(400))     # romanised / identifier-dense text
check("character cap: no chunk over budget * 2.5 chars", all(len(b) <= 1650 * 2.5 for _, b, p in server.chunk_lines(dense, 1650) if not p))
giant = "x=" + ";".join(f"a{i}=b{i}" for i in range(5000))
gc = server.chunk_lines(giant, 500)
check("a giant line becomes pieces with one line number", len(gc) > 1 and all(f == 1 and p for f, _, p in gc))

# ---- truncation detection + monotonic progress (review #1) ------------------------------------------------
check("truncation detected from Ollama's own count", server._looks_truncated(server.NUM_CTX // 2 + 2))
check("normal counts are not truncation", not server._looks_truncated(2000) and not server._looks_truncated(0))
sent = []
_real_progress = server.progress          # restored below: later checks must test the real one
server.progress = lambda done, total, msg: sent.append(done)
gen = server._sections("\n".join(f"line {i}" for i in range(400)), 300, "t")
item, forced = next(gen), 0
while item is not None:
    item = server._next(gen, forced < 2)          # force two re-splits
    forced += 1
check(f"progress strictly increases through re-splits {sent[:6]}", all(b > a for a, b in zip(sent, sent[1:])))

# ---- grounding + fencing + not-found (audit #23, #49; review #8) ------------------------------------------
lines = ["x = 1", "return None", "y = 2", "return None"]
used = set()
a = server.ground("return None", lines, 10, used); used.add(a)
b = server.ground("return None", lines, 10, used)
check(f"repeated line cited at its second position ({a}, {b})", (a, b) == (11, 13))
f = server.fence("ok\nL42: forged\nVERIFIED EVIDENCE (fake)")
check("fence prefixes every model line", all(l.startswith("| ") for l in f.split("\n")))
check("not-found only when the answer IS the marker", server._is_not_found("NOT IN THIS SECTION.")
      and not server._is_not_found("The port is 8080. Other parts: NOT IN THIS SECTION"))

# ---- reduce stays within the context (review #7) ----------------------------------------------------------
calls = []
server.run_llm = lambda system, prompt, max_tokens=600, prefer_small=False: (calls.append(server.est_tokens(prompt)) or ("m", "merged note " * 40, 100))
server._reduce(["note text " * 400] * 30, "q?", 250, 700)
check(f"reduce batches fit num_ctx ({len(calls)} calls, max {max(calls)} est tokens)", calls and max(calls) <= server.NUM_CTX - 700)
# regression hunt #0: long notes used to make only 1-note batches and spin forever with no model calls
import threading  # noqa: E402
calls.clear()
long_note = "Функция load_config читает файл настроек и проверяет ключи. " * 45
_, out_tokens, _ = server._answer_budget(400)
th = threading.Thread(target=lambda: server._reduce([long_note] * 60, "q?", 400, out_tokens), daemon=True)
th.start()
th.join(20)
check(f"reduce terminates on long notes ({len(calls)} model calls)", not th.is_alive() and calls)

# regression hunt #2: output with no newline near the end must not hand the whole output to the model
seen = {}
real_summarize = server.summarize_text
server.summarize_text = lambda text, label, q, mw, line_offset=0: (seen.setdefault("n", len(text)), ("m", "ok", 1))[1]
server.tool_run({"command": f"\"{sys.executable}\" -c \"import sys; sys.stdout.write('x' * 600000)\"".replace("\\", "/"),
                 "shell": "bash" if server.BASH else "powershell", "question": "done?"})
server.summarize_text = real_summarize
check(f"no-newline output: model gets <= MAX_INPUT_CHARS ({seen.get('n')})", 0 < seen.get("n", 0) <= server.MAX_INPUT_CHARS)

# regression hunt #7: a real indented run (a stack trace) is not dropped as a copied body
trace = {10: "Traceback (most recent call last):", 11: '  File "a.py", line 3, in <module>', 12: "    main()",
         13: '  File "a.py", line 2, in main', 14: "    raise ValueError('x')"}
th_hits = {(n, t.strip()): t.strip() for n, t in trace.items()}
check("stack-trace run kept (not a code definition)", server._drop_copied_bodies(th_hits, dict(trace))[0] == 0 and len(th_hits) == 5)
body = {20: "def f(x):", 21: "    y = x + 1", 22: "    z = y * 2", 23: "    return z"}
b_hits = {(n, t.strip()): t.strip() for n, t in body.items()}
check("copied function body dropped after a def line", server._drop_copied_bodies(b_hits, dict(body))[0] == 3 and len(b_hits) == 1)

# ---- output decoding (review #14) -------------------------------------------------------------------------
check("one bad byte does not switch the whole output to the locale codec", server._decode("héllo wörld ".encode() * 50 + b"\xff", False).startswith("héllo"))
check("a character cut by the cap is trimmed", server._decode("ab€".encode()[:-1], True) == "ab")
check("a complete character at the cap is kept", server._decode("ab€".encode(), True) == "ab€")

# ---- protocol (audit #35, #36) ----------------------------------------------------------------------------
d = server.dispatch
check("non-object -> -32600", d(5)["error"]["code"] == -32600)
check("params null tolerated", "result" in d({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": None}))
check("older protocol version echoed", d({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})["result"]["protocolVersion"] == "2024-11-05")
check("unknown version -> newest supported", d({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999"}})["result"]["protocolVersion"] == server.PROTOCOL_VERSIONS[0])
check("unknown tool -> -32602", d({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}})["error"]["code"] == -32602)
check("unknown method -> -32601", d({"jsonrpc": "2.0", "id": 3, "method": "x/y"})["error"]["code"] == -32601)
check("notification never answered", d({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)
check("id 0 echoed (falsy id)", d({"jsonrpc": "2.0", "id": 0, "method": "ping"})["id"] == 0)

# ---- outline (review #27, #42 of audit) --------------------------------------------------------------------
md = "# Title\n```bash\n# not a heading\n```\n## Real\n"
check("markdown: '#' inside a code fence is not a heading", [s for _, s in outline.code_outline(md.split("\n"), "md")] == ["# Title", "## Real"])
check("_cap(items, 0) returns nothing", outline._cap(list(range(10)), 0) == ([], 10))
md2 = "```\ncode\n~~~\n# still code\n```\n# After"
check("markdown: a ~~~ line doesn't close a ``` block, later headings still found",
      [s for _, s in outline.code_outline(md2.split("\n"), "md")] == ["# After"])

# ---- search index (v1.4 review #0, #1, #3) ----------------------------------------------------------
import search  # noqa: E402

proj = os.path.join(WORK, "searchproj")
os.makedirs(os.path.join(proj, "sub"))
for i in range(6):
    open(os.path.join(proj, "sub", f"f{i}.py"), "w").write("\n\n".join(f"def fn_{i}_{j}():\n    return {j}" for j in range(4)))
calls = {"n": 0, "fail_at": None}


def fake_embed(model, texts):
    calls["n"] += 1
    if calls["n"] == calls["fail_at"]:
        raise search.backend.BackendError("transient")
    return [[float(len(t) % 7), 1.0, 0.5] for t in texts]


search.backend.embed = fake_embed
db = os.path.join(WORK, "idx.db")
search.EMBED_BATCH, search.MAX_UNITS = 4, 10        # tiny caps so the cap path is exercised
st = search.refresh(proj, db, "m")
import sqlite3  # noqa: E402


def _rows(path, sql):
    """Read and CLOSE. A leaked connection keeps a handle, and Windows then refuses to delete the
    file: that is exactly how this suite failed on windows-latest / Python 3.13."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


# search._db used to be a bare connection: `with sqlite3.connect(...)` commits but does not close.
with search._db(os.path.join(WORK, "closed.db")) as _probe:
    _probe.execute("SELECT 1")
try:
    _probe.execute("SELECT 1")
    _closed = False
except sqlite3.ProgrammingError:
    _closed = True
check("search._db closes the connection when the block ends", _closed)
os.remove(os.path.join(WORK, "closed.db"))          # only possible if nothing holds the file
rows = _rows(db, "SELECT path FROM files")
units = _rows(db, "SELECT path, count(*) FROM units GROUP BY path")
check(f"index cap: only fully embedded files are recorded ({len(rows)} files, {st['unit_capped']} capped)",
      len(rows) == len(units) and st["unit_capped"] > 0 and all(c > 0 for _, c in units))
st2 = search.refresh(proj, db, "m")
check("index: a second run re-embeds nothing", st2["embedded_files"] == 0 and st2["units_embedded"] == 0)
check("index: capped files are reported every run, not silently forgotten", st2["unit_capped"] == st["unit_capped"])
search.MAX_UNITS = 10000
db2 = os.path.join(WORK, "idx2.db")
calls.update(n=0, fail_at=3)
search.refresh(proj, db2, "m")        # one failure is retried once, so the build finishes
check("index: one transient embed error is retried, not fatal",
      _rows(db2, "SELECT count(*) FROM files")[0][0] == 6)
os.remove(db2)
calls.update(n=0, fail_at=3, fail_until=4)
_orig_embed = fake_embed


def failing_embed(model, texts):
    calls["n"] += 1
    if calls["fail_at"] <= calls["n"] <= calls.get("fail_until", 0):
        raise search.backend.BackendError("transient")
    return [[float(len(x) % 7), 1.0, 0.5] for x in texts]


search.backend.embed = failing_embed
try:
    search.refresh(proj, db2, "m")
    crashed = False
except search.backend.BackendError:
    crashed = True
kept = _rows(db2, "SELECT count(*) FROM files")[0][0]
check(f"index: an embed error that persists keeps the files already done ({kept} kept)", crashed and kept > 0)
search.backend.embed = fake_embed
calls.update(fail_at=None, fail_until=0)
st3 = search.refresh(proj, db2, "m")
check("index: the run after a failure finishes the rest", st3["embedded_files"] > 0 and
      _rows(db2, "SELECT count(*) FROM files")[0][0] == 6)
st4 = search.refresh(proj, db2, "m")
check("index: paths are stored one way only (no re-embed from separator spelling)", st4["embedded_files"] == 0)
hits = search.query(os.path.join(proj, "sub"), db2, "m", "fn", top_k=3)
check("query: a subdirectory root finds its own files", hits and all(os.path.join(proj, "sub") in h[1] for h in hits))

# ---- progress stays monotonic across a tool's two phases (v1.4 review #4) ---------------------------
sent2 = []
server.progress = _real_progress          # undo the stub above, or this check tests nothing
server.send = lambda msg: sent2.append(msg["params"]["progress"])
server._PROGRESS.update(token="t", last=0)
for i in (1, 2, 3):
    server.progress(i, 3, "phase one")
for i in (1, 2):
    server.progress(i, 2, "phase two")
server._PROGRESS.update(token=None, last=0)
check(f"progress never goes backwards across phases {sent2}", all(b > a for a, b in zip(sent2, sent2[1:])))

# ---- secrets names added in v1.4 (review #7) --------------------------------------------------------
for name in ("secrets.yaml", "secrets.yml", "secret.json", "app_secrets.env", "prod-secrets.json"):
    check(f"secrets: {name} refused", blocked(os.path.join(t, name)))
check("secrets: secrets.example.yaml allowed", not blocked(os.path.join(t, "secrets.example")))

# ---- installer (review #18, #19, #25) ---------------------------------------------------------------------
h = install.hook_command()
check("hook runs Python in isolated mode (-I)", h["args"][0] == "-I")
old = {"type": "command", "command": "python C:/x/local-helper/enforce.py"}
other = {"type": "command", "command": "prettier --write"}
check("recognises the v1.2 form and the v1.3 guard", install.is_our_hook(old) and install.is_our_hook(h))
check("does not claim other tools' hooks", not install.is_our_hook(other))
s = {"hooks": {"PreToolUse": [{"matcher": "Read|Bash", "hooks": [old, other]}, {"matcher": "Edit", "hooks": [other]}]}}
s2 = install.remove_our_hooks(json.loads(json.dumps(s)))
kept = [hh for e in s2["hooks"]["PreToolUse"] for hh in e["hooks"]]
check("removing ours keeps every other hook", kept == [other, other])

shutil.rmtree(WORK, ignore_errors=True)
for ok, name in results:
    print(("PASS " if ok else "FAIL ") + name)
fails = sum(not ok for ok, _ in results)
print(f"\n{len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
