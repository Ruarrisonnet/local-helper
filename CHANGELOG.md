# Changelog

## 1.4.0

**Model backends:** Ollama, or any OpenAI-compatible server (LM Studio, llama.cpp's `llama-server`, vLLM, Jan)
- A new `backend.py` handles both: generation, embeddings, model lists, and context overflow. Ollama truncates
  an overflowing prompt silently; OpenAI-compatible servers reject it. Either way the section is split and redone.
  Configure it with `LOCAL_HELPER_BACKEND`, `LOCAL_HELPER_URL` and `LOCAL_HELPER_API_KEY`; `install.py` records
  them for the hook.
- A new `mock_backend.py` stands in for a real server, speaking both APIs with a deterministic rule-based
  "model". `test_models.py` runs every model code path against it, twice (once per API): grounding, fencing,
  re-splitting, reduce, the extract second pass, `local_find`, fallback and errors. So for the first time
  CI tests the model code instead of skipping it. Only Ollama has been run with real models.

**`local_extract`: a second pass for recall**
- A small model reading a chunk misses matches, but answers a yes/no question about one line well. The first
  pass's verified matches reveal a shape (most start with `def`, or `raise`, or `ERROR`). Every other line with
  that shape becomes a candidate, and the model confirms them in batches sized to fit its context (at most 40).
- Measured with the real 7B on five file/query pairs: recall went from **127/178 (71%) to 162-166/178
  (91-93%, two runs)**.
  `raise` statements improved most, 5/18 -> 14/18. Precision stayed at 85-100%.

**New tool: `local_find`**, semantic search over a project
- Code is split per function/class, other files into 40-line windows. Each unit is embedded once
  (`nomic-embed-text` by default) and cached in SQLite, keyed by file size and mtime, so only changed files
  are re-embedded. Queries rank by cosine similarity plus a small bonus for exact identifiers.
- It respects `.gitignore`, your `Read(...)` deny rules and secrets files.
- On 10 plain-language questions about this repo: right function first 6/10, in the top 5 9/10 (MRR 0.73), against
  0/10 and 4/10 (MRR 0.18) for keyword ranking. Small and written by the author, so a sanity check, not a benchmark.

**Fixed after review** (two independent reviewers over the v1.4 changes; 18 findings, all reproduced, all fixed)
- High: indexing a project larger than the 20,000-unit cap marked the files it skipped as indexed anyway, so they
  stayed empty for ever. On the Python standard library, 71% of files were recorded as indexed with no content, and
  re-running never repaired it. Now a file is only recorded when all of its units are stored, what's left out is
  reported, and units already stored count towards the cap.
- High: the whole index build ran in one transaction, so a single failed embedding call (which happened during the
  review, on the stdlib) rolled back everything. Each file is now committed as it finishes, and a failed batch is
  retried once.
- High: `bench.py` would have run its "with local-helper" side with the hook disabled if an `ENFORCE_OFF` file
  existed. It now refuses to start unless the hook is live and the backend and models are reachable, and records
  which local-helper tools each run actually used.
- The extract confirmation batches are sized by estimated tokens (40 lines of base64 or CJK overflowed the context,
  and the model answered in prose while the pass reported full coverage); a truncated batch is no longer counted as
  checked. Progress values stay monotonic across a tool's two phases. Windows path spellings no longer split a
  file's index identity. `secrets.yaml`-style names are refused like `.env`. The hook's state follows
  `LOCAL_HELPER_DATA`, so a benchmark run writes nothing into the repo.
- Four checks in `test_models.py` passed while the feature they named was broken; they now fail when it is.
  `SECURITY.md` said everything stays local, which stopped being true with the OpenAI-compatible backend.

**Two hook bugs that only running the benchmark exposed.** Both affect ordinary use, and the test suite
had no case for either.
- The hook policed `~/.claude/projects`, which is Claude Code's own storage. When a tool result is too big
  to inline, Claude Code writes it to `.../tool-results/toolu_*.txt` and reads it back - and the hook refused
  that read, so **Claude could not see the output of its own Grep**, and got an outline of a temp file
  instead. In a benchmark run it lost its search results this way and answered 54 where the truth was 50.
  That whole directory (spill files, transcripts, the memory folder) is now exempt.
- The hook's "is this narrowed?" check knew `grep`, `wc`, `awk` and `sed` but none of their PowerShell
  equivalents, so it refused `Get-Content app.log | Where-Object { $_ -match ' ERROR ' }` - a pipeline that
  plainly filters. On Windows that cost a run six extra turns working around the refusal, which was the
  entire measured slowdown on that task. `Where-Object`, `ForEach-Object`, `Measure-Object`, `Group-Object`,
  `Compare-Object` and the `?` / `%` aliases now count as narrowing.

**`bench.py`: does it actually save tokens? Mostly no, and now we know why.**
- Runs real headless Claude Code sessions (Sonnet) on the same tasks with and without local-helper, in fresh
  copies of a fixture project, isolated from the user's own setup: `--setting-sources project`,
  `--strict-mcp-config`, the global CLAUDE.md excluded, an explicit environment allowlist, an identical
  shell permission allowlist in both arms, and a pinned model id. It reads the `stream-json` transcript, so
  tool calls and per-message usage are observed rather than inferred, and it refuses to start unless
  `server.py` answers MCP with all 9 tools. 66 valid runs, $5.54.
- **The result: the hook pays, the local model does not.** On the one task where the baseline would otherwise
  read a 137KB corpus whole, cost fell **51%** (fresh tokens 131,655 -> 45,496) and the spread narrowed from
  26k-186k to 34k-88k. Everywhere else local-helper cost **+1% to +27%**. Across 84 sessions Claude called a
  local-helper tool **3 times**; forced to use them it spent **2-5x the tokens and 28-104x the wall-clock**.
  The README now leads with that instead of with "keep bulk text out of Claude's context".
- Two reasons the premise underdelivers, both measured: Claude filters with Grep and the shell rather than
  reading bulk text, so a digest has little to replace; and `local_extract` reproduces every matching line
  (38,762 characters on the log task - larger than the baseline's whole session) instead of pointing at them.
- The first version of this benchmark was wrong in three ways, all found by auditing it before publishing:
  it summed `cache_read` across turns (measuring turn count, not text), scraped tool usage from an output
  format that cannot contain it, and copied a README into the fixture that held the answer to one task.
  The fixture generators now compute ground truth as they write, and `bench_fixture.py` asserts that the
  best single word anyone could grep for scores F1 < 0.6 against the true set - an earlier corpus shared
  the word "handle" across seven of eight phrasings, and both arms got the exact answer by grepping it.

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
