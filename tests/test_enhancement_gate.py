"""#1335: enhancement-shaped proposals aimed at scripts nothing runs are
deferred (``enhancement_without_caller``), before the title-dedup heuristic
in ``llm_proposer._is_duplicate_proposal``, with their own ledger reason
(``enhancement_gate.REASON`` == ``"enhancement_without_caller"``).

Uses synthetic product/instance roots under ``tmp_path`` — never the real
repo — so the caller-index scan is fully controlled per test.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanobot.runtime import demand, enhancement_gate, llm_proposer


@pytest.fixture(autouse=True)
def _clear_caller_index_cache():
    """The module caches ``build_caller_index`` results per-process by
    ``(root, selfevo_repo)`` key; tests must not share that cache."""
    enhancement_gate._index_cache.clear()
    yield
    enhancement_gate._index_cache.clear()


def _make_product_root(
    tmp_path: Path,
    *,
    with_nanobot_file: bool = True,
    nanobot_content: str = "pass\n",
    service_content: str | None = None,
    with_host_eeepc: bool = True,
    name: str = "product",
) -> Path:
    """Both ``_PRODUCT_DIRS`` (``nanobot/`` and ``host/eeepc/``) must exist
    with at least one scannable file for ``CallerIndex.status`` to read "ok"
    (#1335 contract: a missing product dir makes the whole index
    'unavailable'). ``with_host_eeepc=False`` opts a test out of that, to
    exercise the missing-dir path on purpose."""
    product = tmp_path / name
    product.mkdir(parents=True, exist_ok=True)
    if with_nanobot_file:
        nanobot_dir = product / "nanobot" / "runtime"
        nanobot_dir.mkdir(parents=True, exist_ok=True)
        (nanobot_dir / "foo.py").write_text(nanobot_content, encoding="utf-8")
    if with_host_eeepc:
        host_dir = product / "host" / "eeepc"
        host_dir.mkdir(parents=True, exist_ok=True)
        (host_dir / "placeholder.txt").write_text(
            "# host/eeepc placeholder so the product dir is scannable\n", encoding="utf-8"
        )
    if service_content is not None:
        systemd_dir = product / "host" / "eeepc" / "systemd"
        systemd_dir.mkdir(parents=True, exist_ok=True)
        (systemd_dir / "x.service").write_text(service_content, encoding="utf-8")
    return product


def _make_instance_repo(
    tmp_path: Path,
    *,
    scripts: dict[str, str] | None = None,
    extra_files: dict[str, str] | None = None,
    name: str = "instance",
) -> Path:
    instance = tmp_path / name
    for sub in ("scripts", "tests", "lessons", "ops"):
        (instance / sub).mkdir(parents=True, exist_ok=True)
    for fname, content in (scripts or {}).items():
        (instance / "scripts" / fname).write_text(content, encoding="utf-8")
    for relpath, content in (extra_files or {}).items():
        path = instance / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return instance


def _proposal(title: str, target: str) -> dict:
    return {"task_title": title, "target_path": target, "serves": "priority 1", "rationale": "r"}


# --------------------------------------------------------------------------
# 1. is_enhancement_shaped
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Add --json output to scripts/lesson_lookup.py",
        "Extend with CLI arguments and structured scan results",
        "Add dry-run mode, customizable retention days, and JSON summary",
        "Add path filtering options to scripts/x.py",
    ],
)
def test_is_enhancement_shaped_true(title):
    assert enhancement_gate.is_enhancement_shaped(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Fix JSON parsing in scripts/x.py",
        "Add mutation surface validation",
        "Implement retry on gateway timeout",
        "",
        None,
    ],
)
def test_is_enhancement_shaped_false(title):
    assert enhancement_gate.is_enhancement_shaped(title) is False


# --------------------------------------------------------------------------
# 2. Deferred: no caller anywhere that counts
# --------------------------------------------------------------------------


def test_enhancement_deferred_when_only_own_tests_and_lessons_mention_it(tmp_path):
    """Pre-fix (no gate): this proposal would sail through — its only
    'callers' are the script's own test and a lessons note, neither of
    which runs it."""
    product = _make_product_root(tmp_path)  # nanobot/runtime/foo.py, no reference
    instance = _make_instance_repo(
        tmp_path,
        scripts={"dead.py": "# nothing\n"},
        extra_files={
            "tests/test_dead.py": "# uses scripts/dead.py somewhere\n",
            "lessons/lessons.yaml": "note: scripts/dead.py is unused\n",
        },
    )
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    result = enhancement_gate.enhancement_without_caller(proposal, instance, root=product)

    assert result is not None
    feedback, matched = result
    assert matched == "enhancement_without_caller:scripts/dead.py"
    assert "scripts/dead.py" in feedback
    assert "caller index ok" in feedback


# --------------------------------------------------------------------------
# 3. NOT deferred
# --------------------------------------------------------------------------


def test_not_deferred_for_harness_candidate_json_flag(tmp_path):
    """(a) the issue's explicit acceptance: a validate_* --json proposal has
    a consumer (the validator harness) and is never deferred."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path, scripts={"validate_thing.py": "pass\n"})
    proposal = _proposal("Add --json output to scripts/validate_thing.py", "scripts/validate_thing.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None


def test_not_deferred_when_referenced_from_product_runtime(tmp_path):
    """(b) target quoted in nanobot/runtime/foo.py counts as a caller."""
    product = _make_product_root(tmp_path, nanobot_content='CALLERS = ["scripts/live1.py"]\n')
    instance = _make_instance_repo(tmp_path, scripts={"live1.py": "pass\n"})
    proposal = _proposal("Add --json output to scripts/live1.py", "scripts/live1.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None


def test_not_deferred_when_named_in_systemd_unit(tmp_path):
    """(c) target in a systemd unit's ExecStart line counts as a caller."""
    product = _make_product_root(
        tmp_path,
        service_content="[Service]\nExecStart=/usr/bin/python3 scripts/live2.py --daemon\n",
    )
    instance = _make_instance_repo(tmp_path, scripts={"live2.py": "pass\n"})
    proposal = _proposal("Add --json output to scripts/live2.py", "scripts/live2.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None


def test_not_deferred_when_referenced_from_instance_ops(tmp_path):
    """(d) target referenced from the instance repo's ops/ (not a skip dir)
    counts as a caller."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(
        tmp_path,
        scripts={"live3.py": "pass\n"},
        extra_files={"ops/run.sh": "#!/bin/sh\npython3 scripts/live3.py --json\n"},
    )
    proposal = _proposal("Add --json output to scripts/live3.py", "scripts/live3.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None


def test_not_deferred_when_target_does_not_exist(tmp_path):
    """(e) a target that doesn't exist in the instance repo is not this
    gate's business (creating a new script is a different shape)."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path)
    proposal = _proposal("Add --json output to scripts/ghost.py", "scripts/ghost.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None


def test_not_deferred_when_no_selfevo_repo(tmp_path):
    """(f) selfevo_repo=None."""
    product = _make_product_root(tmp_path)
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    assert enhancement_gate.enhancement_without_caller(proposal, None, root=product) is None


def test_not_deferred_for_fix_shaped_title(tmp_path):
    """(g) a fix-shaped title on an otherwise-dead script never matches."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})
    proposal = _proposal("Fix JSON parsing in scripts/dead.py", "scripts/dead.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None


def test_not_deferred_for_non_scripts_target(tmp_path):
    """(h) a target outside scripts/ is out of scope for this gate."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path)
    proposal = _proposal("Add --json output to surfaces/x.py", "surfaces/x.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None


def test_fail_open_when_caller_index_unavailable(tmp_path, caplog):
    """(i) a caller index that scanned zero files is 'unavailable' and never
    defers (absence of data is not a zero) — and it logs a warning."""
    empty_product = tmp_path / "empty_product"
    empty_product.mkdir()
    # scripts/tests/lessons/ops all present but only scripts/ has the (skip-dir)
    # target file — nothing scannable outside the skip dirs.
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    index = enhancement_gate.build_caller_index(empty_product, instance)
    assert index.status == "unavailable"

    with caplog.at_level(logging.WARNING, logger="nanobot.runtime.enhancement_gate"):
        result = enhancement_gate.enhancement_without_caller(proposal, instance, root=empty_product)

    assert result is None
    assert "unavailable" in caplog.text


# --------------------------------------------------------------------------
# 4. build_caller_index
# --------------------------------------------------------------------------


def test_build_caller_index_counts_files_and_finds_callers(tmp_path):
    product = _make_product_root(tmp_path, nanobot_content='call("scripts/dead.py")\n')
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})

    index = enhancement_gate.build_caller_index(product, instance)

    assert index.files_scanned >= 1
    assert index.callers_of("dead.py") == ["nanobot:nanobot/runtime/foo.py"]


def test_build_caller_index_skips_markdown_files(tmp_path):
    product = _make_product_root(tmp_path)  # unrelated nanobot/runtime/foo.py so the index is "ok"
    (product / "nanobot" / "runtime" / "notes.md").write_text(
        "see scripts/dead.py for details\n", encoding="utf-8"
    )
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})

    index = enhancement_gate.build_caller_index(product, instance)

    assert index.status == "ok"
    assert index.callers_of("dead.py") == []


def test_build_caller_index_skips_instance_scripts_tests_lessons_memory_docs_nanobot(tmp_path):
    """#1335 contract change: ``scripts/`` is no longer a skip dir (it is
    now scanned for sibling-script callers) — only tests/lessons/memory/docs
    /nanobot stay skipped at the instance top level. ``scripts/dead.py``
    mentioning only its own basename is still not its own caller."""
    empty_product = tmp_path / "empty_product"
    empty_product.mkdir()
    instance = tmp_path / "instance"
    for sub in ("tests", "lessons", "memory", "docs", "nanobot"):
        directory = instance / sub
        directory.mkdir(parents=True)
        (directory / "ref.py").write_text('"scripts/dead.py"\n', encoding="utf-8")
    (instance / "scripts").mkdir(parents=True)
    (instance / "scripts" / "dead.py").write_text('"scripts/dead.py"\n', encoding="utf-8")

    index = enhancement_gate.build_caller_index(empty_product, instance)

    assert index.files_scanned == 1  # only scripts/dead.py itself; the 5 skip dirs are excluded
    assert index.status == "unavailable"  # both product dirs (nanobot/, host/eeepc/) missing
    assert index.callers_of("dead.py") == []  # dead.py's own self-mention is not its own caller


def test_build_caller_index_caches_by_key(tmp_path):
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})

    first = enhancement_gate.build_caller_index(product, instance)
    second = enhancement_gate.build_caller_index(product, instance)

    assert first is second


