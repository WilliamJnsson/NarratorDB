#!/usr/bin/env python3
"""Run the portable product and protocol tests across Python versions.

The paid-admission modules verify a frozen campaign execution environment,
including byte-level inventory of the Python 3.12 standard library. They are
not product compatibility tests and cannot be rebound to a generic hosted
Python 3.12 runner. Run those modules directly only inside their sealed vendor
environment. Every other unit and protocol test runs on the full CI matrix.
"""

from __future__ import annotations

import unittest


_SEALED_MODULE_PREFIX = "tests.test_v13_paid_admission"


def _cases(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


def build_suite() -> unittest.TestSuite:
    discovered = unittest.defaultTestLoader.discover(".")
    return unittest.TestSuite(
        case
        for case in _cases(discovered)
        if not case.id().startswith(_SEALED_MODULE_PREFIX)
    )


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(build_suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
