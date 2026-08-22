"""Issue #712: strip completed "Current priority target" entries from
goal_text.json's raw text before the bridge injects it verbatim into the
subagent prompt.

The deterministic coordinator path (retired, #916) already skipped done
priorities via the #575 git-log heuristic (`_title_already_done_in_git_log`),
but the bridge's raw-text prompt injection
bypassed it entirely — a completed "Current priority target" kept being
shown/re-proposed every cycle (novelty collapse, per the #711 shadow run).
`filter_completed_priorities_from_goal_text` reuses that exact same
done-detection heuristic to rewrite the raw text before injection.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.goal_text_utils import filter_completed_priorities_from_goal_text
from tests.test_goal_backlog_routing import GOAL_TEXT_JSON, _make_git_repo_with_commit

RAW_TEXT = json.loads(GOAL_TEXT_JSON)["text"]


def test_done_priority_removed_and_moved_to_completed_sentence(tmp_path: Path):
    """Priority 5's title words all match one commit line → removed from
    "Current priority targets:" and listed under "Completed (do not repeat)"."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "feat: write scripts/cycle_logger.py — confirmed done for cycle-999",
        create_files=("scripts/cycle_logger.py",),
    )

    rewritten = filter_completed_priorities_from_goal_text(RAW_TEXT, repo)

    assert rewritten != RAW_TEXT
    # Priority 5 no longer listed under "Current priority targets:"
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    completed_split = targets_section.split("Completed (do not repeat):")
    current_targets_text = completed_split[0]
    assert "Priority 5" not in current_targets_text
    assert "cycle_logger.py" not in current_targets_text
    # Priority 6 (not done) is still listed
    assert "Priority 6" in current_targets_text
    assert "smoke_test_loop.py" in current_targets_text
    # Priority 5's title appears in the Completed sentence
    assert "Completed (do not repeat):" in rewritten
    completed_sentence = rewritten.split("Completed (do not repeat):", 1)[1]
    assert "cycle_logger.py" in completed_sentence


