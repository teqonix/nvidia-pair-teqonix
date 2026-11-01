#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""gufo-masquerade: present GUFO as a llama.cpp *router* to nVidia PAIR.

GUFO v0.20.0 already mimics llama-server's inference surface deliberately
(`/props`, `/slots`, `/completion`, `llamacpp:`-prefixed Prometheus metrics).
What it does NOT implement is the llama.cpp *router* control plane that PAIR's
`nvpair-engine-manager` drives for the `llamacpp` engine profile.

This shim is a single-process reverse proxy that closes exactly those gaps and
passes everything else through byte-transparently, including SSE.

Gap closure (IDs from GUFO_PAIR_FEASABILITY.md §3):
  G1  GET  /props          inject  {"role": "router"}          [BLOCKING]
  G2  POST /models/load    idempotent {"success": true}       [BLOCKING]
  G3  POST /models/unload  {"success": true} | 409 live       [BLOCKING]
  G4  DELETE /models?model drop from virtual inventory         [HIGH]
  G5  GET  /models/sse     in-process pub/sub progress hub    [HIGH]
  G6  GET  /models         enrich rows status.value=loaded    [HIGH]
  G7  POST /v1/embeddings  fast 501, never a hung connection  [MEDIUM]
  G8  POST /models         present-ok / reject policy          [MEDIUM]
  --  inference            raw streaming passthrough, O(1)

Configuration is environment-only (see README.md). No arguments.

Protocol fidelity notes, in the order they bit us:

* PAIR's `probe()` (lifecycle.go:857) does a hard
  `resolveObjectPath(obj, "role")`. A MISSING field is `false`, not a soft
  default. Hence G1 is not cosmetic -- without it the engine never reaches
  ready and is never adopted at all.
* PAIR stamps every probe with `X-NVPAIR-Engine-Identity-Probe: 1`. We strip
  it before talking to GUFO; GUFO has no use for it and it pollutes logs.
* `pullModelLlamaCPPSSE` (pull.go:204) subscribes to `/models/sse` BEFORE
  issuing `POST /models`, because terminal events are one-shot broadcasts.
  The hub is therefore live before the POST handler can publish, and the
  POST handler publishes into queues that already exist.
* PAIR treats a shim that answers `/props` as an adopted engine. So when GUFO
  is unreachable we MUST answer non-200 on both /props and /health, or PAIR
  adopts a dead node and silently fails every inference request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

LOG = logging.getLogger("gufo-masquerade")

ENGINE_IDENTITY_HEADER = "X-NVPAIR-Engine-Identity-Probe"

# Hop-by-hop headers (RFC 7230 §6.1) plus the ones that become lies once we
# re-frame the body. Copying Content-Length off an upstream 200 onto a streamed
# 502 is how you get a client that hangs instead of erroring.
_STRIP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",  # we do not re-encode; keep it honest
        "date",
        "server",
    }
)

