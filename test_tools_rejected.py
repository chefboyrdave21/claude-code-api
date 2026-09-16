#!/usr/bin/env python3
"""
This wrapper cannot execute OpenAI-style function calls: `claude --print` runs
its own tool loop and never surfaces tool_calls back to us. Requests carrying a
`tools` array used to be dropped on the floor, and the caller got a normal
`finish_reason=stop` text answer back — indistinguishable from a model that had
simply chosen not to call anything.

That is the worst possible failure. Observed 2026-09-15: a Hermes cron job that
syncs a child's school calendar was pointed at this backend, its tools vanished,
and it reported "2 events added" having touched nothing. A loud 400 turns that
silent fabrication into a visible failure.
"""
import json
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import server


_TOOLS = [{
    "type": "function",
    "function": {"name": "get_time", "description": "now", "parameters": {}},
}]


class ToolsRejectedTests(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", server.handle_chat_completions)
        return app

    async def _post(self, body: dict):
        return await self.client.post("/v1/chat/completions", json=body)

    async def test_tools_request_is_rejected_not_silently_dropped(self):
        resp = await self._post({
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "what time is it?"}],
            "tools": _TOOLS,
        })
        self.assertEqual(resp.status, 400)
        payload = await resp.json()
        message = payload["error"]["message"]
        self.assertIn("tool", message.lower())
        # The caller has to be able to tell WHY, or they will just retry forever.
        self.assertIn("claude-code-api", message)

    async def test_rejection_happens_before_claude_is_invoked(self):
        """A 400 that still burned a `claude --print` subprocess is a half-fix."""
        with mock.patch.object(server, "_run_claude_json") as runner:
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _TOOLS,
            })
        self.assertEqual(resp.status, 400)
        runner.assert_not_called()

    async def test_streaming_tools_request_is_also_rejected(self):
        """Streaming callers are the ones that fabricate most convincingly."""
        resp = await self._post({
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": _TOOLS,
            "stream": True,
        })
        self.assertEqual(resp.status, 400)

    async def test_tool_choice_none_is_allowed_through(self):
        """`tool_choice: none` means the caller does not want a call at all."""
        with mock.patch.object(server, "_run_claude_json") as runner:
            runner.return_value = ("hello", {"input_tokens": 1, "output_tokens": 1})
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _TOOLS,
                "tool_choice": "none",
            })
        self.assertEqual(resp.status, 200)
        runner.assert_called_once()

    async def test_empty_tools_array_is_not_a_tool_request(self):
        """OpenAI SDKs send `tools: []` freely; that must not 400."""
        with mock.patch.object(server, "_run_claude_json") as runner:
            runner.return_value = ("hello", {"input_tokens": 1, "output_tokens": 1})
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [],
            })
        self.assertEqual(resp.status, 200)
        runner.assert_called_once()

    async def test_plain_request_is_untouched(self):
        with mock.patch.object(server, "_run_claude_json") as runner:
            runner.return_value = ("hello", {"input_tokens": 1, "output_tokens": 1})
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "hello")


if __name__ == "__main__":
    unittest.main()
