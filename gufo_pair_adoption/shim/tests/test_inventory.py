# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`gufo_masquerade.inventory` and the error envelope."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from gufo_masquerade.config import Config  # noqa: E402
from gufo_masquerade.errors import UpstreamUnavailable, json_error  # noqa: E402
from gufo_masquerade.inventory import Inventory  # noqa: E402

from unit_support import (  # noqa: E402
    MODEL,
    FakeGUFO,
    base_env,
    models_body,
    props_body,
)


def make_inventory(env_overrides: dict[str, str] | None = None) -> Inventory:
    return Inventory(Config(environ=base_env(**(env_overrides or {}))))


class JsonErrorTests(unittest.TestCase):
    def test_envelope_shape(self) -> None:
        resp = json_error(409, "unload_unsupported", "nope", known=["a"])
        body = json.loads(resp.body)
        self.assertEqual(resp.status_code, 409)
        self.assertIs(body["success"], False)
        self.assertEqual(body["error"]["type"], "unload_unsupported")
        self.assertEqual(body["error"]["code"], "unload_unsupported")
        self.assertEqual(body["error"]["message"], "nope")
        self.assertEqual(body["known"], ["a"])


class UpstreamReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fake = FakeGUFO()
        self.client = self.fake.client()
        self.inv = make_inventory()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_gufo_models_returns_data_array(self) -> None:
        rows = await self.inv.gufo_models(self.client)
        self.assertEqual([r["id"] for r in rows], [MODEL])

    async def test_models_transport_error_maps_to_upstream_unavailable(self) -> None:
        fake = FakeGUFO({"/models": httpx.ConnectError("connection refused")})
        async with fake.client() as client:
            inv = make_inventory()
            with self.assertRaises(UpstreamUnavailable) as ctx:
                await inv.gufo_models(client)
        self.assertIn("GET /models", str(ctx.exception))

    async def test_models_non_200_maps_to_upstream_unavailable(self) -> None:
        fake = FakeGUFO({"/models": (500, {"error": "boom"})})
        async with fake.client() as client:
            with self.assertRaises(UpstreamUnavailable) as ctx:
                await make_inventory().gufo_models(client)
        self.assertIn("HTTP 500", str(ctx.exception))

    async def test_models_non_json_body(self) -> None:
        fake = FakeGUFO({"/models": lambda r: httpx.Response(200, text="<html>")})
        async with fake.client() as client:
            with self.assertRaises(UpstreamUnavailable) as ctx:
                await make_inventory().gufo_models(client)
        self.assertIn("non-JSON", str(ctx.exception))

    async def test_models_without_data_array(self) -> None:
        fake = FakeGUFO({"/models": (200, {"object": "list"})})
        async with fake.client() as client:
            with self.assertRaises(UpstreamUnavailable) as ctx:
                await make_inventory().gufo_models(client)
        self.assertIn("no data array", str(ctx.exception))

    async def test_props_ok_and_error_taxonomy(self) -> None:
        props = await self.inv.gufo_props(self.client)
        self.assertEqual(props["model_alias"], MODEL)

        for routes, needle in (
            ({"/props": httpx.ConnectError("nope")}, "GET /props"),
            ({"/props": (503, {})}, "HTTP 503"),
            ({"/props": lambda r: httpx.Response(200, text="nope")}, "non-JSON"),
            ({"/props": (200, [1, 2])}, "not an object"),
        ):
            fake = FakeGUFO(routes)
            async with fake.client() as client:
                with self.assertRaises(UpstreamUnavailable) as ctx:
                    await make_inventory().gufo_props(client)
            self.assertIn(needle, str(ctx.exception))

    async def test_serving_model_prefers_alias_then_model_then_none(self) -> None:
        self.assertEqual(await self.inv.serving_model(self.client), MODEL)

        body = props_body()
        del body["model_alias"]
        fake = FakeGUFO({"/props": (200, body)})
        async with fake.client() as client:
            self.assertEqual(await make_inventory().serving_model(client), MODEL)

        body2 = props_body()
        del body2["model_alias"]
        del body2["model"]
        fake2 = FakeGUFO({"/props": (200, body2)})
        async with fake2.client() as client:
            self.assertIsNone(await make_inventory().serving_model(client))

    async def test_cache_respects_ttl(self) -> None:
        ttl_env = base_env(SHIM_INVENTORY_TTL_S="60")
        fake = FakeGUFO()
        async with fake.client() as client:
            inv = Inventory(Config(environ=ttl_env))
            await inv.gufo_models(client)
            await inv.gufo_models(client)
            self.assertEqual(fake.calls_to("/models"), 1, "second call should hit cache")

        zero_env = base_env(SHIM_INVENTORY_TTL_S="0")
        fake2 = FakeGUFO()
        async with fake2.client() as client:
            inv2 = Inventory(Config(environ=zero_env))
            await inv2.gufo_models(client)
            await inv2.gufo_models(client)
            self.assertEqual(fake2.calls_to("/models"), 2, "TTL=0 must never serve cache")

    async def test_mutation_invalidates_cache(self) -> None:
        env = base_env(SHIM_INVENTORY_TTL_S="60")
        fake = FakeGUFO()
        async with fake.client() as client:
            inv = Inventory(Config(environ=env))
            await inv.gufo_models(client)
            await inv.mark_deleted("other/model")  # invalidates
            await inv.gufo_models(client)
            self.assertEqual(fake.calls_to("/models"), 2)


class OverlayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fake = FakeGUFO()
        self.client = self.fake.client()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_status_for(self) -> None:
        inv = make_inventory()
        self.assertEqual(inv.status_for(MODEL), "loaded")
        await inv.mark_downloading("new/model")
        self.assertEqual(inv.status_for("new/model"), "downloading")
        await inv.clear_downloading("new/model")
        self.assertEqual(inv.status_for("new/model"), "loaded")

    async def test_clear_downloading_ignores_wrong_state(self) -> None:
        inv = make_inventory()
        inv.overlay["x"] = "something-else"
        await inv.clear_downloading("x")
        self.assertIn("x", inv.overlay, "only 'downloading' entries are cleared")
        await inv.clear_downloading("never-was")  # must not raise

    async def test_mark_deleted_persists_and_roundtrips(self) -> None:
        inv = make_inventory()
        await inv.mark_deleted(MODEL)
        with open(inv._state_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["deleted"], [MODEL])

        # Point a fresh inventory at the SAME file to prove persistence:
        cfg = Config(environ=base_env(SHIM_STATE_PATH=inv._state_path))
        inv2 = Inventory(cfg)
        inv2.load()
        self.assertEqual(inv2.deleted, {MODEL})

    async def test_load_missing_file_is_silent(self) -> None:
        cfg = Config(environ=base_env(SHIM_STATE_PATH="/nonexistent-dir-xyz/state.json"))
        inv = Inventory(cfg)
        inv.load()  # FileNotFoundError path: no raise, no state
        self.assertEqual(inv.deleted, set())

    async def test_load_corrupt_file_warns_and_starts_clean(self) -> None:
        path = os.path.join(tempfile.mkdtemp(prefix="shim-corrupt-"), "state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        inv = Inventory(Config(environ=base_env(SHIM_STATE_PATH=path)))
        with self.assertLogs("gufo-masquerade.inventory", level="WARNING"):
            inv.load()
        self.assertEqual(inv.deleted, set())

    async def test_load_unreadable_file_warns(self) -> None:
        # A directory where the state file should be raises IsADirectoryError
        # (an OSError) — same tolerated path as corruption.
        d = tempfile.mkdtemp(prefix="shim-dir-state-")
        inv = Inventory(Config(environ=base_env(SHIM_STATE_PATH=d)))
        with self.assertLogs("gufo-masquerade.inventory", level="WARNING"):
            inv.load()
        self.assertEqual(inv.deleted, set())

    async def test_save_failure_is_logged_not_raised(self) -> None:
        # /proc can never hold our temp file: dirname exists but is read-only.
        inv = Inventory(Config(environ=base_env(SHIM_STATE_PATH="/proc/nope/state.json")))
        with self.assertLogs("gufo-masquerade.inventory", level="ERROR"):
            await inv.mark_deleted("a/model")
        self.assertIn("a/model", inv.deleted, "in-memory state must still update")

    async def test_visible_filters_deleted_rows(self) -> None:
        inv = make_inventory()
        rows = models_body("a", "b")["data"]
        self.assertEqual(len(inv.visible(rows)), 2)
        await inv.mark_deleted("a")
        self.assertEqual([r["id"] for r in inv.visible(rows)], ["b"])

    async def test_ids_includes_downloading_overlay(self) -> None:
        inv = make_inventory()
        await inv.mark_downloading("pulling/model")
        ids = await inv.ids(self.client)
        self.assertEqual(ids, {MODEL, "pulling/model"})

    async def test_visible_or_fail_enriches_and_appends_virtual_rows(self) -> None:
        inv = make_inventory()
        raw = models_body(MODEL)["data"] + [
            {"object": "model", "owned_by": "gufo"},  # malformed: no id
        ]
        fake = FakeGUFO({"/models": (200, {"object": "list", "data": raw})})
        async with fake.client() as client:
            await inv.mark_downloading("pull/model")
            rows = await inv.visible_or_fail(client)
        self.assertTrue(all(isinstance(r.get("id"), str) for r in rows))
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[MODEL]["status"]["value"], "loaded")
        self.assertEqual(by_id[MODEL]["status"]["normalized"], "loaded")
        self.assertEqual(by_id["pull/model"]["status"]["value"], "downloading")
        self.assertEqual(by_id["pull/model"]["owned_by"], "gufo-masquerade")

    async def test_visible_or_fail_skips_virtual_dup_of_real_row(self) -> None:
        inv = make_inventory()
        await inv.mark_downloading(MODEL)  # MODEL is also in upstream list
        rows = await inv.visible_or_fail(self.client)
        self.assertEqual([r["id"] for r in rows].count(MODEL), 1)
        self.assertEqual(rows[0]["status"]["value"], "downloading")

    async def test_visible_or_fail_propagates_upstream_failure(self) -> None:
        fake = FakeGUFO({"/models": httpx.ConnectError("dead")})
        async with fake.client() as client:
            with self.assertRaises(UpstreamUnavailable):
                await make_inventory().visible_or_fail(client)

    async def test_is_actively_serving(self) -> None:
        inv = make_inventory()
        self.assertFalse(inv.is_actively_serving("m", None))
        self.assertTrue(inv.is_actively_serving("m", "m"))
        self.assertTrue(inv.is_actively_serving("m", "m:Q4_K_XL"))
        self.assertFalse(inv.is_actively_serving("other", "m:Q4_K_XL"))


if __name__ == "__main__":
    unittest.main()
