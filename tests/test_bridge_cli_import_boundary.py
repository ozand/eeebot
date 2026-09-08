"""Keep the daemon bridge independent from the interactive CLI stack."""

from __future__ import annotations

import subprocess
import sys


def test_bridge_import_does_not_load_cli_commands() -> None:
    """Importing the daemon module must not import ``nanobot.cli``."""
    code = (
        "import sys\n"
        "import nanobot.runtime.bridge\n"
        "assert not any(name == 'nanobot.cli' or name.startswith('nanobot.cli.') for name in sys.modules), "
        "'bridge import must not load the interactive CLI package'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
