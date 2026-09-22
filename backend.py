"""Model backends: Ollama's native API, or any OpenAI-compatible server (LM Studio, llama.cpp's
llama-server, vLLM, Jan, ...). Stdlib only.

    LOCAL_HELPER_BACKEND   "ollama" (default) or "openai"
    LOCAL_HELPER_URL       server address. Defaults: ollama http://127.0.0.1:11434,
                           openai http://127.0.0.1:1234/v1 (LM Studio's default)
    LOCAL_HELPER_OLLAMA    older name for the Ollama address, still honoured
    LOCAL_HELPER_API_KEY   sent as a Bearer token to OpenAI-compatible servers that need one

Shared by server.py (the MCP tools), enforce.py (is the backend up?) and install.py (checks).
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URLS = {"ollama": "http://127.0.0.1:11434", "openai": "http://127.0.0.1:1234/v1"}
REQUEST_TIMEOUT = 300


class BackendError(RuntimeError):
    """The server answered with an error (model not found, bad request...)."""


class ContextOverflow(BackendError):
    """The prompt didn't fit the model's context. Ollama truncates silently instead (see truncated())."""


def _config():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def kind():
    k = (os.environ.get("LOCAL_HELPER_BACKEND") or _config().get("backend") or "ollama").strip().lower()
    return k if k in DEFAULT_URLS else "ollama"


def base_url():
    """Environment first, then config.json (install.py records it there for the hook, which never sees
    `claude mcp add -e` variables), then the backend's default."""
    k = kind()
    url = (os.environ.get("LOCAL_HELPER_URL") or (os.environ.get("LOCAL_HELPER_OLLAMA") if k == "ollama" else None)
           or _config().get("url") or (_config().get("ollama") if k == "ollama" else None) or DEFAULT_URLS[k])
    return str(url).rstrip("/")


def _post(path, payload, timeout=REQUEST_TIMEOUT):
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("LOCAL_HELPER_API_KEY")
    if key and kind() == "openai":
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base_url() + path, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:       # a subclass of URLError: must be caught first
        detail = e.read().decode("utf-8", "replace").strip()[:300]
        low = detail.lower()
        if e.code in (400, 413) and ("context" in low or "too long" in low or "maximum" in low and "token" in low):
            raise ContextOverflow(f"{kind()} server: prompt too long for the model's context: {detail}") from None
        raise BackendError(f"{kind()} server returned HTTP {e.code} for {path}: {detail}") from None


def _get(path, timeout=3):
    headers = {}
    key = os.environ.get("LOCAL_HELPER_API_KEY")
    if key and kind() == "openai":
        headers["Authorization"] = f"Bearer {key}"
    with urllib.request.urlopen(urllib.request.Request(base_url() + path, headers=headers), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def generate(model, system, prompt, max_tokens, num_ctx, num_gpu):
    """-> (text, prompt_tokens). prompt_tokens is 0 when the server doesn't report it."""
    if kind() == "openai":
        data = _post("/chat/completions", {
            "model": model, "temperature": 0.1, "max_tokens": max_tokens, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or choice.get("text") or ""
        return text.strip(), int((data.get("usage") or {}).get("prompt_tokens") or 0)
    data = _post("/api/generate", {
        "model": model, "system": system, "prompt": prompt, "stream": False,
        "options": {"num_ctx": num_ctx, "num_gpu": num_gpu, "temperature": 0.1,
                    "num_predict": max_tokens, "repeat_penalty": 1.1}})
    return data.get("response", "").strip(), int(data.get("prompt_eval_count") or 0)


def truncated(prompt_tokens, num_ctx):
    """Ollama silently evaluates only about half the context when a prompt overflows num_ctx (observed
    3,074 at 6144). OpenAI-compatible servers reject an overflowing prompt instead (ContextOverflow)."""
    return kind() == "ollama" and prompt_tokens > 0 and abs(prompt_tokens - num_ctx // 2) <= 16


def embed(model, texts):
    """-> one vector per text."""
    if not texts:
        return []
    if kind() == "openai":
        data = _post("/embeddings", {"model": model, "input": list(texts)})
        rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        return [r["embedding"] for r in rows]
    data = _post("/api/embed", {"model": model, "input": list(texts), "truncate": True})
    return data.get("embeddings") or []


def list_models():
    """Model names the server has, or None if it can't be reached."""
    try:
        if kind() == "openai":
            return {m.get("id") for m in _get("/models").get("data") or []}
        return {m.get("name") for m in _get("/api/tags").get("models") or []}
    except Exception:
        return None


def has_model(have, name):
    return bool(have) and (name in have or (":" not in name and f"{name}:latest" in have))


def host_port():
    u = urllib.parse.urlparse(base_url())
    return u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)
