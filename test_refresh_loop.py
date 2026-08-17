#!/usr/bin/env python3
"""
Tests for the periodic model-refresh loop.

Scope: that discovery refreshes on an INTERVAL, not only at startup and not only
when someone happens to request /v1/models. Before `_refresh_loop` existed, the
live service ran one refresh in 19.5 hours on an hourly setting, because
`MODEL_REFRESH_INTERVAL` was a TTL checked inside `handle_models()` rather than a
period anything actually slept on.

No network, no credentials, no subprocess: `_refresh_models` is stubbed and
`MODEL_REFRESH_INTERVAL` is patched down so the test does not sleep an hour.
"""
import asyncio
import unittest
from unittest import mock

import server


class RefreshLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_refreshes_repeatedly_without_any_request(self):
        """The whole point: refreshes happen with zero traffic to /v1/models."""
        calls = []

        async def fake_refresh():
            calls.append(1)

        with mock.patch.object(server, "_refresh_models", fake_refresh), \
             mock.patch.object(server, "MODEL_REFRESH_INTERVAL", 0.01):
            task = asyncio.create_task(server._refresh_loop())
            await asyncio.sleep(0.08)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # More than one, which is what distinguishes a LOOP from the startup
        # one-shot that already existed. A single call would pass a naive
        # "did it refresh" assertion while the defect was fully present.
        self.assertGreater(len(calls), 1, f"expected repeated refreshes, got {len(calls)}")

    async def test_one_failing_cycle_does_not_end_the_loop(self):
        """A transient failure must cost one cycle, not all future ones."""
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")

        with mock.patch.object(server, "_refresh_models", flaky), \
             mock.patch.object(server, "MODEL_REFRESH_INTERVAL", 0.01):
            task = asyncio.create_task(server._refresh_loop())
            await asyncio.sleep(0.08)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.assertGreater(len(calls), 1, "loop died on the first exception")

    async def test_loop_is_started_and_cancelled_by_the_app_lifecycle(self):
        async def noop():
            return None

        with mock.patch.object(server, "_refresh_models", noop):
            app = server.build_app()
            self.assertIn(server._on_startup, app.on_startup)
            self.assertIn(server._on_cleanup, app.on_cleanup)
            await server._on_startup(app)
            task = app["refresh_loop"]
            self.assertFalse(task.done())
            await server._on_cleanup(app)
            await asyncio.sleep(0)
            self.assertTrue(task.cancelled() or task.done())


if __name__ == "__main__":
    unittest.main()
