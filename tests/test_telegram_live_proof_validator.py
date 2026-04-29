from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.validate_telegram_live_proof import evaluate_telegram_live_proof_markdown


VALID_EVIDENCE = """# Telegram Live Probe Evidence

## Probe metadata

- Probe date (UTC): 2026-04-29T18:40:00Z
- Operator/account used: allowlisted real Telegram account
- Chat/thread identifier: redacted-real-chat
- Telegram bot identity observed: @nanobot_live_bot
- Host/runtime source being validated: eeepc live gateway
- Transcript source: real allowlisted Telegram chat transcript

## Required command sequence

1. `PING 2026-04-29T18:40:00Z`
2. `/cap_status`
3. `/workspace experiment tiny-runtime-check`
4. `/sub_run --profile research_only --budget micro ping-telegram-live-2026-04-29T18:40:00Z`

## Evidence capture checklist

For each step, record sent timestamp, exact outbound command text, exact inbound reply text, same chat/thread status, and expected runtime truth match.

### Step 1: PING
- Sent at: 2026-04-29T18:40:00Z
- Outbound command text: PING 2026-04-29T18:40:00Z
- Reply received at: 2026-04-29T18:40:01Z
- Reply text: PONG 2026-04-29T18:40:00Z from live Telegram path
- Same Telegram chat/thread? yes
- Matches expected runtime truth? yes
- Classification: success

### Step 2: /cap_status
- Sent at: 2026-04-29T18:40:05Z
- Outbound command text: /cap_status
- Reply received at: 2026-04-29T18:40:06Z
- Reply text: autonomy: enabled=True; dry_run_default=False; model: gpt-5.3-codex; workspace actions enabled
- Contains runtime/model truth? yes
- Matches direct host truth? yes
- Same Telegram chat/thread? yes
- Classification: success

### Step 3: /workspace experiment tiny-runtime-check
- Sent at: 2026-04-29T18:40:10Z
- Outbound command text: /workspace experiment tiny-runtime-check
- Reply received at: 2026-04-29T18:40:12Z
- Reply text: action_id: workspace.experiment.tiny_runtime_check; written: True; executed: True; verified: True
- Same Telegram chat/thread? yes
- Matches expected runtime truth? yes
- Classification: success

### Step 4: /sub_run --profile research_only --budget micro ...
- Sent at: 2026-04-29T18:40:15Z
- Outbound command text: /sub_run --profile research_only --budget micro ping-telegram-live-2026-04-29T18:40:00Z
- Reply received at: 2026-04-29T18:40:17Z
- Reply text: bounded subagent accepted; task_id=sub-redacted; policy gate ok
- Same Telegram chat/thread? yes
- Matches expected runtime truth? yes
- Classification: success

## Final outcome summary

- PING status: success
- /cap_status status: success
- /workspace status: success
- /sub_run status: success
- Final classification: live proof complete

## Closure rule

The remaining Telegram issues can be closed only when this template is filled with a real transcript from an allowlisted account and the captured replies match the expected runtime truth.
"""


def test_valid_real_telegram_evidence_passes() -> None:
    result = evaluate_telegram_live_proof_markdown(VALID_EVIDENCE)

    assert result["schema_version"] == "telegram-live-proof-validation-v1"
    assert result["state"] == "valid"
    assert result["reasons"] == []
    assert result["classification"] == "live proof complete"
    assert result["required_commands_present"] == {
        "ping": True,
        "cap_status": True,
        "workspace_tiny_runtime_check": True,
        "sub_run_micro": True,
    }


def test_missing_real_transcript_is_invalid() -> None:
    evidence = VALID_EVIDENCE.replace(
        "- Transcript source: real allowlisted Telegram chat transcript",
        "- Transcript source:",
    )

    result = evaluate_telegram_live_proof_markdown(evidence)

    assert result["state"] == "invalid"
    assert "missing_real_allowlisted_transcript_source" in result["reasons"]


def test_simulated_or_local_artifact_is_invalid_even_with_success_words() -> None:
    evidence = VALID_EVIDENCE.replace(
        "- Transcript source: real allowlisted Telegram chat transcript",
        "- Transcript source: simulated local CLI artifact, not Telegram",
    )

    result = evaluate_telegram_live_proof_markdown(evidence)

    assert result["state"] == "invalid"
    assert "simulated_or_local_evidence" in result["reasons"]


def test_missing_required_probe_command_is_invalid() -> None:
    evidence = VALID_EVIDENCE.replace("2. `/cap_status`", "2. `/help`")

    result = evaluate_telegram_live_proof_markdown(evidence)

    assert result["state"] == "invalid"
    assert "missing_required_command:cap_status" in result["reasons"]


def test_missing_step_reply_text_is_invalid() -> None:
    evidence = VALID_EVIDENCE.replace("- Reply text: bounded subagent accepted; task_id=sub-redacted; policy gate ok", "- Reply text:")

    result = evaluate_telegram_live_proof_markdown(evidence)

    assert result["state"] == "invalid"
    assert "missing_step_reply_text:step_4_sub_run" in result["reasons"]


def test_wrong_step_outbound_command_is_invalid_even_if_sequence_list_is_correct() -> None:
    evidence = VALID_EVIDENCE.replace(
        "- Outbound command text: /cap_status",
        "- Outbound command text: /help",
    )

    result = evaluate_telegram_live_proof_markdown(evidence)

    assert result["state"] == "invalid"
    assert "step_command_mismatch:step_2_cap_status" in result["reasons"]


def test_successful_workspace_reply_must_contain_runtime_truth_markers() -> None:
    evidence = VALID_EVIDENCE.replace(
        "- Reply text: action_id: workspace.experiment.tiny_runtime_check; written: True; executed: True; verified: True",
        "- Reply text: OK",
    )

    result = evaluate_telegram_live_proof_markdown(evidence)

    assert result["state"] == "invalid"
    assert "step_reply_missing_expected_marker:step_3_workspace:action_id: workspace.experiment.tiny_runtime_check" in result["reasons"]
    assert "step_reply_missing_expected_marker:step_3_workspace:written: True" in result["reasons"]


def test_cli_exits_nonzero_for_invalid_evidence(tmp_path: Path) -> None:
    evidence_path = tmp_path / "invalid.md"
    evidence_path.write_text(VALID_EVIDENCE.replace("Final classification: live proof complete", "Final classification: route parity gap"), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "scripts/validate_telegram_live_proof.py", str(evidence_path)],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["state"] == "invalid"
    assert "final_classification_not_complete" in result["reasons"]
