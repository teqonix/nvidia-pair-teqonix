#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Contract checks: the shim vs PAIR's real client-side decoders.

Runner-agnostic by necessity. This node's seccomp filter kills pytest outright
(`SIGSYS`, on both pytest 8.3 and 9.2), so these checks are plain functions
returning None on pass and raising AssertionError on failure. Drive them with
``run_contract_checks.py`` anywhere; ``test_pair_contract.py`` re-exports them
for environments where pytest works.

Each check asserts against a port of the Go code in nvpair-engine-manager that
consumes the shim, so PASS means "PAIR's own code path accepts this response",
not "the status code was 200".
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from pair_semantics import (
    API_KEY,
    MODEL_ID,
    PAIR_PROBE_HEADER,
    PAIR_READY,
    fresh_shim,
    gufo_get,
    pair_inventory_loaded,
    pair_models_event,
    pair_probe_matches,
    pair_pull_verdict,
    pair_start_ack_accepted,
    resolve_object_path,
)


# ---------------------------------------------------------------------------
# G1  /props role injection  [BLOCKING]
# ---------------------------------------------------------------------------
def g1_props_injects_router_role(shim: httpx.Client) -> None:
    """PAIR's probe must match role==router. This single check decides whether
    the engine is adopted at all."""
    r = shim.get("/props")
    assert r.status_code == PAIR_READY["status"], f"expected 200, got {r.status_code}"
    body = r.json()
    assert pair_probe_matches(body), (
        f"PAIR's probe would REJECT the shim; keys={sorted(body)}"
    )


def g1_props_preserves_gufo_fields(shim: httpx.Client) -> None:
    """The injection must be additive. PAIR shows model/build_info to the
    operator, and a replacement body would blank those out."""
    body = shim.get("/props").json()
    for field in ("model", "model_alias", "model_path", "build_info", "total_slots"):
        assert field in body, f"GUFO field {field!r} was dropped"
    assert body["model_alias"] == MODEL_ID
    assert body["build_info"]["version"] == "0.20.0"


def g1_mock_gufo_genuinely_lacks_role() -> None:
    """Meta-guard: if the mock grows a role, G1 stops being exercised and the
    suite would pass vacuously."""
    body = gufo_get("/props").json()
    assert "role" not in body, "mock GUFO now HAS role -- G1 is not being tested"


# ---------------------------------------------------------------------------
# G2  POST /models/load  [BLOCKING]
# ---------------------------------------------------------------------------
def g2_load_resident_succeeds(shim: httpx.Client) -> None:
    r = shim.post("/models/load", json={"model": MODEL_ID})
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
    assert r.json()["success"] is True


def g2_load_unknown_is_rejected(shim: httpx.Client) -> None:
    """A blind ack would hide a typo until inference failed far away from here."""
    r = shim.post("/models/load", json={"model": "bogus/model-xyz"})
    assert r.status_code in (404, 501), f"got {r.status_code}"
    assert r.json()["success"] is False


def g2_load_missing_field_is_400(shim: httpx.Client) -> None:
    assert shim.post("/models/load", json={}).status_code == 400


# ---------------------------------------------------------------------------
# G3  POST /models/unload  [BLOCKING]
# ---------------------------------------------------------------------------
def g3_cancel_inflight_succeeds(shim: httpx.Client) -> None:
    """pull.go stopLlamaCPPDownload REQUIRES {"success": true} on the cancel
    path, or PAIR cannot abort a download and the row sticks in the UI."""
    r = shim.post("/models/unload", json={"model": "some/model-being-pulled"})
    assert r.status_code == 200, f"cancel path must succeed, got {r.status_code}"
    assert pair_start_ack_accepted(r.json()), "cancel ack must carry success:true"


def g3_unload_live_model_is_honest_409() -> None:
    """Default policy: refuse rather than let PAIR believe a serving model was
    evicted. A lie here surfaces later as inference from a 'gone' model."""
    with fresh_shim() as shim:
        r = shim.post("/models/unload", json={"model": MODEL_ID})
        assert r.status_code == 409, f"expected 409 for live model, got {r.status_code}"
        assert r.json()["success"] is False


