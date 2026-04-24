from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot_ops_dashboard.config import DEFAULT_PROJECT_ROOT, load_config


CANONICAL_REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_ROOT = CANONICAL_REPO_ROOT / "ops" / "dashboard"


def test_default_project_root_is_imported_canonical_dashboard_path(monkeypatch):
    for key in ("NANOBOT_DASHBOARD_ROOT", "NANOBOT_DASHBOARD_DB"):
        monkeypatch.delenv(key, raising=False)

    assert DEFAULT_PROJECT_ROOT == DASHBOARD_ROOT
    assert load_config().project_root == DASHBOARD_ROOT
    assert load_config().db_path == DASHBOARD_ROOT / "data" / "dashboard.sqlite3"


def test_runtime_scripts_resolve_root_from_their_own_location():
    for relative in ("scripts/run_web.sh", "scripts/run_collector.sh", "scripts/install_user_units.sh"):
        text = (DASHBOARD_ROOT / relative).read_text()
        assert "/home/ozand/herkoot/Projects/nanobot-ops-dashboard" not in text
        assert "BASH_SOURCE[0]" in text


def test_systemd_units_point_at_canonical_import_path():
    expected_root = str(DASHBOARD_ROOT)

    for unit in sorted((DASHBOARD_ROOT / "systemd").glob("*ops-dashboard-*.service")):
        text = unit.read_text()
        assert f"WorkingDirectory={expected_root}" in text
        assert f"ExecStart={expected_root}/scripts/" in text


def test_imported_dashboard_readme_marks_sibling_repo_as_noncanonical():
    readme = (DASHBOARD_ROOT / "README.md").read_text()

    assert "Canonical source" in readme
    assert "ops/dashboard" in readme
    assert "ozand/eeebot" in readme
    assert "not the durable source of truth" in readme


def test_tracked_import_excludes_runtime_and_secret_artifacts():
    result = subprocess.run(
        ["git", "ls-files", "ops/dashboard"],
        cwd=CANONICAL_REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    tracked_paths = [Path(line) for line in result.stdout.splitlines() if line.strip()]
    assert tracked_paths

    forbidden_parts = {
        ".env",
        ".pytest_cache",
        "__pycache__",
    }
    forbidden_suffixes = {
        ".sqlite",
        ".sqlite3",
        ".sqlite3-shm",
        ".sqlite3-wal",
        ".db",
        ".pyc",
    }
    forbidden_name_fragments = {
        "id_ed25519",
        "private_key",
    }

    for rel in tracked_paths:
        parts = set(rel.parts)
        lower = str(rel).lower()
        assert not (parts & forbidden_parts), rel
        assert not any(lower.endswith(suffix) for suffix in forbidden_suffixes), rel
        assert not any(fragment in lower for fragment in forbidden_name_fragments), rel
