"""Tests for #903: verb-invariant subject dedup (existence_index) + per-file
edit budget without confirmed use.

Two additions to ``llm_proposer._is_duplicate_proposal``, both fail-open and
gated on ``_proposal_creates_new_file``:

1. Subject dedup (NEW-file proposals only): a verb paraphrase like "audit
   repeat failures" vs an existing ``analyze_repeat_failures.py`` clears the
   plain lexical word-overlap threshold in
   ``cycle_planning._title_already_done_in_git_log`` (the verb dilutes the
   overlap). This check instead compares SUBJECT tokens (generic verbs
   stripped via the shared #902 ``_SATURATED_VERB_STOPLIST``) between the
   proposal and candidate ``scripts/*.py`` files, sourced from
   ``existence_index.related_scripts`` with a bounded plain-glob fallback
   when the FTS index is disabled/empty.
2. Edit budget (EDIT proposals targeting ``scripts/`` only): a target with
   ``>= M`` git commits since its usage-sidecar ``last_used`` timestamp (or
   since creation, if never confirmed used) is rejected — demonstrate usage
   or pick a different target. A confirmed use resets the window.

See the #903 docstrings in ``llm_proposer.py`` for the full design
rationale and the #798/#834 guards this complements without touching.
"""
from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import pytest

from nanobot.runtime import existence_index as ei
from nanobot.runtime import llm_proposer
from tests.test_llm_proposer import _state_dir, _write_goal_text, _write_usage_sidecar

SUBJECT_DEDUP_ENV = llm_proposer._SUBJECT_DEDUP_ENABLED_ENV
EDIT_BUDGET_M_ENV = llm_proposer._EDIT_BUDGET_M_ENV

# A commit date safely before every ``last_used`` timestamp used in this
# module's edit-budget fixtures, so the repo-creation "seed" commit (which
# necessarily touches every fixture script) never itself counts toward a
# budget window that starts at a LATER last_used.
_SEED_COMMIT_DATE = "2018-01-01T00:00:00"


@pytest.fixture(autouse=True)
def _existence_index_disabled_by_default(monkeypatch):
    """The FTS existence index defaults to ENABLED
    (``existence_index.existence_index_enabled()`` is True unless the env
    var is explicitly "0"). Disable it by default across this module so
    most tests deterministically exercise the glob-fallback path regardless
    of whether this Python's sqlite3 build has FTS5 compiled in.
    ``TestSubjectDedupViaExistenceIndex`` explicitly re-enables it within
    its own test body (same ``monkeypatch`` fixture instance, so the later
    ``setenv`` call wins)."""
    monkeypatch.setenv(ei.ENABLED_ENV, "0")


# ─── repo/script fixtures ───────────────────────────────────────────────────


def _git(repo: Path, *args: str, env: dict | None = None) -> None:
    sp.run(["git", "-C", str(repo), *args], capture_output=True, env=env, check=False)


def _init_repo_with_scripts(tmp_path: Path, scripts: list[str]) -> Path:
    """Minimal git repo (one commit) with each of ``scripts`` (repo-relative
    paths under scripts/) present as a placeholder file."""
    import os as _os

    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(_os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    _git(repo, "init", "-b", "main", env=env)
    _git(repo, "config", "user.email", "t@t", env=env)
    _git(repo, "config", "user.name", "t", env=env)
    for rel in scripts:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'"""placeholder for {rel}."""\n', encoding="utf-8")
    _git(repo, "add", "-A", env=env)
    # Dated safely in the past (see _SEED_COMMIT_DATE) so this seed commit —
    # which necessarily touches every fixture script — never itself counts
    # toward an edit-budget window that starts at a later last_used.
    seed_env = dict(env)
    seed_env["GIT_AUTHOR_DATE"] = _SEED_COMMIT_DATE
    seed_env["GIT_COMMITTER_DATE"] = _SEED_COMMIT_DATE
    sp.run(["git", "-C", str(repo), "commit", "-m", "seed"], capture_output=True, env=seed_env, check=False)
    return repo


