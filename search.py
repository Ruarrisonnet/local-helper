"""local_find: semantic search over a project with a local embedding model. Stdlib only.

A project is split into units -- one per function/class for code (from outline.py's definition lines),
40-line windows for everything else -- and each unit is embedded once and cached in SQLite, keyed by the
file's size and mtime, so only changed files are re-embedded. A query is embedded and ranked against every
unit by cosine similarity, plus a small bonus for exact identifier matches (embeddings are weak at those).
"""
import array
import math
import os
import re
import sqlite3
import time

import backend
import outline

UNIT_MAX_LINES = 60
WINDOW_LINES = 40
EMBED_CHARS = 1500           # per unit sent to the embedding model (nomic-embed-text: 2k-token context)
EMBED_BATCH = 32
MAX_FILES = 3000
MAX_UNITS = 20000
TEXT_KINDS_SKIPPED = {"log"}  # logs are what local_run/local_outline are for, not search


def _prefixes(model):
    """nomic-embed-text is trained with task prefixes; other models don't expect them."""
    return ("search_query: ", "search_document: ") if "nomic" in model else ("", "")


def units_of(rel, text):
    """[(start_line, end_line, text)] -- code split at its definitions, other text in windows."""
    lines = text.split("\n")
    kind = outline.kind_of(rel, text)
    starts = []
    if kind not in ("log", "yaml", "toml"):
        starts = [n for n, _ in outline.code_outline(lines, kind)]
    if not starts or starts[0] != 1:
        starts = [1] + starts
    bounds = []
    for i, s in enumerate(starts):
        e = (starts[i + 1] - 1) if i + 1 < len(starts) else len(lines)
        step = UNIT_MAX_LINES if len(starts) > 1 else WINDOW_LINES
        while s <= e:                                         # long units are cut into pieces
            bounds.append((s, min(e, s + step - 1)))
            s += step
    out = []
    for s, e in bounds:
        while e > s and not lines[e - 1].strip():          # end the range at its last non-blank line
            e -= 1
        body = "\n".join(lines[s - 1:e]).strip()
        if body:
            out.append((s, e, body))
    return out


def _db(path):
    db = sqlite3.connect(path, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT, model TEXT, mtime REAL, size INTEGER, "
               "PRIMARY KEY (path, model))")
    db.execute("CREATE TABLE IF NOT EXISTS units (path TEXT, model TEXT, start INTEGER, end INTEGER, "
               "head TEXT, vec BLOB)")
    db.execute("CREATE INDEX IF NOT EXISTS units_path ON units (path, model)")
    return db


