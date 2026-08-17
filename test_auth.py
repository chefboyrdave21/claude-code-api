#!/usr/bin/env python3
"""
Tests for the claude-code-api shared-secret gate.

Run:
    python3 -m unittest discover -v

Scope. These tests cover exactly one thing: that no request without a valid
CCAPI_TOKEN can reach a handler, and that GET /health still can. They do not
call the `claude` CLI, do not touch the network, and do not read
~/.claude/.credentials.json, so they run anywhere aiohttp is importable.

Two collaborators are stubbed for that reason:
  - `server._refresh_models`, which the app's on_startup hook fires and which
    would otherwise read the credentials file and call the Anthropic API.
  - `server._run_claude_json`, which would otherwise spawn a subprocess. Stubbing
    it is what makes "the request reached the handler" observable: if the
    middleware lets a request through, the canned reply comes back, and if it
    does not, the stub is never called.
"""

import os
import unittest
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

import server

GOOD_TOKEN = "unit-test-token-not-a-real-secret"
CANNED_REPLY = "handler reached"

# Every route the app registers, minus /health. Both the /v1 spelling and the
# unprefixed alias are listed on purpose: the aliases at build_app() are easy to
# add and easy to forget to protect, and an auth gate with one unguarded alias is
# not an auth gate.
GUARDED_ROUTES = [
    ("POST", "/v1/chat/completions"),
    ("POST", "/chat/completions"),
    ("GET", "/v1/models"),
    ("GET", "/models"),
]

CHAT_BODY = {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]}


async def _fake_refresh_models() -> None:
    """No-op stand-in for model discovery, so startup makes no network call."""
    return None


async def _fake_run_claude_json(model, prompt, system):
    """Stand-in for the subprocess. Reaching this means auth let the request in."""
    return CANNED_REPLY, {"input_tokens": 1, "output_tokens": 2}


class AuthTestBase(AioHTTPTestCase):
    """Builds the real app with the real middleware, only the leaves stubbed."""

    #: Value for CCAPI_TOKEN during the test. None means "unset".
    token_env: str | None = GOOD_TOKEN

    async def get_application(self):
        patches = [
            mock.patch.object(server, "_refresh_models", _fake_refresh_models),
            mock.patch.object(server, "_run_claude_json", _fake_run_claude_json),
        ]
        env = {} if self.token_env is None else {server.CCAPI_TOKEN_ENV: self.token_env}
        patches.append(mock.patch.dict(os.environ, env, clear=False))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        if self.token_env is None:
            # mock.patch.dict cannot remove a key, and the developer running this
            # may well have CCAPI_TOKEN exported in their own shell. Drop it, and
            # let the patch.dict cleanup above restore whatever was there.
            os.environ.pop(server.CCAPI_TOKEN_ENV, None)
        return server.build_app()