def test_all_done_current_priority_targets_section_empty(tmp_path: Path):
    """When every listed priority matches the log, "Current priority targets:"
    has no remaining entries and both titles are moved to Completed."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "feat: write scripts/cycle_logger.py finished",
        "feat: write scripts/smoke_test_loop.py finished with test",
        create_files=("scripts/cycle_logger.py", "scripts/smoke_test_loop.py"),
    )

    rewritten = filter_completed_priorities_from_goal_text(RAW_TEXT, repo)

    assert "Completed (do not repeat):" in rewritten
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    current_targets_text = targets_section.split("Completed (do not repeat):")[0]
    assert "Priority 5" not in current_targets_text
    assert "Priority 6" not in current_targets_text

    completed_sentence = rewritten.split("Completed (do not repeat):", 1)[1]
    assert "cycle_logger.py" in completed_sentence
    assert "smoke_test_loop.py" in completed_sentence


def test_not_done_priority_left_untouched(tmp_path: Path):
    """A priority with no matching commit stays under "Current priority targets:"
    and the text is returned byte-identical (nothing to move)."""
    repo = _make_git_repo_with_commit(tmp_path, "chore: unrelated housekeeping commit")

    rewritten = filter_completed_priorities_from_goal_text(RAW_TEXT, repo)

    assert rewritten == RAW_TEXT
    assert "Completed (do not repeat):" not in rewritten


def test_fail_open_no_selfevo_repo_root(tmp_path: Path):
    assert filter_completed_priorities_from_goal_text(RAW_TEXT, None) == RAW_TEXT


def test_fail_open_repo_root_not_a_dir(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert filter_completed_priorities_from_goal_text(RAW_TEXT, missing) == RAW_TEXT


def test_fail_open_missing_current_priority_targets_marker(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "feat: write scripts/cycle_logger.py")
    text = "just a plain goal description, no priority targets here."
    assert filter_completed_priorities_from_goal_text(text, repo) == text


def test_fail_open_malformed_priority_section(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "feat: write scripts/cycle_logger.py")
    text = "mission statement\n\nCurrent priority targets:\nnot actually a priority line"
    assert filter_completed_priorities_from_goal_text(text, repo) == text


def test_fail_open_non_string_input(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "feat: write scripts/cycle_logger.py")
    assert filter_completed_priorities_from_goal_text(None, repo) is None  # type: ignore[arg-type]


# ─── #748: artifact+evidence done-detection (P11/P12 confirmed false positives) ───

# Mirrors the real P11/P12 shape from host/eeepc/etc/goal_text.json: short
# titles whose words alone collide with the loop's narrow commit vocabulary.
P11_P12_RAW_TEXT = (
    "eeebot is a resource-aware, self-evolving autonomous agent on a weak eeepc host.\n\n"
    "Current priority targets:\n"
    "(A) Priority 11 — Loop health in dashboard: extend scripts/eeebot_dashboard.py "
    "with a compact loop-health section that reads state/ledger/cycles.jsonl.\n"
    "(B) Priority 12 — Archive old cycle reports: write scripts/archive_old_reports.py "
    "that moves state/reports/*.json older than 30 days into monthly archives."
)


def test_p11_style_false_positive_survives_filtering(tmp_path: Path):
    """Issue #748 confirmed live false match: P11's short title ("Loop health in
    dashboard") word-overlaps a commit about a DIFFERENT artifact
    ("loop health report script"), but the actual target file
    (eeebot_dashboard.py) exists with no commit evidence naming it. With
    artifact+evidence done-detection, P11 must survive as a live priority."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "feat: implement loop health report script",
        "chore: update HISTORY.md with loop_health_report.py",
        create_files=("scripts/eeebot_dashboard.py",),
    )

    rewritten = filter_completed_priorities_from_goal_text(P11_P12_RAW_TEXT, repo)

    assert rewritten == P11_P12_RAW_TEXT
    assert "Completed (do not repeat):" not in rewritten
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    assert "Priority 11" in targets_section


def test_p12_style_false_positive_survives_filtering(tmp_path: Path):
    """Issue #748 confirmed live false match: P12's short title ("Archive old
    cycle reports") word-overlaps unrelated commits ("memory_archiver.py
    self-tests", "cycle_trend.py"), but scripts/archive_old_reports.py does not
    exist at all. P12 must survive as a live priority."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "chore: log cycle-985fb2 — memory_archiver.py self-tests added",
        "feat: add /api/recent-reports ... cycle_trend.py",
    )

    rewritten = filter_completed_priorities_from_goal_text(P11_P12_RAW_TEXT, repo)

    assert rewritten == P11_P12_RAW_TEXT
    assert "Completed (do not repeat):" not in rewritten
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    assert "Priority 12" in targets_section


def test_artifact_positive_match_filters_into_completed(tmp_path: Path):
    """When the target file both exists AND a commit's message contains its
    exact basename, the priority is correctly recognized as done."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "feat: create foo.py to close the gap",
        create_files=("scripts/foo.py",),
    )
    text = (
        "mission statement\n\n"
        "Current priority targets:\n"
        "(A) Priority 20 — Do the thing: write scripts/foo.py that does the thing.\n"
        "(B) Priority 21 — Untouched work: write scripts/bar.py that does other work."
    )

    rewritten = filter_completed_priorities_from_goal_text(text, repo)

    assert rewritten != text
    assert "Completed (do not repeat):" in rewritten
    completed_sentence = rewritten.split("Completed (do not repeat):", 1)[1]
    assert "Do the thing" in completed_sentence
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    current_targets_text = targets_section.split("Completed (do not repeat):")[0]
    assert "Priority 20" not in current_targets_text
    assert "Priority 21" in current_targets_text


