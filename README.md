# local-helper

Offloads bulk file reading from Claude Code to a local Ollama model, so large files never enter
Claude's context. Claude asks a question about a file; a 7B model on your GPU reads it and returns
a short answer with verified line citations.

Claude stays in charge: the local model only reads, it never plans or edits.

## Pieces

| File | What it does |
|---|---|
| `server.py` | MCP server (stdlib only, no pip install). See the tool table below. |
| `outline.py` | Instant outlines with exact line numbers (pattern matching, no model), shared by the server and the hook. |
| `enforce.py` | PreToolUse hook. Denies full reads of files over 500 lines / 40KB. The deny message includes the file's outline, so Claude can go straight to a <=300-line window. |
| `test_server.py` | 22 checks over real MCP stdio, against known answers from a real file. |
| `test_enforce.py` | 17 allow/deny checks for the hook. |

## Tools

| Tool | Model? | Use it for |
|---|---|---|
| `local_outline` | no, exact | A map of any large file: functions/classes with line numbers for code, headings for markdown, grouped error lines for logs. Instant. Start here. |
| `local_run` | optional | Noisy commands (tests, builds, installs). Returns exit code, error lines grouped by shape with repeat counts, and the first/last lines; saves the full log. Pass `question` for a model answer on top. |
| `local_summarize` | yes | A question that needs the whole file read. The answer comes with `L<n>:` lines that are checked against the source. |
| `local_extract` | yes | Every line matching a description that Grep can't express. Around 85% recall. |
| `local_classify` | yes | Sorting short items (files, log lines) into labels. |
| `local_draft` | yes | Boilerplate drafts that you then rewrite. |
| `local_stats` | no | Calls, cache hits, estimated tokens saved. |

`local_summarize` and `local_extract` answers are cached by file content. Ask the same question about an unchanged file and the answer comes back instantly. Edit the file and the answer is recomputed.

## Why the output can be trusted (partly)

Small models make up line numbers. So the model never reports them: it copies source lines word
for word, and the server finds each one in the file, attaches the real line number, and drops any
it can't find. Every `L<n>:` line Claude sees is really in the file. The summary text around those
lines is not checked, and the output says so.

## Measured on an RTX 3050 Laptop (4GB VRAM)

- `local_outline` finds 28/28 top-level functions in 3ms; the 7B model's `local_extract` finds 25/28 in ~17s.
  Use the model where you need understanding, and pattern matching where you need locations.
- `local_run` on a 3,042-line / 65KB test log: about a 2KB digest showing the failing test, 40 repeated
  warnings grouped into one line with a count, and the exit code.
- `qwen2.5-coder:7b-instruct-q3_K_M` with `num_gpu=99`, `num_ctx=6144`: 100% on the GPU, about 0.7GB of
  system RAM, 23.7 tok/s. Left to its defaults, Ollama split it 49/51 CPU/GPU, used 2.2-2.5GB RAM and ran at 11 tok/s.
- Extract: 23-25 of 28 function definitions found (about 85%), and every cited line was real. Not
  complete, so use Grep when you need every match.
- Falls back to `qwen2.5:3b` when free RAM is under 1.5GB, or if the 7B fails to load.

## Setup

```bash
ollama pull qwen2.5-coder:7b-instruct-q3_K_M
ollama pull qwen2.5:3b
claude mcp add --scope user local-helper -- python /path/to/local-helper/server.py
python test_server.py        # edit TARGET to point at a large file of yours first
```

To enforce it, add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Read|Bash|PowerShell",
      "hooks": [{ "type": "command", "command": "python /path/to/local-helper/enforce.py", "timeout": 10 }]
    }]
  }
}
```

Kill switch: create a file named `ENFORCE_OFF` next to `enforce.py`. The hook also steps aside
whenever Ollama isn't running.

Settings you can override with environment variables: `LOCAL_HELPER_BIG`, `LOCAL_HELPER_SMALL`,
`LOCAL_HELPER_MIN_FREE_GB`, `LOCAL_HELPER_OLLAMA`, `LOCAL_HELPER_CACHE_DB`.

**`local_run` runs commands outside Claude Code's Bash permission rules.** Claude Code still asks
permission for the `local_run` tool itself, and the tool is annotated as destructive, but your
`Bash(...)` allow and deny rules don't apply to the commands it runs.
