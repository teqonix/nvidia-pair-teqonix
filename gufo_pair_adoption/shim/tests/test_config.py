# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`gufo_masquerade.config` (pure, synchronous)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gufo_masquerade.config import (  # noqa: E402
    DEFAULT_VERSION,
    PULL_POLICIES,
    Config,
    env_bool,
    env_choice,
    env_int,
)


class EnvBoolTests(unittest.TestCase):
    def test_absent_returns_default(self) -> None:
        self.assertTrue(env_bool("NOPE_X", True))
        self.assertFalse(env_bool("NOPE_X", False))

    def test_truthy_spellings(self) -> None:
        for raw in ("1", "true", "TRUE", "Yes", " on "):
            self.assertTrue(env_bool("V", False, {"V": raw}), raw)

    def test_everything_else_is_false(self) -> None:
        for raw in ("0", "false", "no", "off", "", " ", "banana"):
            self.assertFalse(env_bool("V", True, {"V": raw}), raw)


class EnvIntTests(unittest.TestCase):
    def test_absent_and_blank_return_default(self) -> None:
        self.assertEqual(env_int("NOPE_X", 7), 7)
        self.assertEqual(env_int("V", 7, {"V": "   "}), 7)

    def test_parses(self) -> None:
        self.assertEqual(env_int("V", 7, {"V": "42"}), 42)
        self.assertEqual(env_int("V", 7, {"V": "-3"}), -3)

    def test_garbage_raises_with_variable_name(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            env_int("SHIM_PORT", 8081, {"SHIM_PORT": "http://x"})
        self.assertIn("SHIM_PORT", str(ctx.exception))


class EnvChoiceTests(unittest.TestCase):
    def test_absent_and_blank_return_default(self) -> None:
        self.assertEqual(env_choice("NOPE_X", "present-ok", PULL_POLICIES), "present-ok")
        self.assertEqual(env_choice("V", "reject", PULL_POLICIES, {"V": "  "}), "reject")

    def test_normalises_case(self) -> None:
        self.assertEqual(env_choice("V", "reject", PULL_POLICIES, {"V": " FAKE "}), "fake")

    def test_unknown_raises_listing_alternatives(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            env_choice("SHIM_PULL_POLICY", "present-ok", PULL_POLICIES,
                       {"SHIM_PULL_POLICY": "maybe"})
        self.assertIn("present-ok", str(ctx.exception))
        self.assertIn("SHIM_PULL_POLICY", str(ctx.exception))


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_deployment(self) -> None:
        cfg = Config(environ={})
        self.assertEqual(cfg.listen_host, "127.0.0.1")
        self.assertEqual(cfg.listen_port, 8081)
        self.assertEqual(cfg.gufo_base_url, "http://127.0.0.1:18080")
        self.assertEqual(cfg.pull_policy, "present-ok")
        self.assertTrue(cfg.refuse_unload_serving)
        self.assertEqual(cfg.shim_version, DEFAULT_VERSION)
        self.assertEqual(cfg.log_level, "info")

    def test_base_url_trailing_slash_normalised(self) -> None:
        cfg = Config(environ={"GUFO_BASE_URL": "http://g:1/"})
        self.assertEqual(cfg.gufo_base_url, "http://g:1")

    def test_blank_log_level_falls_back_to_info(self) -> None:
        self.assertEqual(Config(environ={"SHIM_LOG_LEVEL": "  "}).log_level, "info")
        self.assertEqual(Config(environ={"SHIM_LOG_LEVEL": " WARNING "}).log_level, "warning")

    def test_all_knobs_parse(self) -> None:
        cfg = Config(environ={
            "SHIM_HOST": "0.0.0.0",
            "SHIM_PORT": "9000",
            "GUFO_API_KEY": "k",
            "SHIM_ROUTER_ROLE": "super-router",
            "SHIM_PULL_POLICY": "reject",
            "SHIM_REFUSE_UNLOAD_SERVING": "0",
            "SHIM_STATE_PATH": "/tmp/s.json",
            "SHIM_CONNECT_TIMEOUT_S": "2",
            "SHIM_DISCOVERY_TIMEOUT_S": "3",
            "SHIM_READ_TIMEOUT_S": "40",
            "SHIM_INVENTORY_TTL_S": "1",
            "SHIM_SSE_KEEPALIVE_S": "7",
            "SHIM_FAKE_BYTES": "100",
            "SHIM_FAKE_TICKS": "5",
            "SHIM_FAKE_TICK_MS": "50",
            "SHIM_ACCESS_LOG": "yes",
        })
        self.assertEqual(cfg.listen_host, "0.0.0.0")
        self.assertEqual(cfg.listen_port, 9000)
        self.assertEqual(cfg.router_role, "super-router")
        self.assertEqual(cfg.pull_policy, "reject")
        self.assertFalse(cfg.refuse_unload_serving)
        self.assertEqual(cfg.state_path, "/tmp/s.json")
        self.assertEqual(cfg.connect_timeout_s, 2)
        self.assertEqual(cfg.discovery_timeout_s, 3)
        self.assertEqual(cfg.read_timeout_s, 40)
        self.assertEqual(cfg.inventory_ttl_s, 1)
        self.assertEqual(cfg.sse_keepalive_s, 7)
        self.assertEqual(cfg.fake_bytes, 100)
        self.assertEqual(cfg.fake_ticks, 5)
        self.assertAlmostEqual(cfg.fake_tick_s, 0.05)
        self.assertTrue(cfg.access_log)

    def test_frozen_after_construction(self) -> None:
        cfg = Config(environ={})
        with self.assertRaises(Exception):
            cfg.listen_port = 1  # type: ignore[misc]

    def test_gufo_headers(self) -> None:
        self.assertEqual(Config(environ={}).gufo_headers, {})
        self.assertEqual(
            Config(environ={"GUFO_API_KEY": "abc"}).gufo_headers,
            {"Authorization": "Bearer abc"},
        )

    def test_upstream_timeout_selects_read(self) -> None:
        cfg = Config(environ={"SHIM_CONNECT_TIMEOUT_S": "3", "SHIM_READ_TIMEOUT_S": "500"})
        self.assertEqual(cfg.upstream_timeout().connect, 3)
        self.assertEqual(cfg.upstream_timeout().read, 500)
        self.assertEqual(cfg.upstream_timeout(9).read, 9)

    def test_describe_never_leaks_the_key(self) -> None:
        cfg = Config(environ={"GUFO_API_KEY": "supersecret", "SHIM_HOST": "1.2.3.4",
                              "SHIM_PORT": "8081"})
        described = cfg.describe()
        self.assertNotIn("supersecret", str(described))
        self.assertTrue(described["api_key_configured"])
        self.assertEqual(described["listen"], "1.2.3.4:8081")
        self.assertFalse(Config(environ={}).describe()["api_key_configured"])


if __name__ == "__main__":
    unittest.main()
