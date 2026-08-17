#!/usr/bin/env python3
"""
claude-code-api — OpenAI-compatible HTTP wrapper around `claude --print`

Exposes /v1/chat/completions and /v1/models so OpenClaw (and other tools)
can use Claude Code's subscription-covered inference instead of a raw API key.

Architecture:
  - aiohttp HTTP server on port 18782
  - A shared-secret middleware guards every route except GET /health. See
    `auth_middleware` below and SECURITY.md. This endpoint spawns
    `claude --dangerously-skip-permissions`, so an unauthenticated request body
    is arbitrary code execution; the loopback bind must not be the only control.
  - asyncio.Semaphore(CCAPI_MAX_CONCURRENT, default 3) caps concurrent claude
    invocations so background crons don't block interactive turns
  - All modes: claude --print --output-format json  (reliable, no stream-json timeouts)
  - Streaming responses: result is emitted as chunked SSE after the subprocess finishes
    (avoids the 300s timeout caused by opus extended-thinking blocking stream-json stdout)

Usage:
  python3 claude-code-api.py [--port 18782] [--debug]

systemd:
  ~/.config/systemd/user/claude-code-api.service
"""

import argparse
import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from typing import AsyncIterator

import anthropic as anthropic_sdk
from aiohttp import web

CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")

# ─── Configuration ────────────────────────────────────────────────────────────

PORT = 18782
DEFAULT_MODEL = "claude-opus-5"
REQUEST_TIMEOUT = 1800  # seconds per claude call (opus can be slow with large context)
QUEUE_TIMEOUT = 90     # seconds to wait for semaphore before giving up

# ─── Authentication ───────────────────────────────────────────────────────────
#
# The shared secret is read from the CCAPI_TOKEN environment variable and
# compared in constant time. Every route except GET /health requires it.
#
# Why a token at all, on a loopback service: `_run_claude_json` spawns
# `claude --print --dangerously-skip-permissions`, so a request body is arbitrary
# code execution as the service account, and the vision and discovery paths read
# a live OAuth access token AND a durable refresh token off disk. Before this
# existed, every local process on the box, including anything a browser or a
# compromised dependency could reach, had that authority. A loopback bind is not
# an authorization decision.
#
# Why 401-on-every-request rather than refuse-to-start when CCAPI_TOKEN is unset:
# the shipped unit is Restart=always with no StartLimitBurst (see
# claude-code-api.service), so exiting at boot produces a restart loop that never
# stops and takes /health down with it, leaving an operator with no endpoint to
# ask what is wrong. Refusing every authenticated request instead is equally
# fail-closed (nothing reaches the subprocess) but stays diagnosable: /health
# still answers and the 401 body names the variable to set. What it must never do
# is default to open when the variable is missing, which is the failure mode this
# fleet keeps re-learning.
CCAPI_TOKEN_ENV = "CCAPI_TOKEN"
AUTH_HEADER = "X-CCAPI-Token"
# Routes reachable without a token. Keep this to liveness probes only, and keep
# their responses free of anything an unauthenticated caller should not read.
UNAUTHENTICATED_PATHS = frozenset({"/health"})
# /health is unauthenticated, so the discovery error it echoes is bounded rather
# than emitted raw: it is an arbitrary exception string from the Anthropic SDK.
HEALTH_ERROR_MAX_CHARS = 200

# SEED list only. `_refresh_models()` discovers the live set from the Anthropic
# API on boot and hourly, and merges into this set, so new models appear without
# a code change. The merge is additive only: discovery can never remove a model
# from here, so an API hiccup cannot shrink what this wrapper will serve.
#
# This seed exists so the wrapper is useful before the first refresh completes
# and if discovery is ever down. Every entry was verified against the installed
# CLI (2.1.233) on 2026-08-16 with `claude --print --model <id>`, not copied
# from documentation.
#
# History worth keeping: discovery was silently broken from at least 2026-08-15
# because the OAuth token was passed as `api_key=` (sent as `x-api-key`) rather
# than `auth_token=` (sent as `Authorization: Bearer`). It 401ed every hour and
# fell back here, and because a stale list and a fresh one produce an identical
# /v1/models response, nothing downstream could tell. That is why /health now
# reports discovery state explicitly.
VALID_MODELS = {
    # Claude 5 family (current)
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    # Claude 4.x (retained so anything pinned to an explicit id keeps working)
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
}

