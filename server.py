#!/usr/bin/env python3
"""
claude-code-api — OpenAI-compatible HTTP wrapper around `claude --print`

Exposes /v1/chat/completions and /v1/models so OpenClaw (and other tools)
can use Claude Code's subscription-covered inference instead of a raw API key.

Architecture:
  - aiohttp HTTP server on port 18782
  - A shared-secret middleware guards every route except GET /health. See
    `auth_middleware` below and SECURITY.md. Every request spawns `claude`; with
    CCAPI_AGENTIC_TEXT=1 that is `claude --dangerously-skip-permissions`, i.e.
    arbitrary code execution, so the loopback bind must not be the only control.
  - asyncio.Semaphore(CCAPI_MAX_CONCURRENT, default 3) caps concurrent claude
    invocations so background crons don't block interactive turns
  - Every request (text, images, caller tools) runs ONE isolated turn through
    `_run_claude_cli`: stream-json in/out, no built-in tools, no service-account
    settings/CLAUDE.md/hooks. Caller tools are bridged in via tool_bridge_mcp.py
    and returned as tool_calls. Images go in as stream-json image blocks, so
    nothing calls the Anthropic API with the subscription token.
  - Streaming responses: result is emitted as chunked SSE after the subprocess finishes

Usage:
  python3 claude-code-api.py [--port 18782] [--debug]

systemd:
  ~/.config/systemd/user/claude-code-api.service
"""

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sys
import tempfile
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
# code execution as the service account, and the discovery path reads
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

# FALLBACK seed only. `_refresh_models()` discovers the authoritative live set
# from the Anthropic API on boot and hourly. A successful non-empty response
# replaces the served set, so new models appear and retired models disappear
# without a code change. A failed/empty response preserves the last-good set.
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
SEED_MODELS = frozenset({
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
})
VALID_MODELS = set(SEED_MODELS)

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


def _requests_tool_calls(body: dict) -> bool:
    """Return True if the caller expects OpenAI-style function calling.

    `tools: []` is sent freely by OpenAI SDKs and means nothing, and
    `tool_choice: "none"` explicitly asks for no call — neither is a request
    this wrapper has to refuse.
    """
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return False
    return body.get("tool_choice") != "none"


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


