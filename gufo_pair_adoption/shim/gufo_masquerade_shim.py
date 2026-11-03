#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Compatibility launcher for the gufo-masquerade shim.

The implementation lives in the ``gufo_masquerade`` package beside this
file. This shim-of-a-shim exists so every existing launch path keeps
working unchanged:

*   systemd ``ExecStart=/usr/bin/python3 /usr/local/lib/gufo-masquerade/gufo_masquerade_shim.py``
*   ``gufo-pairctl`` ``SHIM_SCRIPT=`` default
*   ``Containerfile.shim`` copy step
*   ``shim/tests/pair_semantics.py`` subprocess spawning

Run ``python -m gufo_masquerade`` directly when you control the working
directory / ``PYTHONPATH``; use this launcher when you don't.

Architecture and PAIR-semantics documentation: ``SHIM_DOCS.md`` at the
repository's ``gufo_pair_adoption/`` root.
"""

from __future__ import annotations

import os
import sys

# Make the sibling `gufo_masquerade` package importable regardless of cwd —
# this file may be executed by absolute path from systemd with `/` as cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from gufo_masquerade.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
