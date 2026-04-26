"""Regression tests for lightweight nanobot package imports."""

from __future__ import annotations

import sys


def test_nanobot_utils_package_does_not_import_eeebot_compatibility_package():
    for module_name in list(sys.modules):
        if module_name == "eeebot" or module_name.startswith("eeebot.") or module_name == "nanobot.utils":
            sys.modules.pop(module_name, None)

    import nanobot.utils  # noqa: F401

    assert "eeebot" not in sys.modules
