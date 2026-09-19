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


# ---------------------------------------------------------------- project map (local_map)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next",
             ".cache", ".idea", ".vscode", "target", "coverage", ".pytest_cache", ".mypy_cache", "runs"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".gz", ".7z",
              ".exe", ".dll", ".so", ".pyd", ".bin", ".pt", ".pth", ".gguf", ".safetensors", ".onnx",
              ".mp3", ".mp4", ".wav", ".ogg", ".woff", ".woff2", ".ttf", ".otf", ".db", ".sqlite", ".pyc",
              ".lock", ".npy", ".npz", ".pkl"}
_NAME = re.compile(r"\b(?:def|class|function\*?|func|fn|struct|interface|type|enum|trait|impl|mod|module|"
                   r"const|let|var|record)\s+([A-Za-z_][\w]*)")


def _list_files(root):
    """Tracked files if root is a git repo (respects .gitignore), else a walk that skips junk dirs."""
    import subprocess
    if os.path.isdir(os.path.join(root, ".git")):
        try:
            r = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return [f for f in r.stdout.splitlines() if f.strip()], "git ls-files"
        except (OSError, subprocess.TimeoutExpired):
            pass
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        out += [os.path.relpath(os.path.join(d, f), root).replace("\\", "/") for f in sorted(files)]
    return out, "directory walk"


def _symbols(text, kind):
    """Names of top-level definitions only (indent 0), so a map stays one line per file."""
    names = []
    for _, line in code_outline(text.split("\n"), kind):
        if line[:1].isspace() or (kind == "md" and line.startswith("###")):
            continue
        m = _NAME.search(line)
        names.append(m.group(1) if m else line.strip().lstrip("#").strip()[:40])
    return names


def map_dir(root, max_chars=12000, max_files=3000):
    root = os.path.abspath(os.path.expanduser(root))
    files, source = _list_files(root)
    entries, total_lines = [], 0
    for rel in files[:max_files]:
        p = os.path.join(root, rel)
        ext = os.path.splitext(rel)[1].lower()
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if ext in BINARY_EXT or size > 2_000_000:
            entries.append((rel, None, size, []))
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        n = text.count("\n") + 1
        total_lines += n
        kind = kind_of(rel, text)
        entries.append((rel, n, size, _symbols(text, kind) if kind not in ("log", "yaml", "toml") else []))

    head = (f"map: {root} | {len(entries)} files | {total_lines:,} text lines | source: {source}"
            + (f" | first {max_files} files only" if len(files) > max_files else ""))

    def render(sym_cap, file_cap):
        by_dir = {}
        for e in entries:
            by_dir.setdefault(os.path.dirname(e[0]) or ".", []).append(e)
        out = [head]
        for d in sorted(by_dir):
            es = by_dir[d]
            lines = sum(e[1] or 0 for e in es)
            out.append(f"{d}/  ({len(es)} files, {lines:,} lines)")
            shown = sorted(es, key=lambda e: -(e[1] or 0))[:file_cap] if file_cap else es
            for rel, n, size, syms in sorted(shown):
                size_s = f"{n:,}L" if n is not None else f"{size // 1024}KB binary"
                sym_s = ""
                if syms and sym_cap:
                    sym_s = "  " + ", ".join(syms[:sym_cap]) + (f" +{len(syms) - sym_cap}" if len(syms) > sym_cap else "")
                out.append(f"  {os.path.basename(rel)}  {size_s}{sym_s}")
            if file_cap and len(es) > file_cap:
                out.append(f"  ... {len(es) - file_cap} smaller files")
        return "\n".join(out)

    # Degrade detail until it fits: fewer symbols, then only the biggest files per directory.
    for sym_cap, file_cap in ((10, 0), (5, 0), (3, 0), (0, 0), (0, 15), (0, 5)):
        text = render(sym_cap, file_cap)
        if len(text) <= max_chars:
            return text
    return text[:max_chars] + "\n... (map truncated; pass a subdirectory as root for detail)"
