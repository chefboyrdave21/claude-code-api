# Claude Code API

An OpenAI-compatible `/v1/chat/completions` endpoint in front of the `claude` CLI.
Point any OpenAI-shaped client at it and inference runs through your existing Claude
Code subscription instead of a metered API key.

> ### ⚠️ This endpoint runs arbitrary commands. Set `CCAPI_TOKEN` before you start it.
>
> The server spawns `claude --print --dangerously-skip-permissions`, so Claude Code's
> Bash, Read, Write, and Edit tools are available with every permission prompt
> disabled. **The body of an HTTP request is an instruction that can execute code as
> the user running the server.**
>
> Every route except `GET /health` requires a shared secret from the `CCAPI_TOKEN`
> environment variable. If it is unset, the service starts and refuses everything with
> a `401`. It also binds `127.0.0.1` by default. Keep both: the token is
> authorization, the loopback bind is containment. Read [`SECURITY.md`](SECURITY.md)
> before running it, and do not expose the port.

## Quick start

```bash
pip install aiohttp anthropic

# Mint a secret. Anything guessable defeats the point.
export CCAPI_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"

python3 server.py                       # 127.0.0.1:18782

# /health needs no token and tells you whether auth is configured.
curl -s http://127.0.0.1:18782/health

# Everything else does.
curl -s http://127.0.0.1:18782/v1/models -H "Authorization: Bearer $CCAPI_TOKEN"
```

## How it works

0. Checks the shared secret. `Authorization: Bearer <token>` or
   `X-CCAPI-Token: <token>`, compared against `CCAPI_TOKEN` with
   `hmac.compare_digest`. No match, no work: `401`, and the subprocess is never
   spawned.
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
- Python 3.11+ with `aiohttp` and `anthropic`. The `anthropic` SDK is **required**,
  not optional: it is imported at module scope and is used for hourly model discovery
  even if you never send an image.
  ```bash
  pip install aiohttp anthropic
  ```
- A value for `CCAPI_TOKEN`. Without it the server starts but answers `401` on
  everything except `/health`.

There is one server: `server.py`. A legacy Node implementation (`server.js`, Express,
port 3456) was deleted on 2026-08-16. It was never deployed, and it never had the
token gate. See `git log -- server.js`.

```bash
python3 server.py                 # 127.0.0.1:18782
python3 server.py --port 3456     # different port
python3 server.py --host 0.0.0.0  # DO NOT. See SECURITY.md.
python3 server.py --debug         # verbose logging
```

## Configuration

| Setting | Where | Default | Notes |
|---|---|---|---|
| `CCAPI_TOKEN` | env | **unset** | **The shared secret.** Required by every route except `GET /health`. Unset or blank means everything 401s. Never commit a value. |
| listen port | `--port` | `18782` | |
| listen host | `--host` | `127.0.0.1` | Leave it. |
| `CCAPI_MAX_CONCURRENT` | env | `3` | Concurrent `claude` subprocesses. |
| `CLAUDE_API_LOAD_MCP` | env | unset (off) | `1` boots the host's MCP servers inside every subprocess. Roughly doubles per-request latency and widens the blast radius. This is a security setting. |
| `REQUEST_TIMEOUT` | constant, `server.py` | `1800` s | Per-call ceiling. Not an env var. |
| `QUEUE_TIMEOUT` | constant, `server.py` | `90` s | How long a request waits for a semaphore slot before giving up. |
| `DEFAULT_MODEL` | constant, `server.py` | `claude-opus-5` | Used for an unrecognised model name. |

## Authentication

Send the secret as either header. `Bearer` is preferred, because an OpenAI-compatible
client already sends its configured `api_key` that way and needs no custom header
plumbing:

```bash
curl -H "Authorization: Bearer $CCAPI_TOKEN" ...
curl -H "X-CCAPI-Token: $CCAPI_TOKEN" ...
```

A failure returns `401` in the OpenAI error shape, so clients report it as an auth
problem rather than a generic server error:

```json
{"error": {"message": "Invalid token.", "type": "invalid_request_error", "code": "invalid_api_key"}}
```

If `CCAPI_TOKEN` is unset the message instead names the variable to set, and
`/health` reports `"auth": "unconfigured"`. Nothing is served in that state; the
process stays up only so the failure is diagnosable. Full rationale, storage, and
rotation are in [`SECURITY.md`](SECURITY.md).

## Endpoints

