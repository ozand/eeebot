"""Deterministic, read-only host health checks for eeebot deployments."""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
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
MID_CYCLE_FRESHNESS = timedelta(minutes=60)  # 3000s executor budget plus margin


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
    for unit in ("eeebot-skill-evals.timer",):
        actions = ("is-enabled", "is-active")
        for action in actions:
            result = _run(runner, ["systemctl", action, unit])
            if result.returncode != 0 and result.stdout.strip() not in {"active", "activating"}:
                failures.append(f"{unit} {action}")
    bridge = _run(runner, ["systemctl", "show", "eeepc-self-evolving-subagent-bridge.service", "-p", "Result", "-p", "ExecMainExitTimestamp", "-p", "ExecMainStatus"])
    values = dict(line.split("=", 1) for line in bridge.stdout.splitlines() if "=" in line)
    if bridge.returncode != 0 or values.get("Result") not in {"success", ""} or values.get("ExecMainStatus") not in {"0", ""}:
        failures.append("bridge last run did not succeed")
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
        files: list[Path] = []
        for path in state_dir.rglob("*"):
            if path.is_file():
                files.append(path)
                if len(files) >= MAX_SCAN_FILES:
                    break
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
            stamp = data.get("last_run_utc") or data.get("updated_at") or data.get("timestamp")
            if not stamp:
                continue
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if now - parsed > DEFAULT_WATERMARK_AGE:
                stale.append(f"{rel} stale")
        except Exception:
            stale.append(f"{rel} missing or invalid")
    return Check("watermarks", "WARN" if stale else "PASS", "; ".join(stale) or "watermarks are fresh")


def _check_integrity(state_dir: Path) -> Check:
    malformed: dict[str, int] = {}
    paths = [state_dir / "ledger" / "cycles.jsonl", state_dir / "completed" / "completed.json", state_dir / "demand" / "rotation.json"]
    for results_dir in (state_dir / "subagents" / "results", state_dir / "subagents" / "archive"):
        try:
            for path in results_dir.glob("*.json"):
                paths.append(path)
                if len(paths) >= MAX_SCAN_FILES:
                    break
        except OSError:
            continue
        if len(paths) >= MAX_SCAN_FILES:
            break
    for path in paths:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            malformed[f"unreadable: {path}"] = 1
            continue
        count = 0
        if path.name.endswith(".jsonl"):
            lines = content.splitlines()[-MAX_LEDGER_LINES:]
            for line in lines:
                try:
                    json.loads(line)
                except Exception:
                    count += 1
        else:
            try:
                json.loads(content)
            except Exception:
                count = 1
        if count:
            malformed[path.name] = count
    reason = "; ".join(f"{name}: {count}" for name, count in malformed.items())
    return Check("integrity", "WARN" if malformed else "PASS", reason or "bounded JSON/JSONL scan passed")


def _environment_names(text: str) -> set[str]:
    return {
        token.split("=", 1)[0]
        for token in text.replace("\\n", " ").split()
        if "=" in token and token.split("=", 1)[0].isidentifier()
    }


def _check_environment(environment: Mapping[str, str], runner: CommandRunner) -> Check:
    expected = ("SUBAGENT_BRIDGE_MODEL",)
    optional = {"SUBAGENT_BRIDGE_MAX_REVISIONS", "SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN"}
    unit = _run(runner, ["systemctl", "show", "eeepc-self-evolving-subagent-bridge.service", "-p", "Environment", "-p", "EnvironmentFiles"])
    environment_text = ""
    environment_files: list[str] = []
    for line in unit.stdout.splitlines():
        if line.startswith("EnvironmentFiles="):
            raw = line.partition("=")[2].strip()
            raw = raw.split(" (ignore_errors=", 1)[0].strip()
            path = raw.lstrip("-")
            if path:
                environment_files.append(path)
        elif line.startswith("Environment="):
            environment_text = line.partition("=")[2]
    present = set(environment) | _environment_names(environment_text)
    skipped: list[str] = []
    for path in environment_files:
        try:
            if path.endswith("/litellm.env") or not Path(path).is_file():
                skipped.append(f"{path}: skipped (unreadable)")
                continue
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    name = line.split("=", 1)[0].strip()
                    if name.isidentifier():
                        present.add(name)
        except OSError:
            skipped.append(f"{path}: skipped (unreadable)")
    missing = [name for name in expected if name not in present]
    notes = [f"defaultable absent: {name}" for name in optional if name not in present]
    reason = "; ".join(((["missing: " + ", ".join(missing)] if missing else ["expected bridge variables present"]) + notes + skipped))
    return Check("environment", "WARN" if missing else "PASS", reason)


def _check_repository(repo_dir: Path, runner: CommandRunner, state_dir: Path | None = None, now: datetime | None = None) -> Check:
    branch_result = _run(runner, ["git", "-C", str(repo_dir), "branch", "--show-current"])
    branch = branch_result.stdout.strip()
    if branch_result.returncode != 0 or branch != "main":
        if branch.startswith("selfevo/cycle-") and state_dir is not None and now is not None:
            latest = None
            try:
                rows = (state_dir / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()[-MAX_LEDGER_LINES:]
            except (OSError, UnicodeError):
                rows = []
            for line in rows:
                try:
                    data = json.loads(line)
                    ts = data.get("ts")
                    if data.get("phase") in {"started", "outcome"} and ts:
                        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        latest = parsed if latest is None or parsed > latest else latest
                except Exception:
                    pass
            calls_path = state_dir / "llm_calls" / f"{now:%Y-%m-%d}.jsonl"
            try:
                heartbeat = datetime.fromtimestamp(calls_path.stat().st_mtime, timezone.utc)
                latest = heartbeat if latest is None or heartbeat > latest else latest
            except OSError:
                pass
            fresh = latest is not None and now - latest <= MID_CYCLE_FRESHNESS
            if fresh:
                return Check("repository", "PASS", f"mid-cycle branch {branch}")
        return Check("repository", "FAIL", f"checkout is {branch or 'unavailable'}")
    status = _run(runner, ["git", "-C", str(repo_dir), "status", "--porcelain"])
    if status.returncode != 0:
        return Check("repository", "WARN", "git status unavailable")
    return Check("repository", "PASS" if not status.stdout.strip() else "WARN", "clean main checkout" if not status.stdout.strip() else "working tree has changes")


def run_doctor(*, state_dir: Path = DEFAULT_STATE_DIR, release_link: Path = DEFAULT_RELEASE_LINK, repo_dir: Path = DEFAULT_REPO_DIR, command_runner: CommandRunner = subprocess.run, now: datetime | None = None, environment: Mapping[str, str] | None = None) -> DoctorResult:
    now = now or datetime.now(timezone.utc)
    checks = [_check_timers(state_dir, command_runner, now), _check_release(release_link), _check_ownership(state_dir), _check_watermarks(state_dir, now), _check_integrity(state_dir), _check_environment(environment or os.environ, command_runner), _check_repository(repo_dir, command_runner, state_dir, now)]
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
