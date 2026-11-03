# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`gufo_masquerade.app` — routes, relay, header policy.

The upstream is an ``httpx.MockTransport`` fake (see ``unit_support``), so
these run in-process with no ports, and forwarding fidelity is asserted from
the fake's recorded requests.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from gufo_masquerade.app import (  # noqa: E402
    ENGINE_IDENTITY_HEADER,
    UNSUPPORTED_PATHS,
    create_app,
    forward_headers,
    json_body,
    passthrough_headers,
    relay,
)
from gufo_masquerade.config import Config  # noqa: E402

from unit_support import (  # noqa: E402
    API_KEY,
    MODEL,
    FakeGUFO,
    base_env,
    build_app,
    models_body,
    props_body,
    shim_http,
)


class PureHelperTests(unittest.TestCase):
    """Header policy and body parsing — no app needed."""

    def test_forward_headers_strips_and_injects(self) -> None:
        scope = {
            "type": "http",
            "headers": [
                (b"host", b"shim:8081"),
                (b"connection", b"keep-alive"),
                (b"transfer-encoding", b"chunked"),
                (b"x-nvpair-engine-identity-probe", b"1"),
                (b"authorization", b"Bearer client-token"),
                (b"content-type", b"application/json"),
            ],
        }
        request = __import__("fastapi").Request(scope)
        cfg = Config(environ={"GUFO_API_KEY": API_KEY})
        out = forward_headers(request, cfg)
        self.assertNotIn("host", {k.lower() for k in out})
        self.assertNotIn("connection", {k.lower() for k in out})
        self.assertNotIn("transfer-encoding", {k.lower() for k in out})
        self.assertNotIn(ENGINE_IDENTITY_HEADER.lower(), {k.lower() for k in out})
        # The client's Authorization is replaced, not appended to.
        self.assertEqual(out["Authorization"], f"Bearer {API_KEY}")
        self.assertEqual(out["content-type"], "application/json")

    def test_forward_headers_without_key_drops_client_auth(self) -> None:
        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Bearer client-token")],
        }
        request = __import__("fastapi").Request(scope)
        out = forward_headers(request, Config(environ={}))
        self.assertNotIn("authorization", {k.lower() for k in out})

    def test_passthrough_headers(self) -> None:
        upstream = httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-length": "42",
                "transfer-encoding": "chunked",
                "date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "server": "gufo",
                "x-custom": "keep-me",
            },
        )
        out = passthrough_headers(upstream)
        self.assertEqual(out["content-type"], "application/json")
        self.assertEqual(out["x-custom"], "keep-me")
        self.assertEqual(out["X-Accel-Buffering"], "no")
        self.assertEqual(out["Cache-Control"], "no-cache")
        for gone in ("content-length", "transfer-encoding", "date", "server"):
            self.assertNotIn(gone, {k.lower() for k in out})


class JsonBodyTests(unittest.IsolatedAsyncioTestCase):
    async def _parse(self, raw: bytes | None) -> Any:
        headers = [(b"content-type", b"application/json")]
        scope = {"type": "http", "headers": headers}

        async def receive():
            if raw is None:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": raw, "more_body": False}

        from starlette.requests import Request

        return await json_body(Request(scope, receive))

    async def test_valid_object(self) -> None:
        self.assertEqual(await self._parse(b'{"model": "m"}'), {"model": "m"})

    async def test_empty_is_empty_dict(self) -> None:
        self.assertEqual(await self._parse(b""), {})

    async def test_invalid_json_is_empty_dict(self) -> None:
        self.assertEqual(await self._parse(b"{oops"), {})

    async def test_non_object_json_is_empty_dict(self) -> None:
        self.assertEqual(await self._parse(b"[1,2]"), {})


class PropsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_injects_role_and_keeps_gufo_fields(self) -> None:
        app, fake = build_app()
        async with shim_http(app) as c:
            r = await c.get("/props")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["role"], "router")
        self.assertEqual(body["model_alias"], MODEL)
        self.assertEqual(body["build_info"]["version"], "0.20.0")
        self.assertEqual(body["masquerade"]["engine"], "gufo")
        self.assertNotIn("role_upstream", body)
        # Upstream must not see the probe stamp, but must see the key.
        _m, _u, headers = fake.requests[-1]
        self.assertNotIn(ENGINE_IDENTITY_HEADER.lower(), {k.lower() for k in headers.keys()})
        self.assertEqual(headers.get("authorization"), f"Bearer {API_KEY}")

    async def test_conflicting_upstream_role_is_overridden_but_recorded(self) -> None:
        fake = FakeGUFO({"/props": (200, props_body(role="worker"))})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            body = (await c.get("/props")).json()
        self.assertEqual(body["role"], "router")
        self.assertEqual(body["role_upstream"], "worker")

    async def test_matching_upstream_role_passes_through_cleanly(self) -> None:
        fake = FakeGUFO({"/props": (200, props_body(role="router"))})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            body = (await c.get("/props")).json()
        self.assertEqual(body["role"], "router")
        self.assertNotIn("role_upstream", body)

    async def test_router_role_is_configurable(self) -> None:
        app, _ = build_app(env=base_env(SHIM_ROUTER_ROLE="super-router"))
        async with shim_http(app) as c:
            body = (await c.get("/props")).json()
        self.assertEqual(body["role"], "super-router")

    async def test_dead_upstream_never_reports_ready(self) -> None:
        fake = FakeGUFO({"/props": httpx.ConnectError("connection refused")})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            r = await c.get("/props")
        self.assertEqual(r.status_code, 502)
        self.assertIs(r.json()["success"], False)
        self.assertEqual(r.json()["error"]["code"], "upstream_unavailable")


class HealthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _each_alias(self, app) -> list[httpx.Response]:
        out = []
        async with shim_http(app) as c:
            for path in ("/health", "/v1/health", "/healthz"):
                out.append(await c.get(path))
        return out

    async def test_ok_embeds_upstream_body(self) -> None:
        app, fake = build_app()
        responses = await self._each_alias(app)
        for r in responses:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "ok")
            self.assertEqual(r.json()["upstream"], {"status": "ok"})
        self.assertEqual(fake.calls_to("/health"), 3)

    async def test_upstream_down_is_503(self) -> None:
        app, _ = build_app(fake=FakeGUFO({"/health": httpx.ConnectError("dead")}))
        for r in await self._each_alias(app):
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()["error"]["code"], "upstream_unavailable")

    async def test_upstream_unhealthy_is_503(self) -> None:
        app, _ = build_app(fake=FakeGUFO({"/health": (500, {"status": "red"})}))
        for r in await self._each_alias(app):
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()["error"]["code"], "upstream_unhealthy")

    async def test_non_json_upstream_health_still_ok(self) -> None:
        fake = FakeGUFO({"/health": lambda r: httpx.Response(200, text="OK")})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            r = await c.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["upstream"], {"status": "ok"})


class ModelsListTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_enriched_deleted_hidden(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            rows = (await c.get("/models")).json()["data"]
        self.assertEqual(rows[0]["status"]["value"], "loaded")
        async with shim_http(app) as c:
            rows_v1 = (await c.get("/v1/models")).json()["data"]
        self.assertEqual(rows_v1, rows)

    async def test_upstream_failure_is_502(self) -> None:
        app, _ = build_app(fake=FakeGUFO({"/models": httpx.ConnectError("dead")}))
        async with shim_http(app) as c:
            r = await c.get("/models")
        self.assertEqual(r.status_code, 502)


class LoadRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_resident_model_acks(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            r = await c.post("/models/load", json={"model": MODEL})
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["success"], True)

    async def test_unknown_model_404_lists_known(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            r = await c.post("/models/load", json={"model": "bogus/x"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["known"], [MODEL])

    async def test_invalid_payloads_400(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            for payload in ({}, {"model": ""}, {"model": "   "}, {"model": 12}):
                r = await c.post("/models/load", json=payload)
                self.assertEqual(r.status_code, 400, payload)
            r = await c.post("/models/load", content=b"not json")
            self.assertEqual(r.status_code, 400)

    async def test_upstream_failure_is_502(self) -> None:
        app, _ = build_app(fake=FakeGUFO({"/models": httpx.ConnectError("dead")}))
        async with shim_http(app) as c:
            r = await c.post("/models/load", json={"model": MODEL})
        self.assertEqual(r.status_code, 502)


class UnloadRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_inflight_succeeds(self) -> None:
        app, _ = build_app(env=base_env(SHIM_PULL_POLICY="fake", SHIM_FAKE_TICK_MS="50"))
        async with shim_http(app) as c:
            ack = await c.post("/models", json={"model": "demo/pull"})
            self.assertIs(ack.json()["success"], True)
            state = (await c.get("/shim/state")).json()
            self.assertIn("demo/pull", state["overlay"])
            r = await c.post("/models/unload", json={"model": "demo/pull"})
            self.assertEqual(r.status_code, 200)
            self.assertIs(r.json()["success"], True)
        await asyncio.sleep(0.05)

    async def test_live_model_refused_by_default(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            r = await c.post("/models/unload", json={"model": MODEL})
        self.assertEqual(r.status_code, 409)
        self.assertIs(r.json()["success"], False)

    async def test_live_model_refuses_tag_prefix_too(self) -> None:
        fake = FakeGUFO({"/props": (200, props_body(model_alias="m:Q4_K_XL"))})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            r = await c.post("/models/unload", json={"model": "m"})
        self.assertEqual(r.status_code, 409)

    async def test_forced_ack_mode(self) -> None:
        app, _ = build_app(env=base_env(SHIM_REFUSE_UNLOAD_SERVING="0"))
        async with shim_http(app) as c:
            r = await c.post("/models/unload", json={"model": MODEL})
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["success"], True)

    async def test_invalid_model_400(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            r = await c.post("/models/unload", json={})
        self.assertEqual(r.status_code, 400)

    async def test_upstream_failure_is_502(self) -> None:
        app, _ = build_app(fake=FakeGUFO({"/props": httpx.ConnectError("dead")}))
        async with shim_http(app) as c:
            r = await c.post("/models/unload", json={"model": MODEL})
        self.assertEqual(r.status_code, 502)


class DeleteRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_hides_model_persistently(self) -> None:
        env = base_env(SHIM_REFUSE_UNLOAD_SERVING="0")
        app, _ = build_app(env=env)
        async with shim_http(app) as c:
            r = await c.delete("/models", params={"model": MODEL})
            self.assertEqual(r.status_code, 200)
            self.assertIs(r.json()["success"], True)
            rows = (await c.get("/models")).json()["data"]
            self.assertEqual(rows, [])
        # A second app over the same state file keeps the deletion.
        app2, _ = build_app(env=env, load_state=True)
        async with shim_http(app2) as c:
            rows = (await c.get("/models")).json()["data"]
        self.assertEqual(rows, [], "deleted model must not resurrect after restart")

    async def test_delete_live_model_409_by_default(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            r = await c.delete("/models", params={"model": MODEL})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "delete_unsupported")

    async def test_delete_unknown_404(self) -> None:
        app, _ = build_app(env=base_env(SHIM_REFUSE_UNLOAD_SERVING="0"))
        async with shim_http(app) as c:
            r = await c.delete("/models", params={"model": "ghost/x"})
        self.assertEqual(r.status_code, 404)

    async def test_missing_param_400(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            self.assertEqual((await c.delete("/models")).status_code, 400)
            self.assertEqual(
                (await c.delete("/models", params={"model": "  "})).status_code, 400
            )

    async def test_upstream_failure_is_502(self) -> None:
        app, _ = build_app(fake=FakeGUFO({"/props": httpx.ConnectError("dead")}))
        async with shim_http(app) as c:
            r = await c.delete("/models", params={"model": MODEL})
        self.assertEqual(r.status_code, 502)


class PullPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_present_ok_resident_publishes_terminal_frames(self) -> None:
        app, _ = build_app()
        hub = app.state.hub
        q = await hub.subscribe(MODEL)
        async with shim_http(app) as c:
            r = await c.post("/models", json={"model": MODEL})
        self.assertEqual(r.status_code, 202)
        self.assertIs(r.json()["success"], True)
        names = [q.get_nowait()["event"] for _ in range(q.qsize())]
        self.assertEqual(names, ["download_progress", "download_finished"])

    async def test_present_ok_unknown_is_501_with_guidance(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            r = await c.post("/models", json={"model": "new/model"})
        self.assertEqual(r.status_code, 501)
        self.assertEqual(r.json()["error"]["code"], "pull_unsupported")
        self.assertEqual(r.json()["known"], [MODEL])

    async def test_present_ok_upstream_failure_is_502(self) -> None:
        app, _ = build_app(fake=FakeGUFO({"/models": httpx.ConnectError("dead")}))
        async with shim_http(app) as c:
            r = await c.post("/models", json={"model": MODEL})
        self.assertEqual(r.status_code, 502)

    async def test_reject_policy(self) -> None:
        app, _ = build_app(env=base_env(SHIM_PULL_POLICY="reject"))
        async with shim_http(app) as c:
            r = await c.post("/models", json={"model": MODEL})
        self.assertEqual(r.status_code, 501)

    async def test_fake_policy_runs_to_completion(self) -> None:
        app, _ = build_app(
            env=base_env(SHIM_PULL_POLICY="fake", SHIM_FAKE_TICKS="2",
                         SHIM_FAKE_TICK_MS="1", SHIM_FAKE_BYTES="20")
        )
        hub = app.state.hub
        q = await hub.subscribe("demo/model")
        async with shim_http(app) as c:
            r = await c.post("/models", json={"model": "demo/model"})
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.json()["policy"], "fake")
            state = (await c.get("/shim/state")).json()
            self.assertEqual(state["overlay"].get("demo/model"), "downloading")
        # Let the background task run to its terminal frame.
        events = []
        for _ in range(20):
            try:
                events.append(await asyncio.wait_for(q.get(), timeout=1.0))
            except asyncio.TimeoutError:
                break
            if events[-1]["event"] == "download_finished":
                break
        self.assertEqual(events[-1]["event"], "download_finished")
        # The done-callback (set discard) is scheduled via call_soon; give it
        # a chance to run before asserting the registry self-cleaned.
        for _ in range(50):
            if not app.state.background_tasks:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(app.state.background_tasks), 0, "task set must self-clean")

    async def test_fake_download_cancellation_publishes_failure(self) -> None:
        from gufo_masquerade.app import fake_download
        from gufo_masquerade.inventory import Inventory
        from gufo_masquerade.sse import ProgressHub

        cfg = Config(environ=base_env(SHIM_FAKE_TICK_MS="1000"))
        hub = ProgressHub()
        inv = Inventory(cfg)
        await inv.mark_downloading("demo/model")
        q = await hub.subscribe("demo/model")
        task = asyncio.create_task(fake_download("demo/model", cfg, hub, inv))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        frame = await asyncio.wait_for(q.get(), timeout=1.0)
        self.assertEqual(frame["event"], "download_failed")
        self.assertEqual(inv.overlay, {}, "overlay must clear even on cancellation")

    async def test_missing_model_400_all_policies(self) -> None:
        for policy in ("present-ok", "fake", "reject"):
            app, _ = build_app(env=base_env(SHIM_PULL_POLICY=policy))
            async with shim_http(app) as c:
                r = await c.post("/models", json={})
            self.assertEqual(r.status_code, 400, policy)


class UnsupportedTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_paths_fail_fast_locally(self) -> None:
        app, fake = build_app()
        async with shim_http(app) as c:
            for path in UNSUPPORTED_PATHS:
                r = await c.post(path, json={"input": "x"})
                self.assertEqual(r.status_code, 501, path)
                self.assertEqual(r.json()["error"]["code"], "unsupported_task", path)
        # None of them may reach GUFO.
        for path in UNSUPPORTED_PATHS:
            self.assertEqual(fake.calls_to(path), 0)

    async def test_get_on_unsupported_path_still_fails_fast(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            r = await c.get("/infill")
        self.assertEqual(r.status_code, 501)


class IntrospectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_state_sse_stats(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            cfg = (await c.get("/shim/config")).json()
            self.assertNotIn("gufo_api_key", cfg)
            self.assertTrue(cfg["api_key_configured"])
            st = (await c.get("/shim/state")).json()
            self.assertEqual(st, {"deleted": [], "overlay": {}})
            stats = (await c.get("/shim/sse_stats")).json()
            self.assertEqual(stats, {"subscribers": 0})

    async def test_unknown_shim_path_is_local_404(self) -> None:
        app, fake = build_app()
        async with shim_http(app) as c:
            r = await c.get("/shim/whatever")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")
        self.assertEqual(fake.calls_to("/shim/whatever"), 0, "must not reach GUFO")

    async def test_introspection_survives_dead_upstream(self) -> None:
        app, _ = build_app(
            fake=FakeGUFO({"/health": httpx.ConnectError("d"),
                           "/props": httpx.ConnectError("d"),
                           "/models": httpx.ConnectError("d")})
        )
        async with shim_http(app) as c:
            for path in ("/shim/config", "/shim/state", "/shim/sse_stats"):
                self.assertEqual((await c.get(path)).status_code, 200, path)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_completion_sync(self) -> None:
        body = {"id": "c", "choices": [{"message": {"content": "hi"}}]}
        fake = FakeGUFO({"/v1/chat/completions": (200, body)})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            r = await c.post("/v1/chat/completions", json={"messages": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "hi")

    async def test_query_string_is_preserved(self) -> None:
        # /slots is pure passthrough (control endpoints like /props are served
        # locally and intentionally do not forward the query string).
        fake = FakeGUFO({"/slots": lambda req: httpx.Response(
            200, json={"echo": str(req.url)})})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            r = await c.get("/slots", params={"model": "x"})
        self.assertEqual(r.status_code, 200)
        method, url, _h = fake.requests[-1]
        self.assertEqual(url.path, "/slots")
        self.assertIn("model=x", str(url.query))

    async def test_passthrough_404_is_not_invented_away(self) -> None:
        app, _ = build_app()
        async with shim_http(app) as c:
            r = await c.get("/definitely-not-real")
        self.assertEqual(r.status_code, 404)

    async def test_upstream_connect_error_is_502_json(self) -> None:
        fake = FakeGUFO({"/completion": httpx.ConnectError("connection refused")})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            r = await c.post("/completion", json={"prompt": "x"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"]["code"], "bad_gateway")

    async def test_upstream_break_midstream_ends_response(self) -> None:
        """Status already sent -> the stream must end quietly, not 500."""

        async def streaming_body():
            yield b"data: part1\n\n"
            raise httpx.ReadError("upstream vanished mid-generation")

        fake = FakeGUFO(
            {"/v1/chat/completions": lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=streaming_body())}
        )
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            r = await c.post("/v1/chat/completions", json={"stream": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"data: part1", r.content)

    async def test_relay_headers_forwarded_and_response_sanitised(self) -> None:
        seen: dict = {}

        def echo(req: httpx.Request) -> httpx.Response:
            seen["auth"] = req.headers.get("authorization")
            seen["probe"] = req.headers.get(ENGINE_IDENTITY_HEADER.lower())
            seen["body"] = req.content
            return httpx.Response(
                201, json={"ok": True},
                headers={"content-length": "9", "date": "now", "x-trace": "t"},
            )

        fake = FakeGUFO({"/tokenize": echo})
        app, _ = build_app(fake=fake)
        async with shim_http(app) as c:
            r = await c.post(
                "/tokenize",
                json={"content": "hello"},
                headers={ENGINE_IDENTITY_HEADER: "1", "Authorization": "Bearer nope"},
            )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.headers["x-trace"], "t")
        self.assertNotIn("date", {k.lower() for k in r.headers.keys()})
        self.assertEqual(seen["auth"], f"Bearer {API_KEY}", "shim's key replaces PAIR's")
        self.assertIsNone(seen["probe"], "probe header must be stripped")
        self.assertEqual(json.loads(seen["body"]), {"content": "hello"})

    async def test_relay_unit_direct(self) -> None:
        """Call relay() with a raw Request to pin its standalone contract."""
        from starlette.requests import Request

        cfg = Config(environ=base_env())
        fake = FakeGUFO({"/abc": (200, {"ok": 1})})
        async with fake.client() as client:

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            scope = {
                "type": "http", "method": "GET", "path": "/abc",
                "query_string": b"", "headers": [],
            }
            response = await relay(Request(scope, receive), "/abc", cfg, client)
            self.assertEqual(response.status_code, 200)


async def _always_true() -> bool:
    return True


class SseRouteTests(unittest.IsolatedAsyncioTestCase):
    """SSE over a real in-process uvicorn server (disconnect semantics)."""

    async def test_frames_flow_over_real_server(self) -> None:
        """Full subscribe-before-POST handshake over a real socket."""
        from unit_support import UvicornServer

        env = base_env(SHIM_PULL_POLICY="fake", SHIM_FAKE_TICKS="3",
                       SHIM_FAKE_TICK_MS="20", SHIM_FAKE_BYTES="30",
                       SHIM_SSE_KEEPALIVE_S="5")
        async with UvicornServer(env=env) as srv:
            async with httpx.AsyncClient(timeout=15.0) as c:
                async with c.stream("GET", srv.url("/models/sse"),
                                    params={"model": "demo/model"}) as stream:
                    lines = stream.aiter_lines()
                    opener = (await anext(lines)).strip()
                    self.assertTrue(opener.startswith(": gufo-masquerade"), opener)
                    ack = await c.post(srv.url("/models"), json={"model": "demo/model"})
                    self.assertIs(ack.json()["success"], True)
                    stats = (await c.get(srv.url("/shim/sse_stats"))).json()
                    self.assertEqual(stats["subscribers"], 1)
                    payload: list[str] = []
                    async for line in lines:
                        line = line.strip()
                        if line.startswith("data: "):
                            payload.append(line)
                        if any(e in line for e in ("download_finished", "download_failed")):
                            break
            events = [json.loads(p[len("data: "):]) for p in payload]
            names = [e["event"] for e in events]
            self.assertEqual(names[:3], ["download_progress"] * 3, payload)
            self.assertEqual(names[-1], "download_finished")
            pcts = [
                e["data"]["progress"]["model.gguf"] for e in events
            ]
            dones = [p["done"] for p in pcts]
            self.assertEqual(dones, sorted(dones), "progress must be monotonic")
            self.assertTrue(all(p["total"] == 30 for p in pcts))

    async def test_idle_stream_emits_keepalives(self) -> None:
        from unit_support import UvicornServer, read_sse_lines

        env = base_env(SHIM_SSE_KEEPALIVE_S="1")
        async with UvicornServer(env=env) as srv:
            async with httpx.AsyncClient(timeout=15.0) as c:
                async with c.stream("GET", srv.url("/models/sse"),
                                    params={"model": "idle/model"}) as stream:
                    lines = await read_sse_lines(stream, 2)
            self.assertTrue(lines[0].startswith(": gufo-masquerade"), lines)
            self.assertTrue(lines[1].startswith(": keepalive"), lines)

    async def test_disconnect_unsubscribes_while_server_keeps_running(self) -> None:
        """The generator must notice the gone client on its own loop.

        This is the disconnect-detection path (not the server-shutdown path):
        the stream is abandoned while uvicorn keeps serving, the generator
        wakes on its keepalive timeout, sees http.disconnect, and unsubscribes.
        """
        from unit_support import UvicornServer

        env = base_env(SHIM_SSE_KEEPALIVE_S="1")
        async with UvicornServer(env=env) as srv:
            async with httpx.AsyncClient(timeout=10.0) as c:
                stream_ctx = c.stream("GET", srv.url("/models/sse"),
                                      params={"model": "gone/soon"})
                stream = await stream_ctx.__aenter__()
                await stream.aiter_bytes().__anext__()  # the open comment
                stats = (await c.get(srv.url("/shim/sse_stats"))).json()
                self.assertEqual(stats["subscribers"], 1)
                await stream.aclose()
                await stream_ctx.__aexit__(None, None, None)
                await asyncio.sleep(2.5)  # > one keepalive cycle
                stats = (await c.get(srv.url("/shim/sse_stats"))).json()
            self.assertEqual(stats["subscribers"], 0,
                             "client disconnect must unsubscribe the hub")

    async def test_disconnected_request_short_circuits_stream(self) -> None:
        """Drive the route directly with a client already gone.

        uvicorn cancels abandoned response tasks rather than letting them
        poll, so the explicit ``is_disconnected`` check can only be reached
        deterministically at unit level: a stub Request reporting
        disconnected must end the stream after the opening comment and
        unsubscribe in the ``finally``.
        """
        from starlette.requests import Request

        app, _ = build_app()
        hub = app.state.hub
        endpoint = next(
            r.endpoint for r in app.routes
            if getattr(r, "path", "") == "/models/sse"
        )

        scope = {"type": "http", "method": "GET", "path": "/models/sse",
                 "query_string": b"model=x", "headers": []}
        request = Request(scope)
        request.is_disconnected = lambda: _always_true()  # type: ignore[method-assign]

        response = await endpoint(request)
        chunks = [chunk async for chunk in response.body_iterator]
        self.assertEqual(chunks, [b": gufo-masquerade sse open\n\n"])
        self.assertEqual(await hub.subscriber_count(), 0)

    async def test_disconnect_unsubscribes_on_abandon(self) -> None:
        from unit_support import UvicornServer

        async with UvicornServer() as srv:
            async with httpx.AsyncClient(timeout=10.0) as c:
                async with c.stream("GET", srv.url("/models/sse"),
                                    params={"model": "gone/soon"}) as stream:
                    async for chunk in stream.aiter_bytes():
                        break  # read one chunk, then leave
                await asyncio.sleep(1.8)  # one keepalive cycle to notice exit
                stats = (await c.get(srv.url("/shim/sse_stats"))).json()
            self.assertEqual(stats["subscribers"], 0,
                             "client disconnect must unsubscribe the hub")


if __name__ == "__main__":
    unittest.main()