# Paths served locally by the shim rather than forwarded.
LOCAL_PREFIXES = (
    "/shim/",  # our own introspection surface
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class Config:
    """Runtime configuration, all of it from the environment.

    Every knob has a value that works for the amd-ai-core-two deployment, so
    the container needs no configuration to start; the knobs exist so the
    deployment can be tuned without a rebuild.
    """

    listen_host: str = field(
        default_factory=lambda: os.environ.get("SHIM_HOST", "127.0.0.1")
    )
    listen_port: int = field(default_factory=lambda: _env_int("SHIM_PORT", 8081))
    gufo_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "GUFO_BASE_URL", "http://127.0.0.1:18080"
        ).rstrip("/")
    )
    gufo_api_key: str = field(default_factory=lambda: os.environ.get("GUFO_API_KEY", ""))

    # The identity PAIR matches on. Overridable only so the shim can be
    # re-pointed at a different manifest without a code change.
    router_role: str = field(
        default_factory=lambda: os.environ.get("SHIM_ROUTER_ROLE", "router")
    )

    # G8: what POST /models does for a model that is not already resident.
    #   present-ok -> 202 + download_finished if resident, else 501 (default)
    #   fake       -> simulate a download with progress frames (demo/testing)
    #   reject     -> always 501
    pull_policy: str = field(
        default_factory=lambda: os.environ.get("SHIM_PULL_POLICY", "present-ok")
    )

    # G3: refuse to report an unload of the model that is actively serving.
    # Honest by default: PAIR's cancel path only needs success for downloads,
    # and claiming we evicted a live model would be a lie that shows up later
    # as inference from a model PAIR believes is gone.
    refuse_unload_serving: bool = field(
        default_factory=lambda: _env_bool("SHIM_REFUSE_UNLOAD_SERVING", True)
    )

    # Persistent overlay so a shim restart does not resurrect deleted models.
    state_path: str = field(
        default_factory=lambda: os.environ.get(
            "SHIM_STATE_PATH", "/var/lib/gufo-masquerade/state.json"
        )
    )

    # Upstream timeouts. The read timeout must exceed the longest sane
    # generation, and connect stays short so a dead GUFO fails fast.
    connect_timeout_s: float = field(
        default_factory=lambda: _env_int("SHIM_CONNECT_TIMEOUT_S", 5)
    )
    read_timeout_s: float = field(
        default_factory=lambda: _env_int("SHIM_READ_TIMEOUT_S", 1800)
    )

    # How long a cached GUFO model list may be trusted for the inventory views.
    inventory_ttl_s: float = field(
        default_factory=lambda: _env_int("SHIM_INVENTORY_TTL_S", 2)
    )

    fake_bytes: int = field(default_factory=lambda: _env_int("SHIM_FAKE_BYTES", 4_000_000_000))
    fake_ticks: int = field(default_factory=lambda: _env_int("SHIM_FAKE_TICKS", 10))
    fake_tick_s: float = field(default_factory=lambda: _env_int("SHIM_FAKE_TICK_MS", 200) / 1000.0)

    shim_version: str = "1.0.0"

    @property
    def gufo_headers(self) -> dict[str, str]:
        # The shim holds GUFO's key so PAIR never has to know it. PAIR's
        # llamacpp profile has no credential binding for the engine.
        if self.gufo_api_key:
            return {"Authorization": f"Bearer {self.gufo_api_key}"}
        return {}


CONFIG = Config()