class TestTokenConfigured(AuthTestBase):
    """CCAPI_TOKEN is set. The normal production state."""

    token_env = GOOD_TOKEN

    async def test_health_needs_no_token(self):
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["auth"], "required")

    async def test_health_leaks_no_secret(self):
        """The one open route must not echo the secret it is protecting."""
        resp = await self.client.get("/health")
        self.assertNotIn(GOOD_TOKEN, await resp.text())

    async def test_no_token_is_401(self):
        for method, path in GUARDED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                resp = await self.client.request(method, path, json=CHAT_BODY)
                self.assertEqual(resp.status, 401)
                self.assertEqual((await resp.json())["error"]["code"], "invalid_api_key")

    async def test_wrong_token_is_401(self):
        for header in ({"Authorization": "Bearer wrong-token"},
                       {server.AUTH_HEADER: "wrong-token"}):
            for method, path in GUARDED_ROUTES:
                with self.subTest(route=f"{method} {path}", header=list(header)[0]):
                    resp = await self.client.request(method, path, json=CHAT_BODY,
                                                     headers=header)
                    self.assertEqual(resp.status, 401)

    async def test_token_prefix_is_401(self):
        """Guards against a regression to startswith/in rather than a full compare."""
        resp = await self.client.post(
            "/v1/chat/completions", json=CHAT_BODY,
            headers={"Authorization": f"Bearer {GOOD_TOKEN[:-1]}"},
        )
        self.assertEqual(resp.status, 401)

    async def test_token_with_extra_suffix_is_401(self):
        resp = await self.client.post(
            "/v1/chat/completions", json=CHAT_BODY,
            headers={"Authorization": f"Bearer {GOOD_TOKEN}x"},
        )
        self.assertEqual(resp.status, 401)

    async def test_empty_bearer_is_401(self):
        resp = await self.client.post(
            "/v1/chat/completions", json=CHAT_BODY,
            headers={"Authorization": "Bearer "},
        )
        self.assertEqual(resp.status, 401)

    async def test_correct_token_bearer_passes_through(self):
        resp = await self.client.post(
            "/v1/chat/completions", json=CHAT_BODY,
            headers={"Authorization": f"Bearer {GOOD_TOKEN}"},
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["choices"][0]["message"]["content"], CANNED_REPLY)

    async def test_correct_token_custom_header_passes_through(self):
        resp = await self.client.post(
            "/v1/chat/completions", json=CHAT_BODY,
            headers={server.AUTH_HEADER: GOOD_TOKEN},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            (await resp.json())["choices"][0]["message"]["content"], CANNED_REPLY)

    async def test_bearer_scheme_is_case_insensitive(self):
        """RFC 7235 says the scheme is case-insensitive; clients vary."""
        resp = await self.client.post(
            "/v1/chat/completions", json=CHAT_BODY,
            headers={"Authorization": f"bearer {GOOD_TOKEN}"},
        )
        self.assertEqual(resp.status, 200)

    async def test_correct_token_reaches_models(self):
        resp = await self.client.get(
            "/v1/models", headers={"Authorization": f"Bearer {GOOD_TOKEN}"})
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["object"], "list")

    async def test_streaming_passes_through_and_terminates(self):
        """The middleware must not break the StreamResponse path."""
        resp = await self.client.post(
            "/v1/chat/completions",
            json={**CHAT_BODY, "stream": True},
            headers={"Authorization": f"Bearer {GOOD_TOKEN}"},
        )
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn(CANNED_REPLY.split(" ")[0], text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"), text[-80:])

    async def test_streaming_without_token_is_401(self):
        """A 401 must land before prepare(), as a real 401, not an SSE error frame."""
        resp = await self.client.post(
            "/v1/chat/completions", json={**CHAT_BODY, "stream": True})
        self.assertEqual(resp.status, 401)
        self.assertNotIn("text/event-stream", resp.headers.get("Content-Type", ""))


class TestTokenUnset(AuthTestBase):
    """CCAPI_TOKEN is unset. Must fail CLOSED, never default to open."""

    token_env = None

    async def test_health_still_answers(self):
        """The whole reason the process starts rather than exiting: stay diagnosable."""
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["auth"], "unconfigured")

    async def test_every_guarded_route_is_401(self):
        for method, path in GUARDED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                resp = await self.client.request(method, path, json=CHAT_BODY)
                self.assertEqual(resp.status, 401)

    async def test_any_presented_token_is_still_401(self):
        """Unset must not mean 'accept anything'."""
        for header in ({"Authorization": f"Bearer {GOOD_TOKEN}"},
                       {server.AUTH_HEADER: GOOD_TOKEN},
                       {"Authorization": "Bearer "},
                       {server.AUTH_HEADER: ""}):
            with self.subTest(header=header):
                resp = await self.client.post("/v1/chat/completions", json=CHAT_BODY,
                                              headers=header)
                self.assertEqual(resp.status, 401)

    async def test_401_says_how_to_configure_it(self):
        resp = await self.client.post("/v1/chat/completions", json=CHAT_BODY)
        self.assertIn(server.CCAPI_TOKEN_ENV, (await resp.json())["error"]["message"])

    async def test_subprocess_is_never_reached(self):
        """The point of the gate: no path to `claude` without a valid token."""
        with mock.patch.object(server, "_run_claude_json") as spy:
            resp = await self.client.post("/v1/chat/completions", json=CHAT_BODY)
            self.assertEqual(resp.status, 401)
            spy.assert_not_called()


class TestTokenBlank(AuthTestBase):
    """CCAPI_TOKEN set to whitespace is 'unset', not 'the secret is a space'."""

    token_env = "   "

    async def test_blank_token_is_treated_as_unset(self):
        self.assertEqual(server.expected_token(), "")
        for header in ({}, {"Authorization": "Bearer    "}, {server.AUTH_HEADER: "   "}):
            with self.subTest(header=header):
                resp = await self.client.post("/v1/chat/completions", json=CHAT_BODY,
                                              headers=header)
                self.assertEqual(resp.status, 401)


class TestUnauthenticatedPathsAreMinimal(unittest.TestCase):
    """A guard on the allowlist itself, which is the one place a mistake is silent."""

    def test_only_health_is_open(self):
        self.assertEqual(set(server.UNAUTHENTICATED_PATHS), {"/health"})


if __name__ == "__main__":
    unittest.main()