def test_extend_priority_not_done_by_shared_target_file(tmp_path: Path):
    """#748 follow-up, fired live 2026-07-15 (R30 wake-up never happened):
    P14 'extend scripts/eeebot_dashboard.py' was read as done because the
    file pre-existed (P7) and its basename appeared in P11's commits. An
    extend-type entry with no verbatim 'Priority N — ...' label evidence in
    the git log must stay a live priority."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "selfevo: auto-commit uncommitted subagent work — Priority 11 — Loop "
        "health in dashboard: extend scripts/eeebot_dashboard.py with a "
        "compact loop-health section",
        create_files=("scripts/eeebot_dashboard.py",),
    )
    text = (
        "mission statement\n\n"
        "Current priority targets:\n"
        "(A) Priority 14 — Demand and idle visibility in dashboard: extend "
        "scripts/eeebot_dashboard.py with a compact demand-status section."
    )

    rewritten = filter_completed_priorities_from_goal_text(text, repo)

    assert rewritten == text
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    assert "Priority 14" in targets_section


def test_add_to_priority_not_done_by_shared_target_file(tmp_path: Path):
    """#769 follow-up, fired live 2026-07-18: P16 phrased 'add ONE function
    render_cycle_strip(...) to scripts/eeebot_dashboard.py' slipped past the
    extend-only carve-out — the file pre-existed and its basename appeared
    in recent commits, so P16 was falsely filtered as done and its R30
    wake-up never fired. 'add ... to <existing file>' (and 'update') are
    modify verbs too."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "selfevo: auto-commit uncommitted subagent work — Priority 11 — Loop "
        "health in dashboard: extend scripts/eeebot_dashboard.py with a "
        "compact loop-health section",
        create_files=("scripts/eeebot_dashboard.py",),
    )
    text = (
        "mission statement\n\n"
        "Current priority targets:\n"
        "(A) Priority 16 — Cycle strip line in dashboard: add ONE function "
        "render_cycle_strip(ledger_path) to scripts/eeebot_dashboard.py and "
        "call it from the main render."
    )

    rewritten = filter_completed_priorities_from_goal_text(text, repo)

    assert rewritten == text
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    assert "Priority 16" in targets_section


def test_extend_priority_done_by_verbatim_label_in_log(tmp_path: Path):
    """The same extend entry IS done once the git log carries its verbatim
    'Priority N — <title>' label (integrated cycles auto-commit the proposal
    title) — P11 keeps reading as done after the extend carve-out."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "selfevo: auto-commit uncommitted subagent work — Priority 11 — Loop "
        "health in dashboard: extend scripts/eeebot_dashboard.py with a "
        "compact loop-health section",
        create_files=("scripts/eeebot_dashboard.py",),
    )
    text = (
        "mission statement\n\n"
        "Current priority targets:\n"
        "(A) Priority 11 — Loop health in dashboard: extend "
        "scripts/eeebot_dashboard.py with a compact loop-health section "
        "that reads state/ledger/cycles.jsonl.\n"
        "(B) Priority 14 — Demand and idle visibility in dashboard: extend "
        "scripts/eeebot_dashboard.py with a compact demand-status section."
    )

    rewritten = filter_completed_priorities_from_goal_text(text, repo)

    targets_section = rewritten.split("Current priority targets:", 1)[1]
    current = targets_section.split("Completed (do not repeat):")[0]
    assert "Priority 11" not in current
    assert "Priority 14" in current
    assert "Loop health in dashboard" in rewritten.split("Completed (do not repeat):", 1)[1]


def test_no_target_file_falls_back_to_word_heuristic(tmp_path: Path):
    """A priority entry naming NO target file path has no artifact signal, so
    `_priority_done_by_artifact` returns None and the old word-overlap
    heuristic (`_title_already_done_in_git_log`) is used unchanged: one
    priority whose title words all match a commit line is filtered, the
    other (no matching commit) is kept."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "feat: refresh dashboard telemetry summary rendering pipeline",
    )
    text = (
        "mission statement\n\n"
        "Current priority targets:\n"
        "(A) Priority 30 — Refresh dashboard telemetry summary: improve how the "
        "operator dashboard summarizes recent telemetry, no code pointer given.\n"
        "(B) Priority 31 — Totally unrelated goal: pursue something with zero "
        "keyword overlap versus recent commits, no code pointer given."
    )

    rewritten = filter_completed_priorities_from_goal_text(text, repo)

    assert "Completed (do not repeat):" in rewritten
    completed_sentence = rewritten.split("Completed (do not repeat):", 1)[1]
    assert "Refresh dashboard telemetry summary" in completed_sentence
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    current_targets_text = targets_section.split("Completed (do not repeat):")[0]
    assert "Priority 30" not in current_targets_text
    assert "Priority 31" in current_targets_text