def _commit_file_change(repo: Path, rel: str, message: str, iso_date: str | None = None) -> None:
    """Append a line to rel and commit it, optionally at a fixed author/
    committer date (so tests can control commit timestamps precisely)."""
    import os as _os

    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"# {message}\n")
    env = dict(_os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    if iso_date:
        env["GIT_AUTHOR_DATE"] = iso_date
        env["GIT_COMMITTER_DATE"] = iso_date
    _git(repo, "add", "-A", env=env)
    sp.run(["git", "-C", str(repo), "commit", "-m", message], capture_output=True, env=env, check=False)


def _fts5_available(tmp_path: Path) -> bool:
    """Best-effort probe: True when a reindex against a throwaway state/repo
    pair succeeds without an 'error' key (FTS5 compiled into this Python's
    sqlite3)."""
    state_dir = tmp_path / "_fts5_probe_state"
    repo = tmp_path / "_fts5_probe_repo"
    repo.mkdir(parents=True)
    try:
        counts = ei.reindex(state_dir, repo)
    except Exception:
        return False
    return "error" not in counts


# ─── Feature 1: verb-invariant subject dedup (glob fallback) ───────────────


class TestSubjectDedupGlobFallback:
    """FTS index left at its default (disabled unless SELFEVO_EXISTENCE_INDEX_ENABLED
    is set), so related_scripts() returns [] and the glob fallback path runs."""

    def test_verb_paraphrase_new_file_rejected(self, tmp_path):
        repo = _init_repo_with_scripts(tmp_path, ["scripts/analyze_repeat_failures.py"])
        state = _state_dir(tmp_path)
        proposal = {
            "task_title": "Create audit_repeat_failures.py to audit repeat failures",
            "target_path": "scripts/audit_repeat_failures.py",
        }
        dup, feedback, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is True
        assert "analyze_repeat_failures.py" in feedback
        assert matched == "subject-duplicate:scripts/analyze_repeat_failures.py"

    def test_genuinely_new_subject_not_rejected(self, tmp_path):
        repo = _init_repo_with_scripts(
            tmp_path,
            ["scripts/analyze_repeat_failures.py", "scripts/check_repeat_failures.py"],
        )
        state = _state_dir(tmp_path)
        proposal = {
            "task_title": "create parse_release_notes.py to parse release notes",
            "target_path": "scripts/parse_release_notes.py",
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_distinct_subjects_sharing_generic_tail_do_not_merge(self, tmp_path):
        """'usage' is not a stoplisted verb, so check_disk_usage and
        audit_memory_usage share only the generic tail token 'usage' (1
        overlap) — below the >=2-token-overlap bar, and neither's subject
        set is a subset of the other's ({'disk','usage'} vs
        {'memory','usage'})."""
        repo = _init_repo_with_scripts(tmp_path, ["scripts/check_disk_usage.py"])
        state = _state_dir(tmp_path)
        proposal = {
            "task_title": "create audit_memory_usage.py to audit memory usage",
            "target_path": "scripts/audit_memory_usage.py",
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_edit_proposal_never_hits_subject_dedup(self, tmp_path):
        """The new-file gate must be respected: an EDIT of an existing file
        whose title paraphrases another existing script's subject must not
        be flagged by the subject-dedup check (it may still be governed by
        the (unrelated) edit-budget check, which needs >= M commits to
        fire — a single fixture commit is far under the default M=5)."""
        repo = _init_repo_with_scripts(
            tmp_path,
            ["scripts/analyze_repeat_failures.py", "scripts/audit_repeat_failures.py"],
        )
        state = _state_dir(tmp_path)
        proposal = {
            "task_title": "audit repeat failures more thoroughly",
            "target_path": "scripts/audit_repeat_failures.py",  # EXISTS -> edit
        }
        dup, _, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False
        assert not matched.startswith("subject-duplicate:")

    def test_kill_switch_disables_subject_check(self, tmp_path, monkeypatch):
        monkeypatch.setenv(SUBJECT_DEDUP_ENV, "0")
        repo = _init_repo_with_scripts(tmp_path, ["scripts/analyze_repeat_failures.py"])
        state = _state_dir(tmp_path)
        proposal = {
            "task_title": "Create audit_repeat_failures.py to audit repeat failures",
            "target_path": "scripts/audit_repeat_failures.py",
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_kill_switch_accepts_false_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv(SUBJECT_DEDUP_ENV, "false")
        repo = _init_repo_with_scripts(tmp_path, ["scripts/analyze_repeat_failures.py"])
        state = _state_dir(tmp_path)
        proposal = {
            "task_title": "Create audit_repeat_failures.py to audit repeat failures",
            "target_path": "scripts/audit_repeat_failures.py",
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_no_selfevo_repo_fails_open(self, tmp_path):
        state = _state_dir(tmp_path)
        proposal = {
            "task_title": "Create audit_repeat_failures.py to audit repeat failures",
            "target_path": "scripts/audit_repeat_failures.py",
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, None, proposal)
        assert dup is False


# ─── Feature 1: through the real existence_index (FTS route) ───────────────


class TestSubjectDedupViaExistenceIndex:
    def test_verb_paraphrase_rejected_via_fts_index(self, tmp_path, monkeypatch):
        if not _fts5_available(tmp_path / "probe"):
            pytest.skip("sqlite3 build lacks FTS5 support")
        monkeypatch.setenv(ei.ENABLED_ENV, "1")
        repo = tmp_path / "repo"
        repo.mkdir()
        script = repo / "scripts" / "analyze_repeat_failures.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text('"""Analyze repeat failures across the ledger."""\n', encoding="utf-8")

        state = _state_dir(tmp_path)
        proposal = {
            "task_title": "Create audit_repeat_failures.py to audit repeat failures",
            "target_path": "scripts/audit_repeat_failures.py",
        }
        dup, feedback, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is True
        assert "analyze_repeat_failures.py" in feedback
        assert matched == "subject-duplicate:scripts/analyze_repeat_failures.py"


# ─── Feature 2: per-file edit budget without confirmed use ─────────────────


class TestEditBudget:
    def test_at_or_above_m_commits_since_last_used_rejected(self, tmp_path):
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        _write_usage_sidecar(state, {"scripts/flaky_tool.py": {"last_used": "2020-01-01T00:00:00+00:00"}})
        for i in range(5):
            _commit_file_change(repo, "scripts/flaky_tool.py", f"revision {i}", iso_date="2021-01-01T00:00:00")

        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, feedback, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is True
        assert "5 revisions" in feedback
        assert matched == "edit-budget:scripts/flaky_tool.py"

    def test_below_m_commits_passes(self, tmp_path):
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        _write_usage_sidecar(state, {"scripts/flaky_tool.py": {"last_used": "2020-01-01T00:00:00+00:00"}})
        for i in range(4):
            _commit_file_change(repo, "scripts/flaky_tool.py", f"revision {i}", iso_date="2021-01-01T00:00:00")

        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_last_used_after_commits_resets_budget(self, tmp_path):
        """A confirmed use stamped AFTER all the revision commits resets the
        window to zero — the count since last_used is 0, well under M."""
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        for i in range(5):
            _commit_file_change(repo, "scripts/flaky_tool.py", f"revision {i}", iso_date="2021-01-01T00:00:00")
        _write_usage_sidecar(state, {"scripts/flaky_tool.py": {"last_used": "2099-01-01T00:00:00+00:00"}})

        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_never_used_counts_full_history(self, tmp_path):
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        # No usage sidecar at all -> never used -> full-history count,
        # which already includes the seed commit + 5 more = 6 >= default M=5.
        for i in range(5):
            _commit_file_change(repo, "scripts/flaky_tool.py", f"revision {i}")

        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, feedback, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is True
        assert "creation" in feedback
        assert matched == "edit-budget:scripts/flaky_tool.py"

    def test_m_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv(EDIT_BUDGET_M_ENV, "2")
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        _write_usage_sidecar(state, {"scripts/flaky_tool.py": {"last_used": "2020-01-01T00:00:00+00:00"}})
        for i in range(2):
            _commit_file_change(repo, "scripts/flaky_tool.py", f"revision {i}", iso_date="2021-01-01T00:00:00")

        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is True

    def test_invalid_m_env_defaults_to_five(self, tmp_path, monkeypatch):
        monkeypatch.setenv(EDIT_BUDGET_M_ENV, "not-a-number")
        assert llm_proposer._edit_budget_m() == 5

    def test_m_zero_disables_check(self, tmp_path, monkeypatch):
        monkeypatch.setenv(EDIT_BUDGET_M_ENV, "0")
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        _write_usage_sidecar(state, {"scripts/flaky_tool.py": {"last_used": "2020-01-01T00:00:00+00:00"}})
        for i in range(10):
            _commit_file_change(repo, "scripts/flaky_tool.py", f"revision {i}", iso_date="2021-01-01T00:00:00")

        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_new_file_proposal_never_hits_edit_budget(self, tmp_path):
        """The edit-budget check is gated on NOT _proposal_creates_new_file
        — a brand-new target_path (however many times some OTHER file has
        been revised) must never be blocked by this check."""
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        for i in range(10):
            _commit_file_change(repo, "scripts/flaky_tool.py", f"revision {i}")

        proposal = {"task_title": "create brand new helper", "target_path": "scripts/brand_new_helper.py"}
        dup, _, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert not matched.startswith("edit-budget:")

    def test_non_scripts_target_not_checked(self, tmp_path):
        repo = _init_repo_with_scripts(tmp_path, ["docs/notes.md"])
        state = _state_dir(tmp_path)
        for i in range(10):
            _commit_file_change(repo, "docs/notes.md", f"revision {i}")

        proposal = {"task_title": "update notes further", "target_path": "docs/notes.md"}
        dup, _, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert not matched.startswith("edit-budget:")

    def test_fail_open_git_unavailable(self, tmp_path, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("git not found")

        import subprocess as _sp

        monkeypatch.setattr(_sp, "check_output", _boom)
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_fail_open_git_timeout(self, tmp_path, monkeypatch):
        def _timeout(*args, **kwargs):
            raise sp.TimeoutExpired(cmd="git", timeout=10)

        import subprocess as _sp

        monkeypatch.setattr(_sp, "check_output", _timeout)
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_unreadable_sidecar_treated_as_never_used_but_still_fail_open_on_git_error(self, tmp_path, monkeypatch):
        """An unreadable/malformed sidecar degrades to '{}' (never used) per
        _load_inventory_usage_entries's own fail-open contract; separately, a
        git failure on top of that still fails open to 'not a duplicate'."""
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        usage_dir = state / "usage"
        usage_dir.mkdir(parents=True)
        (usage_dir / "last_used.json").write_text("{not valid json", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        import subprocess as _sp

        monkeypatch.setattr(_sp, "check_output", _boom)
        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_no_selfevo_repo_fails_open(self, tmp_path):
        state = _state_dir(tmp_path)
        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, None, proposal)
        assert dup is False


# ─── ledger contract: matched_against prefixes recorded via reject path ────


class TestLedgerContract:
    def test_subject_duplicate_matched_against_recorded_via_maybe_propose(self, tmp_path, monkeypatch):
        monkeypatch.setenv(llm_proposer.ENABLED_ENV, "1")
        from nanobot.runtime import demand as demand_mod

        monkeypatch.setenv(demand_mod.ENABLED_ENV, "0")
        monkeypatch.setattr(llm_proposer, "_idle_recorded_this_process", False)

        repo = _init_repo_with_scripts(tmp_path, ["scripts/analyze_repeat_failures.py"])
        state = _state_dir(tmp_path)
        _write_goal_text(state, "no priority section, so should_propose is True")

        calls = {"n": 0}

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kwargs):
            calls["n"] += 1
            return {
                "task_title": "Create audit_repeat_failures.py to audit repeat failures",
                "rationale": "closes a gap",
                "target_path": "scripts/audit_repeat_failures.py",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state, repo)
        assert result is None  # rejected both the initial try and the retry

        rows = [
            json.loads(line)
            for line in (state / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        reject_rows = [r for r in rows if r.get("phase") == "proposer_reject" and r.get("reason") == "self_dedup"]
        assert reject_rows
        assert any(
            str(r.get("matched_against", "")).startswith("subject-duplicate:") for r in reject_rows
        )

    def test_edit_budget_matched_against_recorded_via_direct_guard_call(self, tmp_path):
        """Matches the granularity of the existing #834/#878 direct-guard
        tests in tests/test_llm_proposer.py (they assert on
        _is_duplicate_proposal's own return value rather than driving the
        full maybe_propose LLM-retry loop)."""
        repo = _init_repo_with_scripts(tmp_path, ["scripts/flaky_tool.py"])
        state = _state_dir(tmp_path)
        _write_usage_sidecar(state, {"scripts/flaky_tool.py": {"last_used": "2020-01-01T00:00:00+00:00"}})
        for i in range(5):
            _commit_file_change(repo, "scripts/flaky_tool.py", f"revision {i}", iso_date="2021-01-01T00:00:00")

        proposal = {"task_title": "improve flaky tool further", "target_path": "scripts/flaky_tool.py"}
        dup, _, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is True
        assert matched.startswith("edit-budget:")
