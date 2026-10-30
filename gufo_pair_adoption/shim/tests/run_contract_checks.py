#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Standalone contract-check runner.

Exists because this node's seccomp filter kills pytest with SIGSYS on any real
run (verified on pytest 8.3.5 and 9.2.0; `--version` works, collecting a test
file dies). The checks themselves are plain functions, so they run here with no
pytest dependency at all:

    .venv/bin/python shim/tests/run_contract_checks.py

Exit 0 = all pass, 1 = any failure. Use this on the deployment node; use
pytest where it works.
"""

from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contract_checks import CHECKS  # noqa: E402
from pair_semantics import mock_gufo  # noqa: E402


def main() -> int:
    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    with mock_gufo():
        from pair_semantics import shared_shim

        with shared_shim() as shim:
            for name, fn, needs_shim in CHECKS:
                try:
                    if needs_shim:
                        fn(shim)
                    else:
                        fn()
                except AssertionError as exc:
                    failed.append((name, str(exc).strip() or "assertion failed"))
                    print(f"FAIL  {name}")
                    if str(exc).strip():
                        print(f"      {str(exc).strip()[:400]}")
                except Exception:  # noqa: BLE001 - report, keep going
                    failed.append((name, traceback.format_exc(limit=4).strip()))
                    print(f"ERROR {name}")
                    print("      " + traceback.format_exc(limit=2).strip().replace("\n", "\n      ")[:600])
                else:
                    passed.append(name)
                    print(f"ok    {name}")

    total = len(CHECKS)
    print()
    print("=" * 72)
    print(f"contract checks: {len(passed)}/{total} passed, {len(failed)} failed")
    if failed:
        print()
        print("FAILURES")
        for name, detail in failed:
            print(f"  - {name}")
            for line in detail.splitlines():
                print(f"      {line}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