# --------------------------------------------------------------------------
# Virtual model inventory
# --------------------------------------------------------------------------
class Inventory:
    """GUFO's model list, enriched and overlaid.

    GUFO serves exactly the models compiled/loaded into its one process and has
    no notion of a PAIR-managed cache. We therefore keep GUFO's list as the
    base truth and layer two deltas on top:

      * ``deleted``  -- models PAIR deleted. Persisted, because if the shim
                       restarts and resurrects them, PAIR's UI re-shows models
                       the operator removed.
      * ``overlay``  -- transient pull state (``downloading``) that must NOT
                       survive a restart; a half-download is not resumable
                       here, so we drop it on load.
    """

    def __init__(self, state_path: str) -> None:
        self._state_path = state_path
        self._lock = asyncio.Lock()
        self.deleted: set[str] = set()
        self.overlay: dict[str, str] = {}  # model -> "downloading"
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0

    def load(self) -> None:
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self.deleted = set(raw.get("deleted", []))
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not stop the shim; log and start clean.
            LOG.warning("state file unreadable, starting with empty overlay: %s", exc)
            self.deleted = set()

    def _save_locked(self) -> None:
        payload = {"deleted": sorted(self.deleted), "saved_at": int(time.time())}
        tmp = f"{self._state_path}.tmp"
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self._state_path)
        except OSError as exc:
            # Persistence failing is not worth killing serving over, but the
            # operator must be able to find out why deletions came back.
            LOG.error("could not persist shim state to %s: %s", self._state_path, exc)

    async def mark_deleted(self, model: str) -> None:
        async with self._lock:
            self.deleted.add(model)
            self.overlay.pop(model, None)
            self._cache = None
            self._save_locked()

    async def mark_downloading(self, model: str) -> None:
        async with self._lock:
            self.overlay[model] = "downloading"
            self._cache = None

    async def clear_downloading(self, model: str) -> None:
        async with self._lock:
            if self.overlay.get(model) == "downloading":
                del self.overlay[model]
                self._cache = None

    def status_for(self, model: str) -> str:
        if self.overlay.get(model) == "downloading":
            return "downloading"
        return "loaded"

    async def gufo_models(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """GUFO's raw /models data array (short TTL cached)."""
        async with self._lock:
            fresh = (
                self._cache is not None
                and (time.monotonic() - self._cache_at) < CONFIG.inventory_ttl_s
            )
            if fresh:
                return list(self._cache.get("data", []))
        try:
            resp = await client.get(
                f"{CONFIG.gufo_base_url}/models",
                headers=CONFIG.gufo_headers,
                timeout=httpx.Timeout(CONFIG.connect_timeout_s, read=10.0),
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

    async def serving_model(self, client: httpx.AsyncClient) -> str | None:
        """The model GUFO reports as its primary backend model.

        Used by G3 to decide whether an unload would evict something that is
        actually serving. ``/props.model_alias`` is GUFO's own stable id for
        the loaded backend, so it is the right thing to compare against.
        """
        props = await self.gufo_props(client)
        return props.get("model_alias") or props.get("model")

    async def gufo_props(self, client: httpx.AsyncClient) -> dict[str, Any]:
        try:
            resp = await client.get(
                f"{CONFIG.gufo_base_url}/props",
                headers=CONFIG.gufo_headers,
                timeout=httpx.Timeout(CONFIG.connect_timeout_s, read=10.0),
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

    def visible(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if row.get("id") not in self.deleted]

    async def ids(self, client: httpx.AsyncClient) -> set[str]:
        rows = await self.visible_or_fail(client)
        return {row["id"] for row in rows if isinstance(row.get("id"), str)}

    async def visible_or_fail(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        rows = await self.gufo_models(client)
        out = []
        for row in rows:
            mid = row.get("id")
            if not isinstance(mid, str) or mid in self.deleted:
                continue
            enriched = dict(row)
            enriched["status"] = {
                "value": self.status_for(mid),
                "normalized": self.status_for(mid),
            }
            out.append(enriched)
        # Models that are mid-pull but not yet in GUFO's list still have to be
        # visible, or PAIR's cancel path cannot find them to stop them.
        async with self._lock:
            extra = [
                m for m, st in self.overlay.items() if st == "downloading" and m not in {r.get("id") for r in out}
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


class UpstreamUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------
# G5: SSE progress hub
# --------------------------------------------------------------------------
class ProgressHub:
    """Fan-out bus for ``llamacpp-models-sse`` events.

    Wire format is pinned to PAIR's decoder (pull.go:406 ``llamaCPPModelsEvent``)::

        data: {"model": str, "event": str,
              "data": {"progress": {"<file>": {"done": int, "total": int}}}}

    PAIR computes percent as ``sum(done)/sum(total)`` over the progress map and
    drops any event whose ``model`` does not equal the requested id, so the
    model field is load-bearing on every frame.
    """

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, model: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subs.setdefault(model, set()).add(queue)
        LOG.debug("sse subscriber attached model=%s", model)
        return queue

    async def unsubscribe(self, model: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subs.get(model)
            if subs:
                subs.discard(queue)
                if not subs:
                    del self._subs[model]

    async def publish(self, event: dict[str, Any]) -> None:
        model = event.get("model", "")
        async with self._lock:
            # A "*" subscriber sees every model's frames; an exact subscriber
            # sees only its own. dict.fromkeys de-duplicates the case where the
            # same queue is registered under both keys.
            queues = list(
                dict.fromkeys(
                    (*self._subs.get(model, ()), *self._subs.get("*", ()))
                )
            )
        if not queues:
            LOG.debug("sse publish with no subscribers model=%s event=%s", model, event.get("event"))
            return
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A wedged subscriber must not stall the publisher. Drop the
                # oldest frame so the terminal event always lands.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    LOG.warning("dropped sse frame for stalled subscriber model=%s", model)

    async def subscriber_count(self, model: str | None = None) -> int:
        async with self._lock:
            if model is None:
                return sum(len(q) for q in self._subs.values())
            return len(self._subs.get(model, ()))


def progress_event(model: str, event: str, done: int, total: int, filename: str = "model.gguf") -> dict[str, Any]:
    return {
        "model": model,
        "event": event,
        "data": {"progress": {filename: {"done": int(done), "total": int(total)}}},
    }


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
app = FastAPI(
    title="gufo-masquerade",
    version=CONFIG.shim_version,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
inventory = Inventory(CONFIG.state_path)
hub = ProgressHub()
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(CONFIG.connect_timeout_s, read=CONFIG.read_timeout_s),
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
            follow_redirects=False,
        )
    return _client


def json_error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {
        "success": False,
        "error": {"type": code, "code": code, "message": message},
    }
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def passthrough_headers(upstream: httpx.Response) -> dict[str, str]:
    out = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _STRIP_RESPONSE_HEADERS
    }
    # Explicitly disable proxy buffering for SSE so an nginx/traefik in front
    # of the shim does not turn a stream into a blob.
    out["X-Accel-Buffering"] = "no"
    out["Cache-Control"] = "no-cache"
    return out


# ---- Control surface ------------------------------------------------------

@app.get("/props")
async def props(request: Request) -> Response:
    """G1: GUFO's /props verbatim, plus the ``role`` PAIR's matcher requires.

    Deliberately additive: we copy every GUFO field first and only then set
    ``role``, so GUFO's own fields (model, model_alias, build_info,
    default_generation_settings, modalities) survive untouched. If GUFO ever
    starts reporting a role itself we keep its value only if it is already a
    router-shaped one; otherwise we win, because PAIR's readiness depends on
    it and GUFO's does not.
    """
    try:
        body = await inventory.gufo_props(client())
    except UpstreamUnavailable as exc:
        # Do NOT answer 200 here. PAIR adopts whatever answers /props, so a
        # happy lie about a dead GUFO becomes a node that is "ready" and
        # fails every request.
        return json_error(502, "upstream_unavailable", str(exc))

    merged = dict(body)
    existing = merged.get("role")
    if existing != CONFIG.router_role:
        merged["role"] = CONFIG.router_role
        if existing is not None:
            merged["role_upstream"] = existing
    merged["masquerade"] = {
        "engine": "gufo",
        "shim_version": CONFIG.shim_version,
        "gufo_base_url": CONFIG.gufo_base_url,
    }
    return JSONResponse(status_code=200, content=merged)


@app.get("/health")
@app.get("/v1/health")
@app.get("/healthz")
async def health(request: Request) -> Response:
    """Health is a check on the PAIR, not on the shim's own liveness.

    PAIR polls this every 5s and marks the node unhealthy on non-200. If we
    answered 200 whenever the shim process is alive, a dead GUFO would look
    healthy and requests would fail with no signal anywhere. So: probe GUFO.
    """
    try:
        resp = await client().get(
            f"{CONFIG.gufo_base_url}/health",
            headers=CONFIG.gufo_headers,
            timeout=httpx.Timeout(CONFIG.connect_timeout_s, read=5.0),
        )
    except httpx.HTTPError as exc:
        return json_error(503, "upstream_unavailable", f"GUFO health failed: {exc}")
    if resp.status_code != 200:
        return json_error(
            503, "upstream_unhealthy", f"GUFO health returned HTTP {resp.status_code}"
        )
    try:
        inner = resp.json()
    except ValueError:
        inner = {"status": "ok"}
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "engine": "gufo",
            "masquerade": CONFIG.shim_version,
            "upstream": inner,
        },
    )


@app.get("/models")
@app.get("/v1/models")
async def models(request: Request) -> Response:
    """G6: every row gets ``status.value``; PAIR's loaded_models filter needs it.

    Without enrichment PAIR's ``match: {field: "status.value", in: ["loaded"]}``
    drops every row and the UI reports zero resident models on a node that is
    serving normally.
    """
    try:
        rows = await inventory.visible_or_fail(client())
    except UpstreamUnavailable as exc:
        return json_error(502, "upstream_unavailable", str(exc))
    return JSONResponse(status_code=200, content={"object": "list", "data": rows})


@app.post("/models/load")
async def models_load(request: Request) -> Response:
    """G2: GUFO pre-loads statically; load is therefore an idempotent ack.

    We validate that the requested model actually exists rather than acking
    blindly, so a typo in PAIR surfaces as an error here instead of as a
    mysterious model-not-found at inference time.
    """
    payload = await _json_body(request)
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return json_error(400, "invalid_request", "model is required")
    model = model.strip()

    try:
        known = await inventory.ids(client())
    except UpstreamUnavailable as exc:
        return json_error(502, "upstream_unavailable", str(exc))

    if model in known:
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "model": model,
                "note": "GUFO serves this model statically; already resident",
            },
        )
    # Accepting an unknown model would let PAIR believe it loaded something
    # that inference will then reject.
    return json_error(
        404,
        "model_not_found",
        f"GUFO does not serve {model!r}; GUFO loads its model at startup, "
        f"so model selection is an operator action, not a runtime one",
        known=sorted(known),
    )


@app.post("/models/unload")
async def models_unload(request: Request) -> Response:
    """G3: success for download-cancel; 409 for evicting the live model.

    PAIR routes BOTH 'unload this model' and 'cancel this download' here
    (pull.go:368 stopLlamaCPPDownload posts to /models/unload and requires
    {"success": true}). The cancel case must succeed. The evict case must not
    be a lie, so it 409s and says why.
    """
    payload = await _json_body(request)
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return json_error(400, "invalid_request", "model is required")
    model = model.strip()

    # Cancelling an in-flight (virtual) pull: always honoured.
    await inventory.clear_downloading(model)

    try:
        serving = await inventory.serving_model(client())
    except UpstreamUnavailable as exc:
        return json_error(502, "upstream_unavailable", str(exc))

    if CONFIG.refuse_unload_serving and serving and model in (serving, serving.split(":")[0]):
        return json_error(
            409,
            "unload_unsupported",
            f"{model!r} is the model GUFO is actively serving and GUFO cannot "
            f"evict it at runtime. Stop the gufo container to unload. "
            f"Set SHIM_REFUSE_UNLOAD_SERVING=0 to ack unloads anyway.",
        )

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "model": model,
            "note": "no-op: GUFO has no runtime unload; model remains resident",
        },
    )


