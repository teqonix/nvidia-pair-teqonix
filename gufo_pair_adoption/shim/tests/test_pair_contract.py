# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""pytest entrypoint for the contract checks.

The real assertions live in ``contract_checks.py`` as plain functions, because
pytest is unusable on the current node (seccomp SIGSYS). Where pytest works,
this module runs the same checks; anywhere else use
``run_contract_checks.py``.

    pytest shim/tests/test_pair_contract.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contract_checks import CHECKS  # noqa: E402
from pair_semantics import mock_gufo  # noqa: E402


@pytest.fixture(scope="session")
def gufo_up():
    with mock_gufo():
        yield


@pytest.fixture(scope="session")
def shim(gufo_up):
    from pair_semantics import shared_shim

    with shared_shim() as client:
        yield client


def _make(fn, needs_shim):
    if needs_shim:
        def test(shim):
            fn(shim)
    else:
        def test(gufo_up):
            fn()
    return test


# Generate one pytest test per contract check so failures name the gap.
for _name, _fn, _needs in CHECKS:
    _key = "".join(c if c.isalnum() else "_" for c in _name).strip("_").lower()
    globals()[f"test_{_key}"] = _make(_fn, _needs)
