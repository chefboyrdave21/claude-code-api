#!/usr/bin/env python3
"""
Requests carrying `tools` are served through the MCP bridge: claude sees the
caller's tools as native tools, and its tool_use blocks come back as OpenAI
`tool_calls`.

The failure this guards against is the original one. Until 2026-09-15 a
`tools` array was dropped on the floor and the caller got a normal
`finish_reason=stop` text answer, indistinguishable from a model that chose not
to call anything; a Hermes cron job reported "2 events added" having touched
nothing. A tool request must never reach the plain text path again.
"""
import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import server


_TOOLS = [{
    "type": "function",
    "function": {"name": "get_time", "description": "now",
                 "parameters": {"type": "object", "properties": {}}},
}]
_CALL = {"id": "toolu_1", "type": "function",
         "function": {"name": "get_time", "arguments": "{}"}}


class ToolRequestRoutingTests(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", server.handle_chat_completions)
        return app

    async def _post(self, body: dict):
        return await self.client.post("/v1/chat/completions", json=body)

    async def test_tool_request_never_reaches_the_text_path(self):
        with mock.patch.object(server, "_run_claude_json") as text_path, \
             mock.patch.object(server, "_run_claude_cli") as tool_path:
            tool_path.return_value = ("", [_CALL], {"output_tokens": 3})
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "what time is it?"}],
                "tools": _TOOLS,
            })
        self.assertEqual(resp.status, 200)
        text_path.assert_not_called()
        tool_path.assert_called_once()

    async def test_tool_calls_come_back_in_openai_shape(self):
        with mock.patch.object(server, "_run_claude_cli") as tool_path:
            tool_path.return_value = ("Checking.", [_CALL], {"output_tokens": 3})
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "what time is it?"}],
                "tools": _TOOLS,
            })
        choice = (await resp.json())["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"], [_CALL])
        self.assertEqual(choice["message"]["content"], "Checking.")

    async def test_streaming_tool_calls(self):
        with mock.patch.object(server, "_run_claude_cli") as tool_path:
            tool_path.return_value = ("", [_CALL], {})
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _TOOLS,
                "stream": True,
            })
            raw = await resp.text()
        events = [json.loads(line[6:]) for line in raw.splitlines()
                  if line.startswith("data: {")]
        deltas = [e["choices"][0]["delta"] for e in events]
        calls = [d["tool_calls"][0] for d in deltas if "tool_calls" in d]
        self.assertEqual(calls[0]["id"], "toolu_1")
        self.assertEqual(calls[0]["index"], 0)
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertTrue(raw.rstrip().endswith("data: [DONE]"))

    async def test_tool_free_answer_finishes_with_stop(self):
        with mock.patch.object(server, "_run_claude_cli") as tool_path:
            tool_path.return_value = ("It is noon.", [], {})
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _TOOLS,
            })
        choice = (await resp.json())["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["content"], "It is noon.")

    async def test_tool_errors_are_loud(self):
        with mock.patch.object(server, "_run_claude_cli", side_effect=RuntimeError("boom")):
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _TOOLS,
            })
        self.assertEqual(resp.status, 500)

    async def test_tool_choice_none_uses_the_text_path(self):
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
        """OpenAI SDKs send `tools: []` freely."""
        with mock.patch.object(server, "_run_claude_json") as runner:
            runner.return_value = ("hello", {"input_tokens": 1, "output_tokens": 1})
            resp = await self._post({
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [],
            })
        self.assertEqual(resp.status, 200)
        runner.assert_called_once()


class TranscriptTests(unittest.TestCase):
    def test_lone_user_message_passes_through(self):
        system, prompt = server.messages_to_cli_content([
            {"role": "system", "content": "be Lumina"},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual((system, prompt), ("be Lumina", [{"type": "text", "text": "hi"}]))

    def test_tool_round_trip_is_rendered_with_ids(self):
        _, content = server.messages_to_cli_content([
            {"role": "user", "content": "time?"},
            {"role": "assistant", "content": None, "tool_calls": [_CALL]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "12:00"},
        ])
        self.assertEqual(len(content), 1)
        prompt = content[0]["text"]
        self.assertIn("[called tool get_time id=toolu_1 arguments={}]", prompt)
        self.assertIn('<tool_result id="toolu_1">\n12:00\n</tool_result>', prompt)
        self.assertTrue(prompt.startswith(server.TRANSCRIPT_PREAMBLE))

    def test_lone_user_message_keeps_its_image(self):
        _, content = server.messages_to_cli_content([{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}])
        self.assertEqual(content[1], {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "AAAA"}})

    def test_images_in_a_conversation_are_numbered_and_attached(self):
        _, content = server.messages_to_cli_content([
            {"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "https://x/cat.png"}},
            ]},
            {"role": "assistant", "content": "A cat."},
            {"role": "user", "content": "what color?"},
        ])
        self.assertIn("[image 1, attached below]", content[0]["text"])
        self.assertEqual(content[1], {"type": "text", "text": "[image 1]"})
        self.assertEqual(content[2]["source"], {"type": "url", "url": "https://x/cat.png"})

    def test_tool_choice_instructions(self):
        self.assertEqual(server._tool_choice_instruction("auto"), "")
        self.assertIn("at least one", server._tool_choice_instruction("required"))
        self.assertIn("`get_time`", server._tool_choice_instruction(
            {"type": "function", "function": {"name": "get_time"}}))