@app.delete("/models")
async def models_delete(request: Request) -> Response:
    """G4: drop the model from the shim's virtual inventory.

    GUFO cannot delete weights, so this is an inventory-only delete: the model
    stops being listed and stops being routable through the shim. That matches
    the operator's intent well enough to be useful without pretending we
    reclaimed disk space.
    """
    model = request.query_params.get("model")
    if not model or not model.strip():
        return json_error(400, "invalid_request", "model query parameter is required")
    model = model.strip()

    try:
        serving = await inventory.serving_model(client())
    except UpstreamUnavailable as exc:
        return json_error(502, "upstream_unavailable", str(exc))

    if CONFIG.refuse_unload_serving and serving and model in (serving, serving.split(":")[0]):
        return json_error(
            409,
            "delete_unsupported",
            f"{model!r} is actively served by GUFO and cannot be deleted while "
            f"served. Stop the container first.",
        )

    try:
        known = await inventory.ids(client())
    except UpstreamUnavailable as exc:
        return json_error(502, "upstream_unavailable", str(exc))

    if model not in known:
        return json_error(404, "model_not_found", f"{model!r} is not listed", known=sorted(known))

    await inventory.mark_deleted(model)
    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "model": model,
            "note": "removed from shim inventory; weights on disk untouched",
        },
    )


