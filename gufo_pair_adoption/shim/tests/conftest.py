# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""pytest configuration for the shim test suite.

The unit tests are stdlib unittest classes so they can run on nodes where
pytest is killed by the sandbox seccomp filter (see ``run_unit_tests.py``);
pytest collects those classes natively. This conftest only fixes import
paths so both entrypoints see ``gufo_masquerade`` (shim/) and
``unit_support`` / ``pair_semantics`` (tests/).
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for path in (_HERE, os.path.dirname(_HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)