# Interval for the background refresh loop (_refresh_loop), and the TTL the
# lazy handle_models() path checks. Both, deliberately: the loop is what makes
# refreshes actually periodic, and the lazy check stays as a cheap backstop if
# the loop ever dies.
MODEL_REFRESH_INTERVAL = 3600  # seconds

# Map OpenAI / shorthand / provider-prefixed names → canonical claude model IDs
MODEL_ALIASES: dict[str, str] = {
    # GPT compatibility
    "gpt-4":              "claude-opus-5",
    "gpt-4o":             "claude-sonnet-5",
    "gpt-4-turbo":        "claude-opus-5",
    "gpt-4o-mini":        "claude-haiku-4-5",
    "gpt-3.5-turbo":      "claude-haiku-4-5",
    "gpt-3.5-turbo-16k":  "claude-haiku-4-5",
    # Shorthand. These track the CURRENT family, so a caller asking for "opus"
    # gets today's opus rather than whichever one was current when this file was
    # last touched. Anything needing a specific generation must pin the full id.
    "opus":               "claude-opus-5",
    "sonnet":             "claude-sonnet-5",
    "haiku":              "claude-haiku-4-5",
    "fable":              "claude-fable-5",
    # Provider-prefixed (handle both claude/ and anthropic/ prefixes)
    "claude/claude-opus-5":      "claude-opus-5",
    "claude/claude-sonnet-5":    "claude-sonnet-5",
    "claude/claude-fable-5":     "claude-fable-5",
    "anthropic/claude-opus-5":   "claude-opus-5",
    "anthropic/claude-sonnet-5": "claude-sonnet-5",
    "anthropic/claude-fable-5":  "claude-fable-5",
    "claude/claude-opus-4-8":    "claude-opus-4-8",
    "claude/claude-opus-4-7":    "claude-opus-4-7",
    "claude/claude-opus-4-6":    "claude-opus-4-6",
    "claude/claude-sonnet-4-6":  "claude-sonnet-4-6",
    "claude/claude-haiku-4-5":   "claude-haiku-4-5",
    "anthropic/claude-opus-4-8":   "claude-opus-4-8",
    "anthropic/claude-opus-4-7":   "claude-opus-4-7",
    "anthropic/claude-opus-4-6":   "claude-opus-4-6",
    "anthropic/claude-sonnet-4-6": "claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5":  "claude-haiku-4-5",
}

# ─── Globals ──────────────────────────────────────────────────────────────────

log = logging.getLogger("claude-code-api")
_sem: asyncio.Semaphore | None = None
_last_model_refresh: float = 0.0
# Discovery health, surfaced on /health. False until a refresh actually succeeds,
# so "never worked" is distinguishable from "worked and is current".
_discovery_ok: bool = False
_discovery_error: str | None = None


def sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        n = max(1, int(os.environ.get("CCAPI_MAX_CONCURRENT", "3")))
        _sem = asyncio.Semaphore(n)
        log.info("claude subprocess concurrency: %d", n)
    return _sem


# ─── Authentication ───────────────────────────────────────────────────────────

def expected_token() -> str:
    """The configured shared secret, or "" when CCAPI_TOKEN is unset or blank.

    Read per request rather than cached at import so that a test, and an operator
    who has just corrected the unit environment, sees the current value. A blank
    or whitespace-only value counts as unset: it must never be treated as a
    secret that happens to match a blank header.
    """
    return os.environ.get(CCAPI_TOKEN_ENV, "").strip()


def presented_token(request: web.Request) -> str:
    """Extract the caller's token from either accepted header.

    Two spellings are accepted:
      - `Authorization: Bearer <token>`, because OpenAI-compatible clients send
        their configured api_key this way with no extra configuration.
      - `X-CCAPI-Token: <token>`, for clients that cannot set Authorization.
    """
    header = request.headers.get(AUTH_HEADER, "").strip()
    if header:
        return header
    authz = request.headers.get("Authorization", "").strip()
    if authz[:7].lower() == "bearer ":
        return authz[7:].strip()
    return ""


