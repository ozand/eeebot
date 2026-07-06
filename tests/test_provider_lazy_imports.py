"""Regression guard: heavy provider deps must not load at module-import time.

`nanobot.providers.litellm_provider` used to `import litellm` (which pulls in
`tiktoken`, requiring a Rust toolchain to build on some hosts, e.g. the i386
eeepc host) at module scope, and `nanobot.providers.openai_codex_provider`
used to `import oauth_cli_kit` at module scope. Both are now deferred to
first use (see #657) so importing the modules — and collecting tests that
reference them — stays cheap and doesn't require those packages to be
installed at all. This test runs the import in a subprocess so it reflects a
clean interpreter state, independent of import order elsewhere in the suite.
"""

from __future__ import annotations

import subprocess
import sys


def test_litellm_provider_import_does_not_load_litellm():
    """Importing the module alone must not pull in litellm/tiktoken."""
    code = (
        "import sys\n"
        "import nanobot.providers.litellm_provider\n"
        "assert 'litellm' not in sys.modules, "
        "'litellm_provider module import must not eagerly import litellm'\n"
        "assert 'tiktoken' not in sys.modules, "
        "'litellm_provider module import must not eagerly import tiktoken'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_openai_codex_provider_import_does_not_load_oauth_cli_kit():
    """Importing the module alone must not pull in oauth_cli_kit."""
    code = (
        "import sys\n"
        "import nanobot.providers.openai_codex_provider\n"
        "assert 'oauth_cli_kit' not in sys.modules, "
        "'openai_codex_provider module import must not eagerly import oauth_cli_kit'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_litellm_provider_acompletion_is_module_level_and_patchable():
    """`acompletion` must remain a module attribute so tests can mock.patch it
    before any real litellm import happens (see tests/test_litellm_kwargs.py)."""
    import nanobot.providers.litellm_provider as mod

    assert hasattr(mod, "acompletion")
