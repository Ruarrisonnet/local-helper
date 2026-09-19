"""local-helper: an MCP server that lets Claude hand bulk reading to a local Ollama model.

Dependency-free (stdlib only) stdio JSON-RPC, so it runs on the system Python with no pip install.
The local model is a helper, never the decision-maker: every result is labelled unverified.
"""
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import outline

VERSION = "1.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
USAGE_LOG = os.path.join(HERE, "usage.jsonl")
CACHE_DB = os.environ.get("LOCAL_HELPER_CACHE_DB") or os.path.join(HERE, "cache.db")
CACHE_VERSION = "1.1"       # bump when prompts or grounding change, so stale answers are not served
RUNS_DIR = os.path.join(HERE, "runs")
KEEP_RUNS = 50
RUN_VERBATIM_CHARS = 6000  # command output this small is returned as-is
OLLAMA = os.environ.get("LOCAL_HELPER_OLLAMA", "http://127.0.0.1:11434")
BIG_MODEL = os.environ.get("LOCAL_HELPER_BIG", "qwen2.5-coder:7b-instruct-q3_K_M")
SMALL_MODEL = os.environ.get("LOCAL_HELPER_SMALL", "qwen2.5:3b")
# Measured 2026-09-19: with every layer forced onto the GPU (num_gpu=99) at 6k context the 7B uses
# 3.9-4.1GB VRAM and only ~0.7GB system RAM, at 2x the speed. Left to itself Ollama splits it
# 49/51 CPU/GPU and costs 2.2-2.5GB RAM. 8k context would overflow the 4GB card.
# Loading with too little free RAM has crashed process spawning here (0xC0000142), hence the guard.
MIN_FREE_GB_FOR_BIG = float(os.environ.get("LOCAL_HELPER_MIN_FREE_GB", "1.5"))
NUM_CTX = 6144
NUM_GPU_LAYERS = 99          # "all of them"; if VRAM is taken, the load fails and run_llm falls back to 3B
CHUNK_CHARS = 12000          # ~3.4k tokens of code, leaves room in a 6k context for prompt + answer
EXTRACT_CHUNK_CHARS = 4000   # measured: bigger chunks make small models skim (2/28 found at 14k chars)
MAX_INPUT_CHARS = 240000     # ~60k tokens; beyond this, Claude should grep first
REQUEST_TIMEOUT = 300


