"""Install, check or uninstall local-helper for Claude Code.

    python install.py              register the MCP server and add the enforcement hook
    python install.py --no-hook    register the MCP server only
    python install.py --check      report what is installed, change nothing
    python install.py --uninstall  remove the MCP server and the hook

Never downloads anything: if the backend or a model is missing it prints the command to get it.
Edits ~/.claude/settings.json only to add or remove its own hook entry, after writing a backup.
Works with Ollama (default) or an OpenAI-compatible server: set LOCAL_HELPER_BACKEND=openai and
LOCAL_HELPER_URL before running it, and both are recorded for the hook.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backend  # noqa: E402

SERVER = os.path.join(HERE, "server.py")
HOOK = os.path.join(HERE, "enforce.py")
NAME = "local-helper"
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
MATCHER = "Read|Bash|PowerShell"
MODELS = [os.environ.get("LOCAL_HELPER_BIG", "qwen2.5-coder:7b-instruct-q3_K_M"),
          os.environ.get("LOCAL_HELPER_SMALL", "qwen2.5:3b")]
EMBED_MODEL = os.environ.get("LOCAL_HELPER_EMBED", "nomic-embed-text")
MIN_PY = (3, 8)


def say(ok, msg):
    print(("  ok    " if ok is True else "  --    " if ok is None else "  FIX   ") + msg)


def claude_cli():
    return shutil.which("claude")


def run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------- checks

def check_python():
    ok = sys.version_info[:2] >= MIN_PY
    say(ok, f"Python {sys.version.split()[0]} ({sys.executable})" + ("" if ok else f" -- need {MIN_PY[0]}.{MIN_PY[1]}+"))
    return ok


def check_ollama():
    """Check the configured backend (the name is historical: it also covers OpenAI-compatible servers)."""
    kind, url = backend.kind(), backend.base_url()
    have = backend.list_models()
    if have is None:
        how = ("Install it from https://ollama.com and start it" if kind == "ollama"
               else "Start your OpenAI-compatible server (LM Studio, llama-server, vLLM...)")
        say(False, f"{kind} backend not reachable at {url}. {how}.")
        return False
    say(True, f"{kind} backend reachable at {url}")
    for m in MODELS:
        present = backend.has_model(have, m)
        fix = f"run: ollama pull {m}" if kind == "ollama" else "load it in the server, or set LOCAL_HELPER_BIG/SMALL"
        say(present, f"model {m}" + ("" if present else f" missing -- {fix}"))
    ok = backend.has_model(have, EMBED_MODEL)
    say(ok or None, f"embedding model {EMBED_MODEL} (optional, for local_find)" + (
        "" if ok else " missing -- " + (f"run: ollama pull {EMBED_MODEL}" if kind == "ollama" else "load one and set LOCAL_HELPER_EMBED")))
    return True


def mcp_registered():
    cli = claude_cli()
    if not cli:
        return None
    return run([cli, "mcp", "get", NAME]).returncode == 0


def load_settings():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, encoding="utf-8") as f:
        return json.load(f)


def is_our_hook(h):
    """Any form this project has written: v1.0-1.2's `python .../enforce.py` string, or the v1.3 guard."""
    blob = " ".join([str(h.get("command", ""))] + [str(a) for a in h.get("args") or []])
    low = blob.lower().replace("\\\\", "\\")
    return "enforce.py" in low and ("local-helper" in low or "runpy.run_path" in low
                                    or os.path.normcase(HOOK).lower() in os.path.normcase(low))


def our_hooks(settings):
    return [h for e in (settings.get("hooks") or {}).get("PreToolUse") or [] for h in e.get("hooks") or []
            if is_our_hook(h)]


# ---------------------------------------------------------------- changes

def _settings_target():
    """The real file: a dotfiles setup often symlinks ~/.claude/settings.json, and replacing the link
    with a plain file would silently detach it from the user's dotfiles repo."""
    return os.path.realpath(SETTINGS)


def backup_settings():
    target = _settings_target()
    if os.path.exists(target):
        dst = target + ".bak-local-helper"
        shutil.copy2(target, dst)
        say(True, f"backed up settings to {dst}")


def write_settings(s):
    target = _settings_target()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
        f.write("\n")
    os.replace(tmp, target)


def record_ollama_url():
    """The hook runs outside the MCP server's environment, so a non-default backend or address would not
    reach it and it would think the backend is down (and allow everything). Record them in config.json;
    clear stale ones, which would silently turn the hook off."""
    want = {"backend": os.environ.get("LOCAL_HELPER_BACKEND"),
            "url": os.environ.get("LOCAL_HELPER_URL") or os.environ.get("LOCAL_HELPER_OLLAMA")}
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg = cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        cfg = {}
    before = dict(cfg)
    cfg.pop("ollama", None)                # v1.3's key, superseded by "url"
    for k, v in want.items():
        if v:
            cfg[k] = v
        else:
            cfg.pop(k, None)
    if cfg == before:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    say(True, f"recorded backend {backend.kind()} at {backend.base_url()} in config.json for the hook")


