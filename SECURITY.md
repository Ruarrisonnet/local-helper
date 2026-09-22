# Security Policy

## Supported versions

Only the latest release gets fixes.

| Version | Supported |
| ------- | --------- |
| 1.4.x   | yes       |
| < 1.4   | no        |

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately through GitHub instead:
go to the **Security** tab of this repository and choose **Report a vulnerability**.

Include what you ran, what happened, and what you expected. This is a one-person hobby project, so
there's no guaranteed response time, but reports are taken seriously and fixed in the next release.

## What local-helper can do on your machine

Know these before you install it:

- **`local_run` executes shell commands** that Claude chooses. Claude Code asks your permission before
  each `local_run` call (unless you've allowed the tool). It also re-applies your `permissions.deny` and
  `permissions.ask` rules for `Bash(...)` and `PowerShell(...)` to every sub-command: after splitting on
  shell operators, stripping `VAR=x` and wrapper words like `sudo`, `timeout` and `xargs`, and removing
  program paths. That matching is still text-based. A command hidden inside `bash -c "..."`, `eval`, a
  script, an alias or a variable (`$CMD`) is checked only on its outer text. Don't auto-approve `local_run`
  if you rely on deny rules for safety.
- **The path tools** (`local_outline`, `local_summarize`, `local_extract`, `local_map`) refuse paths your
  `Read(...)` deny/ask rules cover, and obvious secrets files, so allowing them doesn't route around those
  rules. They approximate Claude Code's own path matching, deliberately on the broad side.
- **Output from the local model is untrusted.** Files and command output can contain text written to
  manipulate an AI. The local model can repeat it, and the server passes the model's prose to Claude marked
  as untrusted data. Only the `L<n>:` quotes are checked against the source, and they're checked for
  existence, not intent.
- **Local by default, but it goes where you point it.** Out of the box the server talks only to Ollama on
  `127.0.0.1`, and nothing leaves your machine. But `LOCAL_HELPER_BACKEND=openai` with a `LOCAL_HELPER_URL`
  sends file contents, command output and search text to whatever address you configure, with
  `LOCAL_HELPER_API_KEY` as a bearer token. Point it at a remote server only if you'd be comfortable sending
  that code there.
- **The hook runs on every Read, Bash and PowerShell call.** `install.py` writes it in Python's isolated
  mode (`-I`), so files in your project can't be imported into it. If it errors, or its folder is gone, it
  allows the call rather than blocking you.
- **It writes to its own folder** (or `LOCAL_HELPER_DATA`): `cache.db` (answers), `usage.jsonl` (call
  statistics), `runs/` (full output of the last 50 `local_run` commands, up to 200MB, which may include
  secrets printed by those commands) and `state/` (which line ranges were read, per agent). Delete them any
  time.
