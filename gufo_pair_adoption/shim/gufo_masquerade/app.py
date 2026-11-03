# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""HTTP surface of the gufo-masquerade shim: FastAPI app factory.

The module is structured as a factory (:func:`create_app`) rather than a
module-global ``app`` so that tests can instantiate a shim with an injected
configuration, a fake upstream client, or a scratch inventory/hub, without
touching environment variables or another process. ``__main__.py`` builds the
production instance.

The honesty rule
----------------
**Never report readiness for an upstream we cannot reach.** PAIR adopts any
engine that answers ``GET /props`` with 200 and a matching ``role`` field, and
treats ``/health`` 200 as "route work here". A shim that answered happily
while GUFO was down would be adopted as a healthy node and then fail every
inference request silently, with the operator seeing failures attributed to
the model rather than to the missing backend. So every readiness-bearing
handler converts :class:`~gufo_masquerade.errors.UpstreamUnavailable` into an
honest 502/503.

The introspection surface (``/shim/*``) deliberately does *not* touch the
upstream, so the operator can diagnose a dead-GUFO deployment through the
shim itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Config
from .errors import UpstreamUnavailable, json_error
from .inventory import Inventory
from .sse import WILDCARD, ProgressHub, progress_event

LOG = logging.getLogger("gufo-masquerade")

#: PAIR stamps every identity probe with this header (lifecycle.go). GUFO has
#: no use for it and it pollutes upstream logs, so it is stripped in
#: :func:`forward_headers`.
ENGINE_IDENTITY_HEADER = "X-NVPAIR-Engine-Identity-Probe"

#: Hop-by-hop headers (RFC 7230 6.1) plus the ones that become lies once we
#: re-frame the body. Copying ``Content-Length`` off an upstream 200 onto a
#: streamed 502 is how you get a client that hangs instead of erroring.
STRIP_RESPONSE_HEADERS = frozenset(
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

#: Client-side hop-by-hop headers, plus the PAIR probe stamp.
STRIP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "upgrade",
        "proxy-authorization",
        ENGINE_IDENTITY_HEADER.lower(),
    }
)

#: Paths that fail fast at the shim with 501 (gap G7). GUFO registers some of
#: these as NotImplemented already, but answering here gives PAIR a clear,
#: attributed reason and never opens a GPU slot. The list is a tuple so tests
#: can enumerate it without importing internals.
UNSUPPORTED_PATHS = (
    "/v1/embeddings",
    "/v1/rerank",
    "/v1/reranking",
    "/rerank",
    "/infill",
)


def forward_headers(request: Request, config: Config) -> dict[str, str]:
    """Translate inbound client headers into upstream request headers.

    Drops hop-by-hop headers and the PAIR identity stamp, then injects the
    GUFO bearer token (the shim holds the credential so PAIR never has to).
    Inbound ``Authorization`` from PAIR is intentionally discarded: PAIR's
    llamacpp profile sends none, and whatever a different client sends is
    not GUFO's credential.
    """
    out = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in STRIP_REQUEST_HEADERS and k.lower() != "authorization"
    }
    out.update(config.gufo_headers)
    return out


def passthrough_headers(upstream: httpx.Response) -> dict[str, str]:
    """Upstream response headers minus the ones we must not relay verbatim.

    Adds ``X-Accel-Buffering: no`` so an nginx/traefik in front of the shim
    does not turn an SSE stream into a blob.
    """
    out = {
        k: v for k, v in upstream.headers.items() if k.lower() not in STRIP_RESPONSE_HEADERS
    }
    out["X-Accel-Buffering"] = "no"
    out["Cache-Control"] = "no-cache"
    return out


async def json_body(request: Request) -> dict[str, Any]:
    """Parse the request body as a JSON object, tolerating everything else.

    Returns ``{}`` for empty bodies, invalid JSON, and non-object JSON.
    Callers then fail on their missing required field rather than on a
    parse exception: PAIR sends well-formed bodies, so the lenient path
    exists for operator ``curl`` and probes, which should get a 400 with a
    useful message, not a 500 traceback.
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def relay(request: Request, path: str, config: Config,
                client: httpx.AsyncClient) -> Response:
    """Byte-transparent reverse proxy for one request.

    Streams in both directions: ``aiter_raw`` yields upstream bytes as they
    arrive and never buffers the whole body, keeping an SSE response at O(1)
    memory regardless of generation length. An upstream break mid-stream is
    logged and ends the response rather than raising through the ASGI app —
    the status line has already been sent, so a 500 is not available anymore.

    Args:
        request: The inbound request.
        path: Upstream path (no query).
        config: Configuration (base URL, credentials, timeouts).
        client: Shared upstream client.
    """
    body = await request.body()
    headers = forward_headers(request, config)
    url = f"{config.gufo_base_url}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    try:
        upstream_req = client.build_request(
            request.method, url, content=body, headers=headers
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        return json_error(502, "bad_gateway", f"upstream {request.method} {path}: {exc}")

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            LOG.warning("upstream stream broke %s %s: %s", request.method, path, exc)
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=passthrough_headers(upstream),
    )


async def fake_download(
    model: str, config: Config, hub: ProgressHub, inventory: Inventory
) -> None:
    """Drive a synthetic download for ``SHIM_PULL_POLICY=fake``.

    Emits ``config.fake_ticks`` progress frames then one terminal
    ``download_finished``. If cancelled (the shim is shutting down), publishes
    ``download_failed`` so PAIR's UI does not keep a spinning row, then
    re-raises so cancellation propagates properly.
    """
    total = config.fake_bytes
    try:
        for step in range(1, config.fake_ticks + 1):
            await asyncio.sleep(config.fake_tick_s)
            done = int(total * step / config.fake_ticks)
            await hub.publish(progress_event(model, "download_progress", done, total))
        await hub.publish(progress_event(model, "download_finished", total, total))
        await inventory.clear_downloading(model)
    except asyncio.CancelledError:
        await hub.publish(progress_event(model, "download_failed", 0, total))
        await inventory.clear_downloading(model)
        raise


def create_app(
    config: Config | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    inventory: Inventory | None = None,
    hub: ProgressHub | None = None,
    load_state: bool = True,
) -> FastAPI:
    """Build a shim ASGI application.

    Args:
        config: Configuration to use; defaults to the process
            :data:`~gufo_masquerade.config.CONFIG`.
        client: Pre-built upstream client. When provided, the app does *not*
            close it on shutdown (the caller owns it). When ``None``, the
            lifespan creates and closes one lazily on first use.
        inventory: Inventory override (defaults to one built from ``config``).
        hub: Progress hub override (defaults to a fresh
            :class:`~gufo_masquerade.sse.ProgressHub`).
        load_state: Whether the lifespan loads the persisted overlay from
            ``config.state_path`` (production default). Unit tests pointing
            at a scratch path can set this ``False`` for full isolation.

    Returns:
        A ``FastAPI`` app whose ``app.state`` exposes ``config``,
        ``inventory``, ``hub`` and ``get_client`` for handlers and tests.
    """
    config = config or Config()
    inventory = inventory or Inventory(config)
    hub = hub or ProgressHub()
    owned_client = client

    async def get_client() -> httpx.AsyncClient:
        """Return the shared upstream client, creating it if needed."""
        nonlocal owned_client
        if owned_client is None:
            owned_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    config.connect_timeout_s, read=config.read_timeout_s
                ),
                limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
                follow_redirects=False,
            )
            app.state.owns_client = True
        return owned_client

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if load_state:
            inventory.load()
        # Touch the client so a self-owned one is created inside the loop
        # that will use it (httpx binds its connection pool to a loop).
        await get_client()
        LOG.info(
            "gufo-masquerade %s starting listen=%s:%s upstream=%s pull_policy=%s",
            config.shim_version, config.listen_host, config.listen_port,
            config.gufo_base_url, config.pull_policy,
        )
        try:
            yield
        finally:
            if app.state.owns_client and owned_client is not None:
                await owned_client.aclose()

    app = FastAPI(
        title="gufo-masquerade",
        version=config.shim_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.inventory = inventory
    app.state.hub = hub
    app.state.get_client = get_client
    app.state.owns_client = False

    # ----------------------------------------------------------------
    # Control plane (gaps G1-G8)
    # ----------------------------------------------------------------

    @app.get("/props")
    async def props(request: Request) -> Response:
        """G1: GUFO's ``/props`` verbatim, plus the ``role`` PAIR matches on.

        Deliberately additive: every GUFO field is copied first and only then
        is ``role`` set, so GUFO's own fields (``model``, ``model_alias``,
        ``build_info``, ``default_generation_settings``, ``modalities``)
        survive untouched — PAIR shows several of them to the operator. If
        GUFO ever reports a role itself, the value we need still wins (PAIR's
        readiness depends on it, GUFO's does not) but the upstream value is
        preserved as ``role_upstream`` for diagnosis.

        Upstream failure is a 502, never a happy lie: see the honesty rule in
        the module docstring.
        """
        try:
            body = await inventory.gufo_props(await get_client())
        except UpstreamUnavailable as exc:
            return json_error(502, "upstream_unavailable", str(exc))

        merged = dict(body)
        existing = merged.get("role")
        if existing != config.router_role:
            merged["role"] = config.router_role
            if existing is not None:
                merged["role_upstream"] = existing
        merged["masquerade"] = {
            "engine": "gufo",
            "shim_version": config.shim_version,
            "gufo_base_url": config.gufo_base_url,
        }
        return JSONResponse(status_code=200, content=merged)

    @app.get("/health")
    @app.get("/v1/health")
    @app.get("/healthz")
    async def health(request: Request) -> Response:
        """Health as a statement about GUFO, not about this process.

        PAIR polls this every 5s and marks the node unhealthy on non-200. If
        we answered 200 whenever the shim was alive, a dead GUFO would look
        healthy and requests would fail with no signal anywhere — so the
        probe goes through to the upstream and its body is embedded as
        ``upstream`` for operators.
        """
        try:
            resp = await (await get_client()).get(
                f"{config.gufo_base_url}/health",
                headers=config.gufo_headers,
                timeout=config.upstream_timeout(5.0),
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
                "masquerade": config.shim_version,
                "upstream": inner,
            },
        )

    @app.get("/models")
    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        """G6: inventory with ``status.value`` on every row.

        Without enrichment PAIR's ``match: {field: "status.value", in:
        ["loaded"]}`` filter drops every row and the UI reports zero resident
        models on a node that is serving normally.
        """
        try:
            rows = await inventory.visible_or_fail(await get_client())
        except UpstreamUnavailable as exc:
            return json_error(502, "upstream_unavailable", str(exc))
        return JSONResponse(status_code=200, content={"object": "list", "data": rows})

    @app.post("/models/load")
    async def models_load(request: Request) -> Response:
        """G2: idempotent ack of a load, validated against the inventory.

        GUFO pre-loads statically, so a load of a resident model is a no-op
        we can honour truthfully. We validate that the model actually exists
        rather than acking blindly: a typo in PAIR surfaces here as an error
        naming the known models, instead of as a mysterious model-not-found
        at inference time far away from its cause.
        """
        payload = await json_body(request)
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            return json_error(400, "invalid_request", "model is required")
        model = model.strip()

        try:
            known = await inventory.ids(await get_client())
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
        return json_error(
            404,
            "model_not_found",
            f"GUFO does not serve {model!r}; GUFO loads its model at startup, "
            f"so model selection is an operator action, not a runtime one",
            known=sorted(known),
        )

    @app.post("/models/unload")
    async def models_unload(request: Request) -> Response:
        """G3: success for download-cancel, honest 409 for evicting the live model.

        PAIR routes BOTH "unload this model" and "cancel this download" here
        (pull.go ``stopLlamaCPPDownload`` posts to ``/models/unload`` and
        requires ``{"success": true}``). The cancel case must succeed, so it
        clears the virtual download state unconditionally. The evict case must
        not be a lie: GUFO has no runtime unload, so by default we 409 and
        explain, rather than let PAIR believe a serving model was evicted.
        ``SHIM_REFUSE_UNLOAD_SERVING=0`` turns the evict case into a
        documented no-op ack for operators who prefer a quiet UI.
        """
        payload = await json_body(request)
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            return json_error(400, "invalid_request", "model is required")
        model = model.strip()

        # Cancelling an in-flight (virtual) pull: always honoured.
        await inventory.clear_downloading(model)

        try:
            serving = await inventory.serving_model(await get_client())
        except UpstreamUnavailable as exc:
            return json_error(502, "upstream_unavailable", str(exc))

        if config.refuse_unload_serving and inventory.is_actively_serving(model, serving):
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
        """G4: drop a model from the shim's virtual inventory.

        GUFO cannot delete weights, so this is inventory-only: the model stops
        being listed and stops being routable through the shim. That matches
        operator intent well enough to be useful without pretending we
        reclaimed disk space. The deletion is persisted so a shim restart does
        not resurrect it. Deleting the actively-served model is refused by
        default for the same honesty reason as G3.
        """
        model = request.query_params.get("model")
        if not model or not model.strip():
            return json_error(400, "invalid_request", "model query parameter is required")
        model = model.strip()

        try:
            serving = await inventory.serving_model(await get_client())
            known = await inventory.ids(await get_client())
        except UpstreamUnavailable as exc:
            return json_error(502, "upstream_unavailable", str(exc))

        if config.refuse_unload_serving and inventory.is_actively_serving(model, serving):
            return json_error(
                409,
                "delete_unsupported",
                f"{model!r} is actively served by GUFO and cannot be deleted while "
                f"served. Stop the container first.",
            )

        if model not in known:
            return json_error(
                404, "model_not_found", f"{model!r} is not listed", known=sorted(known)
            )

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

        GUFO has no HTTP weight provisioning: its model graph, tokenizers and
        HIP kernels are compiled in and weights load at process start. The
        ``config.pull_policy`` switch chooses the behaviour:

        ``reject``
            Always 501 with operator instructions.
        ``fake``
            202 immediately, then synthetic progress frames (see
            :func:`fake_download`). Demos and UI testing only.
        ``present-ok`` (default)
            If the model is already resident, 202 plus terminal SSE frames so
            PAIR's handshake completes instead of timing out. Otherwise 501.

        In the ``present-ok`` success path the terminal event is published
        *before* the ack returns: PAIR subscribed first and may already be
        blocked reading the stream.
        """
        payload = await json_body(request)
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            return json_error(400, "invalid_request", "model is required")
        model = model.strip()

        if config.pull_policy == "reject":
            return json_error(
                501,
                "pull_unsupported",
                f"GUFO cannot download models over HTTP. Stage weights out of band "
                f"and restart the gufo container. (SHIM_PULL_POLICY=reject)",
            )

        if config.pull_policy == "fake":
            await inventory.mark_downloading(model)
            task = asyncio.create_task(
                fake_download(model, config, hub, inventory)
            )
            # Keep a strong reference; asyncio only weakly references tasks.
            app.state.background_tasks.add(task)
            task.add_done_callback(app.state.background_tasks.discard)
            return JSONResponse(
                status_code=202,
                content={"success": True, "model": model, "policy": "fake"},
            )

        # present-ok
        try:
            known = await inventory.ids(await get_client())
        except UpstreamUnavailable as exc:
            return json_error(502, "upstream_unavailable", str(exc))

        if model in known:
            await hub.publish(
                progress_event(model, "download_progress", config.fake_bytes, config.fake_bytes)
            )
            await hub.publish(
                progress_event(model, "download_finished", config.fake_bytes, config.fake_bytes)
            )
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

    @app.get("/models/sse")
    async def models_sse(request: Request) -> Response:
        """G5: SSE progress stream.

        PAIR's pull subscribes here before POSTing, and the stream must stay
        open across the whole download. We subscribe on entry, so frames
        published by a concurrent ``POST /models`` land in this queue. A
        keepalive comment every ``SHIM_SSE_KEEPALIVE_S`` holds the connection
        open through idle periods and defeats idle-timeout buffering in
        anything in front of us. A client disconnect unsubscribes through the
        generator's ``finally``.
        """
        model = request.query_params.get("model") or WILDCARD
        queue = await hub.subscribe(model)

        async def stream() -> AsyncIterator[bytes]:
            try:
                yield b": gufo-masquerade sse open\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=config.sse_keepalive_s
                        )
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

    # ----------------------------------------------------------------
    # Introspection (not part of the PAIR contract; never touches GUFO)
    # ----------------------------------------------------------------

    @app.get("/shim/config")
    async def shim_config(request: Request) -> Response:
        """Redaction-safe view of the running configuration."""
        return JSONResponse(status_code=200, content=config.describe())

    @app.get("/shim/state")
    async def shim_state(request: Request) -> Response:
        """The virtual-inventory overlay (deleted models, in-flight pulls)."""
        return JSONResponse(status_code=200, content=inventory.snapshot())

    @app.get("/shim/sse_stats")
    async def shim_sse_stats(request: Request) -> Response:
        """Live SSE subscriber count — the first thing to check when a
        download UI shows no progress."""
        return JSONResponse(
            status_code=200, content={"subscribers": await hub.subscriber_count()}
        )

    @app.api_route("/shim/{unused:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def shim_unknown(request: Request) -> Response:
        """Unknown ``/shim/*`` paths 404 locally instead of reaching GUFO."""
        return json_error(404, "not_found", f"no shim route for /{request.path_params.get('unused', '')}")

    # ----------------------------------------------------------------
    # Everything else: transparent passthrough
    # ----------------------------------------------------------------

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    async def catchall(request: Request) -> Response:
        """Reverse-proxy everything PAIR (or an operator) sends elsewhere.

        Explicit routes above win; this is the inference path plus any llama.cpp
        endpoint the shim has no opinion about (``/slots``, ``/tokenize``,
        ``/completion``, ...). Unsupported tasks fail fast with an attributed
        501 instead of consuming a GPU slot or hanging.
        """
        path = "/" + request.path_params.get("path", "").lstrip("/")

        if path in UNSUPPORTED_PATHS:
            return json_error(
                501,
                "unsupported_task",
                "GUFO has no embedding or reranking model. Route this workload to a "
                "llama.cpp or Ollama node. (gap G7)",
            )

        return await relay(request, path, config, await get_client())

    app.state.background_tasks: set[asyncio.Task] = set()
    return app
