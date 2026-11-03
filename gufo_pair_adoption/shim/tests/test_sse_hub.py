# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`gufo_masquerade.sse` — the progress hub.

The SSE *route* is covered in ``test_app.py``; here the hub is exercised
directly, which is the only sane way to reach its backpressure paths (a
wedged subscriber) without orchestrating slow HTTP clients.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gufo_masquerade.sse import (  # noqa: E402
    MAX_QUEUE,
    WILDCARD,
    ProgressHub,
    progress_event,
)


def ev(model: str = "m", name: str = "download_progress") -> dict:
    return {"model": model, "event": name, "data": {}}


class EventShapeTests(unittest.TestCase):
    def test_wire_format_matches_pair_decoder(self) -> None:
        e = progress_event("m/model", "download_progress", 5, 10, "w.gguf")
        self.assertEqual(
            e,
            {
                "model": "m/model",
                "event": "download_progress",
                "data": {"progress": {"w.gguf": {"done": 5, "total": 10}}},
            },
        )

    def test_non_int_counters_are_coerced(self) -> None:
        e = progress_event("m", "download_progress", 5.9, "10")
        p = e["data"]["progress"]["model.gguf"]
        self.assertEqual((p["done"], p["total"]), (5, 10))


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_subscriber_receives_only_own_model(self) -> None:
        hub = ProgressHub()
        mine = await hub.subscribe("a")
        await hub.publish(ev("a"))
        await hub.publish(ev("b"))
        self.assertEqual(await mine.get(), ev("a"))
        self.assertTrue(mine.empty())

    async def test_wildcard_sees_everything(self) -> None:
        hub = ProgressHub()
        wild = await hub.subscribe(WILDCARD)
        await hub.publish(ev("a"))
        await hub.publish(ev("b"))
        self.assertEqual((await wild.get())["model"], "a")
        self.assertEqual((await wild.get())["model"], "b")

    async def test_dual_registration_delivers_once(self) -> None:
        """Same queue under 'exact' and '*' must receive one copy per event."""
        hub = ProgressHub()
        q = await hub.subscribe("a")
        async with hub._lock:
            hub._subs.setdefault(WILDCARD, set()).add(q)
        await hub.publish(ev("a"))
        self.assertEqual(q.qsize(), 1)

    async def test_unsubscribe_prunes_empty_registry(self) -> None:
        hub = ProgressHub()
        q = await hub.subscribe("a")
        self.assertEqual(await hub.subscriber_count("a"), 1)
        await hub.unsubscribe("a", q)
        self.assertEqual(await hub.subscriber_count("a"), 0)
        self.assertNotIn("a", hub._subs)
        await hub.unsubscribe("a", q)  # idempotent, no raise
        await hub.unsubscribe("never-existed", q)

    async def test_subscriber_count_totals(self) -> None:
        hub = ProgressHub()
        await hub.subscribe("a")
        await hub.subscribe("a")
        await hub.subscribe("b")
        self.assertEqual(await hub.subscriber_count("a"), 2)
        self.assertEqual(await hub.subscriber_count(), 3)

    async def test_publish_without_subscribers_is_silent(self) -> None:
        hub = ProgressHub()
        with self.assertLogs("gufo-masquerade.hub", level="DEBUG"):
            await hub.publish(ev("nobody"))

    async def test_full_queue_drops_oldest_not_the_new_frame(self) -> None:
        """Terminal frames must land; losing a stale progress frame is fine."""
        hub = ProgressHub()
        q = await hub.subscribe("a")
        for i in range(MAX_QUEUE):
            q.put_nowait(ev("a", f"fill-{i}"))
        await hub.publish(ev("a", "download_finished"))
        self.assertEqual(q.qsize(), MAX_QUEUE, "queue must not grow past cap")
        self.assertEqual(
            q.get_nowait()["event"], "fill-1", "oldest frame should have been evicted"
        )
        # Drain to the tail: the new frame must be there.
        while not q.empty():
            item = q.get_nowait()
        self.assertEqual(item["event"], "download_finished")

    async def test_hopeless_queue_warns_and_drops_frame(self) -> None:
        """Pathological queue (can neither drain nor accept) -> logged drop."""

        class Broken:
            def put_nowait(self, item):
                raise asyncio.QueueFull

            def get_nowait(self):
                raise asyncio.QueueEmpty

        hub = ProgressHub()
        async with hub._lock:
            hub._subs.setdefault("a", set()).add(Broken())
        with self.assertLogs("gufo-masquerade.hub", level="WARNING"):
            await hub.publish(ev("a"))


if __name__ == "__main__":
    unittest.main()
