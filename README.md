# local-helper

Offloads bulk file reading from Claude Code to a local Ollama model, so large files never enter
Claude's context. Claude asks a question about a file; a 7B model on your GPU reads it and returns
a short answer with verified line citations.

Claude stays in charge: the local model only reads, it never plans or edits.

## Pieces

| File | What it does |
|---|---|
| `server.py` | MCP server (stdlib only, no pip install). Tools: `local_summarize`, `local_extract`, `local_classify`, `local_draft`, `local_stats`. |
| `enforce.py` | PreToolUse hook. Denies full reads of files over 500 lines / 40KB, so Claude has to use the helper, a <=300-line window, or Grep. |
| `test_server.py` | Runs the server over real MCP stdio and checks results against known answers from a real file. |

## Why the output can be trusted (partly)

Small models make up line numbers. So the model never reports them: it copies source lines word
for word, and the server finds each one in the file, attaches the real line number, and drops any
it can't find. Every `L<n>:` line Claude sees is really in the file. The summary text around those
lines is not checked, and the output says so.

## Measured on an RTX 3050 Laptop (4GB VRAM)

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
`LOCAL_HELPER_MIN_FREE_GB`, `LOCAL_HELPER_OLLAMA`.
