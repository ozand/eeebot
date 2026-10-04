from __future__ import annotations

import json
import os
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
    assert '"$HEALTH_GATE_PYTHON" "$RELEASE_DIR/scripts/export_context_metadata.py"' in script
    assert 'CONTEXT_WORKDIR="$(mktemp -d /tmp/eeebot-context-${COMMIT}.XXXXXX)"' in script
    assert 'CONTEXT_WORKDIR="$(mktemp -d /tmp/eeebot-context-verify.XXXXXX)"' in script
    assert 'trap cleanup_context_workdir EXIT' in script
    exporter = EXPORTER.read_text(encoding="utf-8")
    assert "os.O_EXCL" in exporter and "os.O_NOFOLLOW" in exporter
    assert 'chmod 0644 "$RELEASE_STAGE/context-metadata.json"' in script
    assert 'chmod 0644 "$CONTEXT_METADATA"' in script
    assert 'CONTEXT_SOURCE="$CONTEXT_WORKDIR"' in script
    assert 'CONTEXT_WORKDIR="$(mktemp -d /tmp/eeebot-context-verify.XXXXXX)"' in script
    assert 'if git -C "$REPO_ROOT" cat-file -e "$COMMIT:scripts/export_context_metadata.py"' in script
    assert '"$HEALTH_GATE_PYTHON" "$RELEASE_DIR/scripts/export_context_metadata.py"' in script


def test_metadata_exporter_refuses_symlink_output(tmp_path: Path) -> None:
    destination = tmp_path / "metadata.json"
    protected = tmp_path / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    destination.symlink_to(protected)
    result = subprocess.run([
        sys.executable, str(EXPORTER), "--source-commit", SHA, "--output", str(destination),
    ], cwd=REPO, capture_output=True, text=True)
    assert result.returncode != 0
    assert protected.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask and mode-bit regression")
def test_metadata_exporter_creates_private_metadata_readable_under_umask_077(tmp_path: Path) -> None:
    out = tmp_path / "metadata.json"
    code = "import os,runpy,sys; os.umask(0o077); script,sha,out=sys.argv[1:]; sys.argv=[script,'--source-commit',sha,'--output',out]; runpy.run_path(script,run_name='__main__')"
    result = subprocess.run([sys.executable, "-c", code, str(EXPORTER), SHA, str(out)], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert out.stat().st_mode & 0o777 == 0o644
    assert load_context_metadata(out, SHA)["source_commit"] == SHA


def test_metadata_exporter_does_not_clobber_existing_output(tmp_path: Path) -> None:
    out = tmp_path / "metadata.json"
    out.write_text("keep", encoding="utf-8")
    result = subprocess.run([
        sys.executable, str(EXPORTER), "--source-commit", SHA, "--output", str(out),
    ], cwd=REPO, capture_output=True, text=True)
    assert result.returncode != 0
    assert out.read_text(encoding="utf-8") == "keep"


def test_invalid_artifact_is_rejected_before_current_switch(tmp_path: Path) -> None:
    artifact = tmp_path / "metadata.json"
    artifact.write_text(json.dumps(build_context_metadata(SHA)), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        load_context_metadata(artifact, "0" * 40)
    assert not (tmp_path / "current").exists()
