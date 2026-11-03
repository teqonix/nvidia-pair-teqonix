# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Error vocabulary shared by the shim's layers.

Two distinct families live here:

*   :class:`UpstreamUnavailable` — raised by the inventory layer when GUFO
    cannot be consulted. Handlers translate it into honest non-200 responses;
    see the honesty rule in :mod:`gufo_masquerade.app`.

*   :func:`json_error` — builds the error envelope PAIR's Go clients parse.
    The shape matters more than it looks: PAIR's pull path checks
    ``success == true`` with a *pointer* semantic, so every response —
    success or failure — must carry an explicit boolean ``success`` key.
    A missing key reads as ``nil`` in Go and aborts flows that should have
    succeeded, which is why even SSE-adjacent error paths use this envelope.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class UpstreamUnavailable(RuntimeError):
    """GUFO could not be reached, or answered in a way we cannot trust.

    Raised with a message that already names the upstream call (e.g.
    ``"GET /props: ConnectError(...)"``) so handler-side error bodies are
    actionable without extra logging.
    """


def json_error(
    status: int, code: str, message: str, **extra: Any
) -> JSONResponse:
    """Build a PAIR-parseable error response.

    Args:
        status: HTTP status code to emit.
        code: Machine-readable error code; mirrored into both ``type`` and
            ``code`` inside the error object because different PAIR call
            sites read different fields.
        message: Human-readable explanation for operators and UIs.
            It should explain the *why* and the fix, not just restate the
            status code.
        **extra: Additional top-level keys (e.g. ``known=[...]`` listing
            available models), merged beside ``success``/``error``.

    Returns:
        A ``JSONResponse`` with ``{"success": false, "error": {...}}``.
    """
    body: dict[str, Any] = {
        "success": False,
        "error": {"type": code, "code": code, "message": message},
    }
    body.update(extra)
    return JSONResponse(status_code=status, content=body)