`POST /v1/chat/completions`, `GET /v1/models`, and `GET /health`. The two `/v1` routes
are **also** registered without the prefix (`/chat/completions`, `/models`) for
clients that do not add it. **All four require the token.** Only `GET /health` does
not.

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

The only route that needs no token, so nothing sensitive belongs in it.

```json
{
  "status": "ok",
  "service": "claude-code-api",
  "port": 18782,
  "auth": "required",
  "model_discovery": { "ok": true, "last_success_age_seconds": 120, "stale": false,
                       "models_served": 12, "last_error": null }
}
```

`auth` is `"required"` when `CCAPI_TOKEN` is set and `"unconfigured"` when it is not.
It never contains the secret. `model_discovery.last_error` is truncated, because it is
an arbitrary exception string on an open endpoint; the journal has the full text.

> Known wart: the reported `port` is the module-level `PORT` constant, not the value
> passed to `--port`. If you start the server on a non-default port, `/health` still
> reports `18782`. Trust `ss -tlnp` over this field.

## Models

`DEFAULT_MODEL` is **`claude-opus-5`**. The static seed set is the Claude 5 family
(`claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`) plus retained 4.x ids
(`claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`,
`claude-haiku-4-5`, `claude-haiku-4-5-20251001`), plus whatever hourly discovery adds.

Aliases are accepted so OpenAI-shaped clients work unmodified:

| You send | You get |
|---|---|
| `gpt-4`, `gpt-4-turbo`, `opus` | `claude-opus-5` |
| `gpt-4o`, `sonnet` | `claude-sonnet-5` |
| `fable` | `claude-fable-5` |
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
# 1. Mint the secret first. The unit reads it from this file, not from a tracked
#    Environment= line, because `systemctl show` prints those in the clear.
umask 077
printf 'CCAPI_TOKEN=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  > ~/.config/claude-code-api.env
chmod 600 ~/.config/claude-code-api.env

# 2. Then install the unit.
cp claude-code-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-code-api.service
systemctl --user status claude-code-api.service
journalctl --user -u claude-code-api.service -f
```

Confirm it came up authenticated before pointing a client at it:

```bash
curl -s http://127.0.0.1:18782/health | grep -o '"auth": *"[a-z]*"'   # want: "required"
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
    api_key: ${CCAPI_TOKEN}      # was '' while the endpoint checked nothing
    api_mode: chat_completions
    models:
      - claude-opus-5
      - claude-sonnet-5
      - claude-fable-5
```

with `CCAPI_TOKEN=<the same secret>` in `~/.hermes/.env`. Hermes hands `api_key` to
the OpenAI SDK, which sends it as `Authorization: Bearer`, which is exactly what this
server accepts. That is the whole client-side change.

**Do not** configure this as a global custom header (Hermes `model.default_headers`).
That would attach the secret to every OpenAI-compatible provider Hermes talks to, some
of which are off-box. `api_key` is scoped to the one provider.

## Known limitations

- **Authentication is one shared secret.** No identities, no scopes, no per-caller
  audit trail. Anyone holding the token can run anything the service account can run,
  which is why the loopback bind stays.
- **Concurrency is capped, not unlimited.** Default 3. Beyond that, requests wait up
  to `QUEUE_TIMEOUT` (90 s) for a slot and then fail.
- **No `tool_calls` passthrough.** Tools run inside Claude Code and are not surfaced
  in the OpenAI response format. A client cannot drive tool use through this API.
- **No cross-request memory.** Every call uses `--no-session-persistence`.
- **Streaming is not token-by-token.** SSE chunks are word-boundary splits of an
  already-complete response, so time-to-first-token equals full generation time.
- **`usage` counts come from the CLI's own report** and are `0` when it does not
  supply them.
- **Test coverage is auth-only.** `test_auth.py` (run by the `ci` workflow) proves the
  token gate holds. The request-translation, streaming, vision, and discovery paths
  still have no automated coverage and are verified by hand, per
  [`SOP.md`](SOP.md) section 4.

## Documentation

| File | What it covers |
|---|---|
| [`SOP.md`](SOP.md) | Architecture, deploy, rollback, troubleshooting, live deployment facts. |
| [`SECURITY.md`](SECURITY.md) | Threat model, credential handling, disclosure. **Read before running.** |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to change it, and how to verify a change without a test suite. |
| [`CHANGELOG.md`](CHANGELOG.md) | Reconstructed from git history. |

## License

ISC. See [`LICENSE`](LICENSE).