def free_ram_gb():
    class MEMSTAT(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        s = MEMSTAT()
        s.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        return s.ullAvailPhys / 1024 ** 3
    except Exception:
        return 99.0


def pick_model(prefer_small=False):
    if prefer_small or free_ram_gb() < MIN_FREE_GB_FOR_BIG:
        return SMALL_MODEL
    return BIG_MODEL


def ollama_generate(model, system, prompt, max_tokens=600):
    body = json.dumps({
        "model": model, "system": system, "prompt": prompt, "stream": False,
        "options": {"num_ctx": NUM_CTX, "num_gpu": NUM_GPU_LAYERS, "temperature": 0.1,
                    "num_predict": max_tokens, "repeat_penalty": 1.1},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return json.loads(r.read())["response"].strip()


def run_llm(system, prompt, max_tokens=600, prefer_small=False):
    """Call the chosen model; if the big one fails (OOM, not pulled), retry once on the small one."""
    model = pick_model(prefer_small)
    try:
        return model, ollama_generate(model, system, prompt, max_tokens)
    except Exception:
        if model == SMALL_MODEL:
            raise
        return SMALL_MODEL, ollama_generate(SMALL_MODEL, system, prompt, max_tokens)


def load_input(args):
    if args.get("path"):
        p = os.path.expanduser(args["path"])
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        label = p
    elif args.get("text"):
        text, label = args["text"], "<inline text>"
    else:
        raise ValueError("pass either 'path' or 'text'")
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"{label} is {len(text):,} chars (limit {MAX_INPUT_CHARS:,}). "
                         "Narrow it first (Grep, or Read with offset/limit) and pass the slice as 'text'.")
    return text, label


def chunks(text, size=CHUNK_CHARS):
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):  # break on a line boundary where possible
            nl = text.rfind("\n", start + size // 2, end)
            if nl != -1:
                end = nl + 1
        out.append((start, text[start:end]))
        start = end
    return out


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def log_usage(tool, model, in_chars, out_chars, secs, n_chunks, cached=False):
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "model": model, "cached": cached,
           "in_tok": in_chars // 4, "out_tok": out_chars // 4, "saved_tok": max(0, (in_chars - out_chars) // 4),
           "secs": round(secs, 1), "chunks": n_chunks}
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def header(rec):
    src = "CACHED answer" if rec.get("cached") else f"{rec['chunks']} chunk(s)"
    return (f"[local-helper | {rec['model']} | UNVERIFIED | {src} | "
            f"~{rec['in_tok']:,} tok in -> ~{rec['out_tok']:,} tok out | {rec['secs']}s]\n")


# ---------------------------------------------------------------- tools

# Grounding: the model never reports line numbers (small models hallucinate them and loop on them).
# It copies source lines verbatim; the server finds each copied line in the source, attaches the
# real line number, and drops anything it cannot find. Claude only ever sees verified quotes.

NOT_FOUND = "NOT IN THIS SECTION"

SUMMARY_SYSTEM = ("You are a precise reading assistant. Answer ONLY from the provided text. "
                  "After your answer, add up to 5 lines of the form 'EVIDENCE: <one source line copied "
                  "exactly>' supporting it. If the text does not contain the answer, reply exactly "
                  f"'{NOT_FOUND}'. Never invent content. Be terse.")
EXTRACT_SYSTEM = ("You are a line filter. Copy every source line that matches the request, exactly "
                  "as written, one per line, in order. Output nothing else: no numbering, no "
                  f"commentary, no code fences. If no line matches, reply exactly '{NOT_FOUND}'.")
REDUCE_SYSTEM = ("You merge notes written by readers of different sections of one file into a single "
                 "answer. Use only what the notes say. Keep exact identifiers. Be terse.")


def _norm(s):
    return " ".join(s.strip().strip("`").split())


def ground(quote, lines, first_line):
    """Return the global line number where `quote` appears in this chunk, or None."""
    q = _norm(quote)
    if len(q) < 3:
        return None
    normed = [_norm(ln) for ln in lines]
    for i, n in enumerate(normed):          # 1st choice: the model copied a whole line
        if n and n == q:
            return first_line + i
    if len(q) >= 12:                         # 2nd: a long fragment of a line
        for i, n in enumerate(normed):
            if q in n:
                return first_line + i
    # 3rd: a short fragment such as a bare identifier ("ollama_up") as a whole word. Dropping these
    # was what capped extract recall at 15/28 whatever the model (measured 2026-09-19). The first
    # occurrence may be a mention rather than the definition, but the real line is shown, so it's checkable.
    pat = re.compile(r"(?<![\w.])" + re.escape(q) + r"(?![\w])")
    for i, n in enumerate(normed):
        if pat.search(n):
            return first_line + i
    return None


def _each_chunk(text, size=CHUNK_CHARS):
    for start, body in chunks(text, size):
        yield line_of(text, start), body, body.split("\n")


def _finish(tool, model, text, n_chunks, t0, body):
    rec = log_usage(tool, model, len(text), len(body), time.time() - t0, n_chunks)
    return header(rec) + body


# ---------------------------------------------------------------- answer cache
# Keyed on the file CONTENT (not path or mtime), so an edited file never gets a stale answer and a
# renamed/copied one still hits. A 3B answer is recomputed once the 7B is available again.

def _cache_db():
    db = sqlite3.connect(CACHE_DB, timeout=5)
    db.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, body TEXT, model TEXT, t REAL)")
    return db


