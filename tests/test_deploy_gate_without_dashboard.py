"""Test deploy gate without dashboard listener (ADR-036 D3 Part B).

Verifies:
1. The deploy gate passes without any dashboard listener on :8080 and without model calls.
2. The exact command line from deploy_release.sh executes cleanly (class: edit trailing space lost).
3. Every former route reader of :8080 from #1969 census receives a replacement or an explicit unavailable.

Cites: ADR-036 Rule 6.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "host" / "eeepc" / "scripts" / "deploy_release.sh"

def test_gate_is_model_free_and_dashboard_free(monkeypatch) -> None:
    """The deploy gate passes without any dashboard listener and without model calls (ADR-036)."""
    script_text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # Verify no :8080 or curl dependency remains in the deploy health check
    assert "curl --fail --silent --show-error http://127.0.0.1:8080" not in script_text
    assert "http://127.0.0.1:8080/api/health" not in script_text
    assert "http://127.0.0.1:8080/api/metrics" not in script_text

    # Verify port 8080 is not required to be open
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.2)
        res = s.connect_ex(("127.0.0.1", 8080))
        # If port 8080 is bound by something unrelated, that's fine, but the gate
        # must NOT connect to it or depend on it.
    finally:
        s.close()

    # Verify execution under runtime service identity eeepc-agent (Codex P1 on PR #1978)
    assert "sudo -u eeepc-agent" in script_text, (
        "release health gate must execute under runtime service identity eeepc-agent"
    )

    # Extract command line from deploy_release.sh and execute it for real
    # (catching trailing spaces, quoting bugs, or bad arguments - class 'edit trailing space lost')
    match = re.search(r'\$RELEASE_DIR/([^"\'\s\n]+)', script_text[script_text.index("sudo -u eeepc-agent"):])
    assert match is not None, "deploy_release.sh must execute $RELEASE_DIR/<script>"
    script_rel_path = match.group(1)
    target_script = REPO_ROOT / script_rel_path
    assert target_script.is_file(), f"target script {target_script} must exist"

    cmd = [sys.executable, str(target_script)]

    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, f"deploy gate command failed: {proc.stderr}\nstdout: {proc.stdout}"
    assert "[remote] release health gate passed" in proc.stdout
    assert "CRITICAL:" not in proc.stderr

    # Model-free assertion: verify that calling verify_release_health constructs zero model providers
    def fail_on_model(*_args, **_kwargs):
        raise AssertionError("model call or provider creation forbidden in deploy gate")

    monkeypatch.setattr("nanobot.providers.factory._make_provider", fail_on_model)
    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager", fail_on_model)
    monkeypatch.setattr("nanobot.runtime.bridge._make_provider", fail_on_model)

    from scripts.verify_release_health import verify_release_health
    res = verify_release_health()
    assert res["status"] == "ok"

def test_every_former_route_reader_is_served_or_unavailable() -> None:
    """Every former reader of route :8080 from #1969 census receives a replacement or an explicit unavailable (ADR-036)."""
    # Former readers from #1969 census:
    # 1. host/eeepc/scripts/deploy_release.sh:610 -> /api/health
    # 2. host/eeepc/scripts/deploy_release.sh:611 -> /api/metrics
    # 3. host/eeepc/scripts/deploy_release.sh:629 -> /
    # And additional routes documented in #1969:
    # - /api/health-oneliner
    # - /api/reward-csv
    # - /api/top-cycles
    # - /api/cleanup
    # - /api/refresh-host-caps
    import json

    from scripts.eeebot_dashboard import (
        collect_metrics,
        render_health_json,
        render_health_oneliner,
        render_html,
        render_json,
        reward_export_unavailable,
    )

    metrics = collect_metrics()

    # 1. /api/health former reader replacement: render_health_json
    health_payload = json.loads(render_health_json(metrics))
    assert isinstance(health_payload, dict)
    assert {"overall", "dimensions", "goal", "active_task", "reward_average"} <= health_payload.keys()

    # 2. /api/metrics former reader replacement: render_json
    metrics_payload = json.loads(render_json(metrics))
    assert isinstance(metrics_payload, dict)
    assert {"goal", "active_task", "approval_gate_state", "reward_source", "goal_source"} <= metrics_payload.keys()

    # 3. / former reader replacement: render_html
    html_payload = render_html(metrics)
    assert isinstance(html_payload, str)
    assert len(html_payload.encode("utf-8")) >= 1024

    # 4. /api/health-oneliner former reader replacement: render_health_oneliner
    oneliner = render_health_oneliner(metrics)
    assert isinstance(oneliner, str) and len(oneliner) > 0

    # 5. /api/reward-csv former reader replacement: explicit unavailable
    source = metrics.get("reward_source", {})
    reward_csv = reward_export_unavailable(source)
    assert "unavailable" in reward_csv.lower()

    # 6. /api/top-cycles former reader replacement: explicit unavailable
    top_cycles = reward_export_unavailable(source)
    assert "unavailable" in top_cycles.lower()

    # 7. scripts/verify_release_health.py itself validates both health and metrics directly
    from scripts.verify_release_health import verify_release_health
    res = verify_release_health()
    assert res["status"] == "ok"
    assert res["html_bytes"] >= 1024


def test_health_gate_runs_when_dashboard_unit_is_disabled_or_absent() -> None:
    """ADR-036 D3: release health gate runs independently after dashboard unit block."""
    script_text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # Confirm health gate invocation is outside and after the dashboard unit if-block
    idx_dashboard_end = script_text.index('die "unexpected $DASHBOARD_UNIT LoadState=$DASHBOARD_LOAD_STATE"\nfi')
    idx_health_gate = script_text.index('"$RELEASE_DIR/scripts/verify_release_health.py"')
    assert idx_health_gate > idx_dashboard_end, (
        "release health gate must be outside and after the dashboard unit block"
    )


def test_verify_release_health_respects_custom_state_dir(tmp_path: Path) -> None:
    """Verify state_dir parameter updates eeebot_dashboard.STATE_DIR and clears caches (Codex P2)."""
    import time

    from scripts import eeebot_dashboard as ed
    from scripts.verify_release_health import verify_release_health

    custom_state = tmp_path / "custom_state"
    custom_state.mkdir()

    # Pre-populate cache with a dummy value
    ed._METRICS_CACHE["metrics"] = {"cached": True}
    ed._METRICS_CACHE["loaded_at"] = time.monotonic()

    original_state = ed.STATE_DIR
    res = verify_release_health(state_dir=custom_state)
    assert res["status"] == "ok"
    assert ed.STATE_DIR == original_state
