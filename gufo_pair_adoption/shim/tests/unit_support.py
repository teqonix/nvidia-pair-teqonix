# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the shim's unit tests.

Design: the shim is testable without spawning anything. ``create_app``
accepts an injected ``httpx.AsyncClient``, so unit tests back it with a
``MockTransport`` — a fully in-process fake GUFO whose recorded requests let
us assert on exactly what the shim forwarded (headers, methods, query
strings). No ports, no subprocesses, no races, except for the handful of SSE
tests that need real ASGI disconnect semantics and therefore run the app
under a real in-process uvicorn server.

Run entrypoints (both work everywhere; this node's seccomp kills pytest):

    python shim/tests/run_unit_tests.py
    pytest shim/tests   # where pytest runs
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
from contextlib import asynccontextmanager
from typing import Any

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM_DIR = os.path.dirname(HERE)
if SHIM_DIR not in sys.path:
    sys.path.insert(0, SHIM_DIR)

from gufo_masquerade.app import create_app  # noqa: E402
from gufo_masquerade.config import Config  # noqa: E402
from gufo_masquerade.inventory import Inventory  # noqa: E402
from gufo_masquerade.sse import ProgressHub  # noqa: E402

MODEL = "Qwen3.8-Flash-Next-UD-Q4_K_XL"
API_KEY = "unit-test-key"
FAKE_GUFO = "http://fake-gufo:9"


def base_env(**overrides: str) -> dict[str, str]:
    """Environment for a Config, isolated from the host environment.

    State lands in a fresh temp dir, the inventory cache is disabled (TTL 0)
    so each assertion sees a fresh upstream, and fake downloads are tiny and
    fast. Pass ``overrides`` to bend individual knobs.
    """
    env: dict[str, str] = {
        "GUFO_BASE_URL": FAKE_GUFO,
        "GUFO_API_KEY": API_KEY,
        "SHIM_STATE_PATH": os.path.join(tempfile.mkdtemp(prefix="shim-unit-"), "state.json"),
        "SHIM_INVENTORY_TTL_S": "0",
        "SHIM_SSE_KEEPALIVE_S": "1",
        "SHIM_FAKE_BYTES": "30",
        "SHIM_FAKE_TICKS": "3",
        "SHIM_FAKE_TICK_MS": "5",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def props_body(**extra: Any) -> dict[str, Any]:
    """GUFO ``/props`` faithful to v0.20.0: no ``role`` key."""
    body: dict[str, Any] = {
        "model": MODEL,
        "model_alias": MODEL,
        "template": "",
        "model_info": {},
        "model_path": "/models/x",
        "build_info": {"build_compiler": "gcc-13", "version": "0.20.0"},
        "total_slots": 4,
        "modalities": {"input": ["text"], "output": ["text"]},
    }
    body.update(extra)
    return body


def models_body(*models: str) -> dict[str, Any]:
    """GUFO ``/models`` list without ``status`` — the raw shape PAIR can't use."""
    ids = models or (MODEL,)
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 1780000000, "owned_by": "gufo"}
            for m in ids
        ],
    }


