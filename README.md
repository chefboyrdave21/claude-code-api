# Claude Code API

OpenAI-compatible `/v1/chat/completions` wrapper around the `claude` CLI.
Routes inference through your Claude Code subscription — no API key needed.

## How it works

1. Receives an OpenAI-format `POST /v1/chat/completions` request.
2. Converts the `messages` array → a single prompt string.
3. Invokes `claude --print --output-format json --no-session-persistence --model <model>`.
4. Returns the response in OpenAI-compatible JSON (or chunked SSE if `stream: true`).
5. Concurrent requests are serialised through a queue — the Claude CLI is single-threaded.

> **Why `--output-format json` instead of `stream-json`?**  
> `claude-opus-4-6` with extended thinking blocks stdout for several minutes before
> emitting any text in stream-json mode, causing spurious timeouts. JSON mode runs
> the full inference and returns one clean payload — reliable for all models.
> Streaming clients still receive SSE chunks; the text is chunked after the subprocess
> finishes.

## Prerequisites

- `claude` CLI installed and authenticated (`claude auth status`)
- **Node server:** Node.js ≥ 18 + `npm install`
- **Python server:** Python 3.11+ with `aiohttp` (`pip install aiohttp`)

## Servers

Two interchangeable implementations — pick one:

| File | Runtime | Default port | Notes |
|------|---------|-------------|-------|
| `server.js` | Node.js / Express | 3456 | Original implementation |
| `server.py` | Python / aiohttp | 18782 | Alternative; used for the local user service |

### Node.js

```bash
npm install
node server.js            # port 3456
PORT=18782 node server.js # custom port
```

### Python

```bash
pip install aiohttp
python3 server.py                   # port 18782
python3 server.py --port 3456       # custom port
python3 server.py --debug           # verbose logging
```

## Environment variables (Node)

| Variable  | Default   | Description |
|-----------|-----------|-------------|
| `PORT`    | `3456`    | HTTP listen port |
| `TIMEOUT` | `600000`  | Max ms per claude call (10 min) |

## Endpoints

### POST /v1/chat/completions

```json
{
  "model": "claude-sonnet-4-6",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user",   "content": "What is 2+2?" }
  ],
  "stream": false
}
```

**Non-streaming response:** standard OpenAI chat completion object.  
**Streaming (`"stream": true`):** `text/event-stream` SSE in OpenAI delta format, terminated with `data: [DONE]`.

### GET /v1/models

Returns the list of supported Claude model IDs.

### GET /health

```json
{ "status": "ok", "queue_depth": 0, "running": false, "uptime_seconds": 42 }
```

## Supported models

| Model ID | Alias |
|----------|-------|
| `claude-opus-4-6` | `opus`, `gpt-4`, `gpt-4-turbo` |
| `claude-sonnet-4-6` | `sonnet`, `gpt-4o`, `claude-code` |
| `claude-haiku-4-5` | `haiku`, `gpt-3.5-turbo`, `gpt-4o-mini` |

Provider-prefixed names (`claude-code/claude-sonnet-4-6`) are also accepted.

## Local user service (port 18782)

For the SKCapstone / OpenClaw setup, the Python server runs as a systemd user unit:

```bash
# Install (one-time)
cp claude-code-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-code-api.service

# Status / logs
systemctl --user status claude-code-api.service
journalctl --user -u claude-code-api.service -f
```

The service file in this repo is the system-level template (runs under a specific user,
installs to `/opt/claude-code-api`). See the comments in the file to adapt it.

## OpenClaw configuration

Add a provider in `~/.openclaw/openclaw.json`:

```json
{
  "models": {
    "providers": {
      "claude-code": {
        "baseUrl": "http://127.0.0.1:18782/v1",
        "apiKey": "none",
        "api": "openai-completions",
        "models": [
          { "id": "claude-opus-4-6",   "name": "Claude Opus 4.6 (via CC)",   "contextWindow": 200000, "maxTokens": 32000 },
          { "id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (via CC)", "contextWindow": 200000, "maxTokens": 16000 },
          { "id": "claude-haiku-4-5",  "name": "Claude Haiku 4.5 (via CC)",  "contextWindow": 200000, "maxTokens": 8192 }
        ]
      }
    }
  }
}
```

Set your agent's primary model to `claude-code/claude-sonnet-4-6`.

## Known limitations

- **Single-threaded:** High request rates queue, not fail. Latency scales linearly.
- **No tool_calls passthrough:** Tools are handled internally by Claude Code, not exposed in the OpenAI format.
- **No cross-request memory:** Each call uses `--no-session-persistence`.
- **Streaming granularity:** SSE chunks are word-boundary splits of the completed response, not token-by-token.