def _unauthorized(message: str) -> web.Response:
    """A 401 in the OpenAI error shape, so OpenAI-compatible clients surface it."""
    return web.json_response(
        {"error": {
            "message": message,
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }},
        status=401,
    )


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Require the shared secret on every route except UNAUTHENTICATED_PATHS.

    Fail-closed in both directions: an unset CCAPI_TOKEN refuses everything, and
    a set CCAPI_TOKEN refuses anything that does not match it byte for byte.
    There is no configuration under which a request without a valid token reaches
    the subprocess.
    """
    if request.path in UNAUTHENTICATED_PATHS:
        return await handler(request)

    expected = expected_token()
    if not expected:
        # Deliberately explicit about the cause. This is a loopback service whose
        # operator is the only realistic caller, and a silent 401 with no reason
        # is how a misconfiguration turns into an hour of debugging. An attacker
        # who can already open this socket learns nothing exploitable from it.
        log.error(
            "REFUSING %s %s from %s: %s is unset, so this service is fail-closed. "
            "Set it in the unit environment and restart.",
            request.method, request.path, request.remote, CCAPI_TOKEN_ENV,
        )
        return _unauthorized(
            f"claude-code-api is not configured: {CCAPI_TOKEN_ENV} is unset on the "
            "server, so every authenticated route is refused. Set it in the service "
            "environment (see SECURITY.md) and restart the unit."
        )

    presented = presented_token(request)
    if not presented:
        log.warning("401 %s %s from %s: no credentials presented",
                    request.method, request.path, request.remote)
        return _unauthorized(
            f"Missing credentials. Send 'Authorization: Bearer <token>' or "
            f"'{AUTH_HEADER}: <token>'."
        )

    # compare_digest, not ==, so a wrong token cannot be recovered byte by byte
    # from response timing. Compared as bytes because compare_digest rejects
    # non-ASCII str input.
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        log.warning("401 %s %s from %s: token mismatch",
                    request.method, request.path, request.remote)
        return _unauthorized("Invalid token.")

    return await handler(request)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def normalise_model(model: str) -> str:
    """Return a valid claude model ID, falling back to DEFAULT_MODEL."""
    if model in MODEL_ALIASES:
        return MODEL_ALIASES[model]
    # Strip provider prefix e.g. "claude-code/claude-sonnet-4-6"
    if "/" in model:
        model = model.split("/")[-1]
    if model in VALID_MODELS:
        return model
    log.warning("Unknown model %r, using default %s", model, DEFAULT_MODEL)
    return DEFAULT_MODEL


def _has_images(messages: list) -> bool:
    """Return True if any message contains an image_url content block."""
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def _openai_content_to_anthropic(content) -> list:
    """
    Convert an OpenAI content value to Anthropic content block list.
    Handles: plain string, text blocks, image_url blocks (data URI or https URL).
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    blocks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            blocks.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image_url":
            url = block.get("image_url", {}).get("url", "")
            if url.startswith("data:"):
                # data:image/jpeg;base64,/9j/...
                try:
                    header, data = url.split(",", 1)
                    media_type = header.split(";")[0].split(":")[1]
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    })
                except Exception as exc:
                    log.warning("Skipping malformed image data URI: %s", exc)
            else:
                blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
    return blocks


def messages_to_anthropic(messages: list) -> tuple[str, list]:
    """
    Convert OpenAI messages → (system_str, anthropic_messages_list).
    Used by the SDK path (vision requests).
    """
    system_parts: list[str] = []
    anthropic_msgs: list[dict] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            text = content if isinstance(content, str) else " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
            system_parts.append(text)
        else:
            anthropic_msgs.append({
                "role": role,
                "content": _openai_content_to_anthropic(content),
            })

    return "\n".join(system_parts), anthropic_msgs


def messages_to_prompt(messages: list) -> tuple[str, str]:
    """
    Convert OpenAI-style messages list to (system_prompt, user_prompt) for claude CLI.
    Text-only path — image blocks are silently dropped here (use SDK path for vision).
    """
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Text-only extraction; images handled by SDK path
            content = "\n".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )
        if role == "system":
            system_parts.append(content)
        else:
            turns.append((role, content))

    system = "\n".join(system_parts)

    if len(turns) == 1 and turns[0][0] == "user":
        return system, turns[0][1]

    lines = []
    for role, content in turns:
        prefix = "Human" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {content}")
    lines.append("Assistant:")
    return system, "\n\n".join(lines)


