# Changelog

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