def _cache_key(tool, text, args):
    h = hashlib.sha1()
    params = json.dumps({k: v for k, v in args.items() if k not in ("path", "text")}, sort_keys=True)
    for part in (CACHE_VERSION, tool, params, text):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def cached_call(tool, text, args, compute):
    """compute() -> (model, body, n_chunks). Returns the tool's full text result."""
    t0, key, row = time.time(), _cache_key(tool, text, args), None
    try:
        with _cache_db() as db:
            row = db.execute("SELECT body, model FROM answers WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        pass
    if row and not (row[1] == SMALL_MODEL and pick_model() == BIG_MODEL):
        rec = log_usage(tool, row[1], len(text), len(row[0]), time.time() - t0, 0, cached=True)
        return header(rec) + row[0]
    model, body, n = compute()
    try:
        with _cache_db() as db:
            db.execute("INSERT OR REPLACE INTO answers VALUES (?,?,?,?)", (key, body, model, time.time()))
            db.execute("DELETE FROM answers WHERE key NOT IN (SELECT key FROM answers ORDER BY t DESC LIMIT 2000)")
    except sqlite3.Error:
        pass
    return _finish(tool, model, text, n, t0, body)


def tool_summarize(args):
    text, label = load_input(args)
    q = args.get("question") or "Summarize what this contains and anything notable (errors, key functions, config)."
    max_words = int(args.get("max_words", 250))
    return cached_call("local_summarize", text, args, lambda: summarize_text(text, label, q, max_words))


def summarize_text(text, label, q, max_words):
    model, notes, evidence, dropped, n = None, [], {}, 0, 0
    parts = list(_each_chunk(text))
    for first, body, lines in parts:
        n += 1
        progress(n, len(parts), f"summarize: section {n}/{len(parts)}")
        prompt = (f"Source: {label} (section {n})\nQuestion: {q}\n\n<text>\n{body}\n</text>\n\n"
                  f"Answer in at most {max_words} words, then the EVIDENCE lines.")
        model, ans = run_llm(SUMMARY_SYSTEM, prompt, max_tokens=max_words * 2 + 200)
        prose = []
        for ln in ans.split("\n"):
            if ln.strip().upper().startswith("EVIDENCE:"):
                quote = ln.split(":", 1)[1]
                num = ground(quote, lines, first)
                if num:
                    evidence[num] = lines[num - first].strip()
                else:
                    dropped += 1
            elif ln.strip():
                prose.append(ln)
        prose = "\n".join(prose).strip()
        if prose and NOT_FOUND not in prose.upper():
            notes.append(prose)
    if len(notes) > 1:
        joined = "\n\n".join(f"Notes from section {i + 1}:\n{x}" for i, x in enumerate(notes))
        model, final = run_llm(REDUCE_SYSTEM, f"Question: {q}\n\n{joined}\n\nMerged answer "
                               f"(at most {max_words} words):", max_tokens=max_words * 2)
    elif notes:
        final = notes[0]
    else:
        final = "Nothing relevant found in any section."
    if evidence:
        final += "\n\nEvidence (verified present in source):\n" + "\n".join(
            f"L{k}: {v[:200]}" for k, v in sorted(evidence.items()))
    if dropped:
        final += f"\n({dropped} quoted line(s) not found in source were dropped -- treat the prose with extra suspicion.)"
    return model, final, n


def tool_extract(args):
    text, label = load_input(args)
    return cached_call("local_extract", text, args, lambda: extract_text(text, args["what"]))


def extract_text(text, what):
    model, hits, dropped, n = None, {}, 0, 0
    parts = list(_each_chunk(text, EXTRACT_CHUNK_CHARS))
    for first, body, lines in parts:
        n += 1
        progress(n, len(parts), f"extract: section {n}/{len(parts)}")
        prompt = f"Request: lines containing {what}\n\n<text>\n{body}\n</text>"
        model, ans = run_llm(EXTRACT_SYSTEM, prompt, max_tokens=1500)
        if NOT_FOUND in ans.upper():
            continue
        for ln in ans.split("\n"):
            if not ln.strip() or ln.strip().startswith("```"):
                continue
            num = ground(ln, lines, first)
            if num:
                hits[num] = lines[num - first].strip()
            else:
                dropped += 1
    body = "\n".join(f"L{k}: {v[:200]}" for k, v in sorted(hits.items())) or "No matching lines found."
    body = f"{len(hits)} verified line(s)\n" + body
    if dropped:
        body += f"\n({dropped} model output line(s) did not match the source and were dropped.)"
    body += "\n(Recall is not guaranteed: a small model may miss matches. Grep if completeness matters.)"
    return model, body, n


def tool_classify(args):
    t0 = time.time()
    items, labels = args["items"], args["labels"]
    instr = args.get("instruction", "")
    listing = "\n".join(f"{i}. {it}" for i, it in enumerate(items))
    prompt = (f"{instr}\nAllowed labels: {', '.join(labels)}\n\nItems:\n{listing}\n\n"
              "Reply with one line per item exactly as '<index>: <label>' and nothing else.")
    model, ans = run_llm("You label items. Use only the allowed labels.", prompt,
                         max_tokens=20 * len(items) + 50, prefer_small=len(listing) < 4000)
    rec = log_usage("local_classify", model, len(listing), len(ans), time.time() - t0, 1)
    return header(rec) + ans


def tool_draft(args):
    t0 = time.time()
    ctx = args.get("context", "")
    ctx_block = "Context:\n" + ctx if ctx else ""
    prompt = f"{args['task']}\n\n{ctx_block}\n\nOutput only the draft."
    model, ans = run_llm("You write concise first drafts that a senior engineer will review and edit.",
                         prompt, max_tokens=int(args.get("max_tokens", 800)))
    rec = log_usage("local_draft", model, len(prompt), len(ans), time.time() - t0, 1)
    return header(rec) + ans


# ---------------------------------------------------------------- progress (MCP notifications/progress)
# Long model calls take 10-90s. When the client sends a progressToken, report each finished section
# so the user sees movement instead of a frozen tool call.
_PROGRESS = {"token": None}


def progress(done, total, message):
    if _PROGRESS["token"] is None:
        return
    send({"jsonrpc": "2.0", "method": "notifications/progress",
          "params": {"progressToken": _PROGRESS["token"], "progress": done, "total": total, "message": message}})


# ---------------------------------------------------------------- local_run permission rules
# local_run executes commands itself, so Claude Code's Bash(...) rules never see them. Re-apply the
# user's deny and ask rules here: deny -> refused, ask -> refused with "use the Bash tool so Claude
# Code can ask you". Allow rules are not needed: local_run is itself a permission-gated tool.

def _rule_sources(cwd):
    home = os.path.join(os.path.expanduser("~"), ".claude")
    files = [os.path.join(home, "settings.json"), os.path.join(home, "settings.local.json")]
    if cwd:
        files += [os.path.join(cwd, ".claude", "settings.json"), os.path.join(cwd, ".claude", "settings.local.json")]
    extra = os.environ.get("LOCAL_HELPER_EXTRA_SETTINGS")    # tests point this at a temp settings file
    return files + ([extra] if extra else [])


def _load_rules(cwd, tool):
    rules = {"deny": [], "ask": []}
    for f in _rule_sources(cwd):
        try:
            perms = json.load(open(f, encoding="utf-8")).get("permissions") or {}
        except (OSError, ValueError):
            continue
        for kind in rules:
            for r in perms.get(kind) or []:
                if r == tool:
                    rules[kind].append((r, None))              # bare "Bash" = every command
                elif r.startswith(tool + "(") and r.endswith(")"):
                    rules[kind].append((r, r[len(tool) + 1:-1]))
    return rules


def _rule_matches(pat, cmd):
    if pat is None:
        return True
    pat = pat.strip()
    if pat.endswith(":*"):                                     # legacy prefix syntax, e.g. Bash(npm:*)
        prefix = pat[:-2]
        return cmd == prefix or cmd.startswith(prefix + " ")
    rx = "^" + ".*".join(re.escape(part) for part in pat.split("*")) + "$"
    return re.match(rx, cmd, re.S) is not None


def rule_block(cmd, cwd, shell):
    """(kind, rule) of the first deny/ask rule matching the command or any sub-command, else None."""
    tool = "PowerShell" if shell == "powershell" else "Bash"
    rules = _load_rules(cwd, tool)
    subs = [s.strip() for s in re.split(r"&&|\|\||;|\||\n", cmd) if s.strip()] + [cmd.strip()]
    for kind in ("deny", "ask"):
        for raw, pat in rules[kind]:
            if any(_rule_matches(pat, s) for s in subs):
                return kind, raw
    return None


def tool_outline(args):
    t0 = time.time()
    text, label = load_input(args)
    body = outline.outline_text(text, label, int(args.get("max_items", 120)))
    rec = log_usage("local_outline", "none (deterministic)", len(text), len(body), time.time() - t0, 0)
    return (f"[local-helper | outline | EXACT (pattern match, no model) | ~{rec['in_tok']:,} tok in -> "
            f"~{rec['out_tok']:,} tok out]\n" + body)


def tool_map(args):
    t0 = time.time()
    root = os.path.expanduser(args.get("root") or os.getcwd())
    if not os.path.isdir(root):
        raise ValueError(f"not a directory: {root}")
    body = outline.map_dir(root, int(args.get("max_chars", 12000)))
    log_usage("local_map", "none (deterministic)", 0, len(body), time.time() - t0, 0)
    return f"[local-helper | map | EXACT (no model) | {time.time() - t0:.1f}s]\n" + body


def _bash():
    b = shutil.which("bash")
    return b if b and "system32" not in b.lower() else None   # System32\bash.exe is WSL, not Git Bash


def _kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def _save_run_log(cmd, out):
    os.makedirs(RUNS_DIR, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", cmd)[:40].strip("-") or "cmd"
    path = os.path.join(RUNS_DIR, time.strftime("%Y%m%d-%H%M%S-") + slug + ".log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    logs = sorted(os.listdir(RUNS_DIR))
    for old in logs[:-KEEP_RUNS]:
        try:
            os.remove(os.path.join(RUNS_DIR, old))
        except OSError:
            pass
    return path


def tool_run(args):
    t0 = time.time()
    cmd = args["command"]
    cwd = os.path.expanduser(args.get("cwd") or os.getcwd())
    shell = args.get("shell") or ("bash" if _bash() else "powershell")
    timeout = min(int(args.get("timeout", 600)), 1800)
    blocked = rule_block(cmd, cwd, shell)
    if blocked:
        kind, rule = blocked
        tool = "PowerShell" if shell == "powershell" else "Bash"
        raise PermissionError(
            f"refused: your permissions.{kind} rule {rule} matches this command, and local_run does not "
            f"bypass your rules. " + ("Don't run it." if kind == "deny" else
            f"Run it with the {tool} tool instead so Claude Code can ask the user."))
    if shell == "bash":
        if not _bash():
            raise RuntimeError("no Git Bash found; pass shell='powershell'")
        argv = [_bash(), "-c", cmd]
    else:
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd]
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    try:
        raw, _ = proc.communicate(timeout=timeout)
        code, timed_out = proc.returncode, False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raw, _ = proc.communicate()
        code, timed_out = None, True
    out = (raw or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
    secs = time.time() - t0
    status = f"TIMED OUT after {timeout}s" if timed_out else f"exit {code}"
    nlines = out.count("\n") + 1 if out else 0
    if len(out) <= RUN_VERBATIM_CHARS:
        log_usage("local_run", "none (verbatim)", len(out), len(out), secs, 0)
        return f"[local-helper | run | {status} | {secs:.1f}s | {nlines} lines, returned verbatim]\n{out}"
    log_path = _save_run_log(cmd, out)
    parts = [f"[local-helper | run | {status} | {secs:.1f}s | {nlines:,} lines / {len(out) // 1024}KB | "
             f"full output saved: {log_path} -- Read it with offset/limit if you need more]",
             outline.outline_text(out, log_path, 60)]
    model = "none (deterministic digest)"
    if args.get("question"):
        tail = out[-MAX_INPUT_CHARS:]
        note = "" if len(tail) == len(out) else f" (model read only the last {len(tail) // 1024}KB)"
        model, answer, _ = summarize_text(tail, log_path, args["question"], int(args.get("max_words", 200)))
        parts.append(f"-- answer from {model}, UNVERIFIED{note}:\n{answer}")
    body = "\n".join(parts)
    log_usage("local_run", model, len(out), len(body), time.time() - t0, 0)
    return body


def tool_stats(args):
    if not os.path.exists(USAGE_LOG):
        return "No calls logged yet."
    recs = [json.loads(l) for l in open(USAGE_LOG, encoding="utf-8") if l.strip()]
    by = {}
    for r in recs:
        b = by.setdefault(r["tool"], {"calls": 0, "saved_tok": 0, "secs": 0.0})
        b["calls"] += 1; b["saved_tok"] += r["saved_tok"]; b["secs"] += r["secs"]
    hits = sum(1 for r in recs if r.get("cached"))
    lines = [f"{len(recs)} calls ({hits} served from cache), ~{sum(r['saved_tok'] for r in recs):,} Claude "
             f"tokens saved (est. chars/4, counted even when an answer was unhelpful), "
             f"{sum(r['secs'] for r in recs):.0f}s local compute"]
    for k, b in sorted(by.items()):
        lines.append(f"  {k}: {b['calls']} calls, ~{b['saved_tok']:,} tok saved, {b['secs']:.0f}s")
    lines.append(f"Free RAM now: {free_ram_gb():.1f} GB -> would use {pick_model()}")
    return "\n".join(lines)


SRC = {"path": {"type": "string", "description": "Absolute path of a text file to read locally."},
       "text": {"type": "string", "description": "Inline text instead of a path."}}

TOOLS = {
    "local_outline": (tool_outline, {
        "annotations": {"readOnlyHint": True, "openWorldHint": False}, 
        "description": "INSTANT and EXACT map of a file, no model: every function/class with its line number "
                       "for code, headings for markdown, keys for yaml/toml, and for logs the first/last lines "
                       "plus error/warning lines grouped by shape with repeat counts. Use this FIRST on any "
                       "large file, then Read only the ranges you need.",
        "inputSchema": {"type": "object", "properties": {**SRC,
            "max_items": {"type": "integer", "default": 120}}}}),
    "local_map": (tool_map, {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": "INSTANT map of a whole project, no model: every file grouped by directory with its line "
                       "count and top-level definitions (functions/classes; headings for markdown). Respects "
                       ".gitignore in git repos, skips node_modules/venv/build dirs. Use it to get oriented in an "
                       "unfamiliar codebase instead of many Glob/Read calls. Output is capped (default 12000 "
                       "chars); detail degrades gracefully, so pass a subdirectory as root for more.",
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "Absolute directory path (default: server cwd)."},
            "max_chars": {"type": "integer", "default": 12000}}}}),
    "local_run": (tool_run, {
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
        "description": "Run a shell command whose output would be long (test suites, builds, installs, "
                       "linters, big git logs) WITHOUT its output entering your context. Returns exit code, "
                       "duration, first/last lines and grouped error lines; full output is saved to a log "
                       "file you can Read in windows. Short output (<6KB) comes back verbatim. Pass "
                       "'question' to also get a local-model answer about the output (slower). Uses Git "
                       "Bash by default; shell='powershell' for PowerShell syntax. Commands matching the user's "
                       "Bash/PowerShell deny or ask permission rules are refused; for an 'ask' command, use the "
                       "Bash tool so Claude Code can ask.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "Working directory (absolute)."},
            "shell": {"type": "string", "enum": ["bash", "powershell"]},
            "timeout": {"type": "integer", "default": 600, "description": "Seconds, max 1800."},
            "question": {"type": "string", "description": "Optional: ask the local model about the output."},
            "max_words": {"type": "integer", "default": 200}}, "required": ["command"]}}),
    "local_summarize": (tool_summarize, {
        "annotations": {"readOnlyHint": True, "openWorldHint": False}, 
        "description": "Have a local model read a large file/text and answer a question about it, "
                       "so the full content never enters Claude's context. Output is UNVERIFIED -- "
                       "confirm anything you act on by reading the cited lines.",
        "inputSchema": {"type": "object", "properties": {**SRC,
            "question": {"type": "string", "description": "What you want to know. Be specific."},
            "max_words": {"type": "integer", "default": 250}}}}),
    "local_extract": (tool_extract, {
        "annotations": {"readOnlyHint": True, "openWorldHint": False}, 
        "description": "Have a local model list every occurrence of something (functions, errors, "
                       "config keys, URLs...) in a large file/text, with line numbers. UNVERIFIED.",
        "inputSchema": {"type": "object", "properties": {**SRC,
            "what": {"type": "string", "description": "What to extract."},
            "max_words": {"type": "integer", "default": 300}}, "required": ["what"]}}),
    "local_classify": (tool_classify, {
        "annotations": {"readOnlyHint": True, "openWorldHint": False}, 
        "description": "Have a local model label a list of short items (file names, log lines, "
                       "test names) with one of the given labels. UNVERIFIED.",
        "inputSchema": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "string"}},
            "labels": {"type": "array", "items": {"type": "string"}},
            "instruction": {"type": "string"}}, "required": ["items", "labels"]}}),
    "local_draft": (tool_draft, {
        "annotations": {"readOnlyHint": True, "openWorldHint": False}, 
        "description": "Have a local model write a first draft of boilerplate (docstrings, commit "
                       "message, README section, test scaffolding). Review before using.",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string"}, "context": {"type": "string"},
            "max_tokens": {"type": "integer", "default": 800}}, "required": ["task"]}}),
    "local_stats": (tool_stats, {
        "annotations": {"readOnlyHint": True, "openWorldHint": False}, 
        "description": "Show how many Claude tokens local-helper has saved so far.",
        "inputSchema": {"type": "object", "properties": {}}}),
}


