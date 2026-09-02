"""Tests for #902: assigned-demand rotation + saturated-themes guard.

Rotation (:func:`llm_proposer._select_assigned_demand`) narrows a cycle's
presented demand to ONE least-recently-served item, persisted in
``<state_dir>/demand/rotation.json``. The saturated-themes section
(:func:`llm_proposer._saturated_themes_section`) flags subjects with ``>= K``
unconfirmed same-subject ``scripts/*.py`` files as CLOSED for new proposals.
Both are steering-only (never block/gate anything) and fail-open by
construction — see the #902 docstrings in ``llm_proposer.py`` for the full
design rationale.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import cycle_ledger, demand, llm_proposer
from tests.test_llm_proposer import (
    DEMAND_ENV,
    ENV_VAR,
    _state_dir,
    _write_goal_text,
    _write_usage_sidecar,
)

ROTATION_ENV = llm_proposer._DEMAND_ROTATION_ENABLED_ENV
SATURATED_K_ENV = llm_proposer._SATURATED_THEME_K_ENV


def _item(kind: str, item_id: str, summary: str = "x") -> dict:
    return {
        "kind": kind,
        "id": item_id,
        "summary": summary,
        "evidence": "",
        "affected_path": "",
    }


def _rotation_path(state_dir: Path) -> Path:
    return state_dir / "demand" / "rotation.json"


def _read_rotation(state_dir: Path) -> dict:
    return json.loads(_rotation_path(state_dir).read_text(encoding="utf-8"))


def _write_rotation(state_dir: Path, served: dict) -> None:
    path = _rotation_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": "demand-rotation-v1", "served": served}),
        encoding="utf-8",
    )


def _append_recent_duplicate_failure(
    state_dir: Path,
    demand_id: str,
    *,
    cycle_id: str = "cycle-failed",
    reason: str = "recent_duplicate_failure",
    age_hours: float = 1,
) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat().replace("+00:00", "Z")
    cycle_ledger.append_event(
        state_dir,
        {"phase": "proposed", "cycle_id": cycle_id, "demand_id": demand_id, "ts": ts},
    )
    cycle_ledger.append_event(
        state_dir,
        {"phase": "outcome", "cycle_id": cycle_id, "outcome": "skipped-duplicate", "reason": reason, "ts": ts},
    )


# ─── _select_assigned_demand ────────────────────────────────────────────────


class TestSelectAssignedDemand:
    def test_unserved_item_picked_first(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        items = [_item("priority", "p-1"), _item("priority", "p-2")]
        assert llm_proposer._select_assigned_demand(state_dir, items) == [items[0]]

    def test_second_call_picks_next_unserved_item(self, tmp_path):
        """Persistence across calls via rotation.json: the second call (a
        fresh read from disk, as a new process would do) advances to the
        next never-served item rather than repeating the first."""
        state_dir = _state_dir(tmp_path)
        items = [_item("priority", "p-1"), _item("priority", "p-2")]
        first = llm_proposer._select_assigned_demand(state_dir, items)
        second = llm_proposer._select_assigned_demand(state_dir, items)
        assert first == [items[0]]
        assert second == [items[1]]

    def test_selection_is_stamped_and_persisted_on_disk(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        items = [_item("priority", "p-1")]
        llm_proposer._select_assigned_demand(state_dir, items)
        data = _read_rotation(state_dir)
        assert data["schema_version"] == "demand-rotation-v1"
        assert "p-1" in data["served"]

    def test_oldest_timestamp_wins_when_all_served(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        items = [_item("priority", "p-1"), _item("priority", "p-2"), _item("priority", "p-3")]
        _write_rotation(
            state_dir,
            {
                "p-1": "2026-08-01T00:00:00+00:00",
                "p-2": "2026-08-03T00:00:00+00:00",
                "p-3": "2026-08-02T00:00:00+00:00",
            },
        )
        assert llm_proposer._select_assigned_demand(state_dir, items) == [items[0]]

    def test_tie_break_is_first_in_list_order(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        items = [_item("priority", "p-1"), _item("priority", "p-2")]
        same_ts = "2026-08-01T00:00:00+00:00"
        _write_rotation(state_dir, {"p-1": same_ts, "p-2": same_ts})
        assert llm_proposer._select_assigned_demand(state_dir, items) == [items[0]]

    def test_stale_ids_pruned_from_served(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        items = [_item("priority", "p-1")]
        _write_rotation(
            state_dir,
            {"p-1": "2026-08-01T00:00:00+00:00", "gone-2": "2026-08-01T00:00:00+00:00"},
        )
        llm_proposer._select_assigned_demand(state_dir, items)
        assert "gone-2" not in _read_rotation(state_dir)["served"]

    def test_kill_switch_off_returns_full_list_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ROTATION_ENV, "0")
        state_dir = _state_dir(tmp_path)
        items = [_item("priority", "p-1"), _item("priority", "p-2")]
        result = llm_proposer._select_assigned_demand(state_dir, items)
        assert result is items
        assert not _rotation_path(state_dir).exists()

    def test_kill_switch_accepts_false_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ROTATION_ENV, "false")
        state_dir = _state_dir(tmp_path)
        items = [_item("priority", "p-1")]
        assert llm_proposer._select_assigned_demand(state_dir, items) is items

    def test_corrupted_rotation_file_fails_open_to_full_list(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        path = _rotation_path(state_dir)
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json", encoding="utf-8")
        items = [_item("priority", "p-1"), _item("priority", "p-2")]
        result = llm_proposer._select_assigned_demand(state_dir, items)
        assert result is items

    def test_empty_input_returns_input_unchanged(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert llm_proposer._select_assigned_demand(state_dir, []) == []

    def test_recent_duplicate_failure_cools_demand_before_selection(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setenv("SUBAGENT_BRIDGE_FAILURE_SUPPRESS_HOURS", "24")
        item = _item("defect", "defect-cooled", "repair import resolution")
        _append_recent_duplicate_failure(state_dir, item["id"], age_hours=1)

        assert llm_proposer._select_assigned_demand(state_dir, [item]) == []

    def test_recent_duplicate_failure_cooldown_expires(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setenv("SUBAGENT_BRIDGE_FAILURE_SUPPRESS_HOURS", "24")
        item = _item("defect", "defect-retryable", "repair import resolution")
        _append_recent_duplicate_failure(state_dir, item["id"], age_hours=25)
        _write_rotation(state_dir, {item["id"]: "2026-09-01T00:00:00+00:00"})

        assert llm_proposer._select_assigned_demand(state_dir, [item]) == [item]
        assert item["id"] in _read_rotation(state_dir)["served"]

    def test_newer_non_failure_outcome_ends_cooldown(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        item = _item("defect", "defect-retried", "repair import resolution")
        _append_recent_duplicate_failure(state_dir, item["id"], cycle_id="cycle-old", age_hours=1)
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "cycle-new", "demand_id": item["id"], "ts": ts},
        )
        cycle_ledger.append_event(
            state_dir,
            {"phase": "outcome", "cycle_id": "cycle-new", "outcome": "partial", "reason": "transient", "ts": ts},
        )

        assert llm_proposer._select_assigned_demand(state_dir, [item]) == [item]

    def test_other_duplicate_reasons_do_not_cool_demand(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        already_done = _item("priority", "priority-existing", "existing work")
        existence_duplicate = _item("priority", "priority-indexed", "indexed work")
        _append_recent_duplicate_failure(
            state_dir, already_done["id"], reason="already_done", age_hours=1,
        )
        _append_recent_duplicate_failure(
            state_dir, existence_duplicate["id"], reason="existence_index_duplicate", age_hours=1,
        )

        assert llm_proposer._select_assigned_demand(
            state_dir, [already_done, existence_duplicate]
        ) == [already_done]

    def test_noncooled_demand_wins_over_cooled_demand(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        cooled = _item("defect", "defect-cooled", "repair import resolution")
        available = _item("defect", "defect-available", "repair parser")
        _append_recent_duplicate_failure(state_dir, cooled["id"], age_hours=1)

        assert llm_proposer._select_assigned_demand(
            state_dir, [cooled, available]
        ) == [available]

    def test_cooldown_evidence_failure_fails_open(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        item = _item("defect", "defect-unreadable", "repair import resolution")
        monkeypatch.setattr(llm_proposer, "_load_ledger_rows", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unreadable")))

        assert llm_proposer._select_assigned_demand(state_dir, [item]) == [item]

    def test_maybe_propose_makes_no_llm_call_when_all_demands_are_cooled(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setenv(ENV_VAR, "1")
        monkeypatch.setenv(DEMAND_ENV, "1")
        item = _item("defect", "defect-cooled", "repair import resolution")
        _append_recent_duplicate_failure(state_dir, item["id"], age_hours=1)
        monkeypatch.setattr(demand, "collect_demand", lambda *_args, **_kwargs: [item])
        calls = []
        monkeypatch.setattr(llm_proposer, "propose", lambda *args, **kwargs: calls.append(1))
        monkeypatch.setattr(llm_proposer, "should_propose", lambda *_args, **_kwargs: True)

        assert llm_proposer.maybe_propose(state_dir, None) is None
        assert calls == []


# ─── maybe_propose integration: rotation + assigned wording + noop advance ──


class TestRotationIntegration:
    @pytest.fixture(autouse=True)
    def _demand_on(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        monkeypatch.setenv(DEMAND_ENV, "1")
        monkeypatch.setattr(llm_proposer, "_idle_recorded_this_process", False)

    def _seed_two_priorities(self, state_dir: Path) -> None:
        _write_goal_text(
            state_dir,
            "Mission.\n\nCurrent priority targets:\n"
            "(A) Priority 1 — First thing: do the first thing.\n"
            "(B) Priority 2 — Second thing: do the second thing.\n",
        )

    def test_maybe_propose_presents_only_one_assigned_item(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        self._seed_two_priorities(state_dir)

        items = demand.collect_demand(state_dir, None)
        assert len(items) == 2

        captured: dict = {}

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kwargs):
            captured["context"] = context
            return {"no_valuable_task": True, "reason": "nothing bounded"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        assert llm_proposer.maybe_propose(state_dir, None) is None

        # Only ONE of the two items' ids should appear as an ``[id]`` demand
        # block marker — the other item is withheld this cycle. (Both
        # summaries legitimately also appear in the unrelated ``## Goal``
        # blob, which is unaffected by rotation, so id markers are the only
        # reliable signal of what the ## Demand section itself presented.)
        ctx = captured["context"]
        ids_present = sum(1 for it in items if f"[{it['id']}]" in ctx)
        assert ids_present == 1
        assert "ASSIGNED this one item" in ctx

    def test_noop_skip_advances_rotation(self, tmp_path, monkeypatch):
        """#902 acceptance criterion: a no_valuable_task reply still stamps
        rotation at selection time, so the loop does not stall on one
        unservable item."""
        state_dir = _state_dir(tmp_path)
        self._seed_two_priorities(state_dir)

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kwargs):
            return {"no_valuable_task": True, "reason": "nothing bounded"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        assert llm_proposer.maybe_propose(state_dir, None) is None

        data = _read_rotation(state_dir)
        assert len(data["served"]) == 1

    def test_noop_skip_reason_prefixed_with_assigned_id(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        self._seed_two_priorities(state_dir)

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kwargs):
            return {"no_valuable_task": True, "reason": "nothing bounded"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        llm_proposer.maybe_propose(state_dir, None)

        rows = [
            json.loads(line)
            for line in (state_dir / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        skip_rows = [r for r in rows if r.get("phase") == "proposer_skip"]
        assert len(skip_rows) == 1
        assert skip_rows[0]["reason"].startswith("assigned=")
        assert "nothing bounded" in skip_rows[0]["reason"]

    def test_rotation_kill_switch_off_presents_full_demand_list(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ROTATION_ENV, "0")
        state_dir = _state_dir(tmp_path)
        self._seed_two_priorities(state_dir)
        items = demand.collect_demand(state_dir, None)

        captured: dict = {}

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kwargs):
            captured["context"] = context
            return {"no_valuable_task": True, "reason": "n/a"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        llm_proposer.maybe_propose(state_dir, None)

        ctx = captured["context"]
        ids_present = sum(1 for it in items if f"[{it['id']}]" in ctx)
        assert ids_present == 2
        assert "ASSIGNED this one item" not in ctx
        assert not _rotation_path(state_dir).exists()


# ─── build_context(assigned=...) wording ────────────────────────────────────


class TestBuildContextAssignedWording:
    def test_assigned_true_changes_instruction_wording(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        items = [_item("defect", "defect-abc123", "fix the thing")]
        context = llm_proposer.build_context(state_dir, None, demand_items=items, assigned=True)
        assert "ASSIGNED this one item" in context
        assert "demand defect-abc123" in context
        assert "no_valuable_task" in context
        assert "Select ONE demand item above" not in context

    def test_assigned_default_false_leaves_golden_prompt_unchanged(self, tmp_path):
        """#902: assigned defaults to False so every pre-#902 call site
        (and its golden-prompt assertions) is byte-identical."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        items = [_item("defect", "defect-abc123", "fix the thing")]
        context = llm_proposer.build_context(state_dir, None, demand_items=items)
        assert "Select ONE demand item above and propose a bounded" in context
        assert "ASSIGNED this one item" not in context


