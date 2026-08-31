"""Deterministic, read-only host health checks for eeebot deployments."""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
try:
    import pwd
except ImportError:  # pragma: no cover - Windows development hosts
    pwd = None
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SERVICE_USER = "eeepc-agent"
DEFAULT_STATE_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/state")
DEFAULT_RELEASE_LINK = Path("/opt/eeepc-agent/runtimes/self-evolving-agent/current")
DEFAULT_REPO_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving")
DEFAULT_WATERMARK_AGE = timedelta(hours=48)
MAX_SCAN_FILES = 200
MAX_LEDGER_LINES = 2000


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    reason: str


@dataclass(frozen=True)
class DoctorResult:
    checks: list[Check]
    exit_code: int

    def as_dict(self) -> dict[str, Any]:
        return {"checks": [asdict(check) for check in self.checks], "exit_code": self.exit_code}


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def file_owner(path: Path) -> str:
    uid = path.stat().st_uid
    if pwd is None:
        return str(uid)
    return pwd.getpwuid(uid).pw_name


def _run(command_runner: CommandRunner, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return command_runner(list(args), capture_output=True, text=True, timeout=2)


def _check_timers(state_dir: Path, runner: CommandRunner, now: datetime) -> Check:
    failures: list[str] = []
    for unit in ("eeepc-self-evolving-subagent-bridge.service", "eeebot-skill-evals.timer"):
        actions = ("is-enabled", "is-active") if unit.endswith("timer") else ("is-active",)
        for action in actions:
            result = _run(runner, ["systemctl", action, unit])
            if result.returncode != 0:
                failures.append(f"{unit} {action}")
    ledger = state_dir / "ledger" / "cycles.jsonl"
    try:
        age = time.time() - ledger.stat().st_mtime
        if age > 120 * 60:
            failures.append(f"ledger inactive for {int(age // 60)}m")
    except OSError:
        failures.append("ledger missing")
    return Check("timers", "FAIL" if failures else "PASS", "; ".join(failures) or "bridge and eval timer checks passed")


def _check_release(link: Path) -> Check:
    if not link.is_symlink():
        return Check("release", "FAIL", f"missing current symlink: {link}")
    try:
        target = link.resolve(strict=True)
    except OSError:
        return Check("release", "FAIL", f"current symlink does not resolve: {link}")
    release_id = target.name
    if not release_id or len(release_id) < 8:
        return Check("release", "FAIL", f"unparseable release id: {target}")
    return Check("release", "PASS", f"current -> {release_id}")


def _check_ownership(state_dir: Path) -> Check:
    problems: list[str] = []
    try:
        files = [path for path in state_dir.rglob("*") if path.is_file()][:MAX_SCAN_FILES]
    except OSError as exc:
        return Check("ownership", "FAIL", f"state scan failed: {exc.__class__.__name__}")
    for path in files:
        try:
            owner = file_owner(path)
            mode = stat.S_IMODE(path.stat().st_mode)
            if owner != SERVICE_USER:
                problems.append(f"root-owned or unexpected: {path}")
            if path.name.lower().endswith((".key", ".pem", ".env")) and mode & 0o137:
                problems.append(f"secret-like mode too wide: {path}")
        except OSError:
            problems.append(f"unreadable: {path}")
    return Check("ownership", "FAIL" if problems else "PASS", "; ".join(problems) or "state ownership and modes are sane")


def _check_watermarks(state_dir: Path, now: datetime) -> Check:
    stale: list[str] = []
    for rel in ("reflector/watermark.json", "skill_evals/watermark.json", "knowledge_lift/watermark.json"):
        path = state_dir / rel
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stamp = data.get("last_run_utc")
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if now - parsed > DEFAULT_WATERMARK_AGE:
                stale.append(f"{rel} stale")
        except Exception:
            stale.append(f"{rel} missing or invalid")
    return Check("watermarks", "WARN" if stale else "PASS", "; ".join(stale) or "watermarks are fresh")


def _check_integrity(state_dir: Path) -> Check:
    malformed: dict[str, int] = {}
    paths = [state_dir / "ledger" / "cycles.jsonl", state_dir / "completed" / "completed.json", state_dir / "demand" / "rotation.json"]
    try:
        paths.extend([p for p in (state_dir / "results").glob("*.json")][:MAX_SCAN_FILES])
    except OSError:
        pass
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LEDGER_LINES:]
        except OSError:
            malformed[str(path)] = 1
            continue
        count = 0
        for line in lines:
            try:
                json.loads(line)
            except Exception:
                count += 1
        if count:
            malformed[path.name] = count
    reason = "; ".join(f"{name}: {count}" for name, count in malformed.items())
    return Check("integrity", "WARN" if malformed else "PASS", reason or "bounded JSON/JSONL scan passed")


def _check_environment(environment: Mapping[str, str]) -> Check:
    expected = ("SUBAGENT_BRIDGE_MODEL", "SUBAGENT_BRIDGE_MAX_REVISIONS", "SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN")
    missing = [name for name in expected if not environment.get(name)]
    return Check("environment", "WARN" if missing else "PASS", "missing: " + ", ".join(missing) if missing else "expected bridge variables present")


def _check_repository(repo_dir: Path, runner: CommandRunner) -> Check:
    branch = _run(runner, ["git", "-C", str(repo_dir), "branch", "--show-current"])
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        return Check("repository", "FAIL", f"checkout is {branch.stdout.strip() or 'unavailable'}")
    status = _run(runner, ["git", "-C", str(repo_dir), "status", "--porcelain"])
    if status.returncode != 0:
        return Check("repository", "WARN", "git status unavailable")
    return Check("repository", "PASS" if not status.stdout.strip() else "WARN", "clean main checkout" if not status.stdout.strip() else "working tree has changes")


def run_doctor(*, state_dir: Path = DEFAULT_STATE_DIR, release_link: Path = DEFAULT_RELEASE_LINK, repo_dir: Path = DEFAULT_REPO_DIR, command_runner: CommandRunner = subprocess.run, now: datetime | None = None, environment: Mapping[str, str] | None = None) -> DoctorResult:
    now = now or datetime.now(timezone.utc)
    checks = [_check_timers(state_dir, command_runner, now), _check_release(release_link), _check_ownership(state_dir), _check_watermarks(state_dir, now), _check_integrity(state_dir), _check_environment(environment or os.environ), _check_repository(repo_dir, command_runner)]
    exit_code = 2 if any(check.status == "FAIL" for check in checks) else 1 if any(check.status == "WARN" for check in checks) else 0
    return DoctorResult(checks, exit_code)


def main(argv: Sequence[str] | None = None, *, command_runner: CommandRunner = subprocess.run, environment: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--release-link", type=Path, default=DEFAULT_RELEASE_LINK)
    parser.add_argument("--repo-dir", type=Path, default=DEFAULT_REPO_DIR)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    result = run_doctor(state_dir=args.state_dir, release_link=args.release_link, repo_dir=args.repo_dir, command_runner=command_runner, environment=environment)
    if args.as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print("CHECK       STATUS  REASON")
        print("----------- ------- ------------------------------------------------------------")
        for check in result.checks:
            print(f"{check.name:<11} {check.status:<7} {check.reason}")
        print(f"SUMMARY     exit={result.exit_code}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
