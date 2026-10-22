#!/usr/bin/env python3
"""
A claude child whose exit asyncio never notices must not hold a slot forever.

Twice (2026-10-11, 2026-10-21) ten <defunct> claude children held all ten
concurrency slots until a restart. The mechanism, reproduced below: a stale
selector registration on a reused fd number swallows the child's pidfd, so
PidfdChildWatcher never fires and `await proc.wait()` never returns.
"""
import asyncio
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import server

_REAL_PIDFD_OPEN = os.pidfd_open


def _poisoned_pidfd_open(pid, flags=0):
    """pidfd_open() that lands on an fd number with a stale selector entry.

    Faithful to production: an fd is registered as a reader, then its number is
    taken over by the pidfd without remove_reader. The watcher's _add_reader
    then goes through selector.modify(), which skips epoll_ctl because the
    event mask is unchanged, so the pidfd is never actually polled.
    """
    pidfd = _REAL_PIDFD_OPEN(pid, flags)
    r, w = os.pipe()
    asyncio.get_running_loop()._add_reader(r, lambda: None)
    os.dup2(pidfd, r)  # closes the registered pipe end; r is now the pidfd
    os.close(pidfd)
    os.close(w)
    return r


_FAKE_RESULT = r'''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False, "result": "pong",
                  "usage": {"input_tokens": 1, "output_tokens": 1}}))
'''


class PoisonedWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        server._sem = asyncio.Semaphore(3)
        for patcher in (
            mock.patch.object(os, "pidfd_open", _poisoned_pidfd_open),
            mock.patch.object(server, "REAP_GRACE", 0.3),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_claude(self, script: str) -> None:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "claude")
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, stat.S_IRWXU)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_mechanism_proc_wait_hangs_and_child_stays_a_zombie(self):
        """The bug itself, so a future asyncio that fixes it shows up here."""
        proc = await asyncio.create_subprocess_exec("true")
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(proc.wait(), 1.5)
        self.assertIn(proc.pid, server._zombie_children())
        os.waitpid(proc.pid, 0)

    async def test_reap_collects_a_child_the_watcher_missed(self):
        proc = await asyncio.create_subprocess_exec("true")
        rc = await asyncio.wait_for(server._reap(proc), 10)
        self.assertEqual(rc, 0)
        self.assertEqual(proc.returncode, 0, "transport was not told")
        self.assertNotIn(proc.pid, server._zombie_children())

    async def test_tool_path_releases_its_slot(self):
        self._fake_claude(_FAKE_RESULT)
        started = time.monotonic()
        text, calls, _usage = await asyncio.wait_for(
            server._run_claude_cli("claude-opus-5", "", "ping"), 15)
        self.assertEqual((text, calls), ("pong", []))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(server._sem._value, 3, "a slot leaked")

    async def test_agentic_text_path_releases_its_slot(self):
        self._fake_claude(r'''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"result": "pong", "usage": {"input_tokens": 1}}))
''')
        with mock.patch.dict(os.environ, {"CCAPI_AGENTIC_TEXT": "1"}):
            text, _usage = await asyncio.wait_for(
                server._run_claude_json("claude-opus-5", "ping", ""), 15)
        self.assertEqual(text, "pong")
        self.assertEqual(server._sem._value, 3, "a slot leaked")


class WatchdogPieceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_selector_fd_is_reported_and_cleared(self):
        loop = asyncio.get_running_loop()
        r, w = os.pipe()
        loop._add_reader(r, lambda: None)
        os.close(r)
        os.close(w)
        found = server._clear_stale_selector_fds()
        self.assertTrue(any(f.startswith(f"fd {r}:") for f in found), found)
        self.assertNotIn(r, loop._selector.get_map())

    async def test_live_fds_are_left_alone(self):
        loop = asyncio.get_running_loop()
        r, w = os.pipe()
        loop._add_reader(r, lambda: None)
        try:
            self.assertEqual(server._clear_stale_selector_fds(), [])
            self.assertIn(r, loop._selector.get_map())
        finally:
            loop._remove_reader(r)
            os.close(r)
            os.close(w)

    async def test_zombie_children_finds_an_unreaped_child(self):
        child = subprocess.Popen(["true"])
        deadline = time.monotonic() + 5
        while child.pid not in server._zombie_children():
            self.assertLess(time.monotonic(), deadline, "child never showed as a zombie")
            await asyncio.sleep(0.05)
        child.wait()
        self.assertNotIn(child.pid, server._zombie_children())


if __name__ == "__main__":
    unittest.main()
