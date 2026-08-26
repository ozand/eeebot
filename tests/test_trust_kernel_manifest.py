"""Keep the documented runtime deny-set manifest synchronized with code."""
from __future__ import annotations

import re
from pathlib import Path

from nanobot.runtime.runtime_deny import _RUNTIME_DENY_ALWAYS_FILES


def test_trust_kernel_manifest_matches_runtime_deny_set() -> None:
    doc = Path(__file__).parents[1] / "docs" / "TRUST_KERNEL.md"
    text = doc.read_text(encoding="utf-8")
    match = re.search(r"## Deny-set manifest.*?```text\n(.*?)\n```", text, re.DOTALL)
    assert match, "TRUST_KERNEL.md must contain the fenced deny-set manifest"
    documented = {line.strip() for line in match.group(1).splitlines() if line.strip()}
    assert documented == set(_RUNTIME_DENY_ALWAYS_FILES)
