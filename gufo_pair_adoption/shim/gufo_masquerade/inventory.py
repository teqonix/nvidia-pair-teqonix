# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Virtual model inventory: GUFO's truth, enriched and overlaid.

GUFO serves exactly the models compiled/loaded into its one process and has
no notion of a PAIR-managed model cache. The shim therefore treats GUFO's
``/models`` list as base truth and layers two deltas on top of it:

``deleted``
    Models PAIR deleted (gap G4). **Persisted** to ``SHIM_STATE_PATH``,
    because if the shim restarts and resurrects them, PAIR's UI re-shows
    models the operator removed.

``overlay``
    Transient pull state (``downloading``) that must **not** survive a
    restart — a half-download is not resumable here, so it is dropped on
    load.

Why enrichment is load-bearing (gap G6): PAIR's ``models_dir`` scan filters
rows with ``match: {field: "status.value", in: ["loaded"]}``. GUFO's raw rows
carry no ``status`` key, so without enrichment the UI reports zero resident
models on a node that is serving normally.

All upstream reads go through short-TTL caching (``SHIM_INVENTORY_TTL_S``)
because PAIR polls several of these endpoints at short intervals and GUFO's
``/models``/``/props`` are cheap but not free.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

from .config import Config
from .errors import UpstreamUnavailable

LOG = logging.getLogger("gufo-masquerade.inventory")


