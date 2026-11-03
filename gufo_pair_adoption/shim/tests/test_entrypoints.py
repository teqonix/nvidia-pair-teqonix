# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shim's entrypoints and lifecycle paths.

Covers:

*   ``python gufo_masquerade_shim.py`` — the thin launcher, as a real
    subprocess from an unrelated cwd (exercises its sys.path bootstrap, the
    thing systemd depends on).
*   ``gufo_masquerade.__main__.main`` — with uvicorn faked in, asserting the
    server options (drain window, loopback bind, no server header).
*   The lifespan-owned upstream client: created lazily inside the loop and
    closed on shutdown; injected clients are never closed by the app.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import gufo_masquerade.__main__ as shim_main  # noqa: E402

from gufo_masquerade.app import create_app  # noqa: E402
from gufo_masquerade.config import Config  # noqa: E402

from unit_support import MODEL, base_env, build_app, shim_http  # noqa: E402

SHIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(SHIM_DIR, "gufo_masquerade_shim.py")


def wait_http(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return True
        except Exception:  # noqa: BLE001 - readiness poll
            time.sleep(0.15)
    return False


class LauncherSubprocessTests(unittest.TestCase):
    def test_launcher_boots_shim_from_foreign_cwd(self) -> None:
        """Exactly how systemd starts it: python3 <abs path>, cwd='/'.

        A regression in the launcher's sys.path bootstrap breaks the live
        deployment while every in-repo test still passes, so this subprocess
        check is the one place that is allowed to be slow.
        """
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {
            **os.environ,
            "SHIM_HOST": "127.0.0.1",
            "SHIM_PORT": str(port),
            "GUFO_BASE_URL": "http://127.0.0.1:1",  # dead: fine, /shim/* is local
            "SHIM_STATE_PATH": "/tmp/launcher-test/state.json",
            "SHIM_LOG_LEVEL": "warning",
        }
        proc = subprocess.Popen(
            [sys.executable, LAUNCHER],
            cwd="/",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            self.assertTrue(wait_http(f"{base}/shim/config"), "launcher did not serve")
            cfg = httpx.get(f"{base}/shim/config", timeout=5.0).json()
            self.assertEqual(cfg["gufo_base_url"], "http://127.0.0.1:1")
            # Honesty rule still applies through the launcher path.
            self.assertNotEqual(httpx.get(f"{base}/props", timeout=5.0).status_code, 200)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


class MainFunctionTests(unittest.TestCase):
    """``main()`` with uvicorn replaced — asserts the server contract."""

    def test_main_configures_uvicorn_correctly(self) -> None:
        captured: dict = {}

        def fake_run(app, **kwargs) -> None:
            captured["app"] = app
            captured.update(kwargs)

        fake_module = types.ModuleType("uvicorn")
        fake_module.run = fake_run  # type: ignore[attr-defined]
        saved = sys.modules.get("uvicorn")
        sys.modules["uvicorn"] = fake_module
        saved_env = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(base_env(SHIM_PORT="18099", SHIM_ACCESS_LOG="1",
                                       SHIM_LOG_LEVEL="warning"))
            rc = shim_main.main()
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            if saved is not None:
                sys.modules["uvicorn"] = saved
            else:
                del sys.modules["uvicorn"]

        self.assertEqual(rc, 0)
        self.assertEqual(captured["host"], "127.0.0.1")
        self.assertEqual(captured["port"], 18099)
        self.assertEqual(captured["log_level"], "warning")
        self.assertTrue(captured["access_log"])
        self.assertEqual(captured["timeout_graceful_shutdown"], 10,
                         "PAIR's stop spec grants 10s; the drain window must match")
        self.assertFalse(captured["server_header"])
        self.assertTrue(hasattr(captured["app"], "state"))


class OwnedClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_created_in_lifespan_and_closed_after(self) -> None:
        # No injected client: the app builds its own httpx client inside the
        # lifespan and must close it on shutdown. The fake base URL does not
        # resolve, so /health fails honestly (503) — that is the point.
        app = create_app(Config(environ=base_env()), load_state=False)
        self.assertFalse(app.state.owns_client)
        async with shim_http(app) as c:
            r = await c.get("/health")
            self.assertEqual(r.status_code, 503)
            self.assertTrue(app.state.owns_client)
            client = await app.state.get_client()
            self.assertFalse(client.is_closed)
        self.assertTrue(client.is_closed, "owned client must be closed on shutdown")

    async def test_injected_client_not_closed_by_app(self) -> None:
        app, fake = build_app()
        client = await app.state.get_client()
        async with shim_http(app):
            pass
        self.assertFalse(app.state.owns_client)
        self.assertFalse(client.is_closed, "caller owns injected clients")
        await client.aclose()


class ModuleSurfaceTests(unittest.TestCase):
    def test_public_api(self) -> None:
        import gufo_masquerade

        for name in gufo_masquerade.__all__:
            self.assertTrue(hasattr(gufo_masquerade, name))

    def test_create_app_defaults(self) -> None:
        saved = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(base_env())
            app = create_app()  # default Config() reads ambient env
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(app.state.config.gufo_base_url, "http://fake-gufo:9")
        self.assertIsNotNone(app.state.inventory)
        self.assertIsNotNone(app.state.hub)


if __name__ == "__main__":
    unittest.main()
