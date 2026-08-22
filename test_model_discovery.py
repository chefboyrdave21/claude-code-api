#!/usr/bin/env python3
"""Authoritative model discovery and OAuth child-environment tests."""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import server


class ModelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = set(server.VALID_MODELS)

    def tearDown(self):
        server.VALID_MODELS.clear()
        server.VALID_MODELS.update(self.original)

    async def test_success_replaces_catalog_and_retires_absent_ids(self):
        server.VALID_MODELS.clear()
        server.VALID_MODELS.update({"claude-retired", "claude-still-live"})
        page = SimpleNamespace(data=[
            SimpleNamespace(id="claude-still-live"),
            SimpleNamespace(id="claude-new"),
            SimpleNamespace(id="not-claude"),
        ])
        client = SimpleNamespace(models=SimpleNamespace(list=mock.AsyncMock(return_value=page)))
        with mock.patch.object(server, "_get_oauth_token", return_value="oauth"), \
             mock.patch.object(server.anthropic_sdk, "AsyncAnthropic", return_value=client):
            await server._refresh_models()

        self.assertEqual(server.VALID_MODELS, {"claude-still-live", "claude-new"})

    async def test_failed_discovery_preserves_last_good_catalog(self):
        server.VALID_MODELS.clear()
        server.VALID_MODELS.update({"claude-last-good"})
        client = SimpleNamespace(models=SimpleNamespace(
            list=mock.AsyncMock(side_effect=RuntimeError("offline"))))
        with mock.patch.object(server, "_get_oauth_token", return_value="oauth"), \
             mock.patch.object(server.anthropic_sdk, "AsyncAnthropic", return_value=client):
            await server._refresh_models()

        self.assertEqual(server.VALID_MODELS, {"claude-last-good"})

    async def test_empty_discovery_preserves_last_good_catalog(self):
        server.VALID_MODELS.clear()
        server.VALID_MODELS.update({"claude-last-good"})
        page = SimpleNamespace(data=[])
        client = SimpleNamespace(models=SimpleNamespace(list=mock.AsyncMock(return_value=page)))
        with mock.patch.object(server, "_get_oauth_token", return_value="oauth"), \
             mock.patch.object(server.anthropic_sdk, "AsyncAnthropic", return_value=client):
            await server._refresh_models()

        self.assertEqual(server.VALID_MODELS, {"claude-last-good"})

    async def test_cli_child_drops_stale_oauth_environment_override(self):
        proc = SimpleNamespace(returncode=0, communicate=mock.AsyncMock(
            return_value=(b'{"result":"ok","usage":{}}', b"")))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "stale"}), \
             mock.patch.object(server.asyncio, "create_subprocess_exec",
                               mock.AsyncMock(return_value=proc)) as spawn:
            await server._run_claude_json("claude-test", "hi", "")

        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", spawn.await_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