def g3_unload_can_be_forced_to_ack() -> None:
    """SHIM_REFUSE_UNLOAD_SERVING=0 makes unload a documented no-op ack."""
    with fresh_shim(SHIM_REFUSE_UNLOAD_SERVING="0") as shim:
        r = shim.post("/models/unload", json={"model": MODEL_ID})
        assert r.status_code == 200, f"got {r.status_code}"
        assert r.json()["success"] is True


# ---------------------------------------------------------------------------
# G4  DELETE /models  [HIGH]
# ---------------------------------------------------------------------------
def g4_delete_removes_from_inventory() -> None:
    with fresh_shim(SHIM_REFUSE_UNLOAD_SERVING="0") as shim:
        assert MODEL_ID in {r["id"] for r in shim.get("/models").json()["data"]}
        r = shim.delete("/models", params={"model": MODEL_ID})
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
        assert r.json()["success"] is True
        ids = {row["id"] for row in shim.get("/models").json()["data"]}
        assert MODEL_ID not in ids, "deleted model is still listed"
        assert pair_inventory_loaded(shim.get("/models").json()["data"]) == []


def g4_delete_live_model_refused_by_default() -> None:
    with fresh_shim() as shim:
        r = shim.delete("/models", params={"model": MODEL_ID})
        assert r.status_code == 409, f"expected 409, got {r.status_code}"
        assert r.json()["success"] is False


def g4_delete_unknown_is_404() -> None:
    with fresh_shim(SHIM_REFUSE_UNLOAD_SERVING="0") as shim:
        assert shim.delete("/models", params={"model": "no/such"}).status_code == 404


def g4_delete_requires_model_param() -> None:
    with fresh_shim() as shim:
        assert shim.delete("/models").status_code == 400


# ---------------------------------------------------------------------------
# G5 + G8  SSE pull protocol  [HIGH]
# ---------------------------------------------------------------------------
def g8_pull_resident_completes_handshake(shim: httpx.Client) -> None:
    """present-ok policy against PAIR's real pull state machine.

    Subscribe BEFORE the POST, exactly as pullModelLlamaCPPSSE does, because
    terminal frames are one-shot broadcasts with no replay.
    """
    frames: list[dict[str, Any]] = []
    with shim.stream("GET", "/models/sse", params={"model": MODEL_ID}) as stream:
        time.sleep(0.4)  # let the subscription register
        ack = shim.post("/models", json={"model": MODEL_ID})
        assert ack.status_code in (200, 202), f"start ack status {ack.status_code}"
        payload = ack.json()
        assert pair_start_ack_accepted(payload), (
            f"pull.go would raise unconfirmedStartError: {payload}"
        )
        deadline = time.time() + 10
        for line in stream.iter_lines():
            if line.startswith("data: "):
                ev = pair_models_event(line[len("data: "):])
                if ev:
                    frames.append(ev)
                if ev and ev["event"] in ("download_finished", "download_failed"):
                    break
            if time.time() > deadline:
                break
    assert pair_pull_verdict(frames, MODEL_ID) == "success", f"frames={frames}"


def g5_progress_frames_scale_zero_to_hundred() -> None:
    """PAIR renders percent directly; out-of-range or null would render broken."""
    frames = [
        pair_models_event(
            json.dumps(
                {
                    "model": MODEL_ID,
                    "event": "download_progress",
                    "data": {"progress": {"f.gguf": {"done": d, "total": 1000}}},
                }
            )
        )
        for d in (0, 250, 999, 1000)
    ]
    for f in frames:
        assert f is not None and f["percent"] is not None, f"undecodable frame: {f}"
        assert 0 <= f["percent"] <= 100, f"percent out of range: {f['percent']}"
    assert [f["percent"] for f in frames] == [0, 25, 99, 100]


