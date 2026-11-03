# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Production entrypoint: ``python -m gufo_masquerade``.

Configures logging, builds the app from the process environment, and serves
it with uvicorn. Kept separate from :mod:`gufo_masquerade.app` so importing
the app (tests, tooling) never configures logging or binds ports.
"""

from __future__ import annotations

import logging
import sys

from .app import create_app
from .config import Config


def main(argv: list[str] | None = None) -> int:
    """Start the shim and block until shutdown.

    The process exits 0 on clean SIGTERM/SIGINT. uvicorn's graceful shutdown
    window is set to 10s to match PAIR's stop spec: a SIGTERM must drain
    in-flight generations, not amputate them (GUFO finishes the current turn
    on its own signal handler).

    Args:
        argv: Unused placeholder kept for call-site symmetry; configuration
            is environment-only by design.

    Returns:
        Process exit code (always 0 when uvicorn returns normally).
    """
    config = Config()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    import uvicorn  # imported here so app imports stay side-effect free

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.listen_host,
        port=config.listen_port,
        log_level=config.log_level,
        access_log=config.access_log,
        timeout_graceful_shutdown=10,
        server_header=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the launcher
    raise SystemExit(main())
