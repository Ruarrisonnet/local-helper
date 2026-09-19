"""local-helper: an MCP server that lets Claude hand bulk reading to a local Ollama model.

Dependency-free (stdlib only) stdio JSON-RPC, so it runs on the system Python with no pip install.
The local model is a helper, never the decision-maker: every result is labelled unverified.
"""
import ctypes
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE_LOG = os.path.join(HERE, "usage.jsonl")
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


def log_usage(tool, model, in_chars, out_chars, secs, n_chunks):
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "model": model,
           "in_tok": in_chars // 4, "out_tok": out_chars // 4, "saved_tok": max(0, (in_chars - out_chars) // 4),
           "secs": round(secs, 1), "chunks": n_chunks}
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def header(rec):
    return (f"[local-helper | {rec['model']} | UNVERIFIED | {rec['chunks']} chunk(s) | "
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


def tool_summarize(args):
    t0 = time.time()
    text, label = load_input(args)
    q = args.get("question") or "Summarize what this contains and anything notable (errors, key functions, config)."
    max_words = int(args.get("max_words", 250))
    model, notes, evidence, dropped, n = None, [], {}, 0, 0
    for first, body, lines in _each_chunk(text):
        n += 1
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
    return _finish("local_summarize", model, text, n, t0, final)


def tool_extract(args):
    t0 = time.time()
    text, label = load_input(args)
    model, hits, dropped, n = None, {}, 0, 0
    for first, body, lines in _each_chunk(text, EXTRACT_CHUNK_CHARS):
        n += 1
        prompt = f"Request: lines containing {args['what']}\n\n<text>\n{body}\n</text>"
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
    return _finish("local_extract", model, text, n, t0, body)


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


def tool_stats(args):
    if not os.path.exists(USAGE_LOG):
        return "No calls logged yet."
    recs = [json.loads(l) for l in open(USAGE_LOG, encoding="utf-8") if l.strip()]
    by = {}
    for r in recs:
        b = by.setdefault(r["tool"], {"calls": 0, "saved_tok": 0, "secs": 0.0})
        b["calls"] += 1; b["saved_tok"] += r["saved_tok"]; b["secs"] += r["secs"]
    lines = [f"{len(recs)} calls, ~{sum(r['saved_tok'] for r in recs):,} Claude tokens saved "
             f"(est. chars/4), {sum(r['secs'] for r in recs):.0f}s local compute"]
    for k, b in sorted(by.items()):
        lines.append(f"  {k}: {b['calls']} calls, ~{b['saved_tok']:,} tok saved, {b['secs']:.0f}s")
    lines.append(f"Free RAM now: {free_ram_gb():.1f} GB -> would use {pick_model()}")
    return "\n".join(lines)


SRC = {"path": {"type": "string", "description": "Absolute path of a text file to read locally."},
       "text": {"type": "string", "description": "Inline text instead of a path."}}

TOOLS = {
    "local_summarize": (tool_summarize, {
        "description": "Have a local model read a large file/text and answer a question about it, "
                       "so the full content never enters Claude's context. Output is UNVERIFIED -- "
                       "confirm anything you act on by reading the cited lines.",
        "inputSchema": {"type": "object", "properties": {**SRC,
            "question": {"type": "string", "description": "What you want to know. Be specific."},
            "max_words": {"type": "integer", "default": 250}}}}),
    "local_extract": (tool_extract, {
        "description": "Have a local model list every occurrence of something (functions, errors, "
                       "config keys, URLs...) in a large file/text, with line numbers. UNVERIFIED.",
        "inputSchema": {"type": "object", "properties": {**SRC,
            "what": {"type": "string", "description": "What to extract."},
            "max_words": {"type": "integer", "default": 300}}, "required": ["what"]}}),
    "local_classify": (tool_classify, {
        "description": "Have a local model label a list of short items (file names, log lines, "
                       "test names) with one of the given labels. UNVERIFIED.",
        "inputSchema": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "string"}},
            "labels": {"type": "array", "items": {"type": "string"}},
            "instruction": {"type": "string"}}, "required": ["items", "labels"]}}),
    "local_draft": (tool_draft, {
        "description": "Have a local model write a first draft of boilerplate (docstrings, commit "
                       "message, README section, test scaffolding). Review before using.",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string"}, "context": {"type": "string"},
            "max_tokens": {"type": "integer", "default": 800}}, "required": ["task"]}}),
    "local_stats": (tool_stats, {
        "description": "Show how many Claude tokens local-helper has saved so far.",
        "inputSchema": {"type": "object", "properties": {}}}),
}


# ---------------------------------------------------------------- MCP stdio plumbing

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        return {"protocolVersion": req["params"].get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "local-helper", "version": "1.0.0"}}
    if method == "tools/list":
        return {"tools": [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]}
    if method == "tools/call":
        name = req["params"]["name"]
        fn = TOOLS[name][0]
        try:
            text = fn(req["params"].get("arguments") or {})
            return {"content": [{"type": "text", "text": text}]}
        except urllib.error.URLError as e:
            msg = f"Ollama unreachable at {OLLAMA} ({e}). Start it with `ollama serve`, or just read the file directly."
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
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