def g5_progress_aggregates_multi_file() -> None:
    """PAIR sums done/total across ALL entries in the progress map, so a
    multi-file model reports one coherent percentage."""
    ev = pair_models_event(
        json.dumps(
            {
                "model": MODEL_ID,
                "event": "download_progress",
                "data": {
                    "progress": {
                        "a.gguf": {"done": 300, "total": 1000},
                        "b.gguf": {"done": 200, "total": 1000},
                    }
                },
            }
        )
    )
    assert ev is not None and ev["percent"] == 25, f"multi-file aggregation wrong: {ev}"


def g5_foreign_model_frames_are_dropped() -> None:
    """Confirms the matcher we depend on for deterministic cancellation: PAIR
    ignores frames for models it is not watching."""
    foreign = pair_models_event(
        json.dumps({"model": "other/model", "event": "download_finished", "data": {}})
    )
    assert pair_pull_verdict([foreign], MODEL_ID) == "no_terminal_event"


def g5_sse_stream_stays_open_with_keepalive(shim: httpx.Client) -> None:
    """An idle SSE stream must emit keepalives, or an intermediary (or PAIR's
    read timeout) drops the connection mid-download."""
    lines = []
    with shim.stream("GET", "/models/sse", params={"model": "idle/model"}) as stream:
        for line in stream.iter_lines():
            lines.append(line)
            if len(lines) >= 2:
                break
    assert any(l.startswith(":") for l in lines), f"no keepalive comment in {lines}"


def g5_fake_policy_emits_full_progress() -> None:
    """SHIM_PULL_POLICY=fake drives the whole progress UI for demos/testing."""
    with fresh_shim(SHIM_PULL_POLICY="fake", SHIM_FAKE_TICK_MS="20", SHIM_FAKE_TICKS="6") as shim:
        frames: list[dict[str, Any]] = []
        with shim.stream("GET", "/models/sse", params={"model": "demo/model"}) as stream:
            time.sleep(0.3)
            ack = shim.post("/models", json={"model": "demo/model"})
            assert pair_start_ack_accepted(ack.json())
            deadline = time.time() + 15
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    ev = pair_models_event(line[len("data: "):])
                    if ev:
                        frames.append(ev)
                    if ev and ev["event"] in ("download_finished", "download_failed"):
                        break
                if time.time() > deadline:
                    break
        assert pair_pull_verdict(frames, "demo/model") == "success", f"frames={frames}"
        pcts = [f["percent"] for f in frames if f["event"] == "download_progress"]
        assert len(pcts) >= 3, f"too few progress frames: {pcts}"
        assert pcts == sorted(pcts), f"progress went backwards: {pcts}"


def g8_reject_policy_is_explicit() -> None:
    with fresh_shim(SHIM_PULL_POLICY="reject") as shim:
        r = shim.post("/models", json={"model": MODEL_ID})
        assert r.status_code == 501, f"reject policy must 501, got {r.status_code}"
        assert r.json()["success"] is False


# ---------------------------------------------------------------------------
# G6  inventory load-state  [HIGH]
# ---------------------------------------------------------------------------
def g6_pair_filter_sees_resident_model() -> None:
    """Under PAIR's real `status.value in [loaded]` filter, the GUFO model is
    visible. Without enrichment this returns []."""
    with fresh_shim() as shim:
        rows = shim.get("/models").json()["data"]
        assert pair_inventory_loaded(rows) == [MODEL_ID], f"filter result: {rows}"


def g6_raw_gufo_would_be_invisible() -> None:
    """Proves the enrichment is load-bearing rather than decorative."""
    raw = gufo_get("/models").json()["data"]
    assert pair_inventory_loaded(raw) == [], (
        "mock GUFO now emits status.value -- G6 is no longer exercised"
    )


def g6_every_row_has_status_value() -> None:
    with fresh_shim() as shim:
        for row in shim.get("/models").json()["data"]:
            value, ok = resolve_object_path(row, "status.value")
            assert ok, f"row {row.get('id')} has no status.value -- PAIR drops it"
            assert value in ("loaded", "downloading"), f"unexpected status {value}"


