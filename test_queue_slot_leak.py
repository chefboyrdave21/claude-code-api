#!/usr/bin/env python3
"""
The concurrency semaphore must survive a queue deadline that expires at the
moment acquire() succeeds. It used to leak a slot there, and three leaks left
every later request to die on the 90s QUEUE_TIMEOUT with an empty TimeoutError.
"""
import asyncio
import unittest
from unittest import mock

import server


class _ExpiringTimeout:
    """`asyncio.timeout()` whose deadline expires once the body completes."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        raise TimeoutError


class QueueSlotLeakTests(unittest.IsolatedAsyncioTestCase):
    async def test_slot_released_when_deadline_expires_just_after_acquire(self):
        server._sem = asyncio.Semaphore(3)

        with mock.patch.object(server.asyncio, "timeout", _ExpiringTimeout):
            with self.assertRaises(Exception):
                await server._run_claude_json("claude-opus-5", "hi", "")

        self.assertEqual(server._sem._value, 3, "a slot leaked")

    async def test_queue_timeout_reports_a_usable_message(self):
        """Empty error strings are what made the saturated queue unreadable."""
        server._sem = asyncio.Semaphore(1)
        await server._sem.acquire()

        with mock.patch.object(server, "QUEUE_TIMEOUT", 0.05):
            with self.assertRaises(Exception) as ctx:
                await server._run_claude_json("claude-opus-5", "hi", "")

        self.assertTrue(str(ctx.exception).strip(), "queue timeout said nothing")


if __name__ == "__main__":
    unittest.main()
