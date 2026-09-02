"""#1209: curator writes must survive the cycle-start reset and the integration base reset.

Measured on the host 2026-09-02: the reflector v2 mint wrote ``lessons/lessons.yaml``
into the instance working tree at 06:19:00 MSK; the next bridge run's
``_restore_to_main`` (``git reset --hard && git clean -fd``) erased it at
06:21:45.97 MSK. Staged facts died one step later: the pickup commit landed on
local ``main``, the cycle branch was cut from ``origin/main``, and the integration
step's ``checkout -B main <origin base>`` orphaned it — six of seven pickup
commits dangling (#986) while the journal said "committed N fact(s) on main".

The simulations below mirror the bridge's own git sequence, not a stand-in:
``_cycle_start_reset`` is ``_restore_to_main``; ``_cycle_integration`` is
``_setup_cycle_branch`` (branch from ``origin/main``) followed by
``_integrate_cycle_to_main`` (``checkout -B main origin/main``, ``--no-ff`` merge,
push). Durability is asserted with ``git show origin/main:<path>`` only.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import yaml

from nanobot.runtime.bridge import _pickup_staged_promotions
from nanobot.runtime.knowledge_curator import (
    _STAGED_DIR,
    load_staged_manifest,
    promote_reflector_recommendations_to_v2,
    run_curation,
)

# Spelled out rather than imported so this file still collects against a tree
# that predates the fix and fails on its assertions, not at import time.
LESSONS_KIND = "lessons_v2"
LESSONS_REL = "lessons/lessons.yaml"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _init_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout on ``main`` tracking a bare ``origin`` — the instance repo's shape."""
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "Test")
    (repo / "lessons").mkdir()
    (repo / "memory").mkdir()
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    (repo / "lessons" / "lessons.yaml").write_text("lessons: []\n", encoding="utf-8")
    (repo / "memory" / "index.md").write_text("# Index\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    return repo, origin


def _cycle_start_reset(repo: Path) -> None:
    """bridge._restore_to_main: what runs at the start of every cycle."""
    _git(repo, "reset", "--hard")
    _git(repo, "clean", "-fd")
    _git(repo, "checkout", "main")


def _cycle_integration(repo: Path, name: str) -> None:
    """bridge._setup_cycle_branch + _integrate_cycle_to_main, minus the gate."""
    _git(repo, "fetch", "origin", "main")
    _git(repo, "checkout", "-B", f"selfevo/cycle-{name}", "origin/main")
    (repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", f"{name}.txt")
    _git(repo, "commit", "-m", f"cycle {name}")
    _git(repo, "checkout", "-B", "main", "origin/main")
    _git(repo, "merge", "--no-ff", f"selfevo/cycle-{name}", "-m", f"merge: integrate {name}")
    _git(repo, "push", "origin", "main")


def _origin_main_show(repo: Path, rel: str) -> str | None:
    _git(repo, "fetch", "origin", "main")
    proc = subprocess.run(["git", "-C", str(repo), "show", f"origin/main:{rel}"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def _reflection(state: Path, detail: str, cycle_id: str = "cycle-1209abcdef00") -> None:
    reflector = state / "reflector"
    reflector.mkdir(parents=True, exist_ok=True)
    (reflector / "reflections.jsonl").write_text(json.dumps({
        "cycle_id": cycle_id,
        "summary": "Reflector found an unbounded parser read on a growing journal",
        "recommendations": [{"kind": "approach_hint", "detail": detail}],
    }) + "\n", encoding="utf-8")


def _stage_fact(state: Path, name: str = "durable-fact", lesson_id: str = "L-1209") -> None:
    staged = state / "curator" / _STAGED_DIR
    staged.mkdir(parents=True, exist_ok=True)
    slug = f"memory__facts__{name}.md"
    (staged / slug).write_text(f"# {name}\n\nA fact that must reach origin/main.\n", encoding="utf-8")
    (staged / "manifest.json").write_text(json.dumps([{
        "path": f"memory/facts/{name}.md", "action": "create", "payload_file": slug,
        "lesson_id": lesson_id, "index_line": f"- [{name}](memory/facts/{name}.md)",
        "index_rel": "memory/index.md",
    }]), encoding="utf-8")


def _decisions(state: Path) -> list[dict]:
    path = state / "curator" / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. The mint survives the reset that killed it live
# ---------------------------------------------------------------------------

def test_reflector_mint_survives_cycle_start_reset_and_two_integrations(tmp_path: Path) -> None:
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _reflection(state, "Read a bounded tail of the journal instead of the whole file")

    assert promote_reflector_recommendations_to_v2(repo, state, max_items=2) == 1
    # The curator never touches the checkout now; the card waits in staging.
    assert _git(repo, "status", "--porcelain") == ""
    assert [e["kind"] for e in load_staged_manifest(state) if e.get("kind")] == [LESSONS_KIND]

    _cycle_start_reset(repo)  # 06:21:45.97 MSK — the moment the live card died
    assert _pickup_staged_promotions(repo, state) == 1
    _cycle_integration(repo, "a")  # checkout -B main origin/main + merge + push
    _cycle_start_reset(repo)
    _cycle_integration(repo, "b")

    on_origin = _origin_main_show(repo, LESSONS_REL)
    assert on_origin is not None
    cards = yaml.safe_load(on_origin)["lessons"]
    assert len(cards) == 1
    assert cards[0]["id"].startswith("LESS-REF-1209abcdef00")
    assert cards[0]["solution"] == "Read a bounded tail of the journal instead of the whole file"
    assert cards[0]["tags"] == ["reflector"]
    assert load_staged_manifest(state) == []

    # Selection logic is untouched (#1209 non-goal): a recommendation still in
    # the newest rows re-folds into its card on the next run (#1138 suffixes the
    # id, find_duplicate matches the problem), so the next pickup bumps
    # seen_count on origin/main instead of minting a second card.
    assert promote_reflector_recommendations_to_v2(repo, state, max_items=2) == 1
    _cycle_start_reset(repo)
    assert _pickup_staged_promotions(repo, state) == 1
    cards = yaml.safe_load(_origin_main_show(repo, LESSONS_REL) or "")["lessons"]
    assert len(cards) == 1
    assert cards[0]["seen_count"] == 2


# ---------------------------------------------------------------------------
# 2. A staged fact survives the integration step that orphaned six of seven
# ---------------------------------------------------------------------------

def test_pickup_commit_survives_integration_base_reset(tmp_path: Path) -> None:
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _stage_fact(state)

    assert _pickup_staged_promotions(repo, state) == 1
    _cycle_integration(repo, "a")  # the step that dropped 409c5c29 on the host
    _cycle_start_reset(repo)
    _cycle_integration(repo, "b")

    assert _origin_main_show(repo, "memory/facts/durable-fact.md") is not None
    index = _origin_main_show(repo, "memory/index.md") or ""
    assert "- [durable-fact](memory/facts/durable-fact.md)" in index
    assert _git(repo, "log", "--oneline", "origin/main").count("curator: promote") == 1


# ---------------------------------------------------------------------------
# 3. The record says what survived: staged -> promoted, never promoted early
# ---------------------------------------------------------------------------

def test_decisions_record_staged_then_promoted_with_the_pushed_sha(tmp_path: Path) -> None:
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _reflection(state, "Prefer the bounded tail reader over a whole-file read")

    promote_reflector_recommendations_to_v2(repo, state, max_items=2)
    rows = _decisions(state)
    assert rows, "staging must leave a decision row"
    assert {r["decision"] for r in rows} == {"staged"}
    assert rows[0]["lesson_id"].startswith("LESS-REF-")
    assert rows[0]["target_file"] == LESSONS_REL

    _cycle_start_reset(repo)
    assert _pickup_staged_promotions(repo, state) == 1
    promoted = [r for r in _decisions(state) if r["decision"] == "promoted"]
    assert [r["lesson_id"] for r in promoted] == [rows[0]["lesson_id"]]
    pushed_sha = _git(repo, "rev-parse", "origin/main")
    assert promoted[0]["reason"] == f"pushed to origin/main as {pushed_sha[:12]}"


def test_run_curation_records_staged_not_promoted(tmp_path: Path) -> None:
    """The LLM-curated facts follow the same rule: ``promoted`` is the bridge's word."""
    workspace = tmp_path / "workspace"
    (workspace / "lessons").mkdir(parents=True)
    (workspace / "lessons" / "lessons.yaml").write_text(
        "- id: L1\n  title: insight L1\n  approach: use L1\n  evidence: ['#1209']\n", encoding="utf-8",
    )
    state = tmp_path / "state"

    def llm(messages, model):
        return json.dumps([{
            "action": "create", "path": "memory/facts/novel.md", "content": "novel durable fact",
            "lesson_id": "L1", "reason": "new", "evidence": ["#1209"], "support_claim": "novel durable fact",
        }])

    result = run_curation(workspace, state, llm=llm)
    assert result["ok"] and result["writes"] == 1
    assert {r["decision"] for r in _decisions(state)} == {"staged"}
    assert load_staged_manifest(state)[0]["lesson_id"] == "L1"


# ---------------------------------------------------------------------------
# 4. A write that cannot be made durable is not reported as success
# ---------------------------------------------------------------------------

def test_push_failure_drops_the_commit_keeps_staging_and_records_deferral(tmp_path: Path) -> None:
    repo, origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _stage_fact(state)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))  # origin unreachable
    before = _git(repo, "rev-parse", "HEAD")

    assert _pickup_staged_promotions(repo, state) == 0

    assert _git(repo, "rev-parse", "HEAD") == before, "the undurable commit must be dropped"
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / "memory" / "facts" / "durable-fact.md").exists()
    assert len(load_staged_manifest(state)) == 1, "staging is retained for the next cycle"
    rows = _decisions(state)
    assert [r["decision"] for r in rows] == ["pickup_deferred"]
    assert rows[0]["lesson_id"] == "L-1209"
    assert rows[0]["reason"].startswith("push to origin/main failed:")

    # Restore the remote: the retry on the next cycle boundary succeeds.
    _git(repo, "remote", "set-url", "origin", str(origin))
    assert _pickup_staged_promotions(repo, state) == 1
    assert _origin_main_show(repo, "memory/facts/durable-fact.md") is not None
    assert [r["decision"] for r in _decisions(state)] == ["pickup_deferred", "promoted"]


def test_no_origin_remote_is_not_durable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    state = tmp_path / "state"
    _stage_fact(state)
    before = _git(repo, "rev-parse", "HEAD")

    assert _pickup_staged_promotions(repo, state) == 0
    assert _git(repo, "rev-parse", "HEAD") == before
    assert len(load_staged_manifest(state)) == 1
    assert [r["decision"] for r in _decisions(state)] == ["pickup_deferred"]
    assert "no origin remote" in _decisions(state)[0]["reason"]


# ---------------------------------------------------------------------------
# 5. Pickup-time merge is idempotent and folds duplicates like the mint did
# ---------------------------------------------------------------------------

def test_pickup_applies_cards_idempotently_and_folds_a_filler_duplicate(tmp_path: Path) -> None:
    repo, _origin = _init_repo_with_origin(tmp_path)
    (repo / "lessons" / "lessons.yaml").write_text(yaml.safe_dump({"lessons": [{
        "id": "LESS-000000000000-0000-0000-0000-000000000000",
        "problem": "Node missing",
        "solution": "Apply the reflected error pattern.",
        "seen_count": 1,
        "last_seen": "old-date",
    }]}), encoding="utf-8")
    _git(repo, "commit", "-am", "filler card")
    _git(repo, "push", "origin", "main")
    state = tmp_path / "state"
    reflector = state / "reflector"
    reflector.mkdir(parents=True)
    (reflector / "reflections.jsonl").write_text(json.dumps({
        "cycle_id": "cycle-fold000000001", "summary": "Node missing",
        "recommendations": [{"kind": "error_pattern", "detail": "Run apt-get update to fix missing package listings."}],
    }) + "\n", encoding="utf-8")

    assert promote_reflector_recommendations_to_v2(repo, state) == 1
    assert _pickup_staged_promotions(repo, state) == 1
    cards = yaml.safe_load(_origin_main_show(repo, LESSONS_REL))["lessons"]
    assert len(cards) == 1
    assert cards[0]["solution"] == "Run apt-get update to fix missing package listings."
    assert cards[0]["seen_count"] == 2
    assert load_staged_manifest(state) == []
