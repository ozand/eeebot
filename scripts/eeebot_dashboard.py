#!/usr/bin/env python3
"""Minimal status dashboard for the eeebot self-evolving runtime.

The dashboard intentionally stays dependency-free so it can run on the weak
host even when richer TUI libraries are unavailable.
Supports CLI plain text output and a web interface (--serve).

Source-of-truth decision (#1206): this remains the live dashboard on port 8080.
Live queue, host-capability, cleanup, and system metrics remain authoritative;
retired report/materialized artifacts are retained for context only and must be
labelled stale or unavailable rather than reported as healthy current state.
"""

from __future__ import annotations

import heapq
import html
import http.server
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Live-refresh TUI state
_watch_running = True
_prev_watch_metrics: dict[str, Any] | None = None

# Adaptive refresh state — tracks recent CPU loads to adjust watch interval
_adaptive_load_history: list[float] = []
_ADAPTIVE_LOAD_WINDOW = 5  # number of samples to average
_ADAPTIVE_MIN_INTERVAL = 2.0  # seconds (when system is busy)
_ADAPTIVE_MAX_INTERVAL = 10.0  # seconds (when system is idle)


def _handle_interrupt(signum: int, frame: Any) -> None:
    global _watch_running
    _watch_running = False


def get_cpu_load() -> float:
    """Read current CPU load average from /proc/loadavg (no subprocess overhead).

    Returns the 1-minute load average as a float.  Falls back to 0.0 on error.
    """
    try:
        loadavg = Path("/proc/loadavg").read_text(encoding="utf-8").split()[0]
        return float(loadavg)
    except Exception:
        return 0.0


def get_memory_usage_pct() -> float:
    """Read current memory usage percentage from /proc/meminfo (no subprocess overhead).

    Returns percentage used (0.0–100.0).  Falls back to 0.0 on error.
    """
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        vals: dict[str, int] = {}
        for line in lines:
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val_str = parts[1].strip().split()[0]
                try:
                    vals[key] = int(val_str)
                except ValueError:
                    pass
        total = vals.get("MemTotal", 0)
        available = vals.get("MemAvailable", 0)
        if total > 0:
            return ((total - available) / total) * 100.0
    except Exception:
        pass
    return 0.0


def get_disk_usage_pct() -> float:
    """Read disk usage percentage for / from /proc/mounts + os.statvfs (no subprocess).

    Returns percentage used (0.0–100.0).  Falls back to 0.0 on error.
    """
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        if total > 0:
            return ((total - free) / total) * 100.0
    except Exception:
        pass
    return 0.0


def compute_adaptive_interval(base_interval: float) -> float:
    """Compute an adaptive refresh interval based on recent CPU load.

    When CPU load is high (>1.0 on a single-core eeepc), the interval increases
    to reduce dashboard overhead.  When idle, it decreases for faster feedback.

    Returns a clamped interval in [ADAPTIVE_MIN_INTERVAL, ADAPTIVE_MAX_INTERVAL].
    """
    load = get_cpu_load()
    _adaptive_load_history.append(load)
    if len(_adaptive_load_history) > _ADAPTIVE_LOAD_WINDOW:
        _adaptive_load_history.pop(0)

    avg_load = sum(_adaptive_load_history) / len(_adaptive_load_history)

    # Scale: load=0 → min interval, load=2.0 → max interval
    factor = min(avg_load / 2.0, 1.0)
    interval = _ADAPTIVE_MIN_INTERVAL + factor * (_ADAPTIVE_MAX_INTERVAL - _ADAPTIVE_MIN_INTERVAL)
    return max(_ADAPTIVE_MIN_INTERVAL, min(_ADAPTIVE_MAX_INTERVAL, interval))


REPO_ROOT = Path(__file__).resolve().parents[1]

# Prefer the system agent state dir if it exists, fall back to repo-local state/
_SYSTEM_STATE_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/state")
if os.getenv("EEEBOT_STATE_DIR"):
    STATE_DIR = Path(os.getenv("EEEBOT_STATE_DIR"))
elif _SYSTEM_STATE_DIR.exists():
    STATE_DIR = _SYSTEM_STATE_DIR
else:
    STATE_DIR = REPO_ROOT / "state"
IMPROVEMENT_DIR = Path(
    os.getenv(
        "EEEBOT_IMPROVEMENTS_DIR",
        "/var/lib/eeepc-agent/self-evolving-agent/state/improvements",
    )
)
REPORTS_DIR = Path(
    os.getenv(
        "EEEBOT_REPORTS_DIR",
        "/var/lib/eeepc-agent/self-evolving-agent/state/reports",
    )
)
METRICS_CACHE_TTL_SECONDS = 3.0
TREE_SCAN_CACHE_TTL_SECONDS = 3.0
HOST_CAPS_CACHE_TTL_SECONDS = 30.0
REPORT_SCAN_CACHE_TTL_SECONDS = 5.0
MATERIALIZED_CACHE_TTL_SECONDS = 5.0
ARTIFACT_STALE_AFTER_HOURS = 24.0
_METRICS_CACHE: dict[str, Any] = {"loaded_at": 0.0, "metrics": None}
_SUBAGENT_TREE_CACHE: dict[str, Any] = {"loaded_at": 0.0, "hours": None, "root_mtime_ns": None, "stats": None}
_HOST_CAPS_CACHE: dict[str, Any] = {"loaded_at": 0.0, "host_caps": None}
_REPORT_SCAN_CACHE: dict[str, Any] = {"loaded_at": 0.0, "limit": None, "root_mtime_ns": None, "result": None, "source_status": "missing"}
_MATERIALIZED_CACHE: dict[str, Any] = {"loaded_at": 0.0, "limit": None, "root_mtime_ns": None, "result": None, "source_status": "missing"}


def refresh_host_capabilities() -> dict[str, Any]:
    """Re-scan host hardware and update state/host_capabilities.json."""
    import subprocess

    caps: dict[str, Any] = {}

    # Camera
    try:
        videos = subprocess.check_output(["ls", "/dev/video*"], stderr=subprocess.DEVNULL).decode().strip().split()
        caps["camera"] = {"available": bool(videos), "details": f"Detected {', '.join(videos)}"}
    except Exception:
        caps["camera"] = {"available": False, "details": "not detected"}

    # Bluetooth
    try:
        bt = subprocess.check_output(["lsusb"], stderr=subprocess.DEVNULL).decode()
        bt_lines = [l for l in bt.splitlines() if "Bluetooth" in l or "bluetooth" in l.lower()]
        caps["bluetooth"] = {"available": bool(bt_lines), "details": bt_lines[0].strip() if bt_lines else "not detected"}
    except Exception:
        caps["bluetooth"] = {"available": False, "details": "not detected"}

    # WiFi
    try:
        ifaces = subprocess.check_output(["ip", "-o", "link", "show"], stderr=subprocess.DEVNULL).decode()
        wifi_ifaces = [l.split(":")[1].strip() for l in ifaces.splitlines() if "wlan" in l or "wlp" in l]
        caps["wifi"] = {"available": bool(wifi_ifaces), "details": f"Detected {', '.join(wifi_ifaces)}" if wifi_ifaces else "not detected"}
    except Exception:
        caps["wifi"] = {"available": False, "details": "not detected"}

    # Microphone
    try:
        mic = subprocess.check_output(["arecord", "-l"], stderr=subprocess.DEVNULL).decode()
        caps["microphone"] = {"available": "card" in mic.lower(), "details": mic.strip().split("\n")[0] if mic.strip() else "not detected"}
    except Exception:
        caps["microphone"] = {"available": False, "details": "not detected"}

    # CPU
    try:
        cpu = open("/proc/cpuinfo").read()
        model = [l.split(":")[1].strip() for l in cpu.splitlines() if l.startswith("model name")][0]
        caps["cpu"] = {"available": True, "details": model}
    except Exception:
        caps["cpu"] = {"available": False, "details": "unknown"}

    # Memory
    try:
        mem = open("/proc/meminfo").read()
        lines = {l.split(":")[0]: l.split(":")[1].strip() for l in mem.splitlines()}
        caps["memory"] = {"available": True, "details": f"Mem: total={lines.get('MemTotal', '?')} available={lines.get('MemAvailable', '?')}"}
    except Exception:
        caps["memory"] = {"available": False, "details": "unknown"}

    # Disk
    try:
        disk = subprocess.check_output(["df", "-h", "/"], stderr=subprocess.DEVNULL).decode().strip().split("\n")[1]
        parts = disk.split()
        caps["disk"] = {"available": True, "details": f"{parts[0]} {parts[1]} total, {parts[2]} used, {parts[3]} free, {parts[4]} used on /"}
    except Exception:
        caps["disk"] = {"available": False, "details": "unknown"}

    # Kernel
    try:
        kernel = subprocess.check_output(["uname", "-r"]).decode().strip()
        caps["kernel"] = {"available": True, "details": kernel}
    except Exception:
        caps["kernel"] = {"available": False, "details": "unknown"}

    # Uptime
    try:
        uptime = subprocess.check_output(["uptime", "-p"]).decode().strip()
        caps["uptime"] = {"available": True, "details": uptime}
    except Exception:
        caps["uptime"] = {"available": False, "details": "unknown"}

    # Screen resolution
    try:
        screen_res = "unknown"
        fb_size_path = Path("/sys/class/graphics/fb0/virtual_size")
        if fb_size_path.exists():
            screen_res = fb_size_path.read_text().strip().replace(",", "x")
        else:
            # Fallback to drm modes
            for mode_path in Path("/sys/class/drm").glob("card*-*/modes"):
                if mode_path.exists():
                    lines = mode_path.read_text().strip().splitlines()
                    if lines:
                        screen_res = lines[0].strip()
                        break
        caps["screen"] = {"available": screen_res != "unknown", "details": f"Physical Resolution: {screen_res}"}
    except Exception:
        caps["screen"] = {"available": False, "details": "unknown"}

    caps["_scan_timestamp"] = datetime.now(timezone.utc).isoformat()

    # Write to state file
    host_caps_path = STATE_DIR / "host_capabilities.json"
    host_caps_path.write_text(json.dumps(caps, indent=2) + "\n", encoding="utf-8")

    # Invalidate cache
    _HOST_CAPS_CACHE["host_caps"] = caps
    _HOST_CAPS_CACHE["loaded_at"] = time.monotonic()

    return caps


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (PermissionError, OSError):
        return default
    except json.JSONDecodeError:
        return default