@app.post("/models")
async def models_pull(request: Request) -> Response:
    """G8: honest pull semantics for an engine that cannot pull.

    GUFO has no HTTP weight provisioning: its model graph, tokenizers and HIP
    kernels are compiled in and weights are loaded at process start. Three
    policies:

      present-ok (default) -- if the model is already resident, ack with 202
          and publish download_finished so PAIR's SSE handshake completes
          immediately instead of timing out. Otherwise 501 with instructions.
      fake                 -- emit synthetic progress frames then finish. For
          demos and for exercising PAIR's progress UI end to end. NEVER use
          in production: it tells the operator a model arrived when it did not.
      reject               -- always 501.
    """
    payload = await _json_body(request)
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return json_error(400, "invalid_request", "model is required")
    model = model.strip()

    if CONFIG.pull_policy == "reject":
        return json_error(
            501,
            "pull_unsupported",
            f"GUFO cannot download models over HTTP. Stage weights out of band "
            f"and restart the gufo container. (SHIM_PULL_POLICY=reject)",
        )

    if CONFIG.pull_policy == "fake":
        await inventory.mark_downloading(model)
        asyncio.create_task(_fake_download(model))
        return JSONResponse(
            status_code=202,
            content={"success": True, "model": model, "policy": "fake"},
        )

    # present-ok
    try:
        known = await inventory.ids(client())
    except UpstreamUnavailable as exc:
        return json_error(502, "upstream_unavailable", str(exc))

    if model in known:
        # Publish the terminal event BEFORE the ack returns: PAIR subscribed
        # first and may already be blocked reading.
        await hub.publish(progress_event(model, "download_progress", CONFIG.fake_bytes, CONFIG.fake_bytes))
        await hub.publish(progress_event(model, "download_finished", CONFIG.fake_bytes, CONFIG.fake_bytes))
        return JSONResponse(
            status_code=202,
            content={
                "success": True,
                "model": model,
                "note": "already resident in GUFO; nothing to download",
            },
        )

    return json_error(
        501,
        "pull_unsupported",
        f"GUFO cannot download {model!r} over HTTP. It serves only the model "
        f"it was started with ({sorted(known)}). To change it, stage the model "
        f"and restart the gufo container. (SHIM_PULL_POLICY=present-ok)",
        known=sorted(known),
    )


