"""#1441: loguru formats with ``str.format``, so a ``%s`` log call silently
drops its arguments and emits the format string verbatim.

Nothing raises and the line still appears at the right level -- only the
variable part is missing. The diagnostic and its absence look identical from
the outside.

The rule is per-module, not repository-wide: ``nanobot`` uses BOTH loggers.
Modules on stdlib ``logging`` are correct with ``%s`` and raise
``TypeError: not all arguments converted during string formatting`` if given
``{}``. A check that flagged ``%s`` everywhere would "fix" working code --
matching the pattern is not the same as diagnosing the defect.

Known blind spots of this regex scanner, stated so nobody reads a pass as a
proof (reviewed on #1441):

* ``import loguru`` followed by ``loguru.logger.warning(...)``, or an aliased
  ``from loguru import logger as log`` -- the module is not detected at all.
* a ``logger.warning("... %s")`` written inside a comment or a docstring is
  flagged although nothing is logged. A false positive here is loud, which is
  the safe direction.
* a module that imports loguru and then shadows ``logger`` locally with a
  stdlib logger has its stdlib calls judged by the loguru rule.

None of these forms exist in ``nanobot`` today (verified: 21 loguru modules,
zero offenders). The durable shape for this rule is an AST lint check that
resolves the import form and skips comments and docstrings; this test is a
regression guard for the defect actually found, not a general linter.
"""
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "nanobot"

_LOGURU_IMPORT = re.compile(r"^from loguru import .*\blogger\b", re.MULTILINE)
_PERCENT_STYLE = re.compile(
    r"logger\.(?:trace|debug|info|success|warning|error|critical)\((?:[^()]|\([^()]*\))*%[sdrf]"
)


def test_loguru_drops_percent_style_arguments():
    """Pin the behaviour the rule rests on, so it is not folklore."""
    emitted: list[str] = []
    sink_id = logger.add(emitted.append, format="{message}", level="WARNING")
    try:
        logger.warning("percent: %s", "LOST")
        logger.warning("brace: {}", "KEPT")
    finally:
        logger.remove(sink_id)
    assert "LOST" not in emitted[0] and "%s" in emitted[0]
    assert "KEPT" in emitted[1]


def test_stdlib_logging_is_the_opposite_and_must_not_be_converted():
    """Guards the other direction: stdlib logging needs ``%s`` and keeps it."""
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    log = logging.getLogger("test_1441_stdlib")
    log.propagate = False
    handler = _Capture()
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    try:
        log.warning("percent: %s", "KEPT")
    finally:
        log.removeHandler(handler)
    assert "KEPT" in records[0]


def _loguru_modules() -> list[Path]:
    return [
        path
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if _LOGURU_IMPORT.search(path.read_text(encoding="utf-8", errors="replace"))
    ]


def test_loguru_modules_have_no_percent_style_logging():
    modules = _loguru_modules()
    assert modules, "found no loguru modules -- the scanner is broken, not the code"
    offenders: list[str] = []
    for path in modules:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _PERCENT_STYLE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(PACKAGE_ROOT.parent)}:{line}")
    assert not offenders, (
        "loguru formats with str.format; these calls emit their format string "
        "and drop the value: " + ", ".join(offenders)
    )
