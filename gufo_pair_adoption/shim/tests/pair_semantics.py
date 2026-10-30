# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""PAIR's client-side semantics, re-implemented from nvpair-engine-manager.

These are faithful ports of the Go matchers that will actually consume the
shim. Testing against them (rather than against hand-written expectations)
means a green check means PAIR's own code path accepts the shim's response.

Sources:
  lifecycle.go  probe(), resolveObjectPath(), engineIdentityProbeHeader
  pull.go       llamaCPPModelsEvent, pullModelLlamaCPPSSE, stopLlamaCPPDownload
  registry.go   models_dir match spec

Importable by pytest and by the standalone runner (shim/tests/run_contract_checks.py),
because this node's seccomp filter kills pytest outright on any real run.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(os.path.dirname(HERE), "gufo_masquerade_shim.py")
MOCK = os.path.join(HERE, "mock_gufo.py")

MOCK_PORT = int(os.environ.get("CONTRACT_MOCK_PORT", "18901"))
GUFO_URL = f"http://127.0.0.1:{MOCK_PORT}"
API_KEY = "test-key-123"
MODEL_ID = "gufo/nemotron-3.5-lightning-optiq"

#: PAIR's bundled llamacpp.json readiness probe, verbatim.
PAIR_READY = {
    "http_path": "/props",
    "status": 200,
    "json_match": {"field": "role", "value": "router"},
}

#: PAIR's bundled llamacpp.json models_dir load-state matcher.
PAIR_LOADED_MATCH = {"field": "status.value", "in": ["loaded"]}

#: The header PAIR stamps on identity probes (lifecycle.go:25).
PAIR_PROBE_HEADER = "X-NVPAIR-Engine-Identity-Probe"


# ---------------------------------------------------------------------------
# Go matcher ports
# ---------------------------------------------------------------------------
def resolve_object_path(obj: Any, path: str) -> tuple[Any, bool]:
    """lifecycle.go resolveObjectPath: dotted lookup, (None, False) on miss."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


def pair_probe_matches(body: dict[str, Any], match: dict[str, Any] | None = None) -> bool:
    """lifecycle.go probe() json_match gate.

    The subtlety that makes G1 blocking: a MISSING field is a hard ``False``.
    There is no "unset means don't care". GUFO reports no ``role``, so without
    the shim's injection the engine never reaches ready and is never adopted.
    """
    match = match or PAIR_READY["json_match"]
    value, ok = resolve_object_path(body, match["field"])
    if not ok:
        return False
    return value == match["value"]


def pair_models_event(raw: str) -> dict[str, Any] | None:
    """pull.go llamaCPPModelsEvent decoder over one ``data:`` payload.

    PAIR computes percent as sum(done)/sum(total) across the whole progress
    map, so multi-file downloads aggregate; and it drops frames whose
    ``model`` is not the watched id.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {"model": obj.get("model", ""), "event": obj.get("event", ""), "percent": None}
    data = obj.get("data")
    if isinstance(data, dict):
        progress = data.get("progress")
        if isinstance(progress, dict):
            done = total = 0
            for entry in progress.values():
                if isinstance(entry, dict):
                    done += int(entry.get("done", 0) or 0)
                    total += int(entry.get("total", 0) or 0)
            if total > 0:
                out["percent"] = int(100 * done / total)
    return out


def pair_pull_verdict(events: list[dict[str, Any]], expected_model: str) -> str:
    """pull.go pullModelLlamaCPPSSE loop, reduced to its terminal verdict."""
    for event in events:
        if event.get("model") != expected_model:
            continue  # PAIR: "if event.Model != model { continue }"
        name = event.get("event")
        if name == "download_finished":
            return "success"
        if name == "download_failed":
            return "failed"
        if name == "download_progress":
            continue
    return "no_terminal_event"


def pair_start_ack_accepted(payload: Any) -> bool:
    """pull.go: the start-ack gate.

        if started.Success == nil { return unconfirmedStartError }
        if !*started.Success     { return "engine did not accept" }

    A missing ``success`` key is a FAILURE, not a pass. JSON null is also a
    failure, hence the explicit None check rather than truthiness.
    """
    if not isinstance(payload, dict):
        return False
    success = payload.get("success")
    return success is True


def pair_inventory_loaded(rows: list[dict[str, Any]], match: dict[str, Any] | None = None) -> list[str]:
    """models_dir ``match`` filter: rows whose status.value is in the loaded set."""
    match = match or PAIR_LOADED_MATCH
    out = []
    for row in rows:
        value, ok = resolve_object_path(row, match["field"])
        if ok and value in match["in"]:
            out.append(row.get("id", "?"))
    return out


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return True
        except Exception:  # noqa: BLE001 - readiness poll
            time.sleep(0.2)
    return False


@contextlib.contextmanager
def mock_gufo(port: int = MOCK_PORT) -> Iterator[str]:
    proc = subprocess.Popen(
        [sys.executable, MOCK],
        env={**os.environ, "MOCK_GUFO_PORT": str(port), "MOCK_GUFO_API_KEY": API_KEY},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        if not wait_http(f"{url}/health", 25.0):
            raise RuntimeError(f"mock GUFO did not start on {url}")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextlib.contextmanager
def fresh_shim(**overrides) -> Iterator[httpx.Client]:
    """An isolated shim with a pristine state file.

    Needed for destructive checks: the shim persists its deleted-model overlay
    deliberately, so a delete against a shared instance would leak into every
    later assertion.
    """
    port = free_port()
    state = os.path.join(tempfile.mkdtemp(prefix="shim-state-"), "state.json")
    env = {
        **os.environ,
        "SHIM_HOST": "127.0.0.1",
        "SHIM_PORT": str(port),
        "GUFO_BASE_URL": GUFO_URL,
        "GUFO_API_KEY": API_KEY,
        "SHIM_STATE_PATH": state,
        "SHIM_LOG_LEVEL": "warning",
        "SHIM_INVENTORY_TTL_S": "0",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    proc = subprocess.Popen([sys.executable, SHIM], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_http(f"{base}/shim/config", 25.0):
            err = proc.stderr.read().decode(errors="replace")[-2000:] if proc.poll() is not None else ""
            raise RuntimeError(f"isolated shim did not start: {err}")
        with httpx.Client(base_url=base, timeout=15.0) as client:
            yield client
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextlib.contextmanager
def shared_shim() -> Iterator[httpx.Client]:
    """One shim reused by the non-destructive checks, for speed."""
    with fresh_shim() as client:
        yield client


def gufo_get(path: str, **kw) -> httpx.Response:
    return httpx.get(f"{GUFO_URL}{path}", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=10.0, **kw)