def messages_to_prompt(messages: list) -> tuple[str, str]:
    """
    Convert OpenAI-style messages list to (system_prompt, user_prompt) for claude CLI.
    Legacy CCAPI_AGENTIC_TEXT path only. Image blocks are dropped here.
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


def make_tool_calls_response(
    model: str,
    content: str,
    tool_calls: list,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict:
    """A chat completion that hands tool calls back to the caller."""
    resp = make_completion_response(model, content, prompt_tokens, completion_tokens)
    choice = resp["choices"][0]
    choice["message"] = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
    choice["finish_reason"] = "tool_calls"
    return resp


def make_tool_calls_sse_chunks(model: str, tool_calls: list) -> list[str]:
    """SSE chunks for tool calls: one delta per call, then finish_reason=tool_calls."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def chunk(delta: dict, finish: str | None) -> str:
        return "data: " + json.dumps({
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    out = [chunk({"tool_calls": [dict(call, index=i)]}, None) for i, call in enumerate(tool_calls)]
    out.append(chunk({}, "tool_calls"))
    return out


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

def _child_env() -> dict:
    """Environment for every claude child.

    The Claude CLI owns OAuth refresh and reads its current token from
    ~/.claude/.credentials.json. A token exported into the long-running service
    environment overrides that file forever, so explicitly remove it.
    """
    return {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}


@contextlib.asynccontextmanager
async def _claude_slot():
    """Hold one concurrency slot for the duration of a claude subprocess.

    The slot MUST be released on every exit path, including one taken between
    acquire() returning and the body below starting. Keeping the acquire
    outside the try/finally leaked a slot whenever the queue deadline expired
    at exactly that moment: `asyncio.timeout.__aexit__` raises TimeoutError
    with the slot already held, and nothing ever gave it back. Client
    disconnects drive this (Hermes retries a failed call 5x, cancelling the
    request task each time), so slots bled away until the semaphore was empty
    and EVERY later request sat the full QUEUE_TIMEOUT and failed with a bare
    TimeoutError — an empty "Streaming error:" in the log, "provider failed
    after retries" in Telegram. Only a restart cleared it. Track the
    acquisition and release it from a finally that also covers the acquire.
    """
    acquired = False
    try:
        try:
            async with asyncio.timeout(QUEUE_TIMEOUT):
                await sem().acquire()
                acquired = True
        except TimeoutError:
            # asyncio.TimeoutError stringifies to "", which is what made the
            # saturated-queue failure unreadable. Say what actually happened.
            raise RuntimeError(
                f"queue timeout: no free claude slot within {QUEUE_TIMEOUT}s "
                f"(CCAPI_MAX_CONCURRENT={os.environ.get('CCAPI_MAX_CONCURRENT', '3')})"
            ) from None
        yield
    finally:
        if acquired:
            sem().release()

def _agentic_text() -> bool:
    """CCAPI_AGENTIC_TEXT=1 restores the pre-2026-10 behaviour for tool-free requests."""
    return os.environ.get("CCAPI_AGENTIC_TEXT", "").strip().lower() in ("1", "true", "yes")


async def _run_claude_json(model: str, prompt, system: str) -> tuple[str, dict]:
    """Serve a request without caller tools. Returns (text, usage).

    By default this is one isolated turn through `_run_claude_cli`: no built-in
    tools, no service-account settings, CLAUDE.md or hooks, images included. A
    chat completion should not be able to run commands, and an answer should not
    depend on whose home directory the service runs in.

    CCAPI_AGENTIC_TEXT=1 restores the old path below, where claude runs its own
    Bash/Read/Write loop under --dangerously-skip-permissions (text only, and
    CLAUDE_API_LOAD_MCP applies). Nothing measured on 2026-10-05 needed it.
    """
    if not _agentic_text():
        text, _calls, usage = await _run_claude_cli(model, system, prompt)
        return text, usage

    if isinstance(prompt, list):
        prompt = _text_of(prompt)
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

    async with _claude_slot():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_text.encode()), timeout=REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude timed out after {REQUEST_TIMEOUT}s")

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


# ─── Caller tools: native tool_use through an MCP bridge ─────────────────────
#
# `claude --print` runs its own tool loop and never hands tool_calls back. So the
# caller's tools are shown to claude as a real MCP server (tool_bridge_mcp.py),
# with every built-in tool disabled. When claude calls one, we read the tool_use
# blocks off the stream-json output, wait for the message to close (so parallel
# calls are all captured), kill the process group, and return them as OpenAI
# `tool_calls`. The bridge never executes or answers anything.
#
# Stateless by design: the next request carries the tool results inside its
# message history, which is rendered into the prompt. That survives restarts,
# client retries and client-side history compression, and needs no session
# store. Measured 2026-10-05 against Hermes' real 30-tool set: 3-5s per call,
# parallel calls captured, prompt cache hit on the repeated prefix.
#
# Isolation matters as much as the bridge. Without --setting-sources "" and a
# replaced system prompt, every request inherited the service account's
# CLAUDE.md, SessionStart hooks and the full Claude Code system prompt
# (~13k tokens per call, and the model greeted the operator by name).

BRIDGE_SERVER = "caller"
BRIDGE_PREFIX = f"mcp__{BRIDGE_SERVER}__"
BRIDGE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_bridge_mcp.py")
# Anthropic tool names: ^[a-zA-Z0-9_-]{1,64}$, and the MCP prefix counts.
_TOOL_NAME_MAX = 64 - len(BRIDGE_PREFIX)
_TOOL_NAME_BAD = re.compile(r"[^a-zA-Z0-9_-]")
# stream-json lines carry whole messages (and the init event lists every tool),
# far past asyncio's 64 KiB default line limit.
_STREAM_LINE_LIMIT = 64 * 1024 * 1024

TRANSCRIPT_PREAMBLE = (
    "The conversation so far is below, oldest first. Continue it as the assistant: "
    "respond to the latest message. To use a tool, call it through your tool "
    "interface. Never write tool calls or tool results as text, and never invent "
    "a tool result."
)


def _exposed_tool_names(tools: list) -> dict[str, str]:
    """Map the name claude sees -> the caller's original function name.

    Names that are too long or carry characters the API refuses are rewritten
    to a stable, unique form so the request does not fail outright.
    """
    mapping: dict[str, str] = {}
    for tool in tools:
        name = (tool.get("function") or {}).get("name") or ""
        exposed = name
        if len(name) > _TOOL_NAME_MAX or _TOOL_NAME_BAD.search(name) or not name:
            digest = hashlib.sha256(name.encode()).hexdigest()[:8]
            exposed = f"{_TOOL_NAME_BAD.sub('_', name)[:_TOOL_NAME_MAX - 9]}_{digest}"
        mapping[exposed] = name
    return mapping


def _bridge_tool_specs(tools: list, names: dict[str, str]) -> list[dict]:
    """OpenAI tool definitions -> MCP tools/list entries, in exposed-name space."""
    by_original = {orig: exposed for exposed, orig in names.items()}
    specs = []
    for tool in tools:
        fn = tool.get("function") or {}
        schema = fn.get("parameters") or {}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        specs.append({
            "name": by_original[fn.get("name") or ""],
            "description": fn.get("description") or "",
            "inputSchema": schema,
        })
    return specs


def _tool_choice_instruction(tool_choice) -> str:
    """OpenAI tool_choice -> a system prompt line. "auto"/absent needs none."""
    if tool_choice == "required":
        return "You must call at least one tool in this response."
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return f"You must call the tool `{name}` in this response."
    return ""


def _text_of(content) -> str:
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return content or ""


def messages_to_cli_content(messages: list) -> tuple[str, list]:
    """Render OpenAI messages into (system_prompt, user content blocks) for claude.

    A lone user message keeps its own blocks (text and images, in order). A
    longer conversation, including tool_calls and tool results, is rendered as
    one text transcript; any images in it are numbered in the text and attached
    after it as real image blocks.
    """
    system_parts: list[str] = []
    turns: list[dict] = []
    for msg in messages:
        if msg.get("role") == "system":
            system_parts.append(_text_of(msg.get("content")))
        else:
            turns.append(msg)
    system = "\n\n".join(p for p in system_parts if p)

    if len(turns) == 1 and turns[0].get("role") == "user":
        return system, _openai_content_to_anthropic(turns[0].get("content") or "")

    images: list[dict] = []

    def body(content) -> str:
        if not isinstance(content, list):
            return content or ""
        parts = []
        for block in _openai_content_to_anthropic(content):
            if block["type"] == "image":
                images.append(block)
                parts.append(f"[image {len(images)}, attached below]")
            else:
                parts.append(block.get("text", ""))
        return "\n".join(parts)

    blocks = [TRANSCRIPT_PREAMBLE, ""]
    for msg in turns:
        role = msg.get("role")
        text = body(msg.get("content"))
        if role == "user":
            blocks.append(f"<user>\n{text}\n</user>")
        elif role == "assistant":
            parts = [text] if text else []
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                parts.append(
                    f'[called tool {fn.get("name")} id={call.get("id")} '
                    f'arguments={fn.get("arguments") or "{}"}]'
                )
            blocks.append("<assistant>\n" + "\n".join(parts) + "\n</assistant>")
        elif role == "tool":
            blocks.append(
                f'<tool_result id="{msg.get("tool_call_id", "")}">\n{text}\n</tool_result>'
            )
    content = [{"type": "text", "text": "\n\n".join(blocks)}]
    for i, image in enumerate(images, 1):
        content += [{"type": "text", "text": f"[image {i}]"}, image]
    return system, content


def _kill_group(proc) -> None:
    """Kill claude and the bridge it spawned (they share a process group)."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _run_claude_cli(
    model: str, system: str, content, tools=(), tool_choice=None,
) -> tuple[str, list[dict], dict]:
    """Run one isolated claude turn, with the caller's tools (if any) bridged in.

    `content` is a string or Anthropic user content blocks (text and images),
    sent as a stream-json user message, which is how images reach the model
    without calling the API directly. Returns (text, tool_calls, usage).
    tool_calls are OpenAI-shaped and carry claude's own toolu_ ids, so the
    caller's tool_call_id round-trips cleanly.
    """
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    stdin_line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
    names = _exposed_tool_names(tools)
    instruction = _tool_choice_instruction(tool_choice)
    if instruction:
        system = f"{system}\n\n{instruction}" if system else instruction

    with tempfile.TemporaryDirectory(prefix="ccapi-tools-") as tmp:
        tools_path = os.path.join(tmp, "tools.json")
        with open(tools_path, "w") as f:
            json.dump(_bridge_tool_specs(tools, names), f)
        # A file, not an argument: Hermes' system prompt alone can pass the
        # kernel's 128 KiB single-argument limit.
        system_path = os.path.join(tmp, "system.txt")
        with open(system_path, "w") as f:
            f.write(system or "You are a helpful assistant.")
        mcp_config = {"mcpServers": {BRIDGE_SERVER: {
            "command": sys.executable, "args": [BRIDGE_SCRIPT, tools_path],
        }}} if names else {"mcpServers": {}}
        cmd = [
            "claude", "--print",
            "--model", model,
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",
            "--no-session-persistence",
            "--setting-sources", "",
            "--system-prompt-file", system_path,
            "--tools", "",
            "--strict-mcp-config", "--mcp-config", json.dumps(mcp_config),
        ]
        if names:
            cmd += ["--allowedTools", ",".join(BRIDGE_PREFIX + n for n in names)]
        log.debug("Running (cli): %d tools | stdin=%d chars", len(names), len(stdin_line))

        text_parts: list[str] = []
        calls: list[dict] = []
        usage: dict = {}
        result_text: str | None = None
        async with _claude_slot():
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
                cwd=tmp,
                start_new_session=True,
                limit=_STREAM_LINE_LIMIT,
            )
            # Drained concurrently: a full stderr pipe would stall claude
            # while we wait on stdout.
            stderr_task = asyncio.create_task(proc.stderr.read())
            try:
                proc.stdin.write(stdin_line.encode())
                await proc.stdin.drain()
                proc.stdin.close()
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async for raw in proc.stdout:
                        try:
                            ev = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        kind = ev.get("type")
                        if kind == "assistant":
                            for block in ev.get("message", {}).get("content", []):
                                if block.get("type") == "text" and block.get("text"):
                                    text_parts.append(block["text"])
                                elif block.get("type") == "tool_use":
                                    name = block.get("name", "")
                                    if not name.startswith(BRIDGE_PREFIX):
                                        continue  # built-ins are disabled; ignore anything else
                                    exposed = name[len(BRIDGE_PREFIX):]
                                    calls.append({
                                        "id": block["id"],
                                        "type": "function",
                                        "function": {
                                            "name": names.get(exposed, exposed),
                                            "arguments": json.dumps(block.get("input") or {}),
                                        },
                                    })
                        elif kind == "stream_event":
                            event = ev.get("event", {})
                            etype = event.get("type")
                            if etype == "message_start":
                                usage.update(event.get("message", {}).get("usage") or {})
                            elif etype == "message_delta":
                                usage.update(event.get("usage") or {})
                            elif etype == "message_stop" and calls:
                                # Every tool_use of this message is in. Stop
                                # before claude waits on a bridge that never answers.
                                break
                        elif kind == "result":
                            if ev.get("is_error"):
                                raise RuntimeError(ev.get("result") or "Claude returned an error")
                            result_text = ev.get("result")
                            usage.update(ev.get("usage") or {})
                            break
            except TimeoutError:
                raise RuntimeError(f"claude timed out after {REQUEST_TIMEOUT}s") from None
            finally:
                _kill_group(proc)
                await proc.wait()
                if calls or result_text is not None:
                    stderr_task.cancel()

        if not calls and result_text is None:
            err = (await stderr_task).decode(errors="replace")[:500]
            raise RuntimeError(f"claude exited {proc.returncode} without a result: {err}")

    text = result_text if (result_text is not None and not calls) else "\n\n".join(text_parts)
    return text, calls, usage


async def _refresh_models() -> None:
    """Replace VALID_MODELS from a successful Anthropic catalog response.

    Auth: the token in ~/.claude/.credentials.json is an OAuth access token, and
    an OAuth token must be sent as `Authorization: Bearer`, which the SDK spells
    `auth_token=`. It was previously passed as `api_key=`, which the SDK sends as
    the `x-api-key` header, and Anthropic rejects that with
    "authentication_error: API key is invalid". Discovery had therefore returned
    401 on every attempt since at least 2026-08-15 while the service looked
    healthy, because the failure path just falls back to the static list.

    Successful discovery is authoritative and subtractive: it adds newly
    available models and removes retired ones. Failures and empty responses
    preserve the last-good set, so an outage cannot erase the catalog.
    """
    global _last_model_refresh, _discovery_ok, _discovery_error
    try:
        token = _get_oauth_token()
        client = anthropic_sdk.AsyncAnthropic(auth_token=token)
        page = await asyncio.wait_for(client.models.list(limit=100), timeout=15)
        discovered = {m.id for m in page.data if m.id.startswith("claude-")}
        if discovered:
            added = discovered - VALID_MODELS
            removed = VALID_MODELS - discovered
            VALID_MODELS.clear()
            VALID_MODELS.update(discovered)
            _last_model_refresh = time.time()
            _discovery_ok = True
            _discovery_error = None
            if added:
                log.info("Model discovery: +%d new — %s", len(added), ", ".join(sorted(added)))
            if removed:
                log.info("Model discovery: -%d retired — %s", len(removed), ", ".join(sorted(removed)))
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


async def _handle_tool_completion(
    request: web.Request, body: dict, model: str, messages: list, streaming: bool,
) -> web.StreamResponse:
    """Serve a request that carries `tools`, via the MCP bridge."""
    tools = body["tools"]
    system, content = messages_to_cli_content(messages)
    log.info("→ %s | stream=%s | model=%s | tools=%d | %d msgs%s", request.remote, streaming,
             model, len(tools), len(messages), " | images" if _has_images(messages) else "")
    try:
        text, calls, usage = await _run_claude_cli(
            model, system, content, tools, body.get("tool_choice"),
        )
    except Exception as exc:
        log.error("Tool request error: %s", exc)
        return web.json_response({"error": {"message": str(exc), "type": "server_error"}}, status=500)

    log.info("← %s | model=%s | %d tool calls%s | %d output chars", request.remote, model,
             len(calls), f" ({', '.join(c['function']['name'] for c in calls)})" if calls else "",
             len(text))
    prompt_tokens = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                     + usage.get("cache_creation_input_tokens", 0))
    completion_tokens = usage.get("output_tokens", 0)

    if not streaming:
        if calls:
            return web.json_response(make_tool_calls_response(
                model, text, calls, prompt_tokens, completion_tokens))
        return web.json_response(make_completion_response(
            model, text, prompt_tokens, completion_tokens))

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    try:
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
        if calls:
            for c in make_tool_calls_sse_chunks(model, calls):
                await response.write(c.encode())
        else:
            await response.write(make_sse_chunk(model, "", finish=True).encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
    except Exception:
        pass  # client already disconnected
    return response


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

    if _requests_tool_calls(body):
        return await _handle_tool_completion(request, body, model, messages, streaming)

    if _agentic_text():
        system, prompt = messages_to_prompt(messages)
    else:
        system, prompt = messages_to_cli_content(messages)
    log.info("→ %s | stream=%s | model=%s | %d msgs%s", request.remote, streaming, model,
             len(messages), " | images" if _has_images(messages) else "")

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
            text, usage = await _run_claude_json(model, prompt, system)
        except Exception as exc:
            log.error("Non-stream error: %s", exc)
            return web.json_response(
                {"error": {"message": str(exc), "type": "server_error"}},
                status=500,
            )

        log.info("← %s | model=%s | %d output chars", request.remote, model, len(text))
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