# ─── _saturated_themes_section ──────────────────────────────────────────────


def _write_script(repo: Path, name: str) -> None:
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / name).write_text("# placeholder\n", encoding="utf-8")


class TestSaturatedThemesSection:
    def test_three_unconfirmed_scripts_share_subject(self, tmp_path):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        for name in (
            "check_repeat_failures.py",
            "audit_repeat_failures.py",
            "analyze_repeat_failures.py",
        ):
            _write_script(repo, name)

        section = llm_proposer._saturated_themes_section(state_dir, repo)
        assert "## Saturated themes" in section
        assert "repeat_failures" in section
        assert "3 scripts with no confirmed usage" in section

    def test_confirmed_usage_drops_one_below_threshold(self, tmp_path):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        for name in (
            "check_repeat_failures.py",
            "audit_repeat_failures.py",
            "analyze_repeat_failures.py",
        ):
            _write_script(repo, name)
        _write_usage_sidecar(
            state_dir,
            {"scripts/check_repeat_failures.py": {"last_used": "2026-08-01T00:00:00+00:00", "signal": "run"}},
        )

        section = llm_proposer._saturated_themes_section(state_dir, repo)
        assert section == ""

    def test_k_env_override(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        _write_script(repo, "check_repeat_failures.py")
        _write_script(repo, "audit_repeat_failures.py")

        monkeypatch.setenv(SATURATED_K_ENV, "2")
        section = llm_proposer._saturated_themes_section(state_dir, repo)
        assert "repeat_failures" in section

    def test_invalid_k_env_defaults_to_three(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        _write_script(repo, "check_repeat_failures.py")
        _write_script(repo, "audit_repeat_failures.py")

        monkeypatch.setenv(SATURATED_K_ENV, "not-a-number")
        section = llm_proposer._saturated_themes_section(state_dir, repo)
        assert section == ""  # only 2 unconfirmed, default K=3 not met

    def test_no_scripts_dir_returns_empty(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        state_dir = _state_dir(tmp_path)
        assert llm_proposer._saturated_themes_section(state_dir, repo) == ""

    def test_no_selfevo_repo_returns_empty(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert llm_proposer._saturated_themes_section(state_dir, None) == ""

    def test_init_and_test_files_excluded(self, tmp_path):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        _write_script(repo, "__init__.py")
        _write_script(repo, "test_repeat_failures.py")
        _write_script(repo, "conftest.py")
        _write_script(repo, "check_repeat_failures.py")
        _write_script(repo, "audit_repeat_failures.py")

        section = llm_proposer._saturated_themes_section(state_dir, repo)
        # Only 2 real subject scripts (check/audit) -> below default K=3.
        assert section == ""

    def test_fail_open_on_unreadable_usage_sidecar(self, tmp_path):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        for name in (
            "check_repeat_failures.py",
            "audit_repeat_failures.py",
            "analyze_repeat_failures.py",
        ):
            _write_script(repo, name)
        usage_dir = state_dir / "usage"
        usage_dir.mkdir(parents=True)
        (usage_dir / "last_used.json").write_text("{not valid json", encoding="utf-8")

        section = llm_proposer._saturated_themes_section(state_dir, repo)
        assert "## Saturated themes" in section
        assert "repeat_failures" in section

    def test_section_capped_at_max_chars(self, tmp_path):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        # Many distinct saturated subjects so the section would otherwise
        # exceed the cap.
        for subject_num in range(60):
            for verb in ("check", "audit", "analyze"):
                _write_script(repo, f"{verb}_subject{subject_num}_area.py")

        section = llm_proposer._saturated_themes_section(state_dir, repo)
        assert len(section) <= llm_proposer._MAX_SATURATED_SECTION_CHARS

    def test_files_per_theme_capped_with_ellipsis(self, tmp_path):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        verbs = ["check", "audit", "analyze", "prevent", "detect", "scan"]
        for verb in verbs:
            _write_script(repo, f"{verb}_repeat_failures.py")

        section = llm_proposer._saturated_themes_section(state_dir, repo)
        assert "…" in section


# ─── build_context integration: saturated section placement ────────────────


class TestBuildContextSaturatedSection:
    def test_saturated_section_appended_after_inventory(self, tmp_path):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        for name in (
            "check_repeat_failures.py",
            "audit_repeat_failures.py",
            "analyze_repeat_failures.py",
        ):
            _write_script(repo, name)

        context = llm_proposer.build_context(state_dir, repo)
        assert "## Saturated themes" in context
        inventory_idx = context.find("## Existing scripts")
        saturated_idx = context.find("## Saturated themes")
        if inventory_idx != -1:
            assert saturated_idx > inventory_idx

    def test_no_saturated_section_when_nothing_saturated(self, tmp_path):
        repo = tmp_path / "repo"
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        _write_script(repo, "check_repeat_failures.py")

        context = llm_proposer.build_context(state_dir, repo)
        assert "## Saturated themes" not in context
