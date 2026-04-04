# Claude Code API Wrapper

An OpenAI-compatible `/v1/chat/completions` HTTP API that wraps the `claude` CLI.
Useful for connecting OpenClaw (or any OpenAI-compatible client) to Claude Code via your subscription.

## How it works

1. Receives an OpenAI-format `POST /v1/chat/completions` request.
2. Converts the `messages` array into a single prompt string.
3. Pipes the prompt to `claude --print --permission-mode bypassPermissions` via `child_process.spawn`.
4. Returns the response in OpenAI-compatible JSON (or streams it via SSE if `stream: true`).
5. Concurrent requests are serialised through an in-memory queue — Claude Code is single-threaded.

## Prerequisites

- Node.js ≥ 18
- `claude` CLI installed and authenticated (`claude --version` should work)
- `npm`

## Setup

```bash
git clone <repo>
cd claude-code-api
npm install
node server.js
```

The server starts on **port 3456** by default.

## Environment variables

| Variable  | Default  | Description                              |
|-----------|----------|------------------------------------------|
| `PORT`    | `3456`   | HTTP listen port                         |
| `TIMEOUT` | `120000` | Max ms to wait for claude before killing |

## Endpoints

### POST /v1/chat/completions

Accepts an OpenAI-compatible request body:

```json
{
  "model": "claude-code",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user",   "content": "What is 2+2?" }
  ],
  "stream": false
}
```

**Non-streaming response:**
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "claude-code",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "4" },
    "finish_reason": "stop"
  }]
}
```

**Streaming (`"stream": true`):** returns `text/event-stream` SSE chunks in OpenAI delta format, terminated with `data: [DONE]`.

### GET /health

```json
{
  "status": "ok",
  "queue_depth": 0,
  "running": false,
  "uptime_seconds": 42
}
```

## Production deployment (systemd)

```bash
# 1. Copy files into place
sudo cp -r . /opt/claude-code-api

# 2. Install the service
sudo cp claude-code-api.service /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Enable and start
sudo systemctl enable claude-code-api
sudo systemctl start claude-code-api

# 4. Check logs
journalctl -u claude-code-api -f
```

To override port or timeout without editing the unit file, create a drop-in:

```bash
sudo mkdir -p /etc/systemd/system/claude-code-api.service.d/
sudo tee /etc/systemd/system/claude-code-api.service.d/override.conf <<'EOF'
[Service]
Environment=PORT=8080
Environment=TIMEOUT=180000
EOF
sudo systemctl daemon-reload && sudo systemctl restart claude-code-api
```

## OpenClaw configuration

Point your OpenClaw provider at:

```
Base URL : http://localhost:3456/v1
API Key  : (any non-empty string — not validated)
Model    : claude-code
```