def _pack(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array.array("f", (x / n for x in v)).tobytes()


def _unpack(b):
    a = array.array("f")
    a.frombytes(b)
    return a


def refresh(root, db_path, model, restricted=None, progress=None):
    """Bring the index up to date for root. -> stats dict."""
    root = os.path.abspath(root)
    files, source = outline._list_files(root)
    stats = {"files": 0, "embedded_files": 0, "units_embedded": 0, "skipped": 0, "source": source,
             "capped": max(0, len(files) - MAX_FILES), "unit_capped": 0}
    _, doc_prefix = _prefixes(model)
    todo = []                                   # (abs_path, mtime, size, units)
    seen = set()
    with _db(db_path) as db:
        known = {p: (m, s) for p, m, s in db.execute("SELECT path, mtime, size FROM files WHERE model=?", (model,))}
        # Units already in the index count towards MAX_UNITS, so the cap is a real ceiling on index size
        # rather than a per-run allowance that a second run would quietly exceed.
        stored_units = db.execute("SELECT count(*) FROM units WHERE model=?", (model,)).fetchone()[0]
        for rel in files[:MAX_FILES]:
            # normpath: _list_files gives forward slashes, so on Windows the same file would otherwise be
            # stored under two spellings and re-embedded on every run.
            p = os.path.normpath(os.path.join(root, rel))
            ext = os.path.splitext(rel)[1].lower()
            try:
                st = os.stat(p)
            except OSError:
                continue
            if ext in outline.BINARY_EXT or st.st_size > 2_000_000 or (restricted and restricted(p)):
                stats["skipped"] += 1
                continue
            stats["files"] += 1
            seen.add(p)
            if known.get(p) == (st.st_mtime, st.st_size):
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
                    text = f.read()
            except OSError:
                continue
            if outline.kind_of(rel, text) in TEXT_KINDS_SKIPPED and ext in (".log", ".txt"):
                stats["skipped"] += 1
                continue
            todo.append((p, rel, st.st_mtime, st.st_size, units_of(rel, text)))
        batch, owners = [], []
        total_units = sum(len(t[4]) for t in todo)
        done_units = 0

        def flush():
            nonlocal batch, owners, done_units
            if not batch:
                return
            try:
                vecs = backend.embed(model, [doc_prefix + b for b in batch])
            except backend.BackendError:
                time.sleep(1)                       # one retry: a transient server error shouldn't end the build
                vecs = backend.embed(model, [doc_prefix + b for b in batch])
            for (p, s, e, head), v in zip(owners, vecs):
                db.execute("INSERT INTO units VALUES (?,?,?,?,?,?)", (p, model, s, e, head, _pack(v)))
            done_units += len(batch)
            stats["units_embedded"] += len(batch)
            if progress:
                progress(done_units, total_units, f"find: indexing {done_units} of {total_units} units")
            batch, owners = [], []

        for p, rel, mtime, size, units in todo:
            # A file is only recorded as indexed when ALL of its units were embedded. Recording a
            # partly-embedded file marked it up to date for ever, so on a tree over the cap most files
            # ended up indexed with zero content and re-running never repaired it.
            if len(units) > MAX_UNITS - stored_units - stats["units_embedded"]:
                stats["unit_capped"] += 1
                continue
            db.execute("DELETE FROM units WHERE path=? AND model=?", (p, model))
            for s, e, body in units:
                batch.append(f"{rel}\n{body}"[:EMBED_CHARS])
                owners.append((p, s, e, body.split("\n", 1)[0][:160]))
                if len(batch) >= EMBED_BATCH:
                    flush()
            flush()
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (p, model, mtime, size))
            db.commit()             # keep finished files: one failed embed used to roll back the whole build
            stats["embedded_files"] += 1
        # forget files that are gone from this root
        prefix = os.path.normpath(root) + os.sep
        for (p,) in db.execute("SELECT path FROM files WHERE model=?", (model,)).fetchall():
            if os.path.normpath(p).startswith(prefix) and os.path.normpath(p) not in seen:
                db.execute("DELETE FROM files WHERE path=? AND model=?", (p, model))
                db.execute("DELETE FROM units WHERE path=? AND model=?", (p, model))
    return stats


def query(root, db_path, model, q, top_k=8):
    """-> [(score, path, start, end, head)] best first."""
    root = os.path.abspath(root)
    q_prefix, _ = _prefixes(model)
    qv = _unpack(_pack(backend.embed(model, [q_prefix + q])[0]))
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", q)}
    prefix = os.path.normpath(root) + os.sep
    scored = []
    with _db(db_path) as db:
        for p, s, e, head, blob in db.execute("SELECT path, start, end, head, vec FROM units WHERE model=?", (model,)):
            if not os.path.normpath(p).startswith(prefix):
                continue
            v = _unpack(blob)
            score = sum(a * b for a, b in zip(qv, v))
            low = head.lower()
            score += min(0.15, 0.05 * sum(1 for w in words if w in low))   # exact identifiers named in the query
            scored.append((score, p, s, e, head))
    scored.sort(key=lambda r: -r[0])
    return scored[:top_k]


def find(root, db_path, model, q, top_k=8, restricted=None, progress=None):
    t0 = time.time()
    stats = refresh(root, db_path, model, restricted, progress)
    t1 = time.time()
    hits = query(root, db_path, model, q, top_k)
    return hits, stats, t1 - t0, time.time() - t1