def read_json_state(path: Path) -> tuple[Any, str]:
    """Read one dashboard state source with bounded, non-sensitive status metadata."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        return None, "permission"
    except OSError:
        return None, "unreadable"
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None, "malformed"
    if value is None or value == {} or value == [] or value == "":
        return value, "valid-empty"
    return value, "valid"


def source_status_for_directory(directory: Path, pattern: str) -> str:
    """Classify a bounded JSON source without exposing its contents."""
    try:
        if not directory.exists():
            return "missing"
        matches = list(directory.glob(pattern))
    except PermissionError:
        return "permission"
    except OSError:
        return "unreadable"
    if not matches:
        return "valid-empty"
    statuses = {read_json_state(path)[1] for path in matches[:5]}
    if "valid" in statuses:
        return "valid"
    if "malformed" in statuses:
        return "malformed"
    if "permission" in statuses:
        return "permission"
    if "unreadable" in statuses:
        return "unreadable"
    return "valid-empty"


def source_status_for_artifact(path: Path | None, directory: Path, pattern: str) -> str:
    if path is None:
        return source_status_for_directory(directory, pattern)
    _, status = read_json_state(path)
    return status


def latest_file(directory: Path, pattern: str) -> Path | None:
    try:
        return max(directory.glob(pattern), key=lambda p: p.stat().st_mtime, default=None)
    except Exception:
        return None


def scan_report_artifacts(limit: int = 5) -> tuple[Path | None, dict[str, Any], list[tuple[str, float]]]:
    """Scan report artifacts once and reuse the result for latest-report and trend views.

    Uses directory-mtime-based caching to avoid re-scanning 8000+ report files
    on every call.  Cache is invalidated when the reports directory changes
    or after REPORT_SCAN_CACHE_TTL_SECONDS.
    """
    now = time.monotonic()
    cached_limit = _REPORT_SCAN_CACHE.get("limit")
    cached_root_mtime_ns = _REPORT_SCAN_CACHE.get("root_mtime_ns")
    cached_result = _REPORT_SCAN_CACHE.get("result")
    loaded_at = float(_REPORT_SCAN_CACHE.get("loaded_at", 0.0) or 0.0)
    try:
        root_mtime_ns = REPORTS_DIR.stat().st_mtime_ns if REPORTS_DIR.exists() else None
    except Exception:
        root_mtime_ns = None
    if (
        cached_result is not None
        and cached_limit == limit
        and cached_root_mtime_ns == root_mtime_ns
        and now - loaded_at < REPORT_SCAN_CACHE_TTL_SECONDS
    ):
        return cached_result

    latest_path: Path | None = None
    latest_mtime = -1.0
    recent_heap: list[tuple[float, Path]] = []

    try:
        for path in REPORTS_DIR.glob("evolution-*.json"):
            mtime = path.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = path
            item = (mtime, path)
            if len(recent_heap) < limit:
                heapq.heappush(recent_heap, item)
            else:
                heapq.heappushpop(recent_heap, item)
    except Exception:
        result = (None, {}, [])
        _REPORT_SCAN_CACHE.update({"loaded_at": now, "limit": limit, "root_mtime_ns": root_mtime_ns, "result": result})
        return result

    if latest_path is None:
        result = (None, {}, [])
        _REPORT_SCAN_CACHE.update({"loaded_at": now, "limit": limit, "root_mtime_ns": root_mtime_ns, "result": result})
        return result

    latest_report: dict[str, Any] = {}
    recent_rewards: list[tuple[str, float]] = []
    for _, report in sorted(recent_heap, reverse=True):
        data = load_json(report, {})
        reward = data.get("reward_signal", {}).get("value")
        if isinstance(reward, (int, float)):
            recent_rewards.append((data.get("cycle_id", report.stem), float(reward)))
        if report == latest_path:
            latest_report = data

    if not latest_report:
        latest_report = load_json(latest_path, {})
    result = (latest_path, latest_report, recent_rewards)
    _REPORT_SCAN_CACHE.update({"loaded_at": now, "limit": limit, "root_mtime_ns": root_mtime_ns, "result": result})
    return result


def scan_all_report_rewards(limit: int = 200) -> list[tuple[str, float, str]]:
    """Scan report artifacts and return (cycle_id, reward, result_status) tuples.

    Uses a limit to avoid scanning 8000+ files on the weak host.  Defaults to
    the 200 most recent reports by mtime, which covers the last ~200 cycles.

    Uses directory-mtime-based caching to avoid re-scanning 8000+ report files
    on every call.  Cache is invalidated when the reports directory changes
    or after REPORT_SCAN_CACHE_TTL_SECONDS.
    """
    now = time.monotonic()
    cached_limit = _MATERIALIZED_CACHE.get("limit")
    cached_root_mtime_ns = _MATERIALIZED_CACHE.get("root_mtime_ns")
    cached_result = _MATERIALIZED_CACHE.get("result")
    loaded_at = float(_MATERIALIZED_CACHE.get("loaded_at", 0.0) or 0.0)
    try:
        root_mtime_ns = REPORTS_DIR.stat().st_mtime_ns if REPORTS_DIR.exists() else None
    except Exception:
        root_mtime_ns = None
    if (
        cached_result is not None
        and cached_limit == limit
        and cached_root_mtime_ns == root_mtime_ns
        and now - loaded_at < REPORT_SCAN_CACHE_TTL_SECONDS
    ):
        return cached_result

    rewards: list[tuple[str, float, str]] = []
    try:
        # Use heapq.nlargest for O(n log k) instead of sorted() O(n log n)
        # when only the newest *limit* reports are needed from 8000+ files.
        paths = heapq.nlargest(
            limit,
            REPORTS_DIR.glob("evolution-*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        for path in paths:
            data = load_json(path, {})
            reward = data.get("reward_signal", {}).get("value")
            if isinstance(reward, (int, float)):
                cycle_id = data.get("cycle_id", path.stem)
                result_status = data.get("reward_signal", {}).get("result_status", "unknown")
                rewards.append((str(cycle_id), float(reward), str(result_status)))
    except Exception:
        pass
    _MATERIALIZED_CACHE.update({"loaded_at": now, "limit": limit, "root_mtime_ns": root_mtime_ns, "result": rewards})
    return rewards


def bounded_reward_export(rewards: list[tuple[str, float, str]], source: dict[str, Any]) -> list[tuple[str, float, str]]:
    """Return reward rows only when the report source is explicitly fresh.

    This dashboard treats report artifacts as context-only, so the public
    export remains metadata-only even when a file is fresh.
    """
    return []


def reward_export_unavailable(source: dict[str, Any]) -> str:
    """Return bounded export status without exposing report rows."""
    return f"reward export unavailable (source={_context_only_label(source)})"


def export_reward_csv(rewards: list[tuple[str, float, str]], destination: Path | None = None) -> Path:
    """Export only bounded, fresh-source reward data."""
    import csv as _csv
    destination = destination or Path(
        f"/tmp/eeebot-rewards-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    )
    with open(destination, "w", newline="", encoding="utf-8") as f:
        writer = _csv.writer(f)
        writer.writerow(["cycle_id", "reward", "result_status"])
        for cycle_id, reward, result_status in rewards:
            writer.writerow([cycle_id, reward, result_status])
    return destination


def render_top_cycles(rewards: list[tuple[str, float, str]], top_n: int = 5) -> str:
    """Render best/worst cycles from an already bounded, authorized set."""
    if not rewards:
        return "No reward data available"
    sorted_rewards = sorted(rewards, key=lambda x: x[1], reverse=True)
    best = sorted_rewards[:top_n]
    worst = sorted_rewards[-top_n:]

    lines = [f"Top {top_n} cycles by reward:", "=" * 50]
    for cycle_id, reward, status in best:
        lines.append(f"  {cycle_id}: {reward:.2f} ({status})")
    lines.append("")
    lines.append(f"Bottom {top_n} cycles by reward:")
    lines.append("=" * 50)
    for cycle_id, reward, status in reversed(worst):
        lines.append(f"  {cycle_id}: {reward:.2f} ({status})")
    return "\n".join(lines)


def load_latest_materialized() -> tuple[Path | None, dict[str, Any]]:
    latest = latest_file(IMPROVEMENT_DIR, "materialized-cycle-*.json")
    if not latest:
        return None, {}
    return latest, load_json(latest, {})


def load_latest_report() -> tuple[Path | None, dict[str, Any]]:
    latest_path, latest_report, _ = scan_report_artifacts(limit=1)
    return latest_path, latest_report


def load_recent_rewards(limit: int = 5) -> list[tuple[str, float]]:
    _, _, recent_rewards = scan_report_artifacts(limit=limit)
    return recent_rewards


def scan_subagent_tree_stats(hours: int = 24) -> tuple[int, int, float | None, int, Path | None]:
    now = time.monotonic()
    queue_root = STATE_DIR / "subagents"
    cached_hours = _SUBAGENT_TREE_CACHE.get("hours")
    cached_root_mtime_ns = _SUBAGENT_TREE_CACHE.get("root_mtime_ns")
    cached_stats = _SUBAGENT_TREE_CACHE.get("stats")
    loaded_at = float(_SUBAGENT_TREE_CACHE.get("loaded_at", 0.0) or 0.0)
    try:
        root_mtime_ns = queue_root.stat().st_mtime_ns if queue_root.exists() else None
    except Exception:
        root_mtime_ns = None
    if (
        cached_stats is not None
        and cached_hours == hours
        and cached_root_mtime_ns == root_mtime_ns
        and now - loaded_at < TREE_SCAN_CACHE_TTL_SECONDS
    ):
        return cached_stats

    if not queue_root.exists():
        stats = (0, 0, None, 0, None)
        _SUBAGENT_TREE_CACHE.update({"loaded_at": now, "hours": hours, "root_mtime_ns": root_mtime_ns, "stats": stats})
        return stats

    current_timestamp = datetime.now(timezone.utc).timestamp()
    cutoff = current_timestamp - (hours * 3600)
    queue_depth = 0
    stale_count = 0
    archived_count = 0
    oldest_stale: float | None = None
    oldest_stale_path: Path | None = None
    try:
        for path in queue_root.rglob("*"):
            if not path.is_file():
                continue
            if "archive" in path.parts:
                archived_count += 1
                continue
            queue_depth += 1
            mtime = path.stat().st_mtime
            if mtime < cutoff:
                stale_count += 1
                if oldest_stale is None or mtime < oldest_stale:
                    oldest_stale = mtime
                    oldest_stale_path = path
    except Exception:
        stats = (0, 0, None, 0, None)
        _SUBAGENT_TREE_CACHE.update({"loaded_at": now, "hours": hours, "root_mtime_ns": root_mtime_ns, "stats": stats})
        return stats
    oldest_stale_age_hours = None
    if oldest_stale is not None:
        oldest_stale_age_hours = (current_timestamp - oldest_stale) / 3600
    stats = (queue_depth, stale_count, oldest_stale_age_hours, archived_count, oldest_stale_path)
    _SUBAGENT_TREE_CACHE.update({"loaded_at": now, "hours": hours, "root_mtime_ns": root_mtime_ns, "stats": stats})
    return stats


def scan_queue_stats(hours: int = 24) -> tuple[int, int, float | None]:
    queue_depth, stale_count, oldest_stale_age_hours, _, _ = scan_subagent_tree_stats(hours)
    return queue_depth, stale_count, oldest_stale_age_hours


def queue_tree_stats(hours: int = 24) -> tuple[int, int, float | None, int, Path | None]:
    return scan_subagent_tree_stats(hours)


def count_queue_depth() -> int:
    queue_depth, _, _, _, _ = queue_tree_stats()
    return queue_depth


def count_stale_queue_requests(hours: int = 24) -> int:
    _, stale_count, _, _, _ = queue_tree_stats(hours)
    return stale_count


def count_archived_requests() -> int:
    _, _, _, archived_count, _ = queue_tree_stats()
    return archived_count


def archive_stale_subagent_requests(hours: int = 24, dry_run: bool = False) -> dict[str, Any]:
    """Move subagent request files older than *hours* into state/subagents/archive/.

    Recursively scans all subdirectories (requests/, results/, etc.) so stale
    files nested below the queue root are also archived.  Previously only
    queue_root.iterdir() was used, which missed everything in subdirectories.

    Returns a summary dict with counts and the list of archived paths.
    """
    queue_root = STATE_DIR / "subagents"
    archive_dir = queue_root / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    current_timestamp = datetime.now(timezone.utc).timestamp()
    cutoff = current_timestamp - (hours * 3600)

    archived: list[Path] = []
    skipped: list[tuple[Path, str]] = []

    if not queue_root.exists():
        return {"archived": 0, "skipped": 0, "paths": [], "archive_dir": str(archive_dir)}

    for path in queue_root.rglob("*.json"):
        if not path.is_file():
            continue
        if "archive" in path.parts:
            continue
        mtime = path.stat().st_mtime
        if mtime < cutoff:
            # Preserve relative path structure in archive to avoid filename
            # collisions when files from different subdirectories share the same name.
            # e.g. requests/foo.json and results/foo.json → archive/requests/foo.json
            rel = path.relative_to(queue_root)
            dest = archive_dir / rel
            if dry_run:
                archived.append(path)
            else:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # Handle rare case where an identically-named file already
                    # exists in the archive (e.g. re-archiving after restore).
                    if dest.exists():
                        stem = dest.stem
                        suffix = 1
                        while dest.exists():
                            dest = dest.with_name(f"{stem}_{suffix}{dest.suffix}")
                            suffix += 1
                    path.rename(dest)
                    archived.append(dest)
                except Exception as exc:
                    skipped.append((path, str(exc)))

    return {
        "archived": len(archived),
        "skipped": len(skipped),
        "paths": [str(p) for p in archived],
        "skipped_details": [(str(p), e) for p, e in skipped],
        "archive_dir": str(archive_dir),
    }


def update_health_with_cleanup(archived_count: int) -> dict[str, Any]:
    """Update state/current_health.json with the latest cleanup metadata."""
    health_path = STATE_DIR / "current_health.json"
    health = load_json(health_path, {})
    health["last_subagent_cleanup_count"] = archived_count
    health["last_subagent_cleanup_timestamp"] = datetime.now(timezone.utc).isoformat()
    health_path.write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
    return health


def format_reward_trend(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent reward samples"
    return ", ".join(f"{cycle}={reward:.2f}" for cycle, reward in trend)


def format_reward_momentum(trend: list[tuple[str, float]]) -> str:
    if len(trend) < 2:
        return "insufficient samples"
    latest = trend[0][1]
    previous = trend[1][1]
    delta = latest - previous
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return f"{direction} {delta:+.2f} vs previous"


def format_reward_average(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent reward samples"
    average = sum(reward for _, reward in trend) / len(trend)
    return f"{average:.2f} avg over {len(trend)} sample(s)"


def format_reward_range(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent reward samples"
    rewards = [reward for _, reward in trend]
    return f"{min(rewards):.2f}–{max(rewards):.2f} range"


def compute_reward_distribution(rewards: list[tuple[str, float, str]]) -> dict[str, float]:
    """Compute reward distribution statistics (median, percentiles, std_dev) for the reward history.

    Uses the full reward history (up to 200 cycles) to compute:
    - count: number of samples
    - mean: arithmetic mean
    - median: 50th percentile
    - p10: 10th percentile (bottom 10%)
    - p95: 95th percentile (top 5%)
    - std_dev: population standard deviation
    - pass_rate: fraction of samples with result_status == "PASS"

    Returns a dict with all statistics.  Empty rewards returns zeros.
    """
    if not rewards:
        return {
            "count": 0,
            "mean": 0.0,
            "median": 0.0,
            "p10": 0.0,
            "p95": 0.0,
            "std_dev": 0.0,
            "pass_rate": 0.0,
        }

    values = [r for _, r, _ in rewards]
    n = len(values)
    sorted_values = sorted(values)

    # Mean
    mean = sum(values) / n

    # Median (50th percentile)
    mid = n // 2
    if n % 2 == 0:
        median = (sorted_values[mid - 1] + sorted_values[mid]) / 2.0
    else:
        median = sorted_values[mid]

    # Percentiles using nearest-rank method
    def percentile(data: list[float], p: float) -> float:
        k = (p / 100.0) * (len(data) - 1)
        f = int(k)
        c = f + 1 if f + 1 < len(data) else f
        d = k - f
        return data[f] + d * (data[c] - data[f])

    p10 = percentile(sorted_values, 10)
    p95 = percentile(sorted_values, 95)

    # Population standard deviation
    variance = sum((v - mean) ** 2 for v in values) / n
    std_dev = variance ** 0.5

    # Pass rate
    pass_count = sum(1 for _, _, status in rewards if status == "PASS")
    pass_rate = pass_count / n

    return {
        "count": n,
        "mean": round(mean, 4),
        "median": round(median, 4),
        "p10": round(p10, 4),
        "p95": round(p95, 4),
        "std_dev": round(std_dev, 4),
        "pass_rate": round(pass_rate, 4),
    }


def format_reward_distribution(dist: dict[str, float]) -> str:
    """Format reward distribution stats for CLI display."""
    if dist["count"] == 0:
        return "no reward data"
    return (
        f"n={dist['count']} mean={dist['mean']:.2f} median={dist['median']:.2f} "
        f"p10={dist['p10']:.2f} p95={dist['p95']:.2f} σ={dist['std_dev']:.3f} "
        f"pass={dist['pass_rate']:.0%}"
    )


def format_reward_trend_html(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "<em>No recent cycles</em>"
    badges: list[str] = []
    for cycle, reward in trend:
        color = "#2ecc71" if reward >= 1.2 else ("#3498db" if reward >= 1.0 else "#e74c3c")
        badges.append(
            f"""
        <div class=\"trend-badge\" style=\"background: {color};\">
            <strong>{html.escape(cycle)}</strong>: {reward:.2f}
        </div>
        """
        )
    return "".join(badges)


def format_queue_state(queue_depth: int, stale_count: int) -> str:
    if queue_depth <= 0 and stale_count <= 0:
        return "queue idle"
    if stale_count > 0:
        return f"{stale_count}/{queue_depth} stale"
    return f"{queue_depth} pending"


def format_operator_attention(m: dict[str, Any]) -> str:
    queue_part = format_queue_state(m["queue_depth"], m["stale_queue_requests"])
    gate = m["approval_gate_state"]
    momentum = m["reward_momentum"]
    return f"{queue_part} · gate={gate} · momentum={momentum}"


def format_dashboard_summary(m: dict[str, Any]) -> str:
    queue_part = format_queue_state(m["queue_depth"], m["stale_queue_requests"])
    archived = m.get("archived_count", 0)
    cleanup_count = m.get("last_cleanup_count", "unknown")
    cleanup_recency = m.get("last_cleanup_recency", "unknown")
    cleanup_status = m.get("last_cleanup_status", "unknown")
    queue_hygiene = m.get("queue_hygiene", "unknown")
    queue_priority = m.get("queue_priority", "unknown")
    archive_target = m.get("queue_archive_target", "none")
    cleanup_summary = f"{cleanup_count} cleaned" if cleanup_count != "unknown" else "cleanup unknown"
    return (
        f"{queue_part} · host={m['host_capability_coverage']} · probe={m.get('host_capability_probe', m.get('host_capability_probe_age', 'unknown'))} · "
        f"cleanup={cleanup_summary}, {cleanup_recency}/{cleanup_status} · hygiene={queue_hygiene} · "
        f"priority={queue_priority} · archive={archive_target} · archived={archived} · gate={m['approval_gate_state']}"
    )


def format_focus_line(m: dict[str, Any]) -> str:
    return (
        f"goal={m['goal']} · task={m['active_task']} · "
        f"queue={m['queue_depth']}/{m['stale_queue_requests']} stale · gate={m['approval_gate_state']} · "
        f"momentum={m['reward_momentum']}"
    )


def format_queue_snapshot(
    queue_depth: int,
    stale_count: int,
    archived_count: int,
    approval_gate_state: str,
    last_cleanup_recency: str,
) -> str:
    queue_part = format_queue_state(queue_depth, stale_count)
    return (
        f"{queue_part} · archived={archived_count} · cleanup={last_cleanup_recency} · "
        f"gate={approval_gate_state}"
    )


def format_artifact_freshness(materialized_recency: str, latest_report_recency: str) -> str:
    return f"materialized={materialized_recency}, report={latest_report_recency}"


def format_queue_hygiene(
    queue_depth: int,
    stale_count: int,
    last_cleanup_recency: str,
    last_cleanup_status: str,
) -> str:
    queue_freshness = format_queue_freshness(queue_depth, stale_count)
    return f"{queue_freshness} · cleanup={last_cleanup_recency}/{last_cleanup_status}"


def format_queue_health(health: dict[str, Any]) -> str:
    cleanup_count = health.get("last_subagent_cleanup_count", "unknown")
    cleanup_ts = health.get("last_subagent_cleanup_timestamp", "unknown")
    return f"last cleanup {cleanup_count} @ {cleanup_ts}"


def _cleanup_age_hours(last_cleanup_timestamp: Any) -> float | None:
    if not last_cleanup_timestamp or str(last_cleanup_timestamp).strip().lower() == "unknown":
        return None
    try:
        timestamp = str(last_cleanup_timestamp).replace("Z", "+00:00")
        dt = datetime.fromisoformat(timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def format_cleanup_recency_from_age(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 0:
        return "future"
    if age_hours < 1:
        return f"{age_hours * 60:.0f}m ago"
    return f"{age_hours:.1f}h ago"


def format_cleanup_recency(last_cleanup_timestamp: Any) -> str:
    return format_cleanup_recency_from_age(_cleanup_age_hours(last_cleanup_timestamp))


def format_cleanup_status_from_age(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 0:
        return "future"
    if age_hours >= 24:
        return "stale"
    return "fresh"


def format_cleanup_status(last_cleanup_timestamp: Any) -> str:
    return format_cleanup_status_from_age(_cleanup_age_hours(last_cleanup_timestamp))


def format_queue_pressure(queue_depth: int, stale_count: int, oldest_stale_age_hours: float | None) -> str:
    if queue_depth <= 0:
        return "idle"
    if stale_count <= 0:
        return f"{queue_depth} pending, no stale requests"
    oldest_age = format_oldest_stale_request_age(oldest_stale_age_hours)
    stale_ratio = stale_count / queue_depth
    return f"{stale_count}/{queue_depth} stale ({stale_ratio:.0%}), oldest {oldest_age}"


def format_queue_freshness(queue_depth: int, stale_count: int) -> str:
    if queue_depth <= 0:
        return "idle"
    stale_ratio = stale_count / queue_depth
    return f"{stale_count}/{queue_depth} stale ({stale_ratio:.0%})"


def format_stale_request_reference(
    oldest_stale_age_hours: float | None,
    oldest_stale_request_path: Path | None,
) -> tuple[str, str]:
    age_text = format_oldest_stale_request_age(oldest_stale_age_hours)
    # Public dashboard surfaces retain age but never expose host filesystem paths.
    return age_text, "path-redacted" if oldest_stale_request_path is not None else "none"



def _redact_queue_path(value: Any) -> str:
    """Replace a queue filesystem path while retaining action and age text."""
    text = str(value)
    if " @ /" in text:
        return text.split(" @ /", 1)[0] + " @ path-redacted"
    if text.startswith("/"):
        return "path-redacted" + (text[text.find(" ("):] if " (" in text else "")
    return text


def format_queue_action(
    queue_depth: int,
    stale_count: int,
    stale_request_reference: tuple[str, str],
    last_cleanup_recency: str,
) -> str:
    if queue_depth <= 0:
        return "no queue work pending"
    if stale_count > 0:
        age_text, path_text = stale_request_reference
        return f"archive {stale_count} stale request(s) — oldest {age_text} @ {path_text}"
    if queue_depth >= 20:
        cleanup_text = last_cleanup_recency if last_cleanup_recency else "unknown"
        return f"watch queue pressure — last cleanup {cleanup_text}"
    return "queue healthy; no archive action needed"



def format_queue_archive_target(
    stale_count: int,
    stale_request_reference: tuple[str, str],
) -> str:
    if stale_count <= 0:
        return "none"
    age_text, path_text = stale_request_reference
    return f"{path_text} ({age_text})"



def format_queue_priority(queue_depth: int, stale_count: int, oldest_stale_age_hours: float | None) -> str:
    if queue_depth <= 0:
        return "idle"
    if stale_count > 0:
        if oldest_stale_age_hours is None:
            return "urgent"
        if oldest_stale_age_hours >= 48:
            return "urgent"
        if oldest_stale_age_hours >= 24:
            return "elevated"
        return "watch"
    if queue_depth >= 20:
        return "elevated"
    if queue_depth >= 5:
        return "watch"
    return "normal"



def format_oldest_stale_request_age(oldest_stale_age_hours: float | None) -> str:
    if oldest_stale_age_hours is None:
        return "unknown"
    return f"{oldest_stale_age_hours:.1f}h"


def format_oldest_stale_request_path(oldest_stale_request_path: Path | None) -> str:
    if oldest_stale_request_path is None:
        return "none"
    return "path-redacted"


def format_age_hours(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    return f"{age_hours:.1f}h"


def format_probe_status_from_age(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 0:
        return "future"
    if age_hours >= 24:
        return "stale"
    return "fresh"


def format_host_capability_probe(age_text: str, status: str) -> str:
    if age_text == "unknown" and status == "unknown":
        return "unknown"
    return f"{age_text} ({status})"


def format_host_capability_probe_attention(age_hours: float | None, status: str) -> str:
    if age_hours is None or status == "unknown":
        return "probe unknown"
    if status == "future":
        return "probe timestamp in future"
    if status == "stale":
        return "re-scan host hardware now"
    if age_hours >= 12:
        return "re-scan host hardware soon"
    if age_hours >= 1:
        return "host probe aging"
    return "host probe current"


def format_refresh_timestamp(timestamp: Any) -> str:
    if timestamp is None:
        return "unknown"
    text = str(timestamp).strip()
    return text or "unknown"


def file_age_hours(path: Path | None, now_utc: datetime | None = None) -> float | None:
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
        ref = now_utc or datetime.now(timezone.utc)
        return (ref - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def batch_file_age_hours(paths: list[Path | None], now_utc: datetime | None = None) -> list[float | None]:
    """Compute age-in-hours for multiple paths using a single clock reading.

    Avoids redundant datetime.now(timezone.utc) calls when several file ages
    are needed in the same metrics collection pass.
    """
    ref = now_utc or datetime.now(timezone.utc)
    results: list[float | None] = []
    for path in paths:
        if path is None:
            results.append(None)
            continue
        try:
            mtime = path.stat().st_mtime
            results.append((ref - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds() / 3600)
        except Exception:
            results.append(None)
    return results


def format_file_recency(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 1:
        return f"{age_hours * 60:.0f}m ago"
    return f"{age_hours:.1f}h ago"


def artifact_status(age_hours: float | None, source_status: str = "valid") -> str:
    """Classify a retired artifact without treating it as authoritative state."""
    if source_status != "valid":
        return source_status
    if age_hours is None:
        return "unavailable"
    return "stale" if age_hours >= ARTIFACT_STALE_AFTER_HOURS else "fresh"


def artifact_metadata(age_hours: float | None, source_status: str = "valid") -> dict[str, Any]:
    """Return bounded source metadata without payloads or filesystem paths."""
    return {
        "status": artifact_status(age_hours, source_status),
        "age_hours": round(max(0.0, age_hours), 1) if age_hours is not None else None,
        "authoritative": False,
        "context_only": True,
    }


def select_artifact_source(
    materialized_path: Path | None,
    materialized: dict[str, Any],
    report_path: Path | None,
    report: dict[str, Any],
) -> tuple[dict[str, Any], Path | None, str]:
    """Select by source presence, preserving empty/malformed provenance.

    A present materialized file is authoritative for source selection even when
    its decoded value is ``{}`` after a malformed or valid-empty read. Truthiness
    would silently substitute the report and erase that source state.
    """
    if materialized_path is not None:
        return materialized, materialized_path, "materialized"
    if report_path is not None:
        return report, report_path, "report"
    return {}, None, "none"


def format_artifact_value(value: Any, age_hours: float | None, source_status: str = "valid") -> str:
    """Expose bounded status/age metadata, never raw stale values or paths."""
    metadata = artifact_metadata(age_hours, source_status)
    age = metadata["age_hours"]
    age_text = f"; age={age:.1f}h" if age is not None else ""
    return f"{metadata['status']}{age_text} (context-only artifact)"


def _context_only_label(metadata: dict[str, Any] | None) -> str:
    """Format bounded metadata for a non-authoritative source."""
    metadata = metadata or {}
    status = str(metadata.get("status") or "unavailable")
    age = metadata.get("age_hours")
    age_text = f"; age={float(age):.1f}h" if isinstance(age, (int, float)) else ""
    return f"{status}{age_text} (context-only artifact)"


def _source_is_fresh(metadata: Any) -> bool:
    return isinstance(metadata, dict) and metadata.get("status") == "fresh"


def sanitize_public_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Remove stale payloads and paths while retaining bounded source metadata."""
    sanitized = dict(metrics)
    sanitized["materialized_path"] = None
    sanitized["latest_report_path"] = None
    sanitized["oldest_stale_request_path"] = None
    sanitized["oldest_stale_request_path_text"] = "path-redacted" if metrics.get("oldest_stale_request_path") else "none"
    sanitized["queue_action"] = _redact_queue_path(sanitized.get("queue_action", ""))
    sanitized["queue_archive_target"] = _redact_queue_path(sanitized.get("queue_archive_target", ""))

    for field, source_key in (
        ("goal", "goal_source"),
        ("active_task", "active_task_source"),
        ("approval_gate_state", "approval_gate_source"),
    ):
        metadata = sanitized.get(source_key)
        if not _source_is_fresh(metadata):
            sanitized[field] = _context_only_label(metadata)

    materialized_source = sanitized.get("materialized_source")
    materialized_label = _context_only_label(materialized_source)
    for field in (
        "materialized_cycle",
        "materialized_status",
        "concrete_statement",
        "goal_artifact_signature",
        "next_bounded_candidate",
    ):
        sanitized[field] = materialized_label

    report_source = sanitized.get("reward_source")
    report_label = _context_only_label(report_source)
    sanitized["latest_report_status"] = report_label
    # Report reward payloads are never authoritative on this dashboard, even
    # when their mtime is recent. Expose only bounded source metadata.
    for field in ("reward_average", "reward_momentum", "reward_range"):
        sanitized[field] = report_label
    sanitized["reward_trend"] = []
    sanitized["sparkline_rewards"] = []
    sanitized["reward_distribution"] = {"count": 0}
    sanitized["recent_cycles"] = "no recent reward samples"

    # Rebuild every aggregate that embeds protected fields. Renderers may be
    # called directly with a raw snapshot, so sanitizing only the leaf fields
    # is insufficient: derived strings would otherwise preserve stale payloads.
    sanitized["operator_attention"] = format_operator_attention({
        "queue_depth": sanitized.get("queue_depth", 0),
        "stale_queue_requests": sanitized.get("stale_queue_requests", 0),
        "approval_gate_state": sanitized.get("approval_gate_state", "unavailable"),
        "reward_momentum": sanitized.get("reward_momentum", "unavailable"),
    })
    sanitized["focus_line"] = format_focus_line({
        "goal": sanitized.get("goal", "unavailable"),
        "active_task": sanitized.get("active_task", "unavailable"),
        "queue_depth": sanitized.get("queue_depth", 0),
        "stale_queue_requests": sanitized.get("stale_queue_requests", 0),
        "approval_gate_state": sanitized.get("approval_gate_state", "unavailable"),
        "reward_momentum": sanitized.get("reward_momentum", "unavailable"),
    })
    sanitized["dashboard_summary"] = format_dashboard_summary({
        "queue_depth": sanitized.get("queue_depth", 0),
        "stale_queue_requests": sanitized.get("stale_queue_requests", 0),
        "archived_count": sanitized.get("archived_count", 0),
        "approval_gate_state": sanitized.get("approval_gate_state", "unavailable"),
        "host_capability_coverage": sanitized.get("host_capability_coverage", "unknown"),
        "host_capability_probe": sanitized.get("host_capability_probe", "unknown"),
        "last_cleanup_count": sanitized.get("last_cleanup_count", "unknown"),
        "last_cleanup_recency": sanitized.get("last_cleanup_recency", "unknown"),
        "last_cleanup_status": sanitized.get("last_cleanup_status", "unknown"),
        "queue_hygiene": sanitized.get("queue_hygiene", "unknown"),
        "queue_priority": sanitized.get("queue_priority", "unknown"),
        "queue_archive_target": sanitized.get("queue_archive_target", "none"),
    })
    sanitized["queue_snapshot"] = format_queue_snapshot(
        sanitized.get("queue_depth", 0),
        sanitized.get("stale_queue_requests", 0),
        sanitized.get("archived_count", 0),
        sanitized.get("approval_gate_state", "unavailable"),
        sanitized.get("last_cleanup_recency", "unknown"),
    )
    return sanitized


