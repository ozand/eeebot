#!/usr/bin/env python3
"""Collect a redacted, read-only Telegram live-proof summary.

The collector never talks to Telegram, never reads secrets, and never mutates
state. It takes a filled proof markdown file, validates it with the conservative
validator, and emits a compact redacted JSON summary suitable for review.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.validate_telegram_live_proof import (
    STEP_HEADINGS,
    _field_value,
    _section_after_heading,
    evaluate_telegram_live_proof_markdown,
)

SCHEMA_VERSION = "telegram-live-proof-collector-v1"

REDACTED_VALUE = "[redacted]"
TOKEN_RE = re.compile(r"(?i)(bot)?token\s*[=:]\s*[^\s;,]+")
CHAT_ID_RE = re.compile(r"(?i)chat[_ -]?id\s*[=:]\s*[-]?\d+")
LONG_NUMBER_RE = re.compile(r"\b\d{8,}\b")


def _redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = TOKEN_RE.sub("token=[redacted-token]", value)
    redacted = CHAT_ID_RE.sub("chat_id=[redacted]", redacted)
    redacted = LONG_NUMBER_RE.sub("[redacted-number]", redacted)
    return redacted


def _step_summary(text: str, step_id: str, heading: str) -> dict[str, Any]:
    section = _section_after_heading(text, heading)
    return {
        "step_id": step_id,
        "command": _redact_text(_field_value(section, "Outbound command text")),
        "reply_excerpt": _redact_text(_field_value(section, "Reply text")),
        "classification": _field_value(section, "Classification"),
    }


def _redacted_metadata(text: str) -> dict[str, Any]:
    return {
        "probe_date_utc": _field_value(text, "Probe date (UTC)"),
        "operator_account": REDACTED_VALUE if _field_value(text, "Operator/account used") else None,
        "chat_thread_identifier": REDACTED_VALUE if _field_value(text, "Chat/thread identifier") else None,
        "telegram_bot_identity": _field_value(text, "Telegram bot identity observed"),
        "host_runtime_source": _field_value(text, "Host/runtime source being validated"),
    }


def collect_telegram_live_proof_markdown(text: str, *, collected_at_utc: str | None = None) -> dict[str, Any]:
    """Collect a redacted proof summary from filled evidence markdown."""
    validation = evaluate_telegram_live_proof_markdown(text)
    valid = validation["state"] == "valid"
    reasons = set(validation.get("reasons", []))
    simulated_or_local = "simulated_or_local_evidence" in reasons
    pending_real_transcript = (not valid) and (not simulated_or_local) and "missing_real_allowlisted_transcript_source" in reasons
    steps = []
    if valid:
        steps = [_step_summary(text, step_id, heading) for step_id, heading in STEP_HEADINGS.items()]
    transcript_status = "complete" if valid else ("pending_real_transcript" if pending_real_transcript else "invalid")
    state = "collected" if valid else transcript_status
    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "transcript_status": transcript_status,
        "collected_at_utc": collected_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "validation": validation,
        "command_checklist": validation.get("required_commands_present", {}),
        "redacted_metadata": _redacted_metadata(text),
        "steps": steps,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in {"-h", "--help"}:
        print(f"Usage: {Path(sys.argv[0]).name} <filled-telegram-live-proof.md>", file=sys.stderr)
        return 64
    text = Path(args[0]).read_text(encoding="utf-8")
    result = collect_telegram_live_proof_markdown(text)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["state"] == "collected" else 2


if __name__ == "__main__":
    raise SystemExit(main())
