"""Test deploy gate without dashboard listener (ADR-036 D3 Part B).

Verifies:
1. The deploy gate passes without any dashboard listener on :8080 and without model calls.
2. The exact command line from deploy_release.sh executes cleanly (class: edit trailing space lost).
3. Every former route reader of :8080 from #1969 census receives a replacement or an explicit unavailable.

Cites: ADR-036 Rule 6.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "host" / "eeepc" / "scripts" / "deploy_release.sh"

def test_gate_is_model_free_and_dashboard_free(monkeypatch, tmp_path: Path) -> None:
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

    # Extract and execute the exact verifier invocation, not just its target.
    # A fake sudo consumes the identity flags and execs the remaining argv,
    # preserving shell quoting/spacing while making this host-independent.
    invocation = re.search(
        r"(?m)^if ! (sudo -u eeepc-agent env .*?\"\$RELEASE_DIR/scripts/verify_release_health\.py\"); then$",
        script_text,
    )
    assert invocation is not None, "expected the exact runtime-identity gate command in deploy_release.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        "#!/bin/sh\n[ \"$1\" = \"-u\" ] && shift 2\nexec \"$@\"\n",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)
    release = str(REPO_ROOT)
    env = dict(os.environ)
    env.update({
        "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
        "HEALTH_GATE_PYTHON": sys.executable,
        "RELEASE_DIR": release,
    })
    shell_command = invocation.group(1)
    proc = subprocess.run(
        ["bash", "-c", shell_command],
        cwd=str(REPO_ROOT),
        env=env,
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
    # Full former interface inventory from the handler and D0 census (#1969):
    # GET /, /api/metrics, /api/health, /api/health-oneliner, /api/reward-csv,
    # /api/top-cycles, /api/cleanup, /api/refresh-host-caps; POST /api/cleanup,
    # /api/refresh-host-caps. Unmatched GET/POST paths returned 404.
    import json

    from scripts.eeebot_dashboard import (
        DashboardHTTPRequestHandler,
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

    # Validate full former route inventory from the HTTP handler; each route
    # must have a named local replacement or explicit unavailable treatment.
    handler_source = __import__("inspect").getsource(DashboardHTTPRequestHandler)
    former_routes = {
        "GET /": "render_html",
        "GET /api/metrics": "render_json",
        "GET /api/health": "render_health_json",
        "GET /api/health-oneliner": "render_health_oneliner",
        "GET /api/reward-csv": "reward_export_unavailable",
        "GET /api/top-cycles": "reward_export_unavailable",
        "GET /api/cleanup": "unavailable",
        "GET /api/refresh-host-caps": "probe_host_capabilities.refresh_host_capabilities",
        "POST /api/cleanup": "unavailable",
        "POST /api/refresh-host-caps": "probe_host_capabilities.refresh_host_capabilities",
        "GET unmatched route": "unavailable",
        "POST unmatched route": "unavailable",
    }
    for route, replacement in former_routes.items():
        if route == "GET /":
            assert "render_html(metrics)" in handler_source
            assert len(render_html(metrics).encode("utf-8")) >= 1024
        elif route == "GET /api/metrics":
            assert "render_json(metrics)" in handler_source
            assert isinstance(json.loads(render_json(metrics)), dict)
        elif route == "GET /api/health":
            assert "render_health_json(metrics)" in handler_source
            assert isinstance(json.loads(render_health_json(metrics)), dict)
        elif route == "GET /api/health-oneliner":
            assert "render_health_oneliner(metrics)" in handler_source
            assert render_health_oneliner(metrics)
        elif route in {"GET /api/reward-csv", "GET /api/top-cycles"}:
            assert "reward_export_unavailable(source)" in handler_source
            assert "unavailable" in reward_export_unavailable(metrics.get("reward_source", {})).lower()
        elif route == "GET /api/cleanup":
            assert '"/api/cleanup"' in handler_source and "405" in handler_source
        elif route == "GET /api/refresh-host-caps":
            assert '"/api/refresh-host-caps"' in handler_source and "405" in handler_source
        elif route == "POST /api/cleanup":
            assert 'url.path == "/api/cleanup"' in handler_source
        elif route == "POST /api/refresh-host-caps":
            assert 'url.path == "/api/refresh-host-caps"' in handler_source
        else:
            assert "send_error(404" in handler_source or "Not Found" in handler_source

    # The deploy replacement validates health/metrics and renders all output locally.
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
    """State-directory changes must not leak through dashboard collector caches (Codex P2)."""
    import json

    from scripts.verify_release_health import verify_release_health

    state_a = tmp_path / "state_a"
    state_b = tmp_path / "state_b"
    state_a.mkdir()
    state_b.mkdir()
    caps = {"camera": {"state": "present", "available": True, "details": "camera-A"}}
    (state_a / "host_capabilities.json").write_text(json.dumps(caps), encoding="utf-8")
    caps["camera"] = {"state": "absent", "available": False, "details": "camera-B"}
    (state_b / "host_capabilities.json").write_text(json.dumps(caps), encoding="utf-8")

    first = verify_release_health(state_dir=state_a)
    second = verify_release_health(state_dir=state_b)
    first_coverage = first["metrics"]["host_capability_coverage"]
    second_coverage = second["metrics"]["host_capability_coverage"]
    assert first_coverage != second_coverage, (
        "sequential health checks for different state dirs reused state-dependent cached data"
    )