async def _fake_download(model: str) -> None:
    """Synthetic progress for SHIM_PULL_POLICY=fake."""
    total = CONFIG.fake_bytes
    try:
        for step in range(1, CONFIG.fake_ticks + 1):
            await asyncio.sleep(CONFIG.fake_tick_s)
            done = int(total * step / CONFIG.fake_ticks)
            await hub.publish(progress_event(model, "download_progress", done, total))
        await hub.publish(progress_event(model, "download_finished", total, total))
        await inventory.clear_downloading(model)
    except asyncio.CancelledError:
        await hub.publish(progress_event(model, "download_failed", 0, total))
        await inventory.clear_downloading(model)
        raise


@app.get("/models/sse")
async def models_sse(request: Request) -> Response:
    """G5: SSE progress stream.

    PAIR's pull subscribes here before POSTing, and the stream must stay open
    across the whole download. We subscribe on entry, so frames published by a
    concurrent POST /models land in this queue. A keepalive comment every few
    seconds holds the connection open through idle periods and defeats
    idle-timeout buffering in anything sitting in front of us.
    """
    model = request.query_params.get("model") or "*"
    queue = await hub.subscribe(model)

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield b": gufo-masquerade sse open\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
        finally:
            await hub.unsubscribe(model, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- Inference + everything else: transparent passthrough ----------------

async def _relay(request: Request, path: str) -> Response:
    """Byte-transparent reverse proxy for one request.

    Streams in both directions. ``aiter_raw`` yields upstream bytes as they
    arrive and never buffers the whole body, which keeps an SSE response at O(1)
    memory no matter how long the generation runs. We re-raise client
    disconnects as upstream cancellation so GUFO stops generating instead of
    burning a GPU slot for text nobody will read.
    """
    body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "connection", "keep-alive", "transfer-encoding",
                            "te", "upgrade", "proxy-authorization", ENGINE_IDENTITY_HEADER.lower())
    }
    headers.update(CONFIG.gufo_headers)
    url = f"{CONFIG.gufo_base_url}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    try:
        upstream_req = client().build_request(
            request.method, url, content=body, headers=headers
        )
        upstream = await client().send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        return json_error(502, "bad_gateway", f"upstream {request.method} {path}: {exc}")

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            LOG.warning("upstream stream broke %s %s: %s", request.method, path, exc)
            yield b""
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=passthrough_headers(upstream),
    )


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def catchall(request: Request) -> Response:
    path = "/" + request.path_params.get("path", "").lstrip("/")

    if path.startswith("/shim/"):
        return await _introspect(request, path)

    # G7: fail embeddings fast. GUFO registers /v1/embeddings as
    # NotImplemented(501) already, but answering it here means the shim gives
    # PAIR a clear, attributed reason and never opens a GPU slot.
    if path in ("/v1/embeddings", "/v1/rerank", "/v1/reranking", "/rerank", "/infill"):
        return json_error(
            501,
            "unsupported_task",
            "GUFO has no embedding or reranking model. Route this workload to a "
            "llama.cpp or Ollama node. (see GUFO_PAIR_FEASABILITY.md gap G7)",
        )

    return await _relay(request, path)


