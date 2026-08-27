"""Pytest configuration and test-suite initialization.

Rationale:
When running pytest or python against this repository in environments where another
`tests` package might be present in site-packages or sys.path, we must ensure that
the local workspace `tests/` directory is resolved and importable as `tests.*`.
This allows tests that import helper utilities or fixtures across test modules
(e.g., `from tests.test_cycle_ledger import ...` or `from tests.test_llm_proposer import ...`)
to work reliably during test discovery and execution without requiring ad-hoc sys.path mutations
in every individual test file.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repository root and local tests directory are at the top of sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# If 'tests' was already imported (e.g. from site-packages), extend its __path__
# to include our local tests directory, or re-point it so tests.<module> imports work.
if "tests" in sys.modules:
    _tests_mod = sys.modules["tests"]
    if hasattr(_tests_mod, "__path__"):
        if str(_TESTS_DIR) not in _tests_mod.__path__:
            _tests_mod.__path__.insert(0, str(_TESTS_DIR))
else:
    # If not yet imported, importing it will use sys.path which starts with _REPO_ROOT.
    # We can also explicitly import and ensure __path__ includes _TESTS_DIR.
    import tests  # noqa: F401

    if hasattr(tests, "__path__") and str(_TESTS_DIR) not in tests.__path__:
        tests.__path__.insert(0, str(_TESTS_DIR))
