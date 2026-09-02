"""
nanobot - A lightweight AI agent framework
"""

__version__ = "0.1.4.post5"
__logo__ = "🐈"

# #1197: arm the bridge exit recorder before ``nanobot.runtime.bridge`` is
# imported — an import-time crash there (the 2026-09-01 NameError, #1000/#1142)
# never reaches bridge.py's own code, so the hook must live upstream of it.
# Inert unless this process is ``python -m nanobot.runtime.bridge`` (read from
# ``sys.orig_argv``) or NANOBOT_BRIDGE_EXIT_RECORD=1; ``crash_record`` is
# stdlib-only so arming cannot fail on package code. A failure to arm is
# printed, never swallowed.
try:
    from nanobot import crash_record as _crash_record

    _crash_record.arm()
except Exception as _arm_exc:  # pragma: no cover - visible, not fatal
    import sys as _sys

    print(f"nanobot: bridge exit recorder not armed: {_arm_exc!r}", file=_sys.stderr)
