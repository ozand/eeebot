"""Single-cycle, versioned checkpoints for safe bridge phase progression (#2180).

This module stores phase state only. It does not mint owner IDs, journal
external attempts, classify effect settlement, or authorize side effects.
Callers must hold the bridge lock and supply the existing durable parent owner.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = "bridge-phase-checkpoint-v1"
_RELATIVE_PATH = Path("subagent_bridge") / "phase_checkpoint.json"
_MAX_FILE_BYTES = 16_384
_MAX_MEMOS = 16
_MAX_MEMO_VALUE_BYTES = 128
_RESERVED_MEMO_KEYS = frozenset({"integration_main_sha", "smoke_gate_receipt"})
# This increment checkpoints only the selected post-execution path. Expanding
# phase coverage is an explicit #2180 follow-up; unknown phases fail closed.
_PHASES = ("post_execution", "smoke_gate", "integration", "reflection")
_PHASE_RX = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TOKEN_RX = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHA_RX = re.compile(r"^[a-fA-F0-9]{40,64}$")
_BRANCH_RX = re.compile(r"^[A-Za-z0-9._/-]{1,160}$")
_MEMO_KEY_RX = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MEMO_TEXT_RX = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class CheckpointError(ValueError):
    """Checkpoint is invalid, unavailable, or cannot advance safely."""


class CheckpointHeldError(CheckpointError):
    """An incomplete or mismatched phase must be reconciled, not replayed."""


@dataclass(frozen=True)
class CycleCheckpoint:
    cycle_id: str
    owner_id: str
    branch: str
    base_sha: str
    head_sha: str
    phase: str
    status: str
    memos: dict[str, Any]


def _path(state_dir: Path) -> Path:
    root = Path(state_dir)
    directory = root / _RELATIVE_PATH.parent
    if root.is_symlink() or directory.is_symlink():
        raise CheckpointError("unsafe checkpoint path")
    return directory / _RELATIVE_PATH.name


def _identity(payload: dict[str, Any]) -> CycleCheckpoint:
    expected = {
        "schema", "cycle_id", "owner_id", "branch", "base_sha", "head_sha",
        "phase", "status", "memos",
    }
    if set(payload) != expected or payload.get("schema") != _SCHEMA:
        raise CheckpointError("unsupported checkpoint schema")
    for key in ("cycle_id", "owner_id"):
        if type(payload[key]) is not str or not _TOKEN_RX.fullmatch(payload[key]):
            raise CheckpointError("invalid checkpoint identity")
    if type(payload["branch"]) is not str or not _BRANCH_RX.fullmatch(payload["branch"]):
        raise CheckpointError("invalid checkpoint branch")
    if any(".." in part for part in payload["branch"].split("/")):
        raise CheckpointError("invalid checkpoint branch")
    if any(type(payload[key]) is not str or not _SHA_RX.fullmatch(payload[key]) for key in ("base_sha", "head_sha")):
        raise CheckpointError("invalid checkpoint revision")
    if type(payload["phase"]) is not str or payload["phase"] not in _PHASES:
        raise CheckpointError("invalid checkpoint phase")
    if type(payload["status"]) is not str or payload["status"] not in {"running", "complete"}:
        raise CheckpointError("invalid checkpoint status")
    memos = payload["memos"]
    if type(memos) is not dict or len(memos) > _MAX_MEMOS:
        raise CheckpointError("invalid checkpoint memos")
    for key, value in memos.items():
        if type(key) is not str or not _MEMO_KEY_RX.fullmatch(key):
            raise CheckpointError("invalid checkpoint memo key")
        if key == "smoke_gate_receipt":
            _validate_smoke_gate_receipt(value)
        else:
            _validate_memo_value(value)
    return CycleCheckpoint(
        cycle_id=payload["cycle_id"], owner_id=payload["owner_id"], branch=payload["branch"],
        base_sha=payload["base_sha"], head_sha=payload["head_sha"], phase=payload["phase"],
        status=payload["status"], memos=dict(memos),
    )


def _validate_smoke_gate_receipt(value: Any) -> None:
    if (
        type(value) is not dict or set(value) != {"passed", "tested_head"}
        or value.get("passed") is not True or type(value.get("tested_head")) is not str
        or not _SHA_RX.fullmatch(value["tested_head"])
    ):
        raise CheckpointError("invalid smoke-gate receipt")


def _validate_memo_value(value: Any) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int and value.bit_length() <= 128:
        return
    if type(value) is str and _MEMO_TEXT_RX.fullmatch(value) and len(value.encode("utf-8")) <= _MAX_MEMO_VALUE_BYTES:
        return
    raise CheckpointError("memo value is not an allowlisted scalar")


def _payload(checkpoint: CycleCheckpoint) -> dict[str, Any]:
    return {
        "schema": _SCHEMA, "cycle_id": checkpoint.cycle_id, "owner_id": checkpoint.owner_id,
        "branch": checkpoint.branch, "base_sha": checkpoint.base_sha, "head_sha": checkpoint.head_sha,
        "phase": checkpoint.phase, "status": checkpoint.status, "memos": checkpoint.memos,
    }


def _read_checkpoint(path: Path) -> CycleCheckpoint | None:
    if path.is_symlink():
        raise CheckpointError("unsafe checkpoint file")
    if not path.exists():
        return None
    if not path.is_file():
        raise CheckpointError("unsafe checkpoint file")
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise CheckpointError("checkpoint exceeds size limit")
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    except CheckpointError:
        raise
    except Exception as exc:
        raise CheckpointError("checkpoint is unavailable or corrupt") from exc
    if type(raw) is not dict:
        raise CheckpointError("checkpoint is not an object")
    return _identity(raw)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("duplicate checkpoint key")
        result[key] = value
    return result


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_checkpoint(path: Path, checkpoint: CycleCheckpoint) -> None:
    payload = _payload(checkpoint)
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > _MAX_FILE_BYTES:
        raise CheckpointError("checkpoint exceeds size limit")
    created = not path.parent.exists()
    _identity(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise CheckpointError("unsafe checkpoint file")
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
        if created:
            _fsync_directory(path.parent.parent)
        _fsync_directory(path.parent)
        observed = _read_checkpoint(path)
        if observed != checkpoint:
            raise CheckpointError("checkpoint readback mismatch")
    except CheckpointError:
        raise
    except Exception as exc:
        raise CheckpointError("checkpoint persistence failed") from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_checkpoint(state_dir: Path, *, owner_id: str) -> CycleCheckpoint | None:
    """Read the active bridge checkpoint; unreadable/corrupt state is never empty."""
    if type(owner_id) is not str or not _TOKEN_RX.fullmatch(owner_id):
        raise CheckpointError("invalid caller owner")
    checkpoint = _read_checkpoint(_path(state_dir))
    if checkpoint is not None and checkpoint.owner_id != owner_id:
        raise CheckpointHeldError("checkpoint belongs to a different durable parent")
    return checkpoint


def verify_completed_phase(
    state_dir: Path, *, owner_id: str, cycle_id: str, repo_root: Path,
) -> CycleCheckpoint:
    """Revalidate one completed checkpoint before caller-owned receipt recovery.

    This reads checkpoint state only. It never mints phase or journal receipts;
    callers must separately reconcile the same owner through the intent journal.
    """
    checkpoint = load_checkpoint(state_dir, owner_id=owner_id)
    if checkpoint is None or checkpoint.status != "complete":
        raise CheckpointHeldError("completed checkpoint is unavailable")
    if checkpoint.cycle_id != cycle_id:
        raise CheckpointHeldError("completed checkpoint belongs to a different cycle")
    branch_head = _git_sha(repo_root, f"refs/heads/{checkpoint.branch}")
    if branch_head != checkpoint.head_sha.lower():
        raise CheckpointHeldError("completed checkpoint branch head changed")
    import subprocess

    ancestry = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", checkpoint.base_sha.lower(), branch_head],
        capture_output=True, text=True, timeout=10,
    )
    if ancestry.returncode != 0:
        raise CheckpointHeldError("completed checkpoint base ancestry changed")
    _verify_clean_phase_tree(repo_root, allow_untracked_pytest_cache=True)
    if checkpoint.phase == "post_execution":
        pass
    elif checkpoint.phase == "smoke_gate":
        if checkpoint.memos.get("smoke_gate_receipt") != {
            "passed": True, "tested_head": checkpoint.head_sha.lower(),
        }:
            raise CheckpointHeldError("completed smoke-gate receipt is missing or stale")
    elif checkpoint.phase == "integration":
        _verify_completed_integration(
            repo_root, cycle_id=cycle_id, branch=checkpoint.branch,
            base_sha=checkpoint.base_sha, cycle_head_sha=checkpoint.head_sha,
            main_sha=checkpoint.memos.get("integration_main_sha"),
        )
    else:
        raise CheckpointHeldError("reflection is owned by its separate timer")
    return checkpoint


def begin_phase(
    state_dir: Path, *, owner_id: str, cycle_id: str, branch: str,
    base_sha: str, head_sha: str, phase: str, repo_root: Path,
) -> CycleCheckpoint:
    """Durably admit one phase without minting identity or replaying incomplete work."""
    path = _path(state_dir)
    if phase not in _PHASES:
        raise CheckpointError("unsupported checkpoint phase")
    current = _read_checkpoint(path)
    if current is not None and current.status == "running":
        raise CheckpointHeldError("previous phase has uncertain settlement")
    if current is not None and current.cycle_id != cycle_id:
        # The route journal validates the exact selected receipt prefix before
        # the caller can supply a distinct new owner. This phase-local store
        # does not parse or duplicate that evidence; an unsettled checkpoint
        # can never be rotated.
        if current.status != "complete":
            raise CheckpointHeldError("previous cycle has unfinished phases")
        if current.owner_id == owner_id:
            raise CheckpointHeldError("new cycle requires a distinct durable owner")
    if current is not None and current.cycle_id == cycle_id:
        if (current.owner_id, current.branch, current.base_sha) != (
            owner_id, branch, base_sha,
        ):
            raise CheckpointHeldError("cycle owner, branch identity, or base revision changed")
        if current.head_sha != head_sha.lower():
            import subprocess

            advanced = subprocess.run(
                ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", current.head_sha, head_sha.lower()],
                capture_output=True, text=True, timeout=10,
            )
            if advanced.returncode != 0:
                raise CheckpointHeldError("phase head is not a committed descendant of previous phase")
        if _PHASES.index(phase) != _PHASES.index(current.phase) + 1:
            raise CheckpointHeldError("phase transition is not the next bounded phase")
        memos = dict(current.memos)
        if phase == "integration":
            smoke_receipt = memos.get("smoke_gate_receipt")
            if (
                current.phase != "smoke_gate" or type(smoke_receipt) is not dict
                or smoke_receipt != {"passed": True, "tested_head": head_sha.lower()}
            ):
                raise CheckpointHeldError("integration requires a matching smoke-gate receipt")
            verify_local_branch_head(
                repo_root, branch=branch, expected_base=base_sha, expected_head=head_sha,
            )
        elif phase == "reflection":
            if current.phase != "integration" or current.status != "complete":
                raise CheckpointHeldError("reflection requires a completed integration receipt")
            _verify_completed_integration(
                repo_root, cycle_id=cycle_id, branch=branch, base_sha=base_sha,
                cycle_head_sha=head_sha, main_sha=memos.get("integration_main_sha"),
            )
        else:
            verify_local_branch_head(
                repo_root, branch=branch, expected_base=base_sha, expected_head=head_sha,
            )
    else:
        if phase != _PHASES[0]:
            raise CheckpointHeldError("first phase checkpoint is missing")
        # Keep only one active/most-recent completed cycle. A completed
        # checkpoint is retained until a caller presents the next durable
        # owner and cycle; a running/ambiguous one is never replaced.
        memos = {}
        verify_local_branch_head(
            repo_root, branch=branch, expected_base=base_sha, expected_head=head_sha,
        )
    checkpoint = CycleCheckpoint(cycle_id, owner_id, branch, base_sha, head_sha, phase, "running", memos)
    _write_checkpoint(path, checkpoint)
    return checkpoint


def _git_sha(repo_root: Path, ref: str) -> str:
    """Resolve a local Git ref strictly; missing/error evidence is not empty state."""
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", ref],
        capture_output=True, text=True, timeout=10,
    )
    sha = result.stdout.strip()
    if result.returncode != 0 or not _SHA_RX.fullmatch(sha):
        raise CheckpointHeldError("Git revision evidence unavailable")
    return sha.lower()


def verify_local_branch_head(
    repo_root: Path, *, branch: str, expected_base: str, expected_head: str,
) -> None:
    """Prove checkout identity, branch tip and base ancestry using local Git only."""
    if type(branch) is not str or not _BRANCH_RX.fullmatch(branch) or ".." in branch.split("/"):
        raise CheckpointError("invalid checkpoint branch")
    if any(type(sha) is not str or not _SHA_RX.fullmatch(sha) for sha in (expected_base, expected_head)):
        raise CheckpointError("invalid checkpoint revision")
    repo = Path(repo_root)
    import subprocess
    actual = _git_sha(repo, f"refs/heads/{branch}")
    if actual != expected_head.lower():
        raise CheckpointHeldError("cycle branch head changed")
    ancestry = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", expected_base.lower(), expected_head.lower()],
        capture_output=True, text=True, timeout=10,
    )
    if ancestry.returncode != 0:
        raise CheckpointHeldError("checkpoint base is not an ancestor of branch head")
    result = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or result.stdout.strip() != branch:
        raise CheckpointHeldError("checkout branch identity changed")


def verified_local_branch_tip(repo_root: Path, *, branch: str, expected_base: str) -> str:
    """Read and verify the retained cycle branch tip before phase admission."""
    if type(branch) is not str or not _BRANCH_RX.fullmatch(branch) or ".." in branch.split("/"):
        raise CheckpointError("invalid checkpoint branch")
    if type(expected_base) is not str or not _SHA_RX.fullmatch(expected_base):
        raise CheckpointError("invalid checkpoint revision")
    tip = _git_sha(repo_root, f"refs/heads/{branch}")
    verify_local_branch_head(
        repo_root, branch=branch, expected_base=expected_base, expected_head=tip,
    )
    return tip


def _verify_clean_phase_tree(repo_root: Path, *, allow_untracked_pytest_cache: bool = False) -> None:
    import subprocess

    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, timeout=10,
    )
    if status.returncode != 0:
        raise CheckpointHeldError("phase checkout status is unavailable")
    entries = status.stdout.splitlines()
    if allow_untracked_pytest_cache:
        def _generated_test_artifact(entry: str) -> bool:
            if not entry.startswith("?? "):
                return False
            path = entry[3:].replace(chr(92), "/")
            parts = path.split("/")
            return (
                any(part == ".pytest_cache" for part in parts)
                or "__pycache__" in parts
            )

        entries = [entry for entry in entries if not _generated_test_artifact(entry)]
    if entries:
        raise CheckpointHeldError("phase checkout has uncommitted changes")


def _verify_committed_phase_tip(
    repo_root: Path, *, branch: str, expected_base: str, expected_head: str,
) -> str:
    """Accept only a clean cycle checkout whose committed tip descends from admission."""
    if type(branch) is not str or not _BRANCH_RX.fullmatch(branch) or ".." in branch.split("/"):
        raise CheckpointError("invalid checkpoint branch")
    if any(type(sha) is not str or not _SHA_RX.fullmatch(sha) for sha in (expected_base, expected_head)):
        raise CheckpointError("invalid checkpoint revision")
    import subprocess

    repo = Path(repo_root)
    actual = _git_sha(repo, f"refs/heads/{branch}")
    for ancestor, descendant, reason in (
        (expected_base.lower(), actual, "checkpoint base is not an ancestor of phase tip"),
        (expected_head.lower(), actual, "phase tip does not descend from admitted head"),
    ):
        ancestry = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True, text=True, timeout=10,
        )
        if ancestry.returncode != 0:
            raise CheckpointHeldError(reason)
    checkout = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if checkout.returncode != 0 or checkout.stdout.strip() != branch:
        raise CheckpointHeldError("checkout branch identity changed")
    _verify_clean_phase_tree(repo, allow_untracked_pytest_cache=True)
    return actual


def complete_phase(
    state_dir: Path, *, owner_id: str, cycle_id: str, branch: str,
    base_sha: str, head_sha: str, phase: str, repo_root: Path,
    integration_receipt: dict[str, Any] | None = None,
    smoke_gate_result: dict[str, Any] | None = None,
) -> CycleCheckpoint:
    """Complete a local phase, or integration with an exact verified receipt."""
    path = _path(state_dir)
    current = load_checkpoint(state_dir, owner_id=owner_id)
    if current is None or current.status != "running":
        raise CheckpointHeldError("no unsettled phase checkpoint exists")
    if (current.cycle_id, current.branch, current.base_sha, current.head_sha, current.phase) != (
        cycle_id, branch, base_sha, head_sha, phase,
    ):
        raise CheckpointHeldError("completion does not match the durable phase checkpoint")
    memos = dict(current.memos)
    if phase == "smoke_gate":
        if type(smoke_gate_result) is not dict or set(smoke_gate_result) != {"passed", "tested_head"}:
            raise CheckpointHeldError("smoke-gate result is missing or malformed")
        if smoke_gate_result["passed"] is not True:
            raise CheckpointHeldError("smoke-gate pass is not confirmed")
        if type(smoke_gate_result["tested_head"]) is not str or smoke_gate_result["tested_head"].lower() != head_sha.lower():
            raise CheckpointHeldError("smoke-gate result is stale for the current branch head")
        if integration_receipt is not None:
            raise CheckpointError("integration receipt is only valid for the integration phase")
        verify_local_branch_head(
            repo_root, branch=branch, expected_base=base_sha, expected_head=head_sha,
        )
        _verify_clean_phase_tree(repo_root, allow_untracked_pytest_cache=True)
        if "smoke_gate_receipt" in memos:
            raise CheckpointHeldError("smoke-gate receipt was already memoized")
        memos["smoke_gate_receipt"] = {"passed": True, "tested_head": head_sha.lower()}
    elif smoke_gate_result is not None:
        raise CheckpointError("smoke-gate result is only valid for the smoke_gate phase")
    if phase == "integration":
        main_sha = _verify_integration_receipt(
            repo_root, cycle_id=cycle_id, branch=branch, base_sha=base_sha,
            cycle_head_sha=head_sha, receipt=integration_receipt,
        )
        if "integration_main_sha" in memos:
            raise CheckpointHeldError("integration main revision was already memoized")
        memos["integration_main_sha"] = main_sha
    else:
        if integration_receipt is not None:
            raise CheckpointError("integration receipt is only valid for the integration phase")
        if phase == "reflection":
            _verify_completed_integration(
                repo_root, cycle_id=cycle_id, branch=branch, base_sha=base_sha,
                cycle_head_sha=head_sha, main_sha=memos.get("integration_main_sha"),
            )
        elif phase in {"post_execution", "smoke_gate"}:
            head_sha = _verify_committed_phase_tip(
                repo_root, branch=branch, expected_base=base_sha, expected_head=head_sha,
            )
        else:
            verify_local_branch_head(
                repo_root, branch=branch, expected_base=base_sha, expected_head=head_sha,
            )
    completed = CycleCheckpoint(
        current.cycle_id, current.owner_id, current.branch, current.base_sha,
        head_sha, current.phase, "complete", memos,
    )
    _write_checkpoint(path, completed)
    return completed


def _verify_integration_receipt(
    repo_root: Path, *, cycle_id: str, branch: str, base_sha: str,
    cycle_head_sha: str, receipt: dict[str, Any] | None,
) -> str:
    """Verify a successful bridge push against the retained cycle ref and merge parents."""
    if type(receipt) is not dict or set(receipt) != {"ok", "push_confirmed", "main_sha_after"}:
        raise CheckpointHeldError("integration receipt is missing or malformed")
    main_sha = receipt["main_sha_after"]
    if receipt["ok"] is not True or receipt["push_confirmed"] is not True:
        raise CheckpointHeldError("integration push is not confirmed")
    if type(main_sha) is not str or not _SHA_RX.fullmatch(main_sha):
        raise CheckpointHeldError("integration main revision is invalid")
    branch_sha = _git_sha(repo_root, f"refs/heads/{branch}")
    if branch_sha != cycle_head_sha.lower():
        raise CheckpointHeldError("integration cycle branch head changed")
    actual_main = _git_sha(repo_root, "refs/heads/main")
    actual_origin = _git_sha(repo_root, "refs/remotes/origin/main")
    if actual_main != main_sha.lower() or actual_origin != main_sha.lower():
        raise CheckpointHeldError("integration receipt does not match local and pushed main")
    import subprocess

    parents_result = subprocess.run(
        ["git", "-C", str(repo_root), "show", "-s", "--format=%P", main_sha.lower()],
        capture_output=True, text=True, timeout=10,
    )
    if parents_result.returncode != 0:
        raise CheckpointHeldError("integration merge evidence unavailable")
    parents = parents_result.stdout.split()
    cycle_id_result = subprocess.run(
        ["git", "-C", str(repo_root), "show", "-s", "--format=%(trailers:key=Selfevo-Cycle,valueonly)", main_sha.lower()],
        capture_output=True, text=True, timeout=10,
    )
    if cycle_id_result.returncode != 0 or cycle_id_result.stdout.strip() != cycle_id:
        raise CheckpointHeldError("integration cycle identity does not match merge receipt")
    if len(parents) != 2 or parents[0] != base_sha.lower() or parents[1] != cycle_head_sha.lower():
        raise CheckpointHeldError("integration merge parents do not match checkpoint revisions")
    return main_sha.lower()


def _verify_completed_integration(
    repo_root: Path, *, cycle_id: str, branch: str, base_sha: str,
    cycle_head_sha: str, main_sha: Any,
) -> None:
    if type(main_sha) is not str or not _SHA_RX.fullmatch(main_sha):
        raise CheckpointHeldError("completed integration receipt is missing")
    _verify_integration_receipt(
        repo_root, cycle_id=cycle_id, branch=branch, base_sha=base_sha,
        cycle_head_sha=cycle_head_sha,
        receipt={"ok": True, "push_confirmed": True, "main_sha_after": main_sha},
    )


def memo(state_dir: Path, *, owner_id: str, key: str, value: Any) -> Any:
    """Persist one small first-write-wins decision within the current running phase."""
    if type(key) is not str or not _MEMO_KEY_RX.fullmatch(key) or key in _RESERVED_MEMO_KEYS:
        raise CheckpointError("invalid or reserved memo key")
    _validate_memo_value(value)
    current = load_checkpoint(state_dir, owner_id=owner_id)
    if current is None or current.status != "running":
        raise CheckpointHeldError("memos require an active phase")
    if key in current.memos:
        return current.memos[key]
    if len(current.memos) >= _MAX_MEMOS - len(_RESERVED_MEMO_KEYS):
        raise CheckpointHeldError("memo capacity reached")
    values = dict(current.memos)
    values[key] = value
    updated = CycleCheckpoint(
        current.cycle_id, current.owner_id, current.branch, current.base_sha,
        current.head_sha, current.phase, current.status, values,
    )
    _write_checkpoint(_path(state_dir), updated)
    return value