def remove_our_hooks(s):
    pre = (s.get("hooks") or {}).get("PreToolUse") or []
    kept = []
    for e in pre:
        e = dict(e)
        e["hooks"] = [h for h in e.get("hooks") or [] if not is_our_hook(h)]
        if e["hooks"]:
            kept.append(e)
    if "hooks" in s:
        if kept:
            s["hooks"]["PreToolUse"] = kept
        else:
            s["hooks"].pop("PreToolUse", None)
        if not s["hooks"]:
            s.pop("hooks")
    return s


def hook_command():
    """Exec form (command + args, no shell), so paths with spaces behave the same under bash, sh and
    PowerShell. It runs a one-line guard rather than enforce.py itself: `python missing.py` exits with
    code 2, which Claude Code treats as BLOCK -- so moving or deleting this folder would otherwise block
    every Read/Bash/PowerShell call. The guard exits 0 (allow) when enforce.py is gone."""
    guard = (f"import os,runpy; p={HOOK!r}; "
             "os.path.isfile(p) and runpy.run_path(p, run_name='__main__')")
    # -I (isolated mode) is essential: `python -c` otherwise puts the CURRENT directory -- the user's
    # project, where Claude Code runs hooks -- first on sys.path, so a project's json.py or re.py would be
    # imported and run inside the hook on every Read/Bash call (found in review of v1.3's first draft).
    return {"type": "command", "command": sys.executable, "args": ["-I", "-c", guard], "timeout": 10}


def install_hook():
    s = load_settings()
    backup_settings()
    s = remove_our_hooks(s)   # replace any older form of our entry (e.g. v1.0-1.2 used a shell command string)
    entry = {"matcher": MATCHER, "hooks": [hook_command()]}
    s.setdefault("hooks", {}).setdefault("PreToolUse", []).append(entry)
    write_settings(s)
    say(True, f"hook added to {SETTINGS} (matcher {MATCHER})")


def install_mcp():
    cli = claude_cli()
    if not cli:
        say(False, "`claude` CLI not on PATH. Register the server manually:\n"
                   f"        claude mcp add --scope user {NAME} -- \"{sys.executable}\" \"{SERVER}\"")
        return
    if mcp_registered():
        run([cli, "mcp", "remove", NAME, "--scope", "user"])
    # Keep the user's LOCAL_HELPER_* settings on the server: re-registering without them left the server and
    # the hook pointing at different Ollama addresses.
    envs = [a for k, v in sorted(os.environ.items()) if k.startswith("LOCAL_HELPER_") for a in ("-e", f"{k}={v}")]
    r = run([cli, "mcp", "add", "--scope", "user", *envs, NAME, "--", sys.executable, SERVER])
    say(r.returncode == 0, "MCP server registered (user scope)" if r.returncode == 0 else f"claude mcp add failed: {r.stderr.strip()}")


def uninstall():
    cli = claude_cli()
    if cli and mcp_registered():
        r = run([cli, "mcp", "remove", NAME, "--scope", "user"])
        say(r.returncode == 0, "MCP server removed")
    else:
        say(None, "MCP server was not registered")
    s = load_settings()
    if our_hooks(s):
        backup_settings()
        write_settings(remove_our_hooks(s))
        say(True, "hook removed")
    else:
        say(None, "hook was not installed")
    print(f"\nLeft in place: {HERE} (cache.db, usage.jsonl, runs/, state/). Delete the folder to remove everything.")


def status():
    reg = mcp_registered()
    say(reg if reg is not None else None, "MCP server registered" if reg else
        ("`claude` CLI not found" if reg is None else "MCP server not registered"))
    hooks = our_hooks(load_settings())
    say(bool(hooks) or None, f"hook installed ({len(hooks)} entr{'y' if len(hooks) == 1 else 'ies'})" if hooks else "hook not installed")
    if os.path.exists(os.path.join(HERE, "ENFORCE_OFF")):
        say(None, "ENFORCE_OFF exists: the hook currently allows everything")


def main():
    args = set(sys.argv[1:])
    unknown = args - {"--no-hook", "--check", "--uninstall", "-h", "--help"}
    if unknown or args & {"-h", "--help"}:
        print(__doc__)
        sys.exit(2 if unknown else 0)
    print(f"local-helper at {HERE}\n")
    if "--uninstall" in args:
        uninstall()
        return
    if not check_python():
        sys.exit(1)
    check_ollama()
    if "--check" in args:
        status()
        return
    install_mcp()
    if "--no-hook" not in args:
        install_hook()
        record_ollama_url()
    print("\nDone. Start a new Claude Code session to load the tools.")
    print("Kill switch for the hook: create a file named ENFORCE_OFF in this folder.")


if __name__ == "__main__":
    main()
