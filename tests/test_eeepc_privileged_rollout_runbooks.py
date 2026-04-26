from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_deploy_runbook_preserves_non_sudo_readiness_boundary():
    text = (REPO_ROOT / "docs" / "EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md").read_text(encoding="utf-8")

    assert "Non-sudo readiness boundary" in text
    assert "do not claim host-emitter parity" in text
    assert "sudo -n true" in text
    assert "a password is required" in text
    assert "newest readable `reports/evolution-*.json`" in text
    assert "activation and authoritative parity verification remain privileged steps" in text


def test_apply_gate_runbook_fails_closed_without_readable_current_gate():
    text = (REPO_ROOT / "docs" / "EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md").read_text(encoding="utf-8")

    assert "Do not auto-renew `apply.ok`" in text
    assert "Missing, unreadable, malformed, or expired approval must fail closed" in text
    assert "must not claim the gate is valid unless the current `apply.ok` payload was read" in text
    assert "`expires_at_epoch` is in the future" in text


def test_live_repair_plan_requires_privileged_preflight_before_parity_claim():
    text = (REPO_ROOT / "docs" / "plans" / "2026-04-16-eeepc-live-repair-approval-subagents.md").read_text(encoding="utf-8")

    assert "Slice 0 — privileged readiness preflight" in text
    assert "Ready for privileged rollout requires all of these" in text
    assert "the opencode Nanobot venv can be executed" in text
    assert "outbox/report.index.json`, `goals/registry.json`, and the newest report can be read" in text
    assert "not claim HADI/follow-through host-emitter parity from readable reports alone" in text