# ---------------------------------------------------------------- MCP stdio plumbing

# Sent in the initialize result; Claude Code puts server instructions into Claude's context, so the
# guidance travels with the server instead of depending on a CLAUDE.md being present.
INSTRUCTIONS = (
    "local-helper runs a small local model plus exact pattern-matching tools on this machine, to keep "
    "bulk text out of your context. Unfamiliar project: local_map first (instant). "
    "Big file: local_outline first (instant, exact), then Read only the "
    "line ranges you need; use local_summarize/local_extract when you need a question answered across "
    "the whole file. Noisy command (tests, builds, installs): local_run instead of Bash. Model output is "
    "UNVERIFIED: only the quoted L<n> lines are checked against the source, so Read the cited lines "
    "before acting on a claim. local_extract misses ~15% of matches; use Grep when you need completeness. "
    "Never hand it decisions, plans or edits."
)

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        return {"protocolVersion": req["params"].get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS,
                "serverInfo": {"name": "local-helper", "version": VERSION}}
    if method == "tools/list":
        return {"tools": [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]}
    if method == "tools/call":
        name = req["params"]["name"]
        fn = TOOLS[name][0]
        _PROGRESS["token"] = (req["params"].get("_meta") or {}).get("progressToken")
        try:
            text = fn(req["params"].get("arguments") or {})
            return {"content": [{"type": "text", "text": text}]}
        except urllib.error.URLError as e:
            msg = f"Ollama unreachable at {OLLAMA} ({e}). Start it with `ollama serve`, or just read the file directly."
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
        finally:
            _PROGRESS["token"] = None
        return {"content": [{"type": "text", "text": msg}], "isError": True}
    if method == "ping":
        return {}
    if rid is not None:
        raise LookupError(method)
    return None


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}})
            continue
        rid = req.get("id")
        try:
            result = handle(req)
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "result": result})
        except LookupError as e:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {e}"}})


if __name__ == "__main__":
    main()
