#!/usr/bin/env python3
"""Validate filled evidence for the real Telegram live-proof closure.

The validator is intentionally conservative and text-only: it performs no
network calls, reads no secrets, and rejects simulated/local evidence even when
it contains success-looking replies. It is meant to be run before closing the
remaining real Telegram proof issues.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "telegram-live-proof-validation-v1"

REQUIRED_SECTIONS = {
    "probe_metadata": "## Probe metadata",
    "required_command_sequence": "## Required command sequence",
    "evidence_capture_checklist": "## Evidence capture checklist",
    "step_1_ping": "### Step 1: PING",
    "step_2_cap_status": "### Step 2: /cap_status",
    "step_3_workspace": "### Step 3: /workspace experiment tiny-runtime-check",
    "step_4_sub_run": "### Step 4: /sub_run --profile research_only --budget micro",
    "final_outcome_summary": "## Final outcome summary",
    "closure_rule": "## Closure rule",
}

REQUIRED_COMMAND_PATTERNS = {
    "ping": re.compile(r"`PING\s+[^`]+`|Outbound command text:\s*PING\s+\S+", re.IGNORECASE),
    "cap_status": re.compile(r"`/cap_status`|Outbound command text:\s*/cap_status\b", re.IGNORECASE),
    "workspace_tiny_runtime_check": re.compile(
        r"`/workspace\s+experiment\s+tiny-runtime-check`|Outbound command text:\s*/workspace\s+experiment\s+tiny-runtime-check\b",
        re.IGNORECASE,
    ),
    "sub_run_micro": re.compile(
        r"`/sub_run\s+--profile\s+research_only\s+--budget\s+micro\s+[^`]+`|Outbound command text:\s*/sub_run\s+--profile\s+research_only\s+--budget\s+micro\b",
        re.IGNORECASE,
    ),
}

STEP_HEADINGS = {
    "step_1_ping": REQUIRED_SECTIONS["step_1_ping"],
    "step_2_cap_status": REQUIRED_SECTIONS["step_2_cap_status"],
    "step_3_workspace": REQUIRED_SECTIONS["step_3_workspace"],
    "step_4_sub_run": REQUIRED_SECTIONS["step_4_sub_run"],
}

STEP_COMMAND_PATTERNS = {
    "step_1_ping": re.compile(r"^PING\s+\S+", re.IGNORECASE),
    "step_2_cap_status": re.compile(r"^/cap_status$", re.IGNORECASE),
    "step_3_workspace": re.compile(r"^/workspace\s+experiment\s+tiny-runtime-check$", re.IGNORECASE),
    "step_4_sub_run": re.compile(r"^/sub_run\s+--profile\s+research_only\s+--budget\s+micro\s+\S+", re.IGNORECASE),
}

STEP_REPLY_MARKERS = {
    "step_1_ping": ("PONG",),
    "step_2_cap_status": ("autonomy:", "model:", "workspace"),
    "step_3_workspace": (
        "action_id: workspace.experiment.tiny_runtime_check",
        "written: True",
        "executed: True",
        "verified: True",
    ),
    "step_4_sub_run": ("bounded",),
}

DISQUALIFYING_MARKERS = (
    "simulated",
    "simulation",
    "local cli",
    "local-only",
    "not telegram",
    "not real telegram",
    "synthetic transcript",
    "mock transcript",
    "fixture only",
)


def _has_nonempty_field(text: str, field_name: str) -> bool:
    value = _field_value(text, field_name)
    return bool(value and not value.startswith('-'))


def _field_value(text: str, field_name: str) -> str | None:
    pattern = re.compile(rf"^-\s*{re.escape(field_name)}[ \t]*:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _section_after_heading(text: str, heading: str) -> str:
    start = text.lower().find(heading.lower())
    if start < 0:
        return ""
    remainder = text[start + len(heading):]
    next_heading = re.search(r"^#{2,3}\s+", remainder, re.MULTILINE)
    if next_heading:
        return remainder[: next_heading.start()]
    return remainder


def _classification(text: str) -> str | None:
    value = _field_value(text, "Final classification")
    return value.lower() if value else None


def evaluate_telegram_live_proof_markdown(text: str) -> dict[str, Any]:
    """Return structured validation for a filled Telegram live-proof markdown file."""
    reasons: list[str] = []
    lower_text = text.lower()

    for key, heading in REQUIRED_SECTIONS.items():
        if heading.lower() not in lower_text:
            reasons.append(f"missing_section:{key}")

    transcript_source = _field_value(text, "Transcript source")
    if not transcript_source or "real" not in transcript_source.lower() or "allowlisted" not in transcript_source.lower() or "telegram" not in transcript_source.lower():
        reasons.append("missing_real_allowlisted_transcript_source")

    if any(marker in lower_text for marker in DISQUALIFYING_MARKERS):
        reasons.append("simulated_or_local_evidence")

    command_sequence = _section_after_heading(text, REQUIRED_SECTIONS["required_command_sequence"])
    required_commands_present: dict[str, bool] = {}
    for command_id, pattern in REQUIRED_COMMAND_PATTERNS.items():
        present = bool(pattern.search(command_sequence))
        required_commands_present[command_id] = present
        if not present:
            reasons.append(f"missing_required_command:{command_id}")

    for step_id, heading in STEP_HEADINGS.items():
        section = _section_after_heading(text, heading)
        if not _has_nonempty_field(section, "Sent at"):
            reasons.append(f"missing_step_sent_at:{step_id}")
        outbound_command = _field_value(section, "Outbound command text")
        if not outbound_command:
            reasons.append(f"missing_step_outbound_command:{step_id}")
        elif not STEP_COMMAND_PATTERNS[step_id].search(outbound_command):
            reasons.append(f"step_command_mismatch:{step_id}")
        if not _has_nonempty_field(section, "Reply received at"):
            reasons.append(f"missing_step_reply_received_at:{step_id}")
        reply_text = _field_value(section, "Reply text")
        if not reply_text:
            reasons.append(f"missing_step_reply_text:{step_id}")
        else:
            for marker in STEP_REPLY_MARKERS[step_id]:
                if marker.lower() not in reply_text.lower():
                    reasons.append(f"step_reply_missing_expected_marker:{step_id}:{marker}")
        class_value = _field_value(section, "Classification")
        if not class_value:
            reasons.append(f"missing_step_classification:{step_id}")
        elif class_value.strip().lower() != "success":
            reasons.append(f"step_not_success:{step_id}")

    for summary_name in ("PING status", "/cap_status status", "/workspace status", "/sub_run status"):
        value = _field_value(text, summary_name)
        if not value:
            reasons.append(f"missing_final_status:{summary_name}")
        elif value.lower() != "success":
            reasons.append(f"final_status_not_success:{summary_name}")

    final_classification = _classification(text)
    if final_classification != "live proof complete":
        reasons.append("final_classification_not_complete")

    if "real transcript from an allowlisted account" not in lower_text:
        reasons.append("missing_closure_rule_real_allowlisted_account")

    ordered_reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "valid" if not ordered_reasons else "invalid",
        "reasons": ordered_reasons,
        "classification": final_classification,
        "required_commands_present": required_commands_present,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in {"-h", "--help"}:
        print(f"Usage: {Path(sys.argv[0]).name} <filled-telegram-live-proof.md>", file=sys.stderr)
        return 64
    text = Path(args[0]).read_text(encoding="utf-8")
    result = evaluate_telegram_live_proof_markdown(text)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["state"] == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())
