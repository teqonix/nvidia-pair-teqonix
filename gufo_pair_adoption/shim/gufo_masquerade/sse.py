# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""In-process fan-out bus for ``llamacpp-models-sse`` progress events (G5).

Wire format is pinned to PAIR's decoder (pull.go ``llamaCPPModelsEvent``)::

    data: {"model": str, "event": str,
           "data": {"progress": {"<file>": {"done": int, "total": int}}}}

PAIR computes percent as ``sum(done) / sum(total)`` across the whole progress
map — so multi-file downloads aggregate into one coherent percentage — and
drops any event whose ``model`` does not equal the watched id, which makes
the ``model`` field load-bearing on every frame.

Ordering contract: ``pullModelLlamaCPPSSE`` subscribes to ``/models/sse``
*before* issuing ``POST /models``, because terminal events are one-shot
broadcasts with no replay. The hub therefore must (and does) accept
subscribers before any publisher exists, and publishes must land in queues
that already exist when the POST handler runs.

Backpressure policy: a wedged subscriber must never stall a publisher — the
oldest queued frame is dropped so the *terminal* event always lands. Losing
a mid-download progress frame costs the UI one tick; losing
``download_finished`` strands a download row forever.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

LOG = logging.getLogger("gufo-masquerade.hub")

#: Per-subscriber queue depth. Progress bursts are small (<= fake_ticks +
#: terminal); a queue this deep only fills if the consumer has stalled.
MAX_QUEUE = 256

#: Pseudo-model a subscriber may use to receive every model's frames
#: (``GET /models/sse`` without a ``model=`` parameter).
WILDCARD = "*"


class ProgressHub:
    """Asynchronous publish/subscribe registry keyed by model id."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, model: str) -> asyncio.Queue:
        """Attach a new consumer for ``model`` (or :data:`WILDCARD`)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        async with self._lock:
            self._subs.setdefault(model, set()).add(queue)
        LOG.debug("sse subscriber attached model=%s", model)
        return queue

    async def unsubscribe(self, model: str, queue: asyncio.Queue) -> None:
        """Detach a consumer; prunes the model's registry entry when empty."""
        async with self._lock:
            subs = self._subs.get(model)
            if subs:
                subs.discard(queue)
                if not subs:
                    del self._subs[model]

    async def publish(self, event: dict[str, Any]) -> None:
        """Fan ``event`` out to exact and wildcard subscribers, never blocking.

        ``dict.fromkeys`` de-duplicates the case where the same queue is
        registered under both the exact model and the wildcard, so a
        dual-registered subscriber receives one copy, not two.
        """
        model = event.get("model", "")
        async with self._lock:
            queues = list(
                dict.fromkeys((*self._subs.get(model, ()), *self._subs.get(WILDCARD, ())))
            )
        if not queues:
            LOG.debug(
                "sse publish with no subscribers model=%s event=%s",
                model,
                event.get("event"),
            )
            return
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest frame to make room; see module docstring.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    LOG.warning("dropped sse frame for stalled subscriber model=%s", model)

    async def subscriber_count(self, model: str | None = None) -> int:
        """Number of live subscribers, across all models unless ``model``."""
        async with self._lock:
            if model is None:
                return sum(len(q) for q in self._subs.values())
            return len(self._subs.get(model, ()))


def progress_event(
    model: str,
    event: str,
    done: int,
    total: int,
    filename: str = "model.gguf",
) -> dict[str, Any]:
    """Build one wire-format frame for ``model``.

    Args:
        model: Model id PAIR is watching; frames for other ids are dropped
            by PAIR, so this must match the pull request exactly.
        event: ``download_progress``, ``download_finished`` or
            ``download_failed``.
        done: Bytes completed so far.
        total: Total bytes expected.
        filename: Key in the progress map; PAIR aggregates across keys.

    Returns:
        A JSON-serialisable dict ready for :meth:`ProgressHub.publish`.
    """
    return {
        "model": model,
        "event": event,
        "data": {"progress": {filename: {"done": int(done), "total": int(total)}}},
    }