class ToolNameTests(unittest.TestCase):
    def test_short_names_are_unchanged(self):
        self.assertEqual(server._exposed_tool_names(_TOOLS), {"get_time": "get_time"})

    def test_long_and_invalid_names_are_rewritten_and_map_back(self):
        long_name = "x" * 80
        names = server._exposed_tool_names([
            {"function": {"name": long_name}}, {"function": {"name": "a.b"}},
        ])
        for exposed, original in names.items():
            self.assertLessEqual(len(server.BRIDGE_PREFIX + exposed), 64)
            self.assertRegex(exposed, r"^[a-zA-Z0-9_-]+$")
        self.assertEqual(set(names.values()), {long_name, "a.b"})


_FAKE_TOOL_USE = r'''#!/usr/bin/env python3
import json, sys, time
sys.stdin.read()
def out(o): print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init"})
out({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "On it."}]}})
for i, city in enumerate(["Paris", "Tokyo"]):
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"toolu_{i}",
         "name": "mcp__caller__get_weather", "input": {"city": city}}]}})
out({"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 9}}})
out({"type": "stream_event", "event": {"type": "message_stop"}})
time.sleep(60)  # a real claude would now wait on the bridge forever
'''

_FAKE_TEXT = r'''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "noon"}]}}))
print(json.dumps({"type": "result", "is_error": False, "result": "It is noon.",
                  "usage": {"input_tokens": 2, "output_tokens": 4}}))
'''


class TextPathIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Requests without tools must not get Claude Code's own tools or settings."""

    async def _argv(self, env: dict) -> list:
        server._sem = asyncio.Semaphore(3)
        spawn = mock.AsyncMock(side_effect=RuntimeError("stop before spawning"))
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(server.asyncio, "create_subprocess_exec", spawn):
            with self.assertRaises(RuntimeError):
                await server._run_claude_json("claude-opus-5", "hi", "")
        return list(spawn.await_args.args)

    async def test_default_text_request_is_isolated(self):
        argv = await self._argv({"CCAPI_AGENTIC_TEXT": ""})
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertNotIn("--allowedTools", argv)

    async def test_agentic_flag_restores_the_old_path(self):
        argv = await self._argv({"CCAPI_AGENTIC_TEXT": "1"})
        self.assertIn("--dangerously-skip-permissions", argv)


class RunClaudeToolsTests(unittest.IsolatedAsyncioTestCase):
    """Drive `_run_claude_cli` against a fake `claude` emitting real stream-json."""

    def _fake_claude(self, script: str) -> None:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "claude")
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, stat.S_IRWXU)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        server._sem = asyncio.Semaphore(3)

    async def test_parallel_tool_calls_are_captured_and_claude_is_killed(self):
        self._fake_claude(_FAKE_TOOL_USE)
        started = time.monotonic()
        text, calls, usage = await server._run_claude_cli(
            "claude-opus-5", "sys", "weather?", [{"function": {"name": "get_weather"}}])
        self.assertLess(time.monotonic() - started, 20, "did not stop at message_stop")
        self.assertEqual(text, "On it.")
        self.assertEqual([c["id"] for c in calls], ["toolu_0", "toolu_1"])
        self.assertEqual(calls[1]["function"],
                         {"name": "get_weather", "arguments": '{"city": "Tokyo"}'})
        self.assertEqual(usage["output_tokens"], 9)
        self.assertEqual(server._sem._value, 3, "a slot leaked")

    async def test_plain_answer_uses_the_result_event(self):
        self._fake_claude(_FAKE_TEXT)
        text, calls, usage = await server._run_claude_cli(
            "claude-opus-5", "", "time?", _TOOLS)
        self.assertEqual((text, calls), ("It is noon.", []))
        self.assertEqual(usage["output_tokens"], 4)


if __name__ == "__main__":
    unittest.main()