class Inventory:
    """Thread-safe (single event loop) view over GUFO's model list.

    Args:
        config: The shim configuration; ``state_path``, timeouts and
            ``inventory_ttl_s`` are read from it.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._state_path = config.state_path
        self._lock = asyncio.Lock()
        self.deleted: set[str] = set()
        self.overlay: dict[str, str] = {}  # model -> "downloading"
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0

    # ------------------------------------------------------------------
    # Persistent overlay (G4)
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Load the deleted-model overlay from disk.

        A missing file is normal (first boot). A corrupt or unreadable file
        is logged and ignored: the shim must boot and serve even when its
        bookkeeping is damaged, since refusing to start would take the whole
        engine offline for a cosmetic overlay. Transient download state is
        intentionally never loaded.
        """
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self.deleted = set(raw.get("deleted", []))
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("state file unreadable, starting with empty overlay: %s", exc)
            self.deleted = set()

    def _save_locked(self) -> None:
        """Atomically persist the overlay. Caller must hold ``self._lock``.

        Write-to-temp + :func:`os.replace` so a crash mid-write cannot
        truncate the live state file. Persistence failure is logged, not
        raised: losing the overlay is preferable to failing a delete that
        already succeeded in memory.
        """
        payload = {"deleted": sorted(self.deleted), "saved_at": int(time.time())}
        tmp = f"{self._state_path}.tmp"
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self._state_path)
        except OSError as exc:
            LOG.error("could not persist shim state to %s: %s", self._state_path, exc)

    async def mark_deleted(self, model: str) -> None:
        """Remove ``model`` from PAIR's view, durably."""
        async with self._lock:
            self.deleted.add(model)
            self.overlay.pop(model, None)
            self._cache = None
            self._save_locked()

    # ------------------------------------------------------------------
    # Transient overlay (G5/G8)
    # ------------------------------------------------------------------
    async def mark_downloading(self, model: str) -> None:
        """Mark ``model`` as mid-pull so it appears in inventory as such."""
        async with self._lock:
            self.overlay[model] = "downloading"
            self._cache = None

    async def clear_downloading(self, model: str) -> None:
        """Drop the downloading flag (download finished or was cancelled)."""
        async with self._lock:
            if self.overlay.get(model) == "downloading":
                del self.overlay[model]
                self._cache = None

    def status_for(self, model: str) -> str:
        """PAIR-facing ``status.value`` for a resident model."""
        if self.overlay.get(model) == "downloading":
            return "downloading"
        return "loaded"

    # ------------------------------------------------------------------
    # Upstream reads
    # ------------------------------------------------------------------
    async def gufo_models(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """GUFO's raw ``/models`` ``data`` array, cached for ``inventory_ttl_s``.

        Raises:
            UpstreamUnavailable: transport failure, non-200, non-JSON body,
                or a body without a ``data`` list — all treated identically,
                because a lying upstream is as unusable as a dead one.
        """
        async with self._lock:
            fresh = (
                self._cache is not None
                and (time.monotonic() - self._cache_at) < self._config.inventory_ttl_s
            )
            if fresh:
                return list(self._cache.get("data", []))
        cfg = self._config
        try:
            resp = await client.get(
                f"{cfg.gufo_base_url}/models",
                headers=cfg.gufo_headers,
                timeout=cfg.upstream_timeout(cfg.discovery_timeout_s),
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"GET /models: {exc}") from exc
        if resp.status_code != 200:
            raise UpstreamUnavailable(f"GET /models -> HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise UpstreamUnavailable(f"GET /models: non-JSON body: {exc}") from exc
        data = body.get("data")
        if not isinstance(data, list):
            raise UpstreamUnavailable("GET /models: no data array")
        async with self._lock:
            self._cache = {"data": data}
            self._cache_at = time.monotonic()
        return list(data)

    async def gufo_props(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """GUFO's raw ``/props`` object.

        Raises:
            UpstreamUnavailable: same taxonomy as :meth:`gufo_models`.
        """
        cfg = self._config
        try:
            resp = await client.get(
                f"{cfg.gufo_base_url}/props",
                headers=cfg.gufo_headers,
                timeout=cfg.upstream_timeout(cfg.discovery_timeout_s),
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"GET /props: {exc}") from exc
        if resp.status_code != 200:
            raise UpstreamUnavailable(f"GET /props -> HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise UpstreamUnavailable(f"GET /props: non-JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise UpstreamUnavailable("GET /props: body is not an object")
        return body

    async def serving_model(self, client: httpx.AsyncClient) -> str | None:
        """The model GUFO reports as its primary backend model.

        Used by G3/G4 to decide whether an unload/delete would evict
        something actually serving. ``/props.model_alias`` is GUFO's stable
        id for the loaded backend, so it is the right thing to compare
        against; ``/props.model`` is the fallback.
        """
        props = await self.gufo_props(client)
        return props.get("model_alias") or props.get("model")

    def is_actively_serving(self, model: str, serving: str | None) -> bool:
        """Whether ``model`` names the backend GUFO is currently serving.

        Compares both against ``serving`` verbatim and against its ``name:tag``
        prefix, because PAIR sometimes addresses a model by its bare name
        while GUFO reports a quantised tag (or vice versa).
        """
        if not serving:
            return False
        return model in (serving, serving.split(":")[0])

    # ------------------------------------------------------------------
    # PAIR-facing views
    # ------------------------------------------------------------------
    def visible(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter out deleted rows from a raw upstream row list."""
        return [row for row in rows if row.get("id") not in self.deleted]

    async def ids(self, client: httpx.AsyncClient) -> set[str]:
        """The set of model ids PAIR can currently see.

        Includes virtual ``downloading`` rows: a pull in flight is a model
        PAIR knows about, and ``POST /models/load`` for it must not 404.
        """
        rows = await self.gufo_models(client)
        async with self._lock:
            downloading = {m for m, st in self.overlay.items() if st == "downloading"}
        return {
            row["id"]
            for row in rows
            if isinstance(row.get("id"), str) and row["id"] not in self.deleted
        } | downloading

    async def visible_or_fail(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """The PAIR-facing inventory: enriched, filtered, plus virtual rows.

        Every surviving row gains ``status.value``/``status.normalized``
        (G6). Models mid-pull that are not yet in GUFO's list are appended
        as virtual ``downloading`` rows — without them, PAIR's cancel path
        cannot find a download to stop it.

        Raises:
            UpstreamUnavailable: propagated for the handler to translate.
        """
        rows = await self.gufo_models(client)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            mid = row.get("id")
            if not isinstance(mid, str) or mid in self.deleted:
                continue
            enriched = dict(row)
            status = self.status_for(mid)
            enriched["status"] = {"value": status, "normalized": status}
            out.append(enriched)
            seen.add(mid)
        async with self._lock:
            extra = [
                m for m, st in self.overlay.items()
                if st == "downloading" and m not in seen
            ]
        for mid in extra:
            out.append(
                {
                    "id": mid,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "gufo-masquerade",
                    "status": {"value": "downloading", "normalized": "downloading"},
                }
            )
        return out

    def snapshot(self) -> dict[str, Any]:
        """Consistency-free snapshot of the overlay for ``/shim/state``.

        Reads under no lock on purpose: the values are only ever replaced
        whole under the lock, and this is diagnostics, not decision-making.
        """
        return {"deleted": sorted(self.deleted), "overlay": dict(self.overlay)}