# ─── #773: completed-demand sidecar (ledger-chain done-truth) ────────────────


def _write_completed_sidecar(state_dir: Path, demand_ids: dict[str, dict]) -> None:
    d = state_dir / "demand"
    d.mkdir(parents=True, exist_ok=True)
    (d / "completed.json").write_text(
        json.dumps({"schema_version": "demand-completed-v1", "entries": demand_ids}),
        encoding="utf-8",
    )


def _derived_priority_ids(text: str) -> dict[int, str]:
    """Derive demand ids exactly as `demand._priority_items` does."""
    from nanobot.runtime import demand

    section = text.split("Current priority targets:", 1)[1]
    return {
        int(m.group(1)): demand._make_item(
            "priority", f"Priority {m.group(1)} — {m.group(2).strip()}", m.group(3).strip()
        )["id"]
        for m in demand._PRIORITY_PATTERN.finditer(section)
    }


def test_completed_sidecar_marks_priority_done_with_state_dir(tmp_path: Path):
    """#773: with `state_dir` given, a priority whose derived demand id is in
    the completed sidecar moves to the Completed sentence, even though the
    git log carries NO text evidence for it (refined-title integration)."""
    repo = _make_git_repo_with_commit(tmp_path, "chore: unrelated commit text")
    state_dir = tmp_path / "state"
    ids = _derived_priority_ids(RAW_TEXT)
    _write_completed_sidecar(
        state_dir,
        {ids[5]: {"cycle_id": "c1", "ts": "2026-07-15T23:10:00Z", "files_changed": []}},
    )

    rewritten = filter_completed_priorities_from_goal_text(
        RAW_TEXT, repo, state_dir=state_dir
    )

    targets_section = rewritten.split("Current priority targets:", 1)[1]
    current = targets_section.split("Completed (do not repeat):")[0]
    assert "Priority 5" not in current
    assert "Priority 6" in current
    completed_sentence = rewritten.split("Completed (do not repeat):", 1)[1]
    assert "cycle_logger.py" in completed_sentence


def test_without_state_dir_sidecar_is_invisible(tmp_path: Path):
    """Regression: callers without a `state_dir` keep the exact pre-#773
    behavior — the sidecar existing on disk changes nothing for them."""
    repo = _make_git_repo_with_commit(tmp_path, "chore: unrelated commit text")
    state_dir = tmp_path / "state"
    ids = _derived_priority_ids(RAW_TEXT)
    _write_completed_sidecar(
        state_dir,
        {ids[5]: {"cycle_id": "c1", "ts": "2026-07-15T23:10:00Z", "files_changed": []}},
    )

    assert filter_completed_priorities_from_goal_text(RAW_TEXT, repo) == RAW_TEXT


def test_state_dir_with_empty_or_missing_sidecar_is_noop(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "chore: unrelated commit text")
    assert (
        filter_completed_priorities_from_goal_text(
            RAW_TEXT, repo, state_dir=tmp_path / "no-such-state"
        )
        == RAW_TEXT
    )


def test_corrupt_completed_sidecar_fails_open(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "chore: unrelated commit text")
    state_dir = tmp_path / "state"
    d = state_dir / "demand"
    d.mkdir(parents=True)
    (d / "completed.json").write_text("{{{not json", encoding="utf-8")
    assert (
        filter_completed_priorities_from_goal_text(RAW_TEXT, repo, state_dir=state_dir)
        == RAW_TEXT
    )
