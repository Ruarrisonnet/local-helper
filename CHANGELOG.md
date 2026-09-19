# Changelog

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