def format_recent_cycles(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent cycles"
    return ", ".join(cycle for cycle, _ in trend)


def format_materialized_cycle(materialized: dict[str, Any]) -> str:
    return "context-only artifact" if materialized else "unavailable artifact"


def format_materialized_status(materialized: dict[str, Any]) -> str:
    """Return bounded status metadata, not materialized payload text."""
    return "context-only artifact" if materialized else "unavailable artifact"


def format_next_candidate(materialized: dict[str, Any]) -> str:
    """Do not expose stale candidate titles or identifiers."""
    return "context-only artifact" if materialized else "unavailable artifact"


def format_goal_artifact_signature(materialized: dict[str, Any]) -> str:
    """Do not expose stale payload signatures."""
    return "context-only artifact" if materialized else "unavailable artifact"


def _extract_report_evidence(report: dict[str, Any]) -> str:
    for key in ("evidence_id", "evidence_ref_id"):
        value = report.get(key)
        if value:
            return str(value).strip()

    evidence = report.get("evidence")
    if isinstance(evidence, dict):
        for key in ("evidence_id", "id", "ref_id", "ref", "path", "value"):
            value = evidence.get(key)
            if value:
                return str(value).strip()
    elif evidence is not None:
        text = str(evidence).strip()
        if text:
            return text
    return ""


def format_latest_report_status(report: dict[str, Any]) -> str:
    """Do not expose stale report status, evidence IDs, or payload text."""
    return "context-only artifact" if report else "unavailable artifact"


def format_concrete_statement(materialized: dict[str, Any]) -> str:
    """Do not expose stale materialized payload text."""
    return "context-only artifact" if materialized else "unavailable artifact"


def _load_host_capabilities_uncached() -> dict[str, Any]:
    return load_json(STATE_DIR / "host_capabilities.json", {})


def load_host_capabilities() -> dict[str, Any]:
    now = time.monotonic()
    cached = _HOST_CAPS_CACHE.get("host_caps")
    loaded_at = float(_HOST_CAPS_CACHE.get("loaded_at", 0.0) or 0.0)
    if cached is not None and now - loaded_at < HOST_CAPS_CACHE_TTL_SECONDS:
        return cached
    host_caps = _load_host_capabilities_uncached()
    _HOST_CAPS_CACHE["host_caps"] = host_caps
    _HOST_CAPS_CACHE["loaded_at"] = now
    return host_caps


def scan_host_capabilities(
    host_caps: dict[str, Any],
) -> tuple[list[str], list[tuple[str, str]], str, list[tuple[str, str]], str, str]:
    focus_order = ("camera", "bluetooth", "wifi", "microphone")
    if not host_caps:
        unavailable = "host hardware status unavailable"
        return [], [], unavailable, [], unavailable, unavailable

    available_caps: list[str] = []
    capability_details: list[tuple[str, str]] = []
    focus_status_parts: list[str] = []
    focus_details: list[tuple[str, str]] = []
    missing_focus_devices: list[str] = []

    for name, info in host_caps.items():
        if not isinstance(info, dict):
            continue
        available = bool(info.get("available"))
        details = str(info.get("details", "")).strip() or "available"
        if available:
            available_caps.append(name)
            capability_details.append((name, details))

    available_caps.sort()
    capability_details.sort(key=lambda item: item[0])

    for name in focus_order:
        info = host_caps.get(name, {})
        if not isinstance(info, dict):
            info = {}
        available = bool(info.get("available"))
        details = str(info.get("details", "")).strip() or "available"
        focus_status_parts.append(f"{name}:{'✓' if available else '✗'}")
        focus_details.append((name, details))
        if not available:
            missing_focus_devices.append(name)

    available_count = len(focus_order) - len(missing_focus_devices)
    focus_status = ", ".join(focus_status_parts)
    coverage = f"{available_count}/{len(focus_order)} focus devices available"
    missing = "none" if not missing_focus_devices else ", ".join(missing_focus_devices)
    return available_caps, capability_details, focus_status, focus_details, coverage, missing


def format_host_capability_badges_html(host_capabilities: list[str]) -> str:
    if not host_capabilities:
        return "<em>None detected</em>"
    return "".join(f'<span class="cap-tag">{html.escape(capability)}</span>' for capability in host_capabilities)


def format_host_capability_details_html(details: list[tuple[str, str]]) -> str:
    detail_items = "".join(
        f'<li><strong>{html.escape(name)}</strong>: {html.escape(text or "available")}</li>'
        for name, text in details
    )
    if detail_items:
        return f"<ul class='cap-list'>{detail_items}</ul>"
    return "<em>No capability details available</em>"


def escape_html_text(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def collect_metrics_uncached() -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    captured_at = now_utc.isoformat()
    health = load_json(STATE_DIR / "current_health.json", {})
    materialized_path, materialized = load_latest_materialized()
    latest_report_path, latest_report, recent_rewards = scan_report_artifacts()
    report_source_status = source_status_for_artifact(
        latest_report_path, REPORTS_DIR, "evolution-*.json"
    )
    materialized_source_status = source_status_for_artifact(
        materialized_path, IMPROVEMENT_DIR, "materialized-cycle-*.json"
    )
    selected_source, selected_path, selected_kind = select_artifact_source(
        materialized_path, materialized, latest_report_path, latest_report
    )
    selected_source_status = (
        materialized_source_status if selected_kind == "materialized" else report_source_status
    )
    reward_momentum = format_reward_momentum(recent_rewards)
    reward_average = format_reward_average(recent_rewards)
    reward_range = format_reward_range(recent_rewards)
    host_caps = load_host_capabilities()
    # Batch all file-age computations into a single clock reading
    _probe_age, _materialized_age, _report_age = batch_file_age_hours(
        [STATE_DIR / "host_capabilities.json", materialized_path, latest_report_path],
        now_utc,
    )
    host_capability_probe_age_hours = _probe_age
    host_capability_probe_age = format_age_hours(host_capability_probe_age_hours)
    host_capability_probe_status = format_probe_status_from_age(host_capability_probe_age_hours)
    host_capability_probe = format_host_capability_probe(host_capability_probe_age, host_capability_probe_status)
    host_capability_probe_attention = format_host_capability_probe_attention(
        host_capability_probe_age_hours,
        host_capability_probe_status,
    )

    goal_value = selected_source.get("goal_id", "unknown")
    active_task_value = selected_source.get("feedback_decision", {}).get(
        "selected_task_label",
        selected_source.get("task_id", selected_source.get("current_task_id", "unknown")),
    )
    approval_gate_value = selected_source.get("feedback_decision", {}).get("mode", "unknown")
    # These values come from the retired report/materialized writers. Keep them
    # visible for context, but never present them as current healthy state.
    selected_age = _materialized_age if selected_kind == "materialized" else _report_age
    selected_metadata = artifact_metadata(selected_age, selected_source_status)
    goal = format_artifact_value(goal_value, selected_age, selected_source_status)
    active_task = format_artifact_value(active_task_value, selected_age, selected_source_status)
    approval_gate_state = format_artifact_value(approval_gate_value, selected_age, selected_source_status)

    (
        available_caps,
        capability_details,
        host_focus_status,
        host_focus_details,
        host_capability_coverage,
        host_focus_missing,
    ) = scan_host_capabilities(host_caps)
    host_focus_name_set = {name for name, _ in host_focus_details}

    queue_depth, stale_queue_requests, oldest_stale_age_hours, archived_count, oldest_stale_request_path = scan_subagent_tree_stats()

    last_cleanup_count = health.get("last_subagent_cleanup_count", "unknown")
    last_cleanup_timestamp = health.get("last_subagent_cleanup_timestamp", "unknown")
    last_cleanup_age_hours = _cleanup_age_hours(last_cleanup_timestamp)
    last_cleanup_recency = format_cleanup_recency_from_age(last_cleanup_age_hours)
    last_cleanup_status = format_cleanup_status_from_age(last_cleanup_age_hours)
    stale_request_reference = format_stale_request_reference(
        oldest_stale_age_hours,
        oldest_stale_request_path,
    )
    oldest_stale_request_path_text = stale_request_reference[1]
    queue_action = format_queue_action(
        queue_depth,
        stale_queue_requests,
        stale_request_reference,
        last_cleanup_recency,
    )
    queue_archive_target = format_queue_archive_target(
        stale_queue_requests,
        stale_request_reference,
    )
    queue_priority = format_queue_priority(queue_depth, stale_queue_requests, oldest_stale_age_hours)

    queue_pressure = format_queue_pressure(queue_depth, stale_queue_requests, oldest_stale_age_hours)
    queue_hygiene = format_queue_hygiene(
        queue_depth,
        stale_queue_requests,
        last_cleanup_recency,
        last_cleanup_status,
    )
    # Reuse ages already computed by batch_file_age_hours above (lines 901-904)
    # instead of calling file_age_hours() again which would do redundant stat() + datetime.now() calls.
    materialized_age_hours = _materialized_age
    latest_report_age_hours = _report_age

    artifact_freshness = format_artifact_freshness(
        format_file_recency(materialized_age_hours),
        format_file_recency(latest_report_age_hours),
    )

    dashboard_summary = format_dashboard_summary({
        "queue_depth": queue_depth,
        "stale_queue_requests": stale_queue_requests,
        "archived_count": archived_count,
        "approval_gate_state": approval_gate_state,
        "host_capability_coverage": host_capability_coverage,
        "host_capability_probe": host_capability_probe,
        "last_cleanup_count": last_cleanup_count,
        "last_cleanup_recency": last_cleanup_recency,
        "last_cleanup_status": last_cleanup_status,
        "queue_hygiene": queue_hygiene,
        "queue_priority": queue_priority,
        "queue_archive_target": queue_archive_target,
    })
    queue_snapshot = format_queue_snapshot(
        queue_depth,
        stale_queue_requests,
        archived_count,
        approval_gate_state,
        last_cleanup_recency,
    )

    # Precompute sparkline reward data (last 60 cycles) so render_tui() can
    # reuse it from the metrics cache instead of rescanning the filesystem.
    sparkline_rewards = scan_all_report_rewards(limit=60)

    # Compute reward distribution statistics from the sparkline data
    reward_distribution = compute_reward_distribution(sparkline_rewards)

    # Real-time system metrics (no subprocess overhead — reads /proc directly)
    cpu_load = get_cpu_load()
    mem_pct = get_memory_usage_pct()
    disk_pct = get_disk_usage_pct()

    return sanitize_public_metrics({
        "captured_at": captured_at,
        "goal": goal,
        "goal_source": dict(selected_metadata),
        "active_task": active_task,
        "active_task_source": dict(selected_metadata),
        "recent_cycles": format_recent_cycles(recent_rewards),
        "reward_trend": recent_rewards,
        "reward_momentum": reward_momentum,
        "reward_average": reward_average,
        "reward_range": reward_range,
        "sparkline_rewards": sparkline_rewards,
        "reward_distribution": reward_distribution,
        "reward_source": artifact_metadata(latest_report_age_hours, report_source_status),
        "materialized_source": artifact_metadata(materialized_age_hours, materialized_source_status),
        "operator_attention": format_operator_attention({
            "queue_depth": queue_depth,
            "stale_queue_requests": stale_queue_requests,
            "approval_gate_state": approval_gate_state,
            "reward_momentum": reward_momentum,
        }),
        "host_capability_badges_html": format_host_capability_badges_html(available_caps),
        "host_capability_details_html": format_host_capability_details_html(capability_details),
        "dashboard_summary": dashboard_summary,
        "focus_line": format_focus_line({
            "goal": goal,
            "active_task": active_task,
            "queue_depth": queue_depth,
            "stale_queue_requests": stale_queue_requests,
            "approval_gate_state": approval_gate_state,
            "reward_momentum": reward_momentum,
        }),
        "queue_snapshot": queue_snapshot,
        "materialized_cycle": format_materialized_cycle(materialized),
        "queue_depth": queue_depth,
        "stale_queue_requests": stale_queue_requests,
        "queue_freshness": format_queue_freshness(queue_depth, stale_queue_requests),
        "queue_pressure": queue_pressure,
        "queue_action": queue_action,
        "queue_archive_target": queue_archive_target,
        "queue_priority": queue_priority,
        "queue_hygiene": queue_hygiene,
        "oldest_stale_age_hours": oldest_stale_age_hours,
        "oldest_stale_request_age": format_oldest_stale_request_age(oldest_stale_age_hours),
        "oldest_stale_request_path": oldest_stale_request_path,
        "oldest_stale_request_path_text": oldest_stale_request_path_text,
        "archived_count": archived_count,
        "approval_gate_state": approval_gate_state,
        "approval_gate_source": dict(selected_metadata),
        "materialized_status": format_materialized_status(materialized),
        "concrete_statement": format_concrete_statement(materialized),
        "goal_artifact_signature": format_goal_artifact_signature(materialized),
        "latest_report_status": format_latest_report_status(latest_report),
        "next_bounded_candidate": format_next_candidate(materialized),
        "materialized_path": str(materialized_path) if materialized_path else None,
        "materialized_recency": format_file_recency(materialized_age_hours),
        "materialized_artifact_status": artifact_status(
            materialized_age_hours, materialized_source_status
        ),
        "materialized_source_status": materialized_source_status,
        "latest_report_path": str(latest_report_path) if latest_report_path else None,
        "latest_report_recency": format_file_recency(latest_report_age_hours),
        "latest_report_artifact_status": artifact_status(
            latest_report_age_hours, report_source_status
        ),
        "report_source_status": report_source_status,
        "artifact_freshness": artifact_freshness,
        "last_cleanup_count": last_cleanup_count,
        "last_cleanup_timestamp": last_cleanup_timestamp,
        "queue_health": format_queue_health(health),
        "last_cleanup_recency": last_cleanup_recency,
        "last_cleanup_status": last_cleanup_status,
        "host_capabilities": available_caps,
        "host_capability_details": capability_details,
        "host_focus_status": host_focus_status,
        "host_focus_details": host_focus_details,
        "host_focus_names": sorted(host_focus_name_set),
        "host_focus_name_set": host_focus_name_set,
        "host_capability_coverage": host_capability_coverage,
        "host_focus_missing": host_focus_missing,
        "host_capability_probe_age_hours": host_capability_probe_age_hours,
        "host_capability_probe_age": host_capability_probe_age,
        "host_capability_probe_status": host_capability_probe_status,
        "host_capability_probe": host_capability_probe,
        "host_capability_probe_attention": host_capability_probe_attention,
        # Real-time system metrics (Vector 1: self-optimization monitoring)
        "cpu_load": cpu_load,
        "mem_pct": mem_pct,
        "disk_pct": disk_pct,
    })


def collect_metrics() -> dict[str, Any]:
    now = time.monotonic()
    cached_metrics = _METRICS_CACHE.get("metrics")
    loaded_at = float(_METRICS_CACHE.get("loaded_at", 0.0) or 0.0)
    if cached_metrics is not None and now - loaded_at < METRICS_CACHE_TTL_SECONDS:
        return cached_metrics
    metrics = collect_metrics_uncached()
    _METRICS_CACHE["metrics"] = metrics
    _METRICS_CACHE["loaded_at"] = now
    return metrics


def serialize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    serializable = sanitize_public_metrics(metrics)
    value = serializable.get("oldest_stale_request_path")
    serializable["oldest_stale_request_path"] = str(value) if value else None
    host_focus_name_set = serializable.get("host_focus_name_set")
    if isinstance(host_focus_name_set, set):
        serializable["host_focus_name_set"] = sorted(host_focus_name_set)
    return serializable


def render_json(metrics: dict[str, Any]) -> str:
    return json.dumps(serialize_metrics(metrics), indent=2, sort_keys=True)


def write_snapshot(metrics: dict[str, Any], destination: Path | None = None) -> Path:
    metrics = sanitize_public_metrics(metrics)
    destination = destination or Path(f"/tmp/eeebot-dashboard-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.txt")
    snapshot_lines = [
        "EeeBot Dashboard Snapshot",
        f"Captured At: {metrics['captured_at']}",
        f"Goal: {metrics['goal']}",
        f"Active Task: {metrics['active_task']}",
        f"Queue Snapshot: {metrics['queue_snapshot']}",
        f"Recent Cycles: {metrics['recent_cycles']}",
        f"Reward Trend: {format_reward_trend(metrics['reward_trend'])}",
        f"Reward Average: {metrics['reward_average']}",
        f"Reward Range: {metrics['reward_range']}",
        f"Queue Pressure: {metrics['queue_pressure']}",
        f"Queue Hygiene: {metrics['queue_hygiene']}",
        f"Queue Action: {metrics['queue_action']}",
        f"Queue Archive Target: {metrics['queue_archive_target']}",
        f"Last Cleanup Recency: {metrics['last_cleanup_recency']}",
        f"Last Cleanup Status: {metrics['last_cleanup_status']}",
        f"Approval Gate State: {metrics['approval_gate_state']}",
        f"Materialized Improvement: {metrics['materialized_status']}",
        f"Concrete Statement: {metrics['concrete_statement']}",
        f"Latest Report Status: {metrics['latest_report_status']}",
        f"Artifact Freshness: {metrics['artifact_freshness']}",
        f"Host Focus: {metrics['host_focus_status']}",
        f"Host Capability Coverage: {metrics['host_capability_coverage']}",
        f"Host Capability Probe: {metrics['host_capability_probe']}",
        f"Missing Focus Devices: {metrics['host_focus_missing']}",
        f"CPU Load (1m): {metrics.get('cpu_load', 0.0):.2f}",
        f"Memory Usage: {metrics.get('mem_pct', 0.0):.1f}%",
        f"Disk Usage: {metrics.get('disk_pct', 0.0):.1f}%",
    ]
    destination.write_text("\n".join(snapshot_lines) + "\n", encoding="utf-8")
    return destination


def _build_health_dimensions(m: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Build health dimension tuples (dimension, status, detail) reused by render_health and render_health_json."""
    dims: list[tuple[str, str, str]] = []

    # Queue health
    queue_depth = m["queue_depth"]
    stale_count = m["stale_queue_requests"]
    if queue_depth == 0 and stale_count == 0:
        dims.append(("queue", "OK", "idle"))
    elif stale_count > 0:
        dims.append(("queue", "WARN" if stale_count < 10 else "CRIT", f"{stale_count}/{queue_depth} stale"))
    elif queue_depth >= 20:
        dims.append(("queue", "WARN", f"{queue_depth} pending"))
    else:
        dims.append(("queue", "OK", f"{queue_depth} pending"))

    # Cleanup health
    cleanup_status = m["last_cleanup_status"]
    cleanup_recency = m["last_cleanup_recency"]
    cleanup_health = "OK" if cleanup_status == "fresh" else "WARN"
    dims.append(("cleanup", cleanup_health, cleanup_recency))

    # Host probe health
    probe_attention = m["host_capability_probe_attention"]
    if "current" in probe_attention:
        probe_health = "OK"
    elif "re-scan" in probe_attention:
        probe_health = "WARN"
    elif "aging" in probe_attention:
        probe_health = "OK"
    else:
        probe_health = "WARN"
    dims.append(("host_probe", probe_health, m["host_capability_probe"]))

    # Host coverage
    missing = m["host_focus_missing"]
    coverage_health = "OK" if missing == "none" else "WARN"
    dims.append(("host_coverage", coverage_health, f"{m['host_capability_coverage']} (missing: {missing})"))

    # Reward health: the only reward source is a retired/frozen report family.
    reward_avg = m["reward_average"]
    reward_momentum = m["reward_momentum"]
    report_status = m.get("latest_report_artifact_status", "unavailable")
    reward_health = "OK" if report_status == "fresh" and "no recent" not in reward_avg else "WARN"
    # `sanitize_public_metrics` replaces average, momentum and range with one
    # shared source label when the report family is not authoritative, so the
    # parenthetical would nest that label inside itself:
    #   "stale; age=302.5h (context-only artifact) (stale; age=302.5h (...))"
    # Momentum is only worth printing when it says something the average does not.
    reward_detail = (
        f"{reward_avg}; source={report_status}"
        if reward_momentum == reward_avg
        else f"{reward_avg} ({reward_momentum}); source={report_status}"
    )
    dims.append(("reward", reward_health, reward_detail))

    # Gate health: materialized snapshots have no live writer in this dashboard.
    gate = m["approval_gate_state"]
    materialized_artifact_status = m.get("materialized_artifact_status", "unavailable")
    gate_health = "OK" if materialized_artifact_status == "fresh" else "WARN"
    dims.append(("gate", gate_health, f"{gate}; source={materialized_artifact_status}"))

    # CPU load health (Vector 1: self-optimization monitoring)
    cpu_load = m.get("cpu_load", 0.0)
    cpu_health = "OK" if cpu_load < 1.0 else ("WARN" if cpu_load < 2.0 else "CRIT")
    dims.append(("cpu", cpu_health, f"load={cpu_load:.2f}"))

    # Memory health (Vector 1: self-optimization monitoring)
    mem_pct = m.get("mem_pct", 0.0)
    mem_health = "OK" if mem_pct < 70 else ("WARN" if mem_pct < 90 else "CRIT")
    dims.append(("memory", mem_health, f"{mem_pct:.1f}% used"))

    # Disk health (Vector 1: self-optimization monitoring)
    disk_pct = m.get("disk_pct", 0.0)
    disk_health = "OK" if disk_pct < 80 else ("WARN" if disk_pct < 95 else "CRIT")
    dims.append(("disk", disk_health, f"{disk_pct:.1f}% used"))

    return dims


def render_health(m: dict[str, Any]) -> str:
    """Render a compact health-status block for automation pipelines and operator triage.

    Outputs one line per health dimension with a status indicator (OK/WARN/CRIT)
    so external tooling can parse the overall health posture.
    """
    m = sanitize_public_metrics(m)
    dims = _build_health_dimensions(m)
    lines = [f"{dim}: [{status}] {detail}" for dim, status, detail in dims]

    # Overall
    statuses = [status for _, status, _ in dims]
    if "CRIT" in statuses:
        overall = "CRIT"
    elif "WARN" in statuses:
        overall = "WARN"
    else:
        overall = "OK"

    header = f"EeeBot Health: [{overall}]"
    return "\n".join([header] + lines)


def render_health_json(m: dict[str, Any]) -> str:
    """Render a machine-readable JSON health payload for automation pipelines and monitoring."""
    m = sanitize_public_metrics(m)
    dims = _build_health_dimensions(m)
    statuses = [status for _, status, _ in dims]
    if "CRIT" in statuses:
        overall = "CRIT"
    elif "WARN" in statuses:
        overall = "WARN"
    else:
        overall = "OK"

    payload = {
        "overall": overall,
        "dimensions": {dim: {"status": status, "detail": detail} for dim, status, detail in dims},
        "captured_at": m["captured_at"],
        "goal": m["goal"],
        "active_task": m["active_task"],
        "queue_depth": m["queue_depth"],
        "stale_queue_requests": m["stale_queue_requests"],
        "approval_gate_state": m["approval_gate_state"],
        "reward_average": m["reward_average"],
        "last_cleanup_status": m["last_cleanup_status"],
        "host_capability_coverage": m["host_capability_coverage"],
    }
    return json.dumps(payload, indent=2)


# Module-level constant for diff_metrics — hoisted to eliminate per-call set allocation.
_DIFF_KEYS: set[str] = frozenset({
    "goal", "active_task", "queue_depth", "stale_queue_requests",
    "queue_priority", "queue_pressure", "queue_action",
    "approval_gate_state", "reward_momentum", "reward_average",
    "last_cleanup_status", "last_cleanup_recency",
    "host_capability_coverage", "host_focus_missing",
    "materialized_cycle", "archived_count",
})


def diff_metrics(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Compare bounded public metric snapshots."""
    old = sanitize_public_metrics(old)
    new = sanitize_public_metrics(new)
    diffs: list[tuple[str, Any, Any]] = []
    for key in sorted(_DIFF_KEYS):
        old_val = old.get(key)
        new_val = new.get(key)
        if old_val != new_val:
            diffs.append((key, old_val, new_val))
    return diffs


def render_diff(old: dict[str, Any], new: dict[str, Any]) -> str:
    """Render a human-readable diff without raw stale payloads or paths."""
    diffs = diff_metrics(old, new)
    if not diffs:
        return "No metric changes detected between snapshots."
    lines = ["EeeBot Metrics Diff", "=" * 50]
    for key, old_val, new_val in diffs:
        old_str = str(old_val) if old_val is not None else "(none)"
        new_str = str(new_val) if new_val is not None else "(none)"
        lines.append(f"  {key}:")
        lines.append(f"    - {old_str}")
        lines.append(f"    + {new_str}")
    return "\n".join(lines)


def render_watch_diff(old: dict[str, Any], new: dict[str, Any]) -> str:
    """Render the bounded diff used by the interactive watch surface."""
    return render_diff(old, new)


def _overall_health_status(m: dict[str, Any]) -> str:
    """Compute the overall health status (OK/WARN/CRIT) from health dimensions."""
    dims = _build_health_dimensions(m)
    statuses = [status for _, status, _ in dims]
    if "CRIT" in statuses:
        return "CRIT"
    if "WARN" in statuses:
        return "WARN"
    return "OK"


def render_oneliner(m: dict[str, Any]) -> str:
    """Render a single-line summary for narrow terminals and automation pipelines."""
    m = sanitize_public_metrics(m)
    parts = [
        f"goal={m['goal']}",
        f"task={m['active_task'][:40]}",
        f"queue={m['queue_depth']}/{m['stale_queue_requests']}s",
        f"gate={m['approval_gate_state']}",
        f"priority={m['queue_priority']}",
        f"reward={m['reward_average']}",
        f"cleanup={m['last_cleanup_status']}",
        f"host={m['host_capability_coverage']}",
        f"cpu={m.get('cpu_load', 0.0):.2f}",
        f"mem={m.get('mem_pct', 0.0):.1f}%",
        f"disk={m.get('disk_pct', 0.0):.1f}%",
    ]
    return " | ".join(parts)


def render_health_oneliner(m: dict[str, Any]) -> str:
    """Render a single-line health+context summary for automation pipelines.

    Combines the overall health status with the oneliner so monitoring tools
    get both posture and context in one parseable line.
    """
    m = sanitize_public_metrics(m)
    overall = _overall_health_status(m)
    health_icon = {"OK": "✓", "WARN": "⚠", "CRIT": "✗"}.get(overall, "?")
    return f"[{health_icon} {overall}] | {render_oneliner(m)}"


def render_cli(m: dict[str, Any]) -> str:
    m = sanitize_public_metrics(m)
    lines = [
        "EeeBot Dashboard",
        f"Focus: {m['focus_line']}",
        f"Summary: {m['dashboard_summary']}",
        f"Queue Snapshot: {m['queue_snapshot']}",
        f"Goal: {m['goal']}",
        f"Active Task: {m['active_task']}",
        f"Recent Cycles: {m['recent_cycles']}",
        f"Reward Trend: {format_reward_trend(m['reward_trend'])}",
        f"Reward Momentum: {m['reward_momentum']}",
        f"Reward Average: {m['reward_average']}",
        f"Reward Range: {m['reward_range']}",
       f"Subagent Queue Depth: {m['queue_depth']}",
        f"Stale Queue Requests (>24h): {m['stale_queue_requests']}",
        f"Queue Freshness: {m['queue_freshness']}",
        f"Queue Hygiene: {m['queue_hygiene']}",
        f"Queue Pressure: {m['queue_pressure']}",
        f"Queue Action: {m['queue_action']}",
        f"Queue Archive Target: {m['queue_archive_target']}",
        f"Queue Priority: {m['queue_priority']}",
        f"Operator Attention: {m['operator_attention']}",
        f"Oldest Stale Request Age: {m['oldest_stale_request_age']}",
        f"Oldest Stale Request Path: {m['oldest_stale_request_path_text']}",
        f"Last Cleanup Recency: {m['last_cleanup_recency']}",
        f"Archived Subagent Requests: {m['archived_count']}",

        f"Approval Gate State: {m['approval_gate_state']}",
        f"Materialized Cycle: {m['materialized_cycle']}",
        f"Materialized Improvement: {m['materialized_status']}",
        f"Concrete Statement: {m['concrete_statement']}",
        f"Latest Report Status: {m['latest_report_status']}",
        f"Goal Artifact Signature: {m['goal_artifact_signature']}",
        f"Next Bounded Candidate: {m['next_bounded_candidate']}",
    ]
    if m["materialized_path"]:
        lines.append(f"Materialized Artifact Path: {m['materialized_path']}")
    if m["latest_report_path"]:
        lines.append(f"Latest Report Path: {m['latest_report_path']}")
    lines.append(f"Last Cleanup Count: {m['last_cleanup_count']}")
    lines.append(f"Last Cleanup Timestamp: {m['last_cleanup_timestamp']}")
    lines.append(f"Queue Health: {m['queue_health']}")
    if m["host_capabilities"]:
        lines.append("Host Capabilities: " + ", ".join(m["host_capabilities"]))
        lines.append(f"Host Focus: {m['host_focus_status']}")
        lines.append(f"Host Capability Coverage: {m['host_capability_coverage']}")
        lines.append(f"Missing Focus Devices: {m['host_focus_missing']}")
        for name, details in m["host_focus_details"]:
            lines.append(f"  - {name}: {details}")
        for name, details in m["host_capability_details"]:
            if name not in m["host_focus_name_set"]:
                lines.append(f"  - {name}: {details or 'available'}")
    else:
        lines.append("Host Capabilities: none detected")
        lines.append("Host Focus: host hardware status unavailable")
    return "\n".join(lines)


def _reward_bar(reward: float, width: int = 20, trend: list[tuple[str, float]] | None = None) -> str:
    """Render a simple ASCII bar for a reward value.

    If *trend* is provided, the bar is scaled to the observed min/max range
    so that uniform rewards still produce a meaningful visual.  Falls back
    to a fixed 0–1.5 scale when there is insufficient data.
    """
    if trend and len(trend) >= 2:
        rewards = [r for _, r in trend]
        rmin, rmax = min(rewards), max(rewards)
        span = rmax - rmin
        if span > 0:
            fraction = (reward - rmin) / span
        else:
            fraction = 1.0  # all equal → full bar
    else:
        fraction = min(reward / 1.5, 1.0)
    filled = max(0, min(width, int(fraction * width)))
    return "█" * filled + "░" * (width - filled)


def _reward_sparkline(rewards: list[tuple[str, float, str]], width: int = 60) -> str:
    """Render an ASCII sparkline of reward history.

    Maps reward values to a vertical ASCII chart using block characters.
    The Y-axis spans from 0.0 to 1.5 (or the observed max if higher).
    Returns a multi-line string suitable for TUI embedding.

    Y-axis labels (header/footer) are aligned with the chart body by using
    a fixed label-width prefix so the vertical grid line stays in one column.
    """
    if not rewards:
        return "  no reward data"

    values = [r for _, r, _ in rewards]
    rmin = min(0.0, min(values))
    rmax = max(1.5, max(values))
    span = rmax - rmin if rmax > rmin else 1.0

    # Limit display width to avoid overwhelming narrow terminals
    display_width = min(width, len(values))
    # Take the most recent values if there are more than width
    display_values = values[-display_width:]

    # Build rows from top (high) to bottom (low)
    chart_height = 5
    lines: list[str] = []

    # Fixed label width so header, body, and footer pipes align vertically.
    # Header: "  {rmax:.2f} │" → label is "  {rmax:.2f} " (7 chars)
    _LABEL_PAD = " " * 7

    # Header row with scale
    lines.append(f"  {rmax:.2f} │")

    for row in range(chart_height):
        threshold = rmax - (row / chart_height) * span
        row_chars: list[str] = []
        for val in display_values:
            if val >= threshold - (span / chart_height):
                row_chars.append("█")
            else:
                row_chars.append(" ")
        lines.append(f"{_LABEL_PAD}{'│' + ''.join(row_chars)}")

    # Footer with scale
    lines.append(f"  {rmin:.2f} │{'─' * len(display_values)}")
    lines.append(f"{_LABEL_PAD}└{'─' * len(display_values)}┘ ({len(display_values)} cycles)")

    # Summary line with pass/fail counts for quick operator triage
    pass_count = sum(1 for _, _, status in rewards if status == "PASS")
    fail_count = len(rewards) - pass_count
    summary_parts: list[str] = [f"n={len(rewards)}"]
    if pass_count > 0:
        summary_parts.append(f"pass={pass_count}")
    if fail_count > 0:
        summary_parts.append(f"fail={fail_count}")
    mean_val = sum(values) / len(values)
    summary_parts.append(f"μ={mean_val:.2f}")
    lines.append(f"{_LABEL_PAD}  {' · '.join(summary_parts)}")

    return "\n".join(lines)


def _tui_cell(text: str, width: int = 50) -> str:
    """Truncate and pad *text* to exactly *width* characters for TUI cells."""
    text = str(text)
    if len(text) > width:
        return text[:width - 3] + "..."
    return text.ljust(width)


def render_tui(m: dict[str, Any]) -> str:
    """Render a compact terminal dashboard view for quick operator scans."""
    m = sanitize_public_metrics(m)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Precompute reward range once so _reward_bar doesn't re-scan the trend
    # for each bar (O(n) instead of O(n²) for n trend samples).
    trend = m["reward_trend"]
    if trend and len(trend) >= 2:
        _trend_rewards = [r for _, r in trend]
        _trend_rmin = min(_trend_rewards)
        _trend_rmax = max(_trend_rewards)
        _trend_span = _trend_rmax - _trend_rmin
    else:
        _trend_rmin = _trend_rmax = _trend_span = 0.0

    # Build reward trend with bars (reuse precomputed range)
    trend_lines: list[str] = []
    for cycle, reward in trend:
        short_cycle = cycle[:12]
        if _trend_span > 0:
            fraction = (reward - _trend_rmin) / _trend_span
        else:
            fraction = 1.0
        filled = max(0, min(20, int(fraction * 20)))
        bar = "█" * filled + "░" * (20 - filled)
        trend_lines.append(f"  {short_cycle} [{bar}] {reward:.2f}")
    trend_display = "\n".join(trend_lines) if trend_lines else "  no recent reward samples"

    # Priority color indicator (ASCII)
    priority = m["queue_priority"]
    priority_icon = {"urgent": "🔴", "elevated": "🟡", "watch": "🔵", "normal": "🟢", "idle": "⚪"}.get(priority, "⚪")

    # Cleanup status icon
    cleanup_status = m["last_cleanup_status"]
    cleanup_icon = {"fresh": "✓", "stale": "⚠", "unknown": "?", "future": "!"}.get(cleanup_status, "?")

    # Host probe icon
    probe_attention = m["host_capability_probe_attention"]
    probe_icon = "⚠" if "re-scan" in probe_attention else ("⏳" if "aging" in probe_attention else "✓")

    # Overall health status bar
    dims = _build_health_dimensions(m)
    statuses = [status for _, status, _ in dims]
    if "CRIT" in statuses:
        overall_health = "CRIT"
    elif "WARN" in statuses:
        overall_health = "WARN"
    else:
        overall_health = "OK"
    health_icon = {"OK": "✓", "WARN": "⚠", "CRIT": "✗"}.get(overall_health, "?")
    health_parts = [f"{dim}: {status}" for dim, status, _ in dims]
    health_line = " | ".join(health_parts)

    # Build sparkline for reward history (reuses precomputed data from metrics cache)
    sparkline = _reward_sparkline(m.get("sparkline_rewards", []), width=54)
    sparkline_lines = sparkline.split("\n")

    # Pre-compute system metric strings to avoid backslashes in f-string expressions
    # (Python 3.11 does not allow backslashes inside f-string expressions)
    _cpu_str = f"{m.get('cpu_load', 0.0):.2f}"
    _mem_str = f"{m.get('mem_pct', 0.0):.1f}% used"
    _disk_str = f"{m.get('disk_pct', 0.0):.1f}% used"

    # Pre-compute reward distribution string to avoid {{}} dict-literal in f-string
    # (Python 3.11 raises TypeError: unhashable type: 'dict' when {{}} is used
    #  as a default inside an f-string expression)
    _reward_dist_str = format_reward_distribution(m.get("reward_distribution", {}))[:50]

    lines = [
        "╔══════════════════════════════════════════════════════════╗",
        f"║  EeeBot Self-Evolving Runtime Dashboard  {now_iso}{' ' * max(0, 10 - len(now_iso))}║",
        f"║  Health {health_icon} [{overall_health}]{' ' * max(0, 2)}║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  {health_line:<54} ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Goal          : {_tui_cell(m['goal'])} ║",
        f"║  Active Task   : {_tui_cell(m['active_task'])} ║",
        f"║  Gate          : {_tui_cell(m['approval_gate_state'])} ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Queue {priority_icon}          : {m['queue_depth']} pending / {m['stale_queue_requests']} stale  [{priority}]{' ' * max(0, 10 - len(priority))}║",
        f"║  Queue Action  : {_tui_cell(m['queue_action'])} ║",
        f"║  Archive Target: {_tui_cell(m['queue_archive_target'])} ║",
        "╠══════════════════════════════════════════════════════════╣",
        "║  Reward Trend:",
    ]
    for tl in trend_display.split("\n"):
        lines.append(f"║  {tl:<54} ║")
    lines.append("╠══════════════════════════════════════════════════════════╣")
    lines.append("║  Reward Sparkline (last 60 cycles):")
    for sl in sparkline_lines:
        lines.append(f"║  {sl:<54} ║")
    lines.extend([
        f"║  Momentum      : {_tui_cell(m['reward_momentum'])} ║",
        f"║  Avg           : {_tui_cell(m['reward_average'])} ║",
        f"║  Range         : {_tui_cell(m['reward_range'])} ║",
        f"║  Distribution  : {_tui_cell(_reward_dist_str)} ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Cleanup {cleanup_icon}         : {m['last_cleanup_recency']} ({m['last_cleanup_status']}, count {m['last_cleanup_count']}){' ' * max(0, 5)}║",
        f"║  Host {probe_icon}            : {m['host_capability_coverage']}{' ' * max(0, 20)}║",
        f"║  Probe         : {m['host_capability_probe']}{' ' * max(0, 30)}║",
        f"║  Probe Action  : {_tui_cell(m['host_capability_probe_attention'])} ║",
       f"║  Missing Focus : {_tui_cell(m['host_focus_missing'])} ║",
        "╠══════════════════════════════════════════════════════════╣",
        # Real-time system metrics (Vector 1: self-optimization)
        f"║  CPU Load      : {_tui_cell(_cpu_str)} ║",
        f"║  Memory        : {_tui_cell(_mem_str)} ║",
        f"║  Disk          : {_tui_cell(_disk_str)} ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Materialized  : {_tui_cell(m['materialized_status'])} ║",
        f"║  Next Candidate: {_tui_cell(m['next_bounded_candidate'])} ║",
        "╚══════════════════════════════════════════════════════════╝",
    ])
    return "\n".join(lines)


# Module-level constants for HTML context building — hoisted from _build_html_context()
# to eliminate per-call list and dict allocation during web dashboard rendering.
_HTML_ESCAPE_KEYS: list[str] = [
    "summary_html", "focus_line_html", "goal_html", "recent_cycles_html",
    "materialized_cycle_html", "active_task_html", "queue_action_html",
    "queue_archive_target_html", "queue_priority_html", "queue_hygiene_html",
    "operator_attention_html", "queue_pressure_html", "queue_freshness_html",
    "oldest_stale_path_html", "reward_momentum_html", "reward_average_html",
    "reward_range_html", "latest_report_status_html", "concrete_statement_html",
    "artifact_freshness_html", "goal_artifact_signature_html",
    "next_bounded_candidate_html", "queue_health_html",
    "last_cleanup_timestamp_html", "host_focus_status_html", "host_coverage_html",
    "host_missing_html", "host_capability_probe_html",
    "host_capability_probe_attention_html", "approval_gate_state_html",
    "last_cleanup_count_html", "last_cleanup_recency_html", "queue_depth_html",
    "stale_queue_requests_html", "archived_count_html", "queue_snapshot_html",
    "materialized_status_html",
    # System metrics (Vector 1: self-optimization monitoring)
    "cpu_load_html", "mem_pct_html", "disk_pct_html",
    # Reward distribution statistics
    "reward_distribution_html",
    # Health status is computed below from the dimensions; do not look it up in metrics.
]
_HTML_KEY_MAP: dict[str, str] = {
    "summary_html": "dashboard_summary",
    "focus_line_html": "focus_line",
    "goal_html": "goal",
    "recent_cycles_html": "recent_cycles",
    "materialized_cycle_html": "materialized_cycle",
    "active_task_html": "active_task",
    "queue_action_html": "queue_action",
    "queue_archive_target_html": "queue_archive_target",
    "queue_priority_html": "queue_priority",
    "queue_hygiene_html": "queue_hygiene",
    "operator_attention_html": "operator_attention",
    "queue_pressure_html": "queue_pressure",
    "queue_freshness_html": "queue_freshness",
    "oldest_stale_path_html": "oldest_stale_request_path_text",
    "reward_momentum_html": "reward_momentum",
    "reward_average_html": "reward_average",
    "reward_range_html": "reward_range",
    "latest_report_status_html": "latest_report_status",
    "concrete_statement_html": "concrete_statement",
    "artifact_freshness_html": "artifact_freshness",
    "goal_artifact_signature_html": "goal_artifact_signature",
    "next_bounded_candidate_html": "next_bounded_candidate",
    "queue_health_html": "queue_health",
    "last_cleanup_timestamp_html": "last_cleanup_timestamp",
    "host_focus_status_html": "host_focus_status",
    "host_coverage_html": "host_capability_coverage",
    "host_missing_html": "host_focus_missing",
    "host_capability_probe_html": "host_capability_probe",
    "host_capability_probe_attention_html": "host_capability_probe_attention",
    "approval_gate_state_html": "approval_gate_state",
    "last_cleanup_count_html": "last_cleanup_count",
    "last_cleanup_recency_html": "last_cleanup_recency",
    "queue_depth_html": "queue_depth",
    "stale_queue_requests_html": "stale_queue_requests",
    "archived_count_html": "archived_count",
    "queue_snapshot_html": "queue_snapshot",
    "materialized_status_html": "materialized_status",
    # System metrics (Vector 1: self-optimization monitoring)
    "cpu_load_html": "cpu_load",
    "mem_pct_html": "mem_pct",
    "disk_pct_html": "disk_pct",
    # Reward distribution statistics
    "reward_distribution_html": "reward_distribution",
    # Health status
    "overall_health_html": "overall_health",
    "health_status_html": "health_status",
}


def _build_html_context(m: dict[str, Any]) -> dict[str, str]:
    """Build an escaped HTML context dict from metrics, replacing 40+ individual
    escape_html_text() assignments with a single declarative mapping.

    Keys that are already pre-escaped HTML (badges, details) pass through directly.
    Keys that need conditional escaping (paths) use a lambda.
    Conditional HTML fragments (materialized/report paths) are precomputed here
    so render_html can use str.format_map(ctx) without inline Python expressions.
    """
    # Keys whose values are already pre-escaped HTML fragments
    passthrough = {
        "caps_html": m["host_capability_badges_html"],
        "cap_details_html": m["host_capability_details_html"],
        "rewards_html": format_reward_trend_html(m["reward_trend"]),
    }

    ctx: dict[str, str] = dict(passthrough)
    for html_key in _HTML_ESCAPE_KEYS:
        ctx[html_key] = escape_html_text(m[_HTML_KEY_MAP[html_key]])

    # Conditional keys (escape only if truthy)
    # Do not render report/materialized filesystem paths in public HTML.
    ctx["materialized_path_html"] = ""
    ctx["latest_report_path_html"] = ""
    ctx["oldest_stale_age_html"] = escape_html_text(m["oldest_stale_request_age"])
    ctx["materialized_path_block"] = ""
    ctx["latest_report_path_block"] = ""

    # System metrics (Vector 1: self-optimization monitoring)
    ctx["cpu_load_html"] = f"{m.get('cpu_load', 0.0):.2f}"
    ctx["mem_pct_html"] = f"{m.get('mem_pct', 0.0):.1f}%"
    ctx["disk_pct_html"] = f"{m.get('disk_pct', 0.0):.1f}%"

    # Reward distribution statistics
    reward_dist = m.get("reward_distribution", {})
    if isinstance(reward_dist, dict) and reward_dist.get("count", 0) > 0:
        ctx["reward_distribution_html"] = format_reward_distribution(reward_dist)
    else:
        ctx["reward_distribution_html"] = "no reward data"

    # Health status
    overall = _overall_health_status(m)
    health_icon = {"OK": "✓", "WARN": "⚠", "CRIT": "✗"}.get(overall, "?")
    ctx["overall_health_html"] = overall
    ctx["health_status_html"] = health_icon
    # Precompute health badge CSS so the HTML template doesn't need inline Python
    health_bg = {"OK": "#065f46", "WARN": "#92400e", "CRIT": "#991b1b"}.get(overall, "#1e3a8a")
    health_fg = {"OK": "#6ee7b7", "WARN": "#fcd34d", "CRIT": "#fca5a5"}.get(overall, "#93c5fd")
    ctx["health_badge_bg"] = health_bg
    ctx["health_badge_fg"] = health_fg

    return ctx


def render_html(m: dict[str, Any]) -> str:
    """Render the HTML dashboard using str.format_map(ctx) so the template
    contains no inline Python — all escaping and conditional fragments are
    precomputed in _build_html_context.  Replaces 40+ manual variable
    assignments with a single declarative format call."""
    m = sanitize_public_metrics(m)
    ctx = _build_html_context(m)
    return _HTML_TEMPLATE.format_map(ctx)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="15">

    <title>EeeBot Self-Evolving Runtime Dashboard</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #0f141c;
            color: #e1e6f0;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
        }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #1f2937;
            padding-bottom: 15px;
            margin-bottom: 25px;
        }}
        h1 {{
            margin: 0;
            font-size: 24px;
            color: #38bdf8;
        }}
        .refresh-indicator {{
            font-size: 12px;
            color: #64748b;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 25px;
        }}
        .card {{
            background: #1e293b;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -1px rgba(0,0,0,0.06);
            border: 1px solid #334155;
        }}
        .card h2 {{
            margin-top: 0;
            margin-bottom: 15px;
            font-size: 16px;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .metric {{
            font-size: 15px;
            line-height: 1.6;
        }}
        .metric-item {{
            margin-bottom: 10px;
        }}
        .metric-label {{
            color: #94a3b8;
            font-weight: 500;
        }}
        .metric-value {{
            color: #f8fafc;
            font-weight: bold;
        }}
        .trend-container {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }}
        .trend-badge {{
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 13px;
            color: white;
        }}
        .cap-tag {{
            display: inline-block;
            background: #0f172a;
            color: #38bdf8;
            border: 1px solid #1e293b;
            padding: 4px 8px;
            border-radius: 4px;
            margin-right: 5px;
            margin-bottom: 5px;
            font-size: 12px;
        }}
        .cap-list {{
            margin: 8px 0 0 18px;
            padding: 0;
            color: #cbd5e1;
            font-size: 13px;
            line-height: 1.5;
        }}
        .status-badge {{
            display: inline-block;
            background: #1e3a8a;
            color: #93c5fd;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }}
        .path {{
            font-family: monospace;
            font-size: 12px;
            background: #0f172a;
            padding: 4px;
            border-radius: 4px;
            word-break: break-all;
            color: #cbd5e1;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>EeeBot Self-Evolving Runtime</h1>
                <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">Host: eeepc (Tailscale Node)</div>
            </div>
            <div class="refresh-indicator">Auto-refreshing every 15s</div>
        </header>

        <div class="grid">
            <div class="card">
                <h2>Active Context</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Summary:</span>
                        <div class="metric-value" style="color: #34d399; margin-top: 2px;">{summary_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Focus:</span>
                        <div class="metric-value" style="color: #60a5fa; margin-top: 2px;">{focus_line_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Goal:</span>
                        <div class="metric-value" style="color: #38bdf8; margin-top: 2px;">{goal_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Queue Snapshot:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #cbd5e1;">{queue_snapshot_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Recent Cycles:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{recent_cycles_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Materialized Cycle:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{materialized_cycle_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Active Task:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{active_task_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Queue Action:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #34d399;">{queue_action_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Queue Priority:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #f59e0b;">{queue_priority_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Operator Attention:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #f8fafc;">{operator_attention_html}</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Execution & Queues</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Subagent Queue Depth:</span>
                        <span class="metric-value" style="color: #f59e0b;">{queue_depth_html} pending</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Stale Queue Requests (&gt;24h):</span>
                        <span class="metric-value" style="color: #ef4444;">{stale_queue_requests_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Freshness:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_freshness_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Pressure:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_pressure_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Hygiene:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_hygiene_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Action:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #34d399;">{queue_action_html}</div>
                    </div>
                     <div class="metric-item">
                         <span class="metric-label">Queue Archive Target:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_archive_target_html}</div>
                     </div>
                     <div class="metric-item">
                         <span class="metric-label">Queue Priority:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #f59e0b;">{queue_priority_html}</div>
                     </div>

                    <div class="metric-item">
                        <span class="metric-label">Operator Attention:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #f8fafc;">{operator_attention_html}</div>
                    </div>
                     <div class="metric-item">
                          <span class="metric-label">Oldest Stale Request Age:</span>
                          <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{oldest_stale_age_html}</div>
                      </div>
                     <div class="metric-item">
                         <span class="metric-label">Oldest Stale Request Path:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{oldest_stale_path_html}</div>
                     </div>

                    <div class="metric-item">
                        <span class="metric-label">Reward Momentum:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{reward_momentum_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Reward Average:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{reward_average_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Reward Range:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{reward_range_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Archived Requests:</span>
                        <span class="metric-value">{archived_count_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Approval Gate:</span>
                        <span class="status-badge">{approval_gate_state_html}</span>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Recent Reward Signals</h2>
                <div class="trend-container">
                    {rewards_html}
                </div>
            </div>
        </div>

        <div class="grid">
            <div class="card" style="grid-column: span 2;">
                <h2>Materialized Output</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Latest Improvement:</span>
                        <div class="metric-value" style="font-weight: normal; margin-top: 2px;">{materialized_status_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Concrete Statement:</span>
                        <div class="metric-value" style="font-weight: normal; margin-top: 2px; color: #34d399;">{concrete_statement_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Latest Report Status:</span>
                        <div class="metric-value" style="font-weight: normal; margin-top: 2px; color: #f59e0b;">{latest_report_status_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Artifact Freshness:</span>
                        <div class="metric-value" style="font-weight: normal; margin-top: 2px; color: #cbd5e1;">{artifact_freshness_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Goal Artifact Signature:</span>
                        <div class="metric-value" style="font-weight: normal; color: #a855f7;">{goal_artifact_signature_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Next Bounded Candidate:</span>
                        <div class="metric-value" style="font-weight: normal; color: #10b981;">{next_bounded_candidate_html}</div>
                    </div>
                    {materialized_path_block}
                     {latest_report_path_block}
                </div>
            </div>

           <div class="card">
                <h2>System Health</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Overall Status:</span>
                        <span class="status-badge" style="background: {health_badge_bg}; color: {health_badge_fg};">{health_status_html} {overall_health_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">CPU Load (1m):</span>
                        <span class="metric-value" style="color: #38bdf8;">{cpu_load_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Memory Usage:</span>
                        <span class="metric-value" style="color: #f472b6;">{mem_pct_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Disk Usage:</span>
                        <span class="metric-value" style="color: #a78bfa;">{disk_pct_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Last Cleanup Count:</span>
                        <span class="metric-value">{last_cleanup_count_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Last Cleanup Time:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #94a3b8;">{last_cleanup_timestamp_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Health:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_health_html}</div>
                    </div>
                     <div class="metric-item">
                          <span class="metric-label">Last Cleanup Recency:</span>
                          <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{last_cleanup_recency_html}</div>
                      </div>

                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Host Capabilities:</span>
                         <div style="margin-top: 5px;">{caps_html}</div>
                         <div class="metric-value" style="margin-top: 6px; font-weight: normal; color: #cbd5e1;">{host_coverage_html}</div>
                           <div class="metric-value" style="margin-top: 4px; font-weight: normal; color: #94a3b8;">Probe: {host_capability_probe_html}</div>
                           <div class="metric-value" style="margin-top: 4px; font-weight: normal; color: #94a3b8;">Probe Action: {host_capability_probe_attention_html}</div>

                          <div class="metric-value" style="margin-top: 4px; font-weight: normal; color: #94a3b8;">Focus: {host_focus_status_html}</div>

                         <div class="metric-value" style="margin-top: 4px; font-weight: normal; color: #94a3b8;">Missing: {host_missing_html}</div>
                         <div style="margin-top: 8px;">{cap_details_html}</div>

                    </div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""


class DashboardHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            metrics = collect_metrics()
            html_content = render_html(metrics)
            self.wfile.write(html_content.encode("utf-8"))
        elif self.path == "/api/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            metrics = collect_metrics()
            json_output = render_json(metrics)
            self.wfile.write(json_output.encode("utf-8"))
        elif self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            metrics = collect_metrics()
            health_json = render_health_json(metrics)
            self.wfile.write(health_json.encode("utf-8"))
        elif self.path == "/api/health-oneliner":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            metrics = collect_metrics()
            oneliner = render_health_oneliner(metrics)
            self.wfile.write(oneliner.encode("utf-8"))
        elif self.path == "/api/reward-csv":
            metrics = collect_metrics()
            body = "status,source_status,age_hours,context_only\n"
            source = metrics.get("reward_source", {})
            body += f"unavailable,{source.get('status', 'unavailable')},{source.get('age_hours') or ''},true\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        elif self.path.startswith("/api/top-cycles"):
            import urllib.parse as _urllib_parse
            params = _urllib_parse.urlparse(self.path).query
            top_n = 5
            for param in params.split("&"):
                if param.startswith("top_n="):
                    try:
                        top_n = int(param.split("=")[1])
                    except ValueError:
                        pass
            metrics = collect_metrics()
            source = metrics.get("reward_source", {})
            output = reward_export_unavailable(source)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(output.encode("utf-8"))
        elif self.path == "/api/cleanup":
            import urllib.parse as _urllib_parse
            params = _urllib_parse.urlparse(self.path).query
            hours = 24
            dry_run = False
            for param in params.split("&"):
                if param.startswith("hours="):
                    try:
                        hours = int(param.split("=")[1])
                    except ValueError:
                        pass
                elif param == "dry_run":
                    dry_run = True
            result = archive_stale_subagent_requests(hours=hours, dry_run=dry_run)
            if result["archived"] > 0 and not dry_run:
                update_health_with_cleanup(result["archived"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
        elif self.path == "/api/refresh-host-caps":
            caps = refresh_host_capabilities()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(caps).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress request logging to avoid cluttered console output
        pass


def serve(host: str, port: int) -> None:
    print(f"Starting dashboard web server on http://{host}:{port}")
    server = http.server.HTTPServer((host, port), DashboardHTTPRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard server.")
        sys.exit(0)


def main() -> None:
    # Use a set for O(1) flag lookups instead of repeated O(n) list scans.
    # With 15+ flag checks, this eliminates ~15× len(sys.argv) comparisons.
    _flags = set(sys.argv)

    if "--help" in _flags or "-h" in _flags:
        print("""EeeBot Self-Evolving Runtime Dashboard

Usage:
  python3 scripts/eeebot_dashboard.py [OPTIONS]

Modes:
  (no flags)       Print full CLI dashboard (default)
  --tui, -t        Compact TUI box-drawing view
  --watch, -w      Live-refresh TUI (default 5s, use --interval N)
  --json, -j       Machine-readable JSON metrics
  --health, -H     Health status block (OK/WARN/CRIT per dimension)
   --health-json    JSON health payload for automation
   --health-oneliner  Single-line health+context summary for automation pipelines
   --oneliner, -1   Single-line summary for narrow terminals
   --snapshot, -S   Write a text snapshot to /tmp/
   --serve, -s      Start web server (default :8080, use --port N)
   --export-html    Write a static HTML snapshot to /tmp/
   --export-json    Write a JSON metrics snapshot to /tmp/
   --diff           Compare current metrics with last JSON snapshot
   --export-csv     Export full reward history to CSV in /tmp/
   --top-cycles     Show best and worst N cycles by reward (use --top-n N)
   --cleanup-queue, -C
                      Archive subagent requests older than 24h (use --hours N)
   --dry-run        With --cleanup-queue: show what would be archived
   --refresh-host-caps, -R
                      Re-scan host hardware and update host_capabilities.json

Examples:
  python3 scripts/eeebot_dashboard.py --tui
  python3 scripts/eeebot_dashboard.py --watch --interval 10
  python3 scripts/eeebot_dashboard.py --serve --port 9090
  python3 scripts/eeebot_dashboard.py --export-html
  python3 scripts/eeebot_dashboard.py --export-json && sleep 10 && python3 scripts/eeebot_dashboard.py --diff
""")
        return

    if "--export-csv" in _flags:
        source = collect_metrics().get("reward_source", {})
        print(reward_export_unavailable(source))
        return

    if "--top-cycles" in _flags:
        source = collect_metrics().get("reward_source", {})
        print(reward_export_unavailable(source))
        return

    if "--cleanup-queue" in _flags or "-C" in _flags:
        hours = 24
        dry_run = "--dry-run" in sys.argv
        for i, arg in enumerate(sys.argv):
            if arg == "--hours" and i + 1 < len(sys.argv):
                hours = int(sys.argv[i + 1])

        print(f"Scanning for subagent requests older than {hours}h...")
        result = archive_stale_subagent_requests(hours=hours, dry_run=dry_run)

        if dry_run:
            print(f"[DRY RUN] Would archive {result['archived']} stale request(s)")
            for p in result["paths"][:10]:
                print(f"  {p}")
            if result["archived"] > 10:
                print(f"  ... and {result['archived'] - 10} more")
        else:
            print(f"Archived {result['archived']} stale request(s) to {result['archive_dir']}/")
            if result["skipped"] > 0:
                print(f"Skipped {result['skipped']} request(s) due to errors:")
                for p, e in result["skipped_details"][:5]:
                    print(f"  {p}: {e}")

            # Update health record
            new_health = update_health_with_cleanup(result["archived"])
            print(f"Updated state/current_health.json: cleanup_count={new_health.get('last_subagent_cleanup_count')}, timestamp={new_health.get('last_subagent_cleanup_timestamp')}")

        # Show post-cleanup stats
        queue_depth, stale_count, oldest_age, archived_total, _ = scan_subagent_tree_stats()
        print(f"\nPost-cleanup queue: {queue_depth} pending, {stale_count} stale, {archived_total} archived total")
        return

    if "--refresh-host-caps" in _flags or "-R" in _flags:
        caps = refresh_host_capabilities()
        print("Host capabilities refreshed:")
        for name, info in caps.items():
            if name.startswith("_"):
                continue
            status = "✓" if info.get("available") else "✗"
            print(f"  {status} {name}: {info.get('details', 'unknown')}")
        print(f"\nScan timestamp: {caps.get('_scan_timestamp', 'unknown')}")
        return

    if "--snapshot" in _flags or "-S" in _flags:
        metrics = collect_metrics()
        snapshot_path = write_snapshot(metrics)
        print(f"Snapshot written to {snapshot_path}")
        return

    if "--export-html" in _flags:
        metrics = collect_metrics()
        html_path = Path(f"/tmp/eeebot-dashboard-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html")
        html_path.write_text(render_html(metrics), encoding="utf-8")
        print(f"HTML snapshot written to {html_path}")
        return

    if "--json" in _flags or "-j" in _flags:
        print(render_json(collect_metrics()))
        return

    if "--health" in _flags or "-H" in _flags:
        print(render_health(collect_metrics()))
        return

    if "--health-json" in _flags:
        print(render_health_json(collect_metrics()))
        return

    if "--health-oneliner" in _flags:
        print(render_health_oneliner(collect_metrics()))
        return

    if "--export-json" in _flags:
        metrics = collect_metrics()
        json_path = Path(f"/tmp/eeebot-dashboard-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
        json_path.write_text(render_json(metrics), encoding="utf-8")
        print(f"JSON snapshot written to {json_path}")
        return

    if "--diff" in _flags:
        # Load previous JSON snapshot from /tmp if available, compare with current
        import glob as _glob
        prev_files = sorted(_glob.glob("/tmp/eeebot-dashboard-*.json"), key=os.path.getmtime, reverse=True)
        if not prev_files:
            print("No previous JSON snapshot found in /tmp/. Run --export-json first.")
            return
        prev_metrics = load_json(Path(prev_files[0]), {})
        if not prev_metrics:
            print(f"Could not parse previous snapshot: {prev_files[0]}")
            return
        current_metrics = collect_metrics()
        print(render_diff(prev_metrics, current_metrics))
        return

    if "--oneliner" in _flags or "-1" in _flags:
        print(render_oneliner(collect_metrics()))
        return

    if "--tui" in _flags or "-t" in _flags:
        print(render_tui(collect_metrics()))
        return

    if "--watch" in sys.argv or "-w" in sys.argv:
        # Live-refresh TUI mode — clears screen and re-renders every N seconds.
        # Shows a diff section when metrics change between refreshes.
        # Uses adaptive refresh: interval scales with CPU load to reduce overhead
        # on the constrained eeepc host (Vector 1: self-optimization).
        base_interval = 5
        for i, arg in enumerate(sys.argv):
            if arg == "--interval" and i + 1 < len(sys.argv):
                base_interval = int(sys.argv[i + 1])

        signal.signal(signal.SIGINT, _handle_interrupt)
        signal.signal(signal.SIGTERM, _handle_interrupt)

        while _watch_running:
            # Clear screen (ANSI escape)
            sys.stdout.write("\033[2J\033[H")
            metrics = collect_metrics()
            print(render_tui(metrics))

            # Show diff from previous refresh
            if _prev_watch_metrics is not None:
                diffs = diff_metrics(_prev_watch_metrics, metrics)
                if diffs:
                    print("\n  ── Changes since last refresh ──")
                    print(render_watch_diff(_prev_watch_metrics, metrics))
                else:
                    print("\n  ── No changes since last refresh ──")
            else:
                print("\n  ── Initial refresh (diff available next cycle) ──")

            # Adaptive interval: increase when CPU is busy, decrease when idle
            effective_interval = compute_adaptive_interval(base_interval)
            cpu_load = metrics.get("cpu_load", 0.0)
            print(f"\n  Press Ctrl+C to stop (refresh every {effective_interval:.1f}s, load={cpu_load:.2f})")
            sys.stdout.flush()
            _prev_watch_metrics = metrics
            time.sleep(effective_interval)
        return

    if "--watch-json" in sys.argv:
        # Machine-readable live-refresh mode for automation pipelines.
        # Emits one JSON object per line (JSONL) with a timestamp, suitable
        # for piping into monitoring tools or log aggregators.
        # Uses adaptive refresh like --watch but outputs JSON instead of TUI.
        base_interval = 5
        for i, arg in enumerate(sys.argv):
            if arg == "--interval" and i + 1 < len(sys.argv):
                base_interval = int(sys.argv[i + 1])

        signal.signal(signal.SIGINT, _handle_interrupt)
        signal.signal(signal.SIGTERM, _handle_interrupt)

        while _watch_running:
            metrics = collect_metrics()
            serializable = serialize_metrics(metrics)
            serializable["_watch_timestamp"] = datetime.now(timezone.utc).isoformat()
            print(json.dumps(serializable))
            sys.stdout.flush()

            effective_interval = compute_adaptive_interval(base_interval)
            time.sleep(effective_interval)
        return

    if "--serve" in sys.argv or "-s" in sys.argv:
        port = 8080
        host = "0.0.0.0"

        # Simple arg parsing
        for i, arg in enumerate(sys.argv):
            if arg in ("--port", "-p") and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
            if arg == "--host" and i + 1 < len(sys.argv):
                host = sys.argv[i + 1]

        serve(host, port)
    else:
        metrics = collect_metrics()
        print(render_cli(metrics))


if __name__ == "__main__":
    main()