class FakeGUFO:
    """A MockTransport-backed stand-in for GUFO.

    Route tables map a path to either a callable ``(request) -> httpx.Response``
    or an exception instance to raise (simulating transport failure). The
    default table mirrors GUFO v0.20.0: healthy, serving :data:`MODEL`, no
    ``role``, no ``status`` fields, 404 elsewhere.

    Every observed request is recorded in :attr:`requests` as
    ``(method, url, headers)`` so tests can assert on forwarding fidelity.
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.requests: list[tuple[str, httpx.URL, httpx.Headers]] = []
        base: dict[str, Any] = {
            "/health": httpx.Response(200, json={"status": "ok"}),
            "/props": httpx.Response(200, json=props_body()),
            "/models": httpx.Response(200, json=models_body()),
        }
        self.routes = {**base, **(routes or {})}

    @staticmethod
    def _streamed(response: httpx.Response) -> httpx.Response:
        """Rewrap a materialised response body as a chunk stream.

        httpx's MockTransport fully reads static bodies, after which the
        relay's ``response.aiter_raw()`` raises ``StreamConsumed`` — a fake
        limitation, not shim behaviour (a real socket-backed response always
        streams). Real streaming is what the shim must handle, so every
        static fake response is converted to generator content here.
        Responses whose body was never materialised (generator content —
        e.g. the mid-stream-failure fixtures) already stream and pass
        through untouched.
        """
        try:
            body = response.content
        except httpx.ResponseNotRead:
            return response

        async def chunks():
            for i in range(0, len(body), 4096):
                yield body[i : i + 4096]

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return httpx.Response(response.status_code, headers=headers, content=chunks())

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url, request.headers))
        route = self.routes.get(request.url.path, 404)
        if route == 404:
            return self._streamed(httpx.Response(404, json={"error": {"message": "not found"}}))
        if callable(route):
            return self._streamed(route(request))
        if isinstance(route, Exception):
            raise route
        if isinstance(route, tuple):  # (status, json_body)
            return self._streamed(httpx.Response(route[0], json=route[1]))
        if isinstance(route, httpx.Response):
            return self._streamed(route)
        return route

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def calls_to(self, path: str) -> int:
        return sum(1 for _, url, _ in self.requests if url.path == path)


@asynccontextmanager
async def shim_http(
    app, base_url: str = "http://shim"
) -> Any:
    """Run the app's real lifespan and hand back an ASGI-transport client.

    ``httpx.ASGITransport`` deliberately does not run ASGI lifespan events,
    so we drive them explicitly — that is what loads the persisted overlay
    and creates/closes the owned upstream client, exactly like production.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=base_url, timeout=15.0
        ) as client:
            yield client


def build_app(
    env: dict[str, str] | None = None,
    routes: dict[str, Any] | None = None,
    *,
    fake: FakeGUFO | None = None,
    load_state: bool = False,
    **create_kw: Any,
):
    """Assemble ``(app, fake_gufo)`` with a fake upstream injected.

    ``load_state`` defaults to False because unit tests construct Configs
    pointing at scratch paths and should opt in explicitly when testing
    persistence.
    """
    fake = fake or FakeGUFO(routes)
    config = Config(environ=env if env is not None else base_env())
    app = create_app(config, client=fake.client(), load_state=load_state, **create_kw)
    return app, fake


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UvicornServer:
    """Real in-process uvicorn server — only for SSE/disconnect semantics.

    Everything else in the suite runs through ``ASGITransport``; these few
    tests need genuine ``http.disconnect`` delivery and real socket timing.
    """

    def __init__(
        self,
        env: dict[str, str] | None = None,
        fake: FakeGUFO | None = None,
    ) -> None:
        import uvicorn

        self.port = free_port()
        config = Config(environ=env if env is not None else base_env())
        self.fake = fake or FakeGUFO()
        self._fake_client = self.fake.client()
        self.app = create_app(config, client=self._fake_client)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=self.port,
                log_level="critical",
                lifespan="on",
            )
        )
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> "UvicornServer":
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                return self
            await asyncio.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    async def __aexit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        if self.task:
            await asyncio.wait_for(self.task, timeout=15)
        await self._fake_client.aclose()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


async def read_sse_lines(response: httpx.Response, count: int, timeout: float = 8.0) -> list[str]:
    """Read ``count`` non-empty SSE lines from a streaming response."""
    lines: list[str] = []
    async for line in response.aiter_lines():
        line = line.strip()
        if line:
            lines.append(line)
        if len(lines) >= count:
            break
    return lines


__all__ = [
    "API_KEY",
    "FAKE_GUFO",
    "FakeGUFO",
    "Inventory",
    "MODEL",
    "ProgressHub",
    "UvicornServer",
    "base_env",
    "build_app",
    "free_port",
    "models_body",
    "props_body",
    "read_sse_lines",
    "shim_http",
]
