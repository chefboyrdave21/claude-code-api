# Claude Code API

An OpenAI-compatible `/v1/chat/completions` endpoint in front of the `claude` CLI.
Point any OpenAI-shaped client at it and inference runs through your existing Claude
Code subscription instead of a metered API key.

> ### ⚠️ This is an unauthenticated endpoint that runs arbitrary commands
>
> The server spawns `claude --print --dangerously-skip-permissions`, so Claude Code's
> Bash, Read, Write, and Edit tools are available with every permission prompt
> disabled. There is no API key, no token, and no authorization check anywhere in this
> repository. **The body of an HTTP request is an instruction that can execute code as
> the user running the server.** It binds `127.0.0.1` by default and that bind is the
> only thing protecting it. Read [`SECURITY.md`](SECURITY.md) before running it, and
> do not expose the port.

## How it works

1. Receives an OpenAI-format `POST /v1/chat/completions`.
2. Flattens the `messages` array into a single prompt string, piped to the subprocess
   on **stdin** (a CLI argument overflows with `[Errno 7] Argument list too long` on
   large contexts).
3. Invokes `claude --print --dangerously-skip-permissions --model <model>
   --output-format json --no-session-persistence`, holding an
   `asyncio.Semaphore` so no more than `CCAPI_MAX_CONCURRENT` (default **3**)
   subprocesses run at once.
4. Returns an OpenAI-compatible completion object, or chunked SSE if `stream: true`.

**Requests containing images take a different path.** `server.py` detects
`image_url` content blocks and calls the Anthropic SDK directly rather than the CLI,
authenticating with the OAuth access token read from `~/.claude/.credentials.json`.
That path does **not** consume the semaphore.

> **Why `--output-format json` instead of `stream-json`?**
> Opus with extended thinking blocks stdout for minutes before emitting any text in
> stream-json mode, which reads as a timeout. JSON mode runs the full inference and
> returns one clean payload. Streaming clients still get SSE: the completed text is
> chunked on word boundaries after the subprocess exits.

## Prerequisites

- `claude` CLI installed, authenticated, and on the server process's `PATH`.
- **Python server (the one that is actually maintained):** Python 3.11+ with
  `aiohttp` and `anthropic`. The `anthropic` SDK is **required**, not optional: it is
  imported at module scope and is used for hourly model discovery even if you never
  send an image.
  ```bash
  pip install aiohttp anthropic
  ```
- **Node server (legacy, see below):** Node.js 18+ and `npm install`.

## The two servers are not equals

| File | Runtime | Status |
|---|---|---|
| `server.py` | Python / aiohttp | **Maintained. This is what runs.** |
| `server.js` | Node / Express | **Legacy. Unmaintained, not deployed.** |

`server.js` was the original implementation and has since drifted: its default model
is `claude-sonnet-4-6`, its `VALID_MODELS` set has no `claude-opus-4-7` or
`claude-opus-4-8`, it has no vision path, and it has no model auto-discovery. It is
kept for reference. Nothing on the maintainer's fleet runs it. Prefer `server.py`
unless you specifically want the Node version, and expect to update it yourself.

```bash
python3 server.py                 # 127.0.0.1:18782
python3 server.py --port 3456     # different port
python3 server.py --host 0.0.0.0  # DO NOT. See SECURITY.md.
python3 server.py --debug         # verbose logging
```

## Configuration

### `server.py`

| Setting | Where | Default | Notes |
|---|---|---|---|
| listen port | `--port` | `18782` | |
| listen host | `--host` | `127.0.0.1` | Leave it. |
| `CCAPI_MAX_CONCURRENT` | env | `3` | Concurrent `claude` subprocesses. |
| `CLAUDE_API_LOAD_MCP` | env | unset (off) | `1` boots the host's MCP servers inside every subprocess. Roughly doubles per-request latency and widens the blast radius. This is a security setting. |
| `REQUEST_TIMEOUT` | constant, `server.py` | `1800` s | Per-call ceiling. Not an env var. |
| `QUEUE_TIMEOUT` | constant, `server.py` | `90` s | How long a request waits for a semaphore slot before giving up. |
| `DEFAULT_MODEL` | constant, `server.py` | `claude-opus-4-8` | Used for an unrecognised model name. |

### `server.js` (legacy)

| Variable | Default | Notes |
|---|---|---|
| `PORT` | `3456` | |
| `TIMEOUT` | `600000` ms | |

## Endpoints