# --------------------------------------------------------------------------
# 5. End-to-end through llm_proposer.maybe_propose
# --------------------------------------------------------------------------


def test_proposer_defers_enhancement_without_caller_with_its_own_reason(tmp_path, monkeypatch):
    """Pre-fix: this proposal would have passed dedup and been written; no
    ``enhancement_without_caller`` reason existed."""
    state = tmp_path / "state"
    (state / "goals").mkdir(parents=True)
    (state / "ledger").mkdir()
    (state / "demand").mkdir()

    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})

    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    # same gates as tests/test_llm_proposer.py's autouse fixture: proposer on,
    # the pre-#760 supply-driven policy (no demand items needed to fire)
    monkeypatch.setenv("SELFEVO_LLM_PROPOSER_ENABLED", "1")
    monkeypatch.setenv("SELFEVO_DEMAND_DRIVEN_ENABLED", "0")
    (state / "goals" / "goal_text.json").write_text(
        json.dumps({"text": "no priority section, so should_propose is True"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        llm_proposer,
        "propose",
        lambda context, *, rejection_reason=None, timeout=120.0, system_prompt=None: dict(proposal),
    )
    monkeypatch.setattr(enhancement_gate, "product_root", lambda: product)

    assert llm_proposer.maybe_propose(state, instance) is None

    rows = [
        json.loads(line)
        for line in (state / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rejects = [r for r in rows if r.get("phase") == "proposer_reject"]
    assert rejects
    last = rejects[-1]
    assert last["reason"] == "enhancement_without_caller"
    assert last["target_path"] == "scripts/dead.py"
    assert last["matched_against"] == "enhancement_without_caller:scripts/dead.py"
    # The ledger caps ``detail`` at 200 chars, so the caller-index description
    # must lead the feedback to survive into the row: a deferral row has to
    # prove what was scanned, not only that something was refused.
    assert last["detail"].startswith("caller index ok: ")
    assert "files under nanobot/, host/eeepc/, instance/" in last["detail"]
    assert "scripts/dead.py" in last["detail"]


# --------------------------------------------------------------------------
# 6. _dedup_reject_reason
# --------------------------------------------------------------------------


def test_dedup_reject_reason_mapping():
    assert llm_proposer._dedup_reject_reason("enhancement_without_caller:scripts/x.py") == "enhancement_without_caller"
    assert llm_proposer._dedup_reject_reason("futile_surface:g1") == "futile_surface"
    assert llm_proposer._dedup_reject_reason("anything else") == "self_dedup"


# --------------------------------------------------------------------------
# 7. Allowlist drift, near-miss titles, nested/sibling callers, unavailable
#    variants, fail-open, backslash normalisation, and the exhaustion coupling
#    (#1335 follow-up tests)
# --------------------------------------------------------------------------


def test_harness_candidate_pattern_matches_validator_harness_allowlist():
    """The two regexes are duplicated on purpose (this module must not import
    the harness) — pin them equal so they cannot silently drift apart."""
    from nanobot.runtime import validator_harness

    assert enhancement_gate._HARNESS_CANDIDATE_RE.pattern == validator_harness._ALLOWLIST_RE.pattern


@pytest.mark.parametrize(
    "title",
    [
        "Add error handling for JSON decode failures",
        "Add CLI smoke test for scripts/x.py",
        "Add retry on JSON decode error in scripts/x.py",
    ],
)
def test_is_enhancement_shaped_near_miss_false(title):
    assert enhancement_gate.is_enhancement_shaped(title) is False


@pytest.mark.parametrize(
    "title",
    [
        "Add --json output to scripts/x.py",
        "Add JSON output formatting to scripts/x.py",
        "Extend with CLI arguments and structured scan results",
    ],
)
def test_is_enhancement_shaped_near_miss_true(title):
    assert enhancement_gate.is_enhancement_shaped(title) is True


def test_nested_instance_skip_dir_is_still_scanned(tmp_path):
    """``_INSTANCE_SKIP_DIRS`` applies to the instance root's direct children
    only — a nested ``ops/scripts/run.sh`` is scanned even though a top-level
    ``scripts/`` sibling exists too."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(
        tmp_path,
        scripts={"dead.py": "pass\n"},
        extra_files={"ops/scripts/run.sh": "#!/bin/sh\npython3 scripts/dead.py --json\n"},
    )
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None

    index = enhancement_gate.build_caller_index(product, instance)
    assert "instance:ops/scripts/run.sh" in index.callers_of("dead.py")


def test_instance_sibling_script_counts_as_caller(tmp_path):
    """A sibling script under the (now-scanned) instance ``scripts/`` dir
    referencing the target counts as a caller."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(
        tmp_path,
        scripts={"wrapper.py": "calls scripts/dead.py\n", "dead.py": "pass\n"},
    )
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None

    index = enhancement_gate.build_caller_index(product, instance)
    assert "instance:scripts/wrapper.py" in index.callers_of("dead.py")


def test_self_reference_does_not_count_as_own_caller(tmp_path):
    """A script's own usage/docstring line naming itself is excluded — with
    nothing else referencing it, it is still deferred."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(
        tmp_path,
        scripts={"dead.py": "Usage: python scripts/dead.py --json\n"},
    )
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    index = enhancement_gate.build_caller_index(product, instance)
    assert index.callers_of("dead.py") == []

    result = enhancement_gate.enhancement_without_caller(proposal, instance, root=product)
    assert result is not None


def test_missing_product_dir_is_unavailable(tmp_path, caplog):
    """A product root with ``nanobot/`` but no ``host/eeepc/`` is
    'unavailable' — the gate never defers, and it logs a warning."""
    product = _make_product_root(tmp_path, with_host_eeepc=False)
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    index = enhancement_gate.build_caller_index(product, instance)
    assert index.status == "unavailable"
    assert "missing host/eeepc/" in index.describe()

    with caplog.at_level(logging.WARNING, logger="nanobot.runtime.enhancement_gate"):
        result = enhancement_gate.enhancement_without_caller(proposal, instance, root=product)

    assert result is None
    assert "unavailable" in caplog.text


def test_truncated_index_is_unavailable(tmp_path, monkeypatch):
    """Hitting the file cap mid-walk marks the index truncated (and hence
    unavailable), even though files were actually scanned."""
    monkeypatch.setattr(enhancement_gate, "_MAX_FILES", 2)
    product = _make_product_root(tmp_path)
    nanobot_dir = product / "nanobot" / "runtime"
    (nanobot_dir / "foo2.py").write_text("pass\n", encoding="utf-8")
    (nanobot_dir / "foo3.py").write_text("pass\n", encoding="utf-8")
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    index = enhancement_gate.build_caller_index(product, instance)
    assert index.truncated is True
    assert index.status == "unavailable"

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None


def test_fail_open_when_build_caller_index_raises(tmp_path, monkeypatch, caplog):
    """A ``build_caller_index`` exception must never surface as a deferral —
    fail-open, with a logged 'gate error' trace."""
    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(enhancement_gate, "build_caller_index", _raise)
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    with caplog.at_level(logging.WARNING, logger="nanobot.runtime.enhancement_gate"):
        result = enhancement_gate.enhancement_without_caller(proposal, instance, root=product)

    assert result is None
    assert "gate error" in caplog.text


def test_backslash_target_path_is_normalised(tmp_path):
    """A ``target_path`` spelled with a backslash still matches
    ``scripts/<name>.py`` and reports the forward-slash form."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts\\dead.py")

    result = enhancement_gate.enhancement_without_caller(proposal, instance, root=product)

    assert result is not None
    _, matched = result
    assert matched == "enhancement_without_caller:scripts/dead.py"


def test_windows_style_reference_in_scanned_file_counts_as_caller(tmp_path):
    """``_scan`` normalises backslashes in file text before matching, so a
    Windows-style ``scripts\\dead.py`` reference in a scanned file counts."""
    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(
        tmp_path,
        scripts={"dead.py": "pass\n"},
        extra_files={"ops/run.sh": "python3 scripts\\dead.py --json\n"},
    )
    proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")

    assert enhancement_gate.enhancement_without_caller(proposal, instance, root=product) is None

    index = enhancement_gate.build_caller_index(product, instance)
    assert "instance:ops/run.sh" in index.callers_of("dead.py")


def _write_ledger_row(state_dir: Path, row: dict) -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    row = dict(row)
    row.setdefault("ts", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    with open(ledger_dir / "cycles.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_proposer_e2e_retry_then_self_dedup_on_recent_proposed_title(tmp_path, monkeypatch):
    """End-to-end retry branch: the first proposal is deferred as
    ``enhancement_without_caller`` (dead script); the retry, told the
    rejection reason, comes back with a normal-target proposal whose title
    duplicates a recently-``proposed`` ledger row — ``maybe_propose`` returns
    None with a ``self_dedup`` reject and no ``detail`` key."""
    state = tmp_path / "state"
    (state / "goals").mkdir(parents=True)
    (state / "ledger").mkdir()
    (state / "demand").mkdir()

    product = _make_product_root(tmp_path)
    instance = _make_instance_repo(tmp_path, scripts={"dead.py": "pass\n"})

    _write_ledger_row(state, {"phase": "proposed", "task_title": "Harden the widget parser"})

    enhancement_proposal = _proposal("Add --json output to scripts/dead.py", "scripts/dead.py")
    dup_proposal = _proposal("Harden the widget parser", "docs/notes.md")

    monkeypatch.setenv("SELFEVO_LLM_PROPOSER_ENABLED", "1")
    monkeypatch.setenv("SELFEVO_DEMAND_DRIVEN_ENABLED", "0")
    (state / "goals" / "goal_text.json").write_text(
        json.dumps({"text": "no priority section, so should_propose is True"}), encoding="utf-8"
    )

    calls = {"n": 0}

    def _fake_propose(context, *, rejection_reason=None, timeout=120.0, system_prompt=None):
        calls["n"] += 1
        if calls["n"] == 1:
            assert rejection_reason is None
            return dict(enhancement_proposal)
        assert rejection_reason  # the retry must have been told why
        return dict(dup_proposal)

    monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
    monkeypatch.setattr(llm_proposer, "_recent_git_log", lambda *a, **k: "")
    monkeypatch.setattr(enhancement_gate, "product_root", lambda: product)

    assert llm_proposer.maybe_propose(state, instance) is None
    assert calls["n"] == 2

    rows = [
        json.loads(line)
        for line in (state / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rejects = [r for r in rows if r.get("phase") == "proposer_reject"]
    assert rejects
    last = rejects[-1]
    assert last["reason"] == "self_dedup"
    assert "detail" not in last


def test_consecutive_self_dedup_rejects_counts_enhancement_without_caller(tmp_path):
    """#1335: an ``enhancement_without_caller`` reject is an exhausting
    reason too, so it must contribute to the trailing self-dedup streak the
    same way a plain ``self_dedup`` reject does."""
    state = tmp_path / "state"
    _write_ledger_row(state, {"phase": "proposer_reject", "reason": "self_dedup"})
    _write_ledger_row(state, {"phase": "proposer_reject", "reason": "enhancement_without_caller"})

    assert llm_proposer._consecutive_self_dedup_rejects(state) == 2
    assert demand._EXHAUSTING_REJECT_REASONS == frozenset({"self_dedup", "enhancement_without_caller"})
