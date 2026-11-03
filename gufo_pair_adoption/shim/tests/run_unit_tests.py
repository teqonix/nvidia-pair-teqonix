#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Standalone unit-test runner (and coverage driver).

Why not just pytest: this node's seccomp filter kills pytest with SIGSYS on
any real run (collect-only included). The unit tests are stdlib
``unittest``/``IsolatedAsyncioTestCase``, so they run here with zero pytest
dependency:

    .venv/bin/python shim/tests/run_unit_tests.py            # plain
    .venv/bin/coverage run shim/tests/run_unit_tests.py      # instrumented
    .venv/bin/coverage report -m --include='*/gufo_masquerade/*'

Exit 0 = all pass. Where pytest works, ``test_unit_pytest.py`` runs the same
cases.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UNIT_MODULES = (
    "test_config",
    "test_inventory",
    "test_sse_hub",
    "test_app",
    "test_entrypoints",
)


def build_suite() -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in UNIT_MODULES:
        suite.addTests(loader.loadTestsFromName(module))
    return suite


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(build_suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