# ---------------------------------------------------------------------------
# G7  fail-fast on unsupported tasks
# ---------------------------------------------------------------------------
def g7_unsupported_tasks_fail_fast(shim: httpx.Client) -> None:
    """A sub-second 501 beats a connection that hangs until the router times
    out, which is the failure mode that looks like a dead node."""
    for path in ("/v1/embeddings", "/v1/rerank", "/rerank", "/infill"):
        t0 = time.time()
        r = shim.post(path, json={"input": "x"})
        elapsed = time.time() - t0
        assert r.status_code == 501, f"{path} -> {r.status_code}"
        assert elapsed < 2.0, f"{path} took {elapsed:.1f}s, not fail-fast"
        assert r.json()["success"] is False


# ---------------------------------------------------------------------------
# Passthrough: inference, streaming, auth bridging
# ---------------------------------------------------------------------------
def pass_chat_sync(shim: httpx.Client) -> None:
    r = shim.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "mock answer"


def pass_chat_stream_is_incremental(shim: httpx.Client) -> None:
    """Must arrive as produced, not be buffered to the end -- a buffered SSE
    stream looks exactly like a hung assistant."""
    chunks: list[str] = []
    first_at: float | None = None
    t0 = time.time()
    with shim.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x"}], "stream": True},
    ) as stream:
        assert stream.headers.get("content-type", "").startswith("text/event-stream"), (
            f"content-type lost: {stream.headers.get('content-type')}"
        )
        for line in stream.iter_lines():
            if line.startswith("data: "):
                if first_at is None:
                    first_at = time.time() - t0
                chunks.append(line)
    total = time.time() - t0
    assert any("chat.completion.chunk" in c for c in chunks), f"no chunks: {chunks}"
    assert chunks[-1] == "data: [DONE]", f"no terminal [DONE]: {chunks[-1]}"
    assert first_at is not None and first_at < total * 0.75, (
        f"stream looks buffered: first={first_at:.3f}s total={total:.3f}s"
    )


