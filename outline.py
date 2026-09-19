"""Deterministic file outlines: no model, instant, exact line numbers.

Shared by server.py (the local_outline tool, local_run) and enforce.py (a map of the file
goes into every deny message, so a blocked Read tells Claude where to look).
"""
import os
import re

CODE_PATTERNS = {
    "py": [r"^\s*(async\s+)?def\s+\w+", r"^\s*class\s+\w+"],
    "js": [r"^\s*(export\s+)?(default\s+)?(async\s+)?function\*?\s+\w+",
           r"^\s*(export\s+)?(default\s+)?(abstract\s+)?class\s+\w+",
           r"^\s*(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s+)?(function\b|\([^)]*\)\s*=>|\w+\s*=>)",
           r"^\s*(export\s+)?(declare\s+)?(interface|type|enum)\s+\w+",
           r"^\s+(static\s+)?(async\s+)?(get\s+|set\s+)?(?!(if|for|while|switch|catch|return|function)\b)\w+\s*\([^)]*\)\s*\{"],
    "go": [r"^func\s", r"^type\s+\w+"],
    "rs": [r"^\s*(pub(\([\w:]+\))?\s+)?(async\s+)?(unsafe\s+)?(fn|struct|enum|trait|impl|mod|macro_rules!)\b"],
    "java": [r"^\s*(public|private|protected|internal|abstract|sealed|static|final|partial)?\s*(class|interface|enum|record|struct)\s+\w+",
             r"^\s*(public|private|protected|internal|static|override|virtual|async|final|synchronized|fun)\b[^=;]*\w+\s*\([^;]*$"],
    "c": [r"^(?!\s*(if|for|while|switch|return|else)\b)[A-Za-z_][\w\s\*:<>,&]*[\s\*&]\**~?\w+\s*\([^;]*\)\s*(const\s*)?\{?\s*$",
          r"^\s*(typedef\s+)?(struct|class|enum|union|namespace)\s+\w+"],
    "rb": [r"^\s*(def|class|module)\s+"],
    "php": [r"^\s*(abstract\s+|final\s+)?(class|interface|trait)\s+\w+", r"^\s*(public|private|protected|static|\s)*function\s+\w+"],
    "sh": [r"^\s*(function\s+)?[\w-]+\s*\(\)\s*\{?", r"^\s*function\s+[\w-]+"],
    "ps1": [r"^\s*function\s+[\w-]+", r"^\s*class\s+\w+"],
    "md": [r"^#{1,6}\s+\S"],
    "yaml": [r"^[A-Za-z_][\w-]*\s*:"],
    "toml": [r"^\s*\[[^\]]+\]"],
    "sql": [r"^\s*(create|alter)\s+(or\s+replace\s+)?(table|view|function|procedure|index|trigger)\b"],
}
EXT_KIND = {
    ".py": "py", ".pyw": "py", ".js": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".jsx": "js", ".go": "go", ".rs": "rs", ".java": "java", ".kt": "java", ".cs": "java",
    ".scala": "java", ".swift": "java", ".c": "c", ".h": "c", ".cpp": "c", ".cc": "c", ".hpp": "c",
    ".rb": "rb", ".php": "php", ".sh": "sh", ".bash": "sh", ".ps1": "ps1", ".psm1": "ps1",
    ".md": "md", ".markdown": "md", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".sql": "sql",
}
SIGNAL = re.compile(r"\b(error|exception|traceback|fatal|panic|fail(ed|ure|s)?|assert(ion)?error|"
                    r"denied|refused|timed? ?out|warn(ing)?|segfault|killed|abort(ed)?)\b", re.I)
_NOISE = re.compile(r"\d+|0x[0-9a-f]+|[0-9a-f]{8,}", re.I)


def kind_of(path, text=""):
    k = EXT_KIND.get(os.path.splitext(path)[1].lower())
    if k:
        return k
    if text.lstrip().startswith(("#!/bin/bash", "#!/usr/bin/env bash", "#!/bin/sh")):
        return "sh"
    if text.lstrip().startswith(("#!/usr/bin/env python", "#!/usr/bin/python")):
        return "py"
    return "log"


def _clip(s, n=120):
    s = s.rstrip()
    return s if len(s) <= n else s[: n - 3] + "..."


def code_outline(lines, kind):
    pats = [re.compile(p, re.I if kind == "sql" else 0) for p in CODE_PATTERNS[kind]]
    return [(i + 1, _clip(ln)) for i, ln in enumerate(lines) if any(p.search(ln) for p in pats)]


def signal_lines(lines):
    """Error/warning lines, grouped by message shape so a 1000x-repeated error is one entry."""
    groups = {}
    for i, ln in enumerate(lines):
        if SIGNAL.search(ln):
            key = _NOISE.sub("#", ln.strip())[:160]
            g = groups.setdefault(key, [i + 1, _clip(ln), 0])
            g[2] += 1
    return sorted(groups.values())


def _cap(items, max_items):
    if len(items) <= max_items:
        return items, 0
    head = max_items * 2 // 3
    return items[:head] + items[-(max_items - head):], len(items) - max_items


def outline_text(text, path="", max_items=80):
    lines = text.split("\n")
    kind = kind_of(path, text)
    out = [f"outline: {path or '<text>'} | {len(lines):,} lines | {len(text) // 1024}KB | kind={kind}"]
    if kind != "log":
        items, hidden = _cap(code_outline(lines, kind), max_items)
        if items:
            out += [f"L{n}: {s}" for n, s in items]
            if hidden:
                out.insert(1 + max_items * 2 // 3, f"... {hidden} more ...")
            return "\n".join(out)
        out[0] += " (no definitions matched; showing log view)"
    head = [f"L{i + 1}: {_clip(l)}" for i, l in enumerate(lines[:5])]
    tail_start = max(5, len(lines) - 10)
    tail = [f"L{i + 1}: {_clip(lines[i])}" for i in range(tail_start, len(lines)) if lines[i].strip()]
    sig, hidden = _cap(signal_lines(lines), max_items // 2)
    out += ["-- first lines:"] + head
    if sig:
        out.append(f"-- error/warning lines ({len(sig) + hidden} distinct shapes, first occurrence, xN = repeats):")
        out += [f"L{n}: {s}" + (f"  x{c}" if c > 1 else "") for n, s, c in sig]
        if hidden:
            out.append(f"... {hidden} more shapes ...")
    out += ["-- last lines:"] + tail
    return "\n".join(out)


def outline_file(path, max_items=80):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return outline_text(f.read(), path, max_items)
