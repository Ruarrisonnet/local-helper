# Changelog

## 1.3.0: first public release

Getting ready to publish took three review rounds, each by independent AI reviewers:

1. **Audit of v1.2.** Three reviewers covered privacy, portability and security, and a fourth tried to
   disprove every finding. 38 findings survived: 1 high, 9 medium, 28 low.
2. **Review of the first v1.3 draft.** 41 findings survived (1 high, 10 medium, 30 low). Among them was a
   high-severity regression the draft itself had introduced: `install.py`'s hook ran `python -c`, which puts
   the project folder first on `sys.path`, so a repo's own `json.py` would have run inside the hook on every
   call. The draft never shipped. It is fixed below, with a test that runs the exact installed command from
   a project full of hostile module files.
3. **A regression hunt** over those fixes found 19 more problems. One was high: the hook read Claude Code's
   standard heredoc commit (`git commit -m "$(cat <<'EOF' ...`) line by line, so a message line like
   "Fix type errors in server.py" was treated as the command `type server.py` and the commit was denied.
   All 19 are fixed.

`test_units.py` (95 checks) targets individual fixes. 18 fixes were spot-checked by undoing each one, and
the tests caught 17. The one miss is a second safeguard whose failure the first one already covers. Not
every fix has its own check.

**Works on machines other than the author's**
- High: the server and hook decoded stdin as cp1252 on Windows. A path like `C:\Users\José\...` became `JosÃ©`,
  which broke every path tool, and **the hook silently allowed full reads** for those users. Both now decode UTF-8 bytes.
- Git Bash is found the way Claude Code finds it (`CLAUDE_CODE_GIT_BASH_PATH`, then git's install folder,
  then the standard locations). A stock Git for Windows install has no bash on PATH, so v1.2 fell back to
  PowerShell without saying so.
- On Linux/macOS, `local_run` timeouts now kill the whole process group. Before, background children kept it hanging.
- Free RAM is measured on Linux (`/proc/meminfo`) and macOS (`vm_stat`), not only Windows.
- On Windows, a timeout kills everything the command started, through a Job Object. `taskkill /T` walks the
  process tree, which Git Bash's fork emulation breaks, so `sleep 30 &`-style children survived. A test proves
  the child is dead: it would write a marker file if it had lived.
- Tests are self-contained: they run against copies in a temp folder, with a scrubbed environment and an empty
  home folder, and use this repo's own `server.py` as the file under test. Model checks show as SKIP (one line
  each) without Ollama. CI runs on Linux, macOS and Windows.
- New `install.py` (install / `--check` / `--no-hook` / `--uninstall`) and `LICENSE` (MIT). `SECURITY.md`
  was rewritten (it had been GitHub's unedited template).

**Correctness**
- Chunks are sized in estimated tokens instead of characters. 12,000 chars of a UUID log is ~9,500 tokens, and
  Ollama silently kept only 3,074 of them. Calibrated against Ollama's real counts on 12 kinds of text,
  including base64, JWTs, UUID logs, ANSI logs, UTF-16 read as UTF-8 and rare CJK, the estimate came out at
  or above the real count on every one. Truncation is also detected from Ollama's own count (it keeps about
  num_ctx/2), and the section is re-split and redone. The first draft's detector could never fire.
- `local_extract` drops copied function bodies. After copying a matching line, the model often copies the
  whole block after it: real lines, so they passed the check, but 67 of 138 cited lines were off-target. A run
  of 4+ consecutive cited lines indented deeper than the first is now dropped. Measured precision after the
  change: 50/55, 33/33 and 26/26 on three files, with recall unchanged at 51/65, 30/48 and 25/28. That recall
  range (60-90%) is lower than v1.2's single-file figure suggested.
- Merging per-section notes is batched to fit the context. It used to overflow on big files.
- Line numbers now match Read/grep for files with lone `\r` (progress-bar logs).
- `local_run` with a `question`: a model failure no longer throws away the command's result, and on huge
  output the evidence line numbers refer to the saved log, not the tail the model read.
- The paging limit is per agent. Subagents share the parent's `session_id` (checked by logging real hook
  input), so in v1.2 one subagent could use up another's limit.
- Parallel hook calls no longer race on the state file (lock file plus a unique temp file). A Read past the end
  of the file no longer creates an inverted range that shrinks the used count.
- Hook: `cat /c/Users/...` (Git Bash spelling) is recognised on Windows. A `| head` excuses only its own command,
  not a bare `cat` elsewhere on the line. Only the program position counts (`grep type big.py` is not a dump), every
  pipeline stage is checked, heredoc bodies such as commit messages are ignored, and continuation lines are
  joined. Files containing NUL bytes count as binary (UTF-16 text excepted), and the binary extension
  list is shared with `local_map`. Huge files are counted in 1MB blocks, with no outline above 20MB, so the
  deny arrives well inside the hook's 10s limit (a 23MB file is tested). `exempt_dirs` must be a list: a
  string `"~"` would have exempted the whole home folder. A non-default Ollama address is recorded in
  `config.json`, because the hook never sees variables given to `claude mcp add -e`.
- Repeated identical lines are cited at their real positions, not all at the first copy.
- The cache key includes the model names, and after the big model fails it isn't retried on every chunk.
- MCP: protocol version negotiation, batches, non-object messages, `params: null`, -32602 for bad tool calls.
  None of these can crash the server any more.