def pass_legacy_completion(shim: httpx.Client) -> None:
    r = shim.post("/completion", json={"prompt": "hello"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["text"] == "mock completion"


def pass_slots(shim: httpx.Client) -> None:
    r = shim.get("/slots")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def pass_shim_holds_key_so_pair_sends_none(shim: httpx.Client) -> None:
    """PAIR sends no credential to the llamacpp engine. The shim must be open
    to PAIR while still authenticating upstream to GUFO."""
    assert httpx.get(f"http://127.0.0.1:18901/props", timeout=10.0).status_code == 401, (
        "mock GUFO must reject unauthenticated direct access"
    )
    assert shim.get("/props").status_code == 200, "shim must be open to PAIR"


def pass_probe_header_is_stripped(shim: httpx.Client) -> None:
    """PAIR stamps probes with the identity header; GUFO must not see it.
    Verified by the stamped request still succeeding through the shim."""
    r = shim.get("/props", headers={PAIR_PROBE_HEADER: "1"})
    assert r.status_code == 200
    assert pair_probe_matches(r.json())


def pass_unlisted_path_is_not_invented(shim: httpx.Client) -> None:
    """Unlisted paths forward verbatim; the shim must never fabricate a 200."""
    r = shim.get("/definitely-not-a-real-endpoint")
    assert r.status_code != 200, "shim invented a 200 for an unknown path"
    assert r.status_code in (404, 502), f"unexpected {r.status_code}"


# ---------------------------------------------------------------------------
# Honesty when GUFO is down
# ---------------------------------------------------------------------------
def dead_upstream_never_reports_ready() -> None:
    """A shim answering 200 on /props with a dead GUFO gets adopted as a
    healthy node, then fails every request silently. Non-200 is mandatory."""
    with fresh_shim(GUFO_BASE_URL="http://127.0.0.1:1", GUFO_API_KEY="x") as shim:
        assert shim.get("/props").status_code != 200, "shim lied about a dead GUFO on /props"
        assert shim.get("/health").status_code in (502, 503), "shim lied on /health"
        cfg = shim.get("/shim/config").json()
        assert cfg["gufo_base_url"] == "http://127.0.0.1:1", (
            "diagnostic surface must stay available so the operator can see why"
        )


def dead_upstream_does_not_break_control_surface() -> None:
    with fresh_shim(GUFO_BASE_URL="http://127.0.0.1:1") as shim:
        for path in ("/shim/config", "/shim/state", "/shim/sse_stats"):
            r = shim.get(path)
            assert r.status_code == 200, f"{path} should work with upstream down, got {r.status_code}"


#: (name, fn, needs_shared_shim). Checks with needs_shared_shim=False build
#: their own isolated shim, because they mutate persisted state or config.
CHECKS: list[tuple[str, Callable[..., None], bool]] = [
    ("G1 /props injects role=router and PAIR probe matches", g1_props_injects_router_role, True),
    ("G1 /props preserves GUFO fields", g1_props_preserves_gufo_fields, True),
    ("G1 mock GUFO genuinely lacks role", g1_mock_gufo_genuinely_lacks_role, False),
    ("G2 load resident model succeeds", g2_load_resident_succeeds, True),
    ("G2 load unknown model is rejected", g2_load_unknown_is_rejected, True),
    ("G2 load missing field is 400", g2_load_missing_field_is_400, True),
    ("G3 cancel in-flight download succeeds", g3_cancel_inflight_succeeds, True),
    ("G3 unload live model is honest 409", g3_unload_live_model_is_honest_409, False),
    ("G3 unload can be forced to ack", g3_unload_can_be_forced_to_ack, False),
    ("G4 delete removes from inventory", g4_delete_removes_from_inventory, False),
    ("G4 delete live model refused by default", g4_delete_live_model_refused_by_default, False),
    ("G4 delete unknown model is 404", g4_delete_unknown_is_404, False),
    ("G4 delete requires model param", g4_delete_requires_model_param, False),
    ("G8 pull resident model completes SSE handshake", g8_pull_resident_completes_handshake, True),
    ("G5 progress frames scale 0..100", g5_progress_frames_scale_zero_to_hundred, False),
    ("G5 progress aggregates multi-file downloads", g5_progress_aggregates_multi_file, False),
    ("G5 foreign-model frames dropped by PAIR matcher", g5_foreign_model_frames_are_dropped, False),
    ("G5 SSE stream emits keepalives when idle", g5_sse_stream_stays_open_with_keepalive, True),
    ("G5 fake policy emits full progress", g5_fake_policy_emits_full_progress, False),
    ("G8 reject policy is explicit 501", g8_reject_policy_is_explicit, False),
    ("G6 PAIR filter sees resident model", g6_pair_filter_sees_resident_model, False),
    ("G6 raw GUFO would be invisible to PAIR filter", g6_raw_gufo_would_be_invisible, False),
    ("G6 every row carries status.value", g6_every_row_has_status_value, False),
    ("G7 unsupported tasks fail fast", g7_unsupported_tasks_fail_fast, True),
    ("PASS chat sync", pass_chat_sync, True),
    ("PASS chat stream is incremental", pass_chat_stream_is_incremental, True),
    ("PASS legacy /completion", pass_legacy_completion, True),
    ("PASS /slots", pass_slots, True),
    ("PASS shim holds GUFO key so PAIR sends none", pass_shim_holds_key_so_pair_sends_none, True),
    ("PASS identity probe header stripped", pass_probe_header_is_stripped, True),
    ("PASS unlisted path not invented as 200", pass_unlisted_path_is_not_invented, True),
    ("DEAD upstream never reports ready", dead_upstream_never_reports_ready, False),
    ("DEAD upstream keeps control surface alive", dead_upstream_does_not_break_control_surface, False),
]