`POST /v1/chat/completions`, `GET /v1/models`, and `GET /health`. The two `/v1` routes
are **also** registered without the prefix (`/chat/completions`, `/models`) for
clients that do not add it.

### `POST /v1/chat/completions`

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

Non-streaming returns a standard OpenAI chat completion object. Streaming returns
`text/event-stream` SSE in OpenAI delta format, terminated with `data: [DONE]`.
Errors return HTTP 500 with `{"error": {"message": ..., "type": "server_error"}}`,
or, mid-stream, an error object emitted as one final SSE frame.

### `GET /v1/models`

Returns the current model list. The set is seeded from a static list in `server.py`
and then **refreshed from the Anthropic API at most once an hour**, so a newly
released model appears without a code change. Discovery failures are logged and
fall back to the static seed.

### `GET /health`

```json
{ "status": "ok", "service": "claude-code-api", "port": 18782 }
```

> Known wart: the reported `port` is the module-level `PORT` constant, not the value
> passed to `--port`. If you start the server on a non-default port, `/health` still
> reports `18782`. Trust `ss -tlnp` over this field.

## Models

`DEFAULT_MODEL` is **`claude-opus-4-8`**. The static seed set is `claude-opus-4-8`,
`claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`, and
`claude-haiku-4-5-20251001`, plus whatever hourly discovery adds.

Aliases are accepted so OpenAI-shaped clients work unmodified:

| You send | You get |
|---|---|
| `gpt-4`, `gpt-4-turbo`, `opus` | `claude-opus-4-8` |
| `gpt-4o`, `sonnet` | `claude-sonnet-4-6` |
| `gpt-4o-mini`, `gpt-3.5-turbo`, `gpt-3.5-turbo-16k`, `haiku` | `claude-haiku-4-5` |

Provider-prefixed names are accepted too: explicit `claude/...` and `anthropic/...`
entries are mapped, and any other `prefix/model` has the prefix stripped before
lookup. An unrecognised name logs a warning and falls back to `DEFAULT_MODEL` rather
than erroring.

## Running it as a service

`claude-code-api.service` in this repository is a systemd **user** unit. It must be a
user unit: the subprocess needs the invoking user's `HOME`, `PATH`, and
`~/.claude/.credentials.json`.

```bash
cp claude-code-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-code-api.service
systemctl --user status claude-code-api.service
journalctl --user -u claude-code-api.service -f
```

Adjust the checkout path in `ExecStart` if you did not clone to
`~/clawd/skcapstone-repos/claude-code-api`. [`SOP.md`](SOP.md) documents the
maintainer's live deployment, including where it currently differs from this file.

## Using it from a client

Any OpenAI-compatible client works. The maintainer's setup consumes it from
**Hermes**, configured in `~/.hermes/config.yaml` as a provider:

```yaml
  - name: claude-code
    base_url: http://127.0.0.1:18782/v1
    api_key: ''
    api_mode: chat_completions
    models:
      - claude-opus-4-8
      - claude-opus-4-7
      - claude-opus-4-6
```

`api_key` is empty because the endpoint does not check one. That is the whole
security posture: see [`SECURITY.md`](SECURITY.md).

## Known limitations

- **No authentication.** By design, and the reason for the loopback bind.
- **Concurrency is capped, not unlimited.** Default 3. Beyond that, requests wait up
  to `QUEUE_TIMEOUT` (90 s) for a slot and then fail.
- **No `tool_calls` passthrough.** Tools run inside Claude Code and are not surfaced
  in the OpenAI response format. A client cannot drive tool use through this API.
- **No cross-request memory.** Every call uses `--no-session-persistence`.
- **Streaming is not token-by-token.** SSE chunks are word-boundary splits of an
  already-complete response, so time-to-first-token equals full generation time.
- **`usage` counts come from the CLI's own report** and are `0` when it does not
  supply them.
- **No test suite.** `npm test` is still the `npm init` placeholder that exits 1.
  There is no CI that runs the server. Changes are verified by hand.

## Documentation

| File | What it covers |
|---|---|
| [`SOP.md`](SOP.md) | Architecture, deploy, rollback, troubleshooting, live deployment facts. |
| [`SECURITY.md`](SECURITY.md) | Threat model, credential handling, disclosure. **Read before running.** |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to change it, and how to verify a change without a test suite. |
| [`CHANGELOG.md`](CHANGELOG.md) | Reconstructed from git history. |

## License

ISC. See [`LICENSE`](LICENSE), which matches the `license` field in `package.json`.
