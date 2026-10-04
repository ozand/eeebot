from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nanobot.runtime.context_metadata import build_context_metadata, load_context_metadata

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "host/eeepc/scripts/deploy_release.sh"
EXPORTER = REPO / "scripts/export_context_metadata.py"
SHA = "c7e180d7122605082afc561225051e9a2c26f7f2"


def test_exporter_uses_explicit_full_sha_and_emits_bounded_metadata(tmp_path: Path) -> None:
    out = tmp_path / "metadata.json"
    subprocess.run([
        sys.executable, str(EXPORTER), "--source-commit", SHA, "--output", str(out),
    ], cwd=REPO, check=True)
    loaded = load_context_metadata(out, SHA)
    assert loaded["source_commit"] == SHA
    assert out.stat().st_size < 8192


def test_exporter_rejects_short_sha(tmp_path: Path) -> None:
    result = subprocess.run([
        sys.executable, str(EXPORTER), "--source-commit", "HEAD", "--output", str(tmp_path / "x"),
    ], cwd=REPO, capture_output=True, text=True)
    assert result.returncode != 0
    assert "full lowercase" in result.stderr


def test_release_archive_is_generated_from_selected_commit_not_checkout_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "marker.txt").write_text("selected", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "marker.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "selected"], check=True, capture_output=True)
    selected = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "marker.txt").write_text("ambient head changed", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "ambient"], check=True, capture_output=True)
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", selected], check=True, capture_output=True)
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        assert tar.extractfile("marker.txt").read() == b"selected"


def test_deploy_source_contains_preflip_metadata_validation_in_both_paths() -> None:
    script = DEPLOY.read_text(encoding="utf-8")
    assert "context-metadata.json" in script
    assert script.index("context-metadata.json") < script.index("updating current symlink")
    assert "VERIFY_ONLY" in script and "context metadata" in script.lower()
    assert 'git -C "$REPO_ROOT" archive --format=tar "$COMMIT"' in script
    assert '--validate --source-commit "$FULL_COMMIT" --output "$GATE_TMP/context-metadata.json"' in script
    assert '--validate --source-commit "$FULL_COMMIT" --output "$RELEASE_DIR/context-metadata.json"' in script


def test_invalid_artifact_is_rejected_before_current_switch(tmp_path: Path) -> None:
    artifact = tmp_path / "metadata.json"
    artifact.write_text(json.dumps(build_context_metadata(SHA)), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        load_context_metadata(artifact, "0" * 40)
    assert not (tmp_path / "current").exists()
