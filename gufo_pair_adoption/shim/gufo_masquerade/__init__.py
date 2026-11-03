# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""gufo-masquerade: present GUFO as a llama.cpp *router* to nVidia PAIR.

GUFO v0.20.0 already mimics llama-server's inference surface deliberately
(``/props``, ``/slots``, ``/completion``, ``llamacpp:``-prefixed Prometheus
metrics). What it does NOT implement is the llama.cpp *router* control plane
that PAIR's ``nvpair-engine-manager`` drives for the ``llamacpp`` engine
profile.

This package is a single-process reverse proxy that closes exactly those
gaps and passes everything else through byte-transparently, including SSE:

======  ==========================  =========================================
Gap     Endpoint                    Shim behaviour
======  ==========================  =========================================
G1      GET  /props                 inject ``{"role": "router"}`` [BLOCKING]
G2      POST /models/load           idempotent, validated ``{"success": true}``
G3      POST /models/unload         cancel ack / honest 409 on live model
G4      DELETE /models              drop from virtual inventory (persisted)
G5      GET  /models/sse            in-process pub/sub progress hub
G6      GET  /models                enrich rows ``status.value=loaded``
G7      POST /v1/embeddings         fast 501, never a hung connection
G8      POST /models                present-ok / fake / reject policy
--      inference                   raw streaming passthrough, O(1) memory
======  ==========================  =========================================

Module map:

*   :mod:`gufo_masquerade.config` — environment parsing (:class:`Config`).
*   :mod:`gufo_masquerade.errors` — :class:`UpstreamUnavailable`, the PAIR
    error envelope.
*   :mod:`gufo_masquerade.inventory` — virtual model inventory (G4/G6).
*   :mod:`gufo_masquerade.sse` — progress hub (G5).
*   :mod:`gufo_masquerade.app` — the :func:`create_app` factory wiring it
    all together.
*   :mod:`gufo_masquerade.__main__` — uvicorn entrypoint.

Configuration is environment-only; see ``SHIM_DOCS.md`` beside the package.
"""

from .app import create_app
from .config import Config
from .errors import UpstreamUnavailable
from .inventory import Inventory
from .sse import ProgressHub

__all__ = ["Config", "create_app", "Inventory", "ProgressHub", "UpstreamUnavailable"]