- `local_map` works from a subdirectory of a repo and with non-ASCII file names. Markdown headings are
  named correctly.

**Security**
- Model prose is fenced (`| ` on every line) under a `MODEL TEXT (untrusted...)` header, so a planted file can't
  make the model forge `VERIFIED EVIDENCE` lines. The test uses a real injection fixture: the model repeated the
  planted lines, and they stayed fenced.
- `local_run` checks **both** the Bash and PowerShell rule families whatever the shell. v1.2 skipped Bash rules for
  `shell='powershell'`. It splits on unquoted `;` `|` `&` and newlines, and on `$(` and backticks even inside
  double quotes. It strips `VAR=x` assignments and wrapper words, skipping options and numbers to find the real
  program. A word right after an option is tried both ways, because it may be that option's argument
  (`sudo -u deploy rm`, `xargs -0 -n 1 rm`, `timeout -k 5 60 git push`, `do`/`then`/`if`/`!`). `command -v x` only
  looks `x` up, so it isn't treated as running it. Program paths, quotes (spaces included), `.exe` and PowerShell's
  `&` are removed.
  It reads project rules from every ancestor folder. `Bash(git push *)` covers a bare `git push`. PowerShell
  rules, and Bash program names on Windows, match case-insensitively. A deny that matches only text inside
  quotes (a commit message mentioning `rm -rf`) is downgraded to "ask".
- The path tools honour your `Read(...)` deny/ask rules, including the Windows forms `//c/...`, `C:/...`,
  `C:\...`, `//C:/...`, drive roots and `//**/...`. A directory rule covers the files inside it. Paths are
  canonicalised (symlinks, 8.3 short names, `\?\` prefixes, `\localhost\c$` admin shares), and alternate data
  streams are refused. Obvious secrets files are refused by default, and
  `local_map` lists restricted files by name only.
- The hook entry written by `install.py` runs Python in isolated mode (`-I`), so no project file can be imported
  into it.
- `local_map` runs git with `core.fsmonitor` forced off, so an untrusted repo's config can't run programs.
- `local_run` streams output to disk with a 20MB cap per run and a 200MB cap in total. Memory use stays flat.
- The hook entry written by `install.py` is fail-open. `python missing.py` exits with code 2, which Claude Code
  treats as block, so a moved folder would have blocked every Read/Bash call.

## 1.2.0

**Hook: paging limit**
- v1.1 left a loophole: reading a big file 300 lines at a time costs as many tokens as reading it whole.
  The hook now tracks the distinct lines read of each large file per session, with a limit of
  max(600, a quarter of the file). Re-reading a range is always free, so re-checking code after an Edit costs
  nothing. Each session has its own limit, and old records are cleaned up after 2 days.

**local_run follows your permission rules**
- v1.1's documented risk: `local_run` ran commands outside Claude Code's Bash rules. It now reads
  `permissions.deny` and `permissions.ask` from `~/.claude/settings(.local).json` and the project's
  `.claude/settings(.local).json`. Matching commands are refused, including a match after `&&`, `;` or `|`.
  For an `ask` command, it points Claude to the Bash tool so you get asked. Both the `Bash(...)` and
  `PowerShell(...)` rule forms are supported, including the legacy `:*` prefix syntax.

**New tool: `local_map`**
- An instant map of a whole project, no model: files grouped by directory with line counts and top-level
  definitions. Uses `git ls-files` in repos (so .gitignore applies), skips node_modules/venv/build, and flags
  binaries. Detail is reduced step by step to fit the output limit. Measured: 27-file repo in 331ms, 2.7KB.

**Progress updates**
- Summarize and extract send MCP `notifications/progress` after each section when the client asks, so
  a 30-90s call shows "section 2/3" instead of looking stuck.

**Tests**
- 32 server checks (+10) and 24 hook checks (+7, covering the paging limit).

## 1.1.0

**Better help for Claude**
- New `outline.py`: exact outlines by pattern matching, no model. 28/28 functions in 3ms, compared with 25/28 in ~17s from the 7B.
- The hook's deny message now includes the file's outline, so a blocked Read shows Claude where to look.
- The server sends usage instructions when it connects, so the guidance works even without a CLAUDE.md.
- Tools carry MCP annotations (read-only or destructive).

**New tools**
- `local_outline`: an instant, exact map of any file (code, markdown, yaml/toml, sql, logs).
- `local_run`: runs noisy commands without their output entering Claude's context. Reports the exit code, groups
  error lines by shape with repeat counts, shows the first and last lines, and saves the full log to `runs/` (last 50
  kept). Optional `question` for a model answer. Timeouts kill the whole process tree.

**Features**
- Answer cache (SQLite, keyed by file content) for `local_summarize` and `local_extract`. A 3B answer is
  recomputed once the 7B is available.
- `local_stats` reports cache hits, and says plainly that tokens-saved counts even unhelpful answers.

**Fixes found by testing**
- Adding a random tag to a query to dodge the cache made extract find 0 lines: the model searched for the tag.
  Tests now use a throwaway cache file (`LOCAL_HELPER_CACHE_DB`) instead.
- Tests now check that extracted lines are relevant, not just present in the file (26/26).
- New `test_enforce.py` (17 checks) keeps the hook's behaviour from drifting.

## 1.0.0

First release: MCP server with grounded summarize/extract, the enforcement hook, and the 7B running fully on the GPU.