def make_completion_response(
    model: str,
    content: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict:
    """Build an OpenAI-compatible chat completion response object."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def make_sse_chunk(model: str, delta: str, finish: bool = False) -> str:
    """Format a single SSE data line for streaming chat completions."""
    obj = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": delta} if delta else {},
            "finish_reason": "stop" if finish else None,
        }],
    }
    return f"data: {json.dumps(obj)}\n\n"


# ─── Claude subprocess helpers ────────────────────────────────────────────────

async def _run_claude_json(model: str, prompt: str, system: str) -> tuple[str, dict]:
    """
    Run `claude --print --output-format json` and return (text_result, usage_dict).
    Acquires the global semaphore to serialise calls.
    """
    # Pipe prompt via stdin — avoids [Errno 7] Argument list too long on large contexts.
    # System prompt is prepended inline; --append-system-prompt would also be a CLI arg.
    stdin_text = f"[System: {system}]\n\n{prompt}" if system else prompt

    cmd = [
        "claude", "--print",
        "--dangerously-skip-permissions",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
    ]
    # By default, don't boot the ~/.claude.json MCP servers (skcapstone/skchat/
    # skmemory) on every spawn — this is a text-completion API, the caller brings
    # its own tools, and loading them roughly DOUBLES per-request latency
    # (~6s → ~3s). Set CLAUDE_API_LOAD_MCP=1 to restore MCP tools inside the wrapper.
    if os.environ.get("CLAUDE_API_LOAD_MCP", "").strip().lower() not in ("1", "true", "yes"):
        cmd += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

    log.debug("Running (non-stream): %s | stdin=%d chars", " ".join(cmd[:6]) + " ...", len(stdin_text))

    async with asyncio.timeout(QUEUE_TIMEOUT):
        await sem().acquire()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_text.encode()), timeout=REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude timed out after {REQUEST_TIMEOUT}s")
    finally:
        sem().release()

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {err}")

    raw = stdout.decode(errors="replace").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude returned non-JSON: {raw[:200]}") from exc

    if result.get("is_error"):
        raise RuntimeError(result.get("result", "Claude returned an error"))

    text = result.get("result", "")
    usage = result.get("usage", {})
    return text, usage


async def _refresh_models() -> None:
    """Discover available models from the Anthropic API and merge into VALID_MODELS.

    Auth: the token in ~/.claude/.credentials.json is an OAuth access token, and
    an OAuth token must be sent as `Authorization: Bearer`, which the SDK spells
    `auth_token=`. It was previously passed as `api_key=`, which the SDK sends as
    the `x-api-key` header, and Anthropic rejects that with
    "authentication_error: API key is invalid". Discovery had therefore returned
    401 on every attempt since at least 2026-08-15 while the service looked
    healthy, because the failure path just falls back to the static list.

    Merge-only, never subtractive: a discovery result can add models but can
    never remove one, so a partial or empty response cannot shrink what this
    wrapper will serve.
    """
    global _last_model_refresh, _discovery_ok, _discovery_error
    try:
        token = _get_oauth_token()
        client = anthropic_sdk.AsyncAnthropic(auth_token=token)
        page = await asyncio.wait_for(client.models.list(limit=100), timeout=15)
        discovered = {m.id for m in page.data if m.id.startswith("claude-")}
        if discovered:
            added = discovered - VALID_MODELS
            VALID_MODELS.update(discovered)
            _last_model_refresh = time.time()
            _discovery_ok = True
            _discovery_error = None
            if added:
                log.info("Model discovery: +%d new — %s", len(added), ", ".join(sorted(added)))
            log.info("Model discovery complete: %d models", len(VALID_MODELS))
        else:
            # A 200 that lists nothing is not success. Say so rather than
            # recording a refresh that discovered nothing.
            _discovery_ok = False
            _discovery_error = "API returned no claude-* models"
            log.error("Model discovery returned an EMPTY model list; keeping the static list")
    except Exception as exc:
        _discovery_error = str(exc)
        # ERROR, not WARNING, and it says what the consequence is. A stale list
        # and a fresh one produce an identical /v1/models response, so the log
        # is the only place this is visible. /health carries it too.
        log.error(
            "Model discovery FAILED, serving a possibly STALE static list of %d models: %s",
            len(VALID_MODELS), exc,
        )


def _get_oauth_token() -> str:
    """Read the current OAuth access token from Claude Code credentials."""
    with open(CREDS_PATH) as f:
        creds = json.load(f)
    return creds["claudeAiOauth"]["accessToken"]


async def _run_claude_sdk(model: str, system: str, anthropic_msgs: list) -> tuple[str, dict]:
    """
    Call the Anthropic API directly via SDK for vision / multi-modal requests.
    Uses the OAuth access token from ~/.claude/.credentials.json (subscription-covered,
    same token Claude Code uses — re-read on every call so expiry is handled).
    Does NOT consume the CLI semaphore — SDK calls are async and concurrent-safe.
    """
    token = _get_oauth_token()
    client = anthropic_sdk.AsyncAnthropic(api_key=token)

    kwargs: dict = {
        "model": model,
        "max_tokens": 4096,
        "messages": anthropic_msgs,
    }
    if system:
        kwargs["system"] = system

    log.debug("SDK call: model=%s msgs=%d (vision path)", model, len(anthropic_msgs))

    try:
        response = await asyncio.wait_for(
            client.messages.create(**kwargs),
            timeout=REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"Anthropic SDK call timed out after {REQUEST_TIMEOUT}s")

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return text, usage


async def _fake_stream_chunks(text: str, chunk_size: int = 80) -> AsyncIterator[str]:
    """
    Break a completed response into chunks for SSE emission.

    We always use --output-format json (blocking) for the subprocess — stream-json
    mode causes opus extended-thinking to block stdout for minutes before emitting
    any assistant events. Fake-streaming is more reliable and still lets clients
    receive incremental SSE deltas.
    """
    # Emit in word-boundary chunks to look natural
    words = text.split(" ")
    buf = ""
    for word in words:
        buf += word + " "
        if len(buf) >= chunk_size:
            yield buf
            buf = ""
            await asyncio.sleep(0)  # yield to event loop
    if buf:
        yield buf


# ─── HTTP Handlers ────────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    # model_discovery is reported explicitly because a stale model list and a
    # freshly discovered one produce an identical /v1/models response. Without
    # this, "discovery has been 401ing for days" is indistinguishable from
    # "discovery is working", which is exactly how the list went two
    # generations stale unnoticed.
    #
    # This route is UNAUTHENTICATED (see UNAUTHENTICATED_PATHS), so nothing here
    # may be sensitive. last_error is an arbitrary exception string, so it is
    # truncated rather than echoed whole; the journal keeps the full text.
    age = (time.time() - _last_model_refresh) if _last_model_refresh else None
    return web.json_response({
        "status": "ok",
        "service": "claude-code-api",
        "port": PORT,
        "auth": "required" if expected_token() else "unconfigured",
        "model_discovery": {
            "ok": _discovery_ok,
            "last_success_age_seconds": round(age) if age is not None else None,
            "stale": _discovery_ok and age is not None and age > MODEL_REFRESH_INTERVAL * 2,
            "models_served": len(VALID_MODELS),
            "last_error": _discovery_error[:HEALTH_ERROR_MAX_CHARS] if _discovery_error else None,
        },
    })


async def handle_models(request: web.Request) -> web.Response:
    if time.time() - _last_model_refresh > MODEL_REFRESH_INTERVAL:
        asyncio.create_task(_refresh_models())
    now = int(time.time())
    models = [
        {
            "id": m,
            "object": "model",
            "created": now,
            "owned_by": "anthropic",
        }
        for m in sorted(VALID_MODELS)
    ]
    return web.json_response({"object": "list", "data": models})


async def handle_chat_completions(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text=str(exc))

    model = normalise_model(body.get("model", DEFAULT_MODEL))
    messages = body.get("messages", [])
    streaming = body.get("stream", False)

    if not messages:
        raise web.HTTPBadRequest(text="messages array is required")

    vision = _has_images(messages)

    if vision:
        system, anthropic_msgs = messages_to_anthropic(messages)
        log.info("→ %s | stream=%s | model=%s | vision=True | %d msgs",
                 request.remote, streaming, model, len(anthropic_msgs))
    else:
        system, prompt = messages_to_prompt(messages)
        log.info("→ %s | stream=%s | model=%s | %d chars",
                 request.remote, streaming, model, len(prompt))

    if streaming:
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)

        try:
            if vision:
                text, usage = await _run_claude_sdk(model, system, anthropic_msgs)
            else:
                text, usage = await _run_claude_json(model, prompt, system)

            role_chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())

            async for delta in _fake_stream_chunks(text):
                if delta:
                    await response.write(make_sse_chunk(model, delta).encode())

            await response.write(make_sse_chunk(model, "", finish=True).encode())
            await response.write(b"data: [DONE]\n\n")

        except Exception as exc:
            log.error("Streaming error: %s", exc)
            try:
                err_chunk = json.dumps({"error": {"message": str(exc), "type": "server_error"}})
                await response.write(f"data: {err_chunk}\n\n".encode())
            except Exception:
                pass  # client already disconnected — nothing to write to

        try:
            await response.write_eof()
        except Exception:
            pass
        return response

    else:
        try:
            if vision:
                text, usage = await _run_claude_sdk(model, system, anthropic_msgs)
            else:
                text, usage = await _run_claude_json(model, prompt, system)
        except Exception as exc:
            log.error("Non-stream error: %s", exc)
            return web.json_response(
                {"error": {"message": str(exc), "type": "server_error"}},
                status=500,
            )

        log.info("← %s | model=%s | %d output chars | vision=%s",
                 request.remote, model, len(text), vision)
        resp = make_completion_response(
            model=model,
            content=text,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
        )
        return web.json_response(resp)


# ─── App factory & main ───────────────────────────────────────────────────────

async def _refresh_loop() -> None:
    """Refresh the model list on a real interval, forever.

    Without this, discovery ran exactly ONCE at startup and then only lazily,
    from handle_models(), when a caller happened to request /v1/models past the
    TTL. `MODEL_REFRESH_INTERVAL` reads like a period and was actually a
    cache-expiry checked on one endpoint, so if nothing asked, nothing refreshed.

    Measured on the live service before this existed: uptime 19.5h, hourly
    interval, and `last_success_age_seconds` was 70069. One refresh in nineteen
    hours. The gateway keeps its own catalog and rarely re-asks, so in practice
    the model list was frozen at boot and a newly released model would not have
    appeared until someone restarted the service.

    It never lets an exception end the loop: a transient network failure must
    cost one cycle, not all future ones. That is the same failure this whole
    file has now hit twice, a mechanism that stops working while continuing to
    look alive, so the loop is deliberately hard to kill and /health reports
    `stale` when it has silently stopped anyway.
    """
    while True:
        try:
            await asyncio.sleep(MODEL_REFRESH_INTERVAL)
            await _refresh_models()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Model refresh loop iteration failed, continuing: %s", exc)


async def _on_startup(app: web.Application) -> None:
    asyncio.create_task(_refresh_models())
    app["refresh_loop"] = asyncio.create_task(_refresh_loop())


async def _on_cleanup(app: web.Application) -> None:
    task = app.get("refresh_loop")
    if task is not None:
        task.cancel()


def build_app() -> web.Application:
    # auth_middleware is attached at construction, not registered per route, so a
    # route added later cannot be forgotten. Everything except
    # UNAUTHENTICATED_PATHS is guarded by default, including the unprefixed
    # aliases below.
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    # Also handle without /v1 prefix for flexibility
    app.router.add_get("/models", handle_models)
    app.router.add_post("/chat/completions", handle_chat_completions)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code API — OpenAI-compatible wrapper")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    log.info("Claude Code API starting on %s:%d", args.host, args.port)
    log.info("Supported models: %s", ", ".join(sorted(VALID_MODELS)))

    # Say the auth state out loud at boot. "Unconfigured" is not a fatal error
    # (see the CCAPI_TOKEN block at the top of this file for why the process
    # still starts), but it must never be silent: the service is useless in that
    # state and the operator needs to find out here rather than from a 401 in
    # some downstream client's log.
    if expected_token():
        log.info("Auth: REQUIRED. %s is set; send it as 'Authorization: Bearer <token>' "
                 "or '%s: <token>'. GET /health stays open.", CCAPI_TOKEN_ENV, AUTH_HEADER)
    else:
        log.error("Auth: UNCONFIGURED. %s is unset, so every route except GET /health "
                  "will return 401. This is fail-closed on purpose. Set %s in the unit "
                  "environment and restart.", CCAPI_TOKEN_ENV, CCAPI_TOKEN_ENV)

    app = build_app()
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