async def _introspect(request: Request, path: str) -> Response:
    """Shim's own diagnostics -- not part of the PAIR contract."""
    if path == "/shim/config":
        return JSONResponse(
            status_code=200,
            content={
                "listen": f"{CONFIG.listen_host}:{CONFIG.listen_port}",
                "gufo_base_url": CONFIG.gufo_base_url,
                "router_role": CONFIG.router_role,
                "pull_policy": CONFIG.pull_policy,
                "refuse_unload_serving": CONFIG.refuse_unload_serving,
                "state_path": CONFIG.state_path,
                "api_key_configured": bool(CONFIG.gufo_api_key),
                "shim_version": CONFIG.shim_version,
            },
        )
    if path == "/shim/state":
        async with inventory._lock:  # noqa: SLF001 - introspection only
            return JSONResponse(
                status_code=200,
                content={
                    "deleted": sorted(inventory.deleted),
                    "overlay": dict(inventory.overlay),
                },
            )
    if path == "/shim/sse_stats":
        return JSONResponse(status_code=200, content={"subscribers": await hub.subscriber_count()})
    return json_error(404, "not_found", f"no shim route for {path}")


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(
        level=os.environ.get("SHIM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    inventory.load()

    import uvicorn

    LOG.info(
        "gufo-masquerade %s starting listen=%s:%s upstream=%s pull_policy=%s",
        CONFIG.shim_version,
        CONFIG.listen_host,
        CONFIG.listen_port,
        CONFIG.gufo_base_url,
        CONFIG.pull_policy,
    )
    uvicorn.run(
        app,
        host=CONFIG.listen_host,
        port=CONFIG.listen_port,
        log_level=os.environ.get("SHIM_LOG_LEVEL", "info").lower(),
        access_log=_env_bool("SHIM_ACCESS_LOG", False),
        # A SIGTERM must drain, not amputate: PAIR's stop spec grants 10s and
        # GUFO finishes the in-flight turn on its own signal handler.
        timeout_graceful_shutdown=10,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
