from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.collect_telegram_live_proof import collect_telegram_live_proof_markdown


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


def test_collector_redacts_sensitive_metadata_and_keeps_step_summary() -> None:
    result = collect_telegram_live_proof_markdown(VALID_EVIDENCE)

    assert result["schema_version"] == "telegram-live-proof-collector-v1"
    assert result["state"] == "collected"
    assert result["validation"]["state"] == "valid"
    assert result["redacted_metadata"] == {
        "probe_date_utc": "2026-04-29T18:40:00Z",
        "operator_account": "[redacted]",
        "chat_thread_identifier": "[redacted]",
        "telegram_bot_identity": "@nanobot_live_bot",
        "host_runtime_source": "eeepc live gateway",
    }
    assert result["steps"][0] == {
        "step_id": "step_1_ping",
        "command": "PING 2026-04-29T18:40:00Z",
        "reply_excerpt": "PONG 2026-04-29T18:40:00Z from live Telegram path",
        "classification": "success",
    }
    assert result["steps"][2]["reply_excerpt"].endswith("verified: True")


def test_collector_keeps_validation_failures_and_avoids_fake_collection() -> None:
    evidence = VALID_EVIDENCE.replace("- Transcript source: real allowlisted Telegram chat transcript", "- Transcript source: simulated local CLI artifact, not Telegram")

    result = collect_telegram_live_proof_markdown(evidence, collected_at_utc="2026-04-29T19:00:00Z")

    assert result["state"] == "invalid"
    assert result["transcript_status"] == "invalid"
    assert result["validation"]["state"] == "invalid"
    assert "simulated_or_local_evidence" in result["validation"]["reasons"]
    assert result["steps"] == []


def test_collector_classifies_blank_template_as_pending_real_transcript() -> None:
    template = Path("docs/userstory/TELEGRAM_LIVE_PROBE_EVIDENCE_TEMPLATE.md").read_text(encoding="utf-8")

    result = collect_telegram_live_proof_markdown(template, collected_at_utc="2026-04-29T19:00:00Z")

    assert result["state"] == "pending_real_transcript"
    assert result["transcript_status"] == "pending_real_transcript"
    assert result["collected_at_utc"] == "2026-04-29T19:00:00Z"
    assert result["command_checklist"] == {
        "ping": True,
        "cap_status": True,
        "workspace_tiny_runtime_check": True,
        "sub_run_micro": True,
    }
    assert result["steps"] == []


def test_collector_redacts_secret_like_reply_and_metadata() -> None:
    evidence = VALID_EVIDENCE.replace(
        "PONG 2026-04-29T18:40:00Z from live Telegram path",
        "PONG token=123456:ABCDEF chat_id=987654321 from live Telegram path",
    )

    result = collect_telegram_live_proof_markdown(evidence, collected_at_utc="2026-04-29T19:00:00Z")

    dumped = json.dumps(result)
    assert "123456:ABCDEF" not in dumped
    assert "987654321" not in dumped
    assert "[redacted-token]" in dumped
    assert "chat_id=[redacted]" in dumped


def test_cli_writes_collected_json_for_valid_evidence(tmp_path: Path) -> None:
    evidence_path = tmp_path / "proof.md"
    evidence_path.write_text(VALID_EVIDENCE, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "scripts/collect_telegram_live_proof.py", str(evidence_path)],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["state"] == "collected"
    assert result["validation"]["state"] == "valid"
