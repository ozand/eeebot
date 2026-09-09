"""#1447: the memory-index accounting must survive to a queryable surface,
and must never be the previous cycle's numbers.

`memory/index.md` is instance-owned. The loop can rewrite it, delete it, or
leave it undecodable, and all three previously ended somewhere unhelpful:

* deleted  -> ``last_index_fit`` kept the PREVIOUS build's accounting, so one
  cycle would journal another cycle's numbers as its own;
* undecodable -> ``UnicodeDecodeError`` propagated out of prompt construction,
  letting the instance fail its own cycle with one bad byte;
* renamed rule entry -> ``resident_matched`` / ``resident_missing`` were added
  by #1443 but dropped by an allowlist in ``ContextBuilder``, so the signal
  never reached a caller at all.

ADR-002 / #1173: an unreadable input is a value, not an exception.
"""
from __future__ import annotations

from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore

RULES = [
    "# Memory index", "", "## Facts (memory/facts/)", "",
    "* [Identity](facts/identity.md)",
    "* [Write target: workspace](facts/write-target.md)",
    "* [DO NOT touch](facts/do-not-touch.md)",
    "* [Rules](facts/rules.md)",
    "* [Key paths on host](facts/key-paths.md)",
]


def _index(tmp_path: Path, lines: list[str]) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir(exist_ok=True)
    path = mem / "index.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_absent_index_reports_missing_and_never_the_previous_accounting(tmp_path: Path):
    """The stale-fit bug: a deleted index must not inherit the last build."""
    index = _index(tmp_path, RULES + [f"- [F{i}](f/{i}.md) filler" for i in range(200)])
    store = MemoryStore(tmp_path)
    store.get_memory_context(loop=True, max_chars=500)
    assert store.last_index_fit["source_chars"] > 0

    index.unlink()
    assert store.get_memory_context(loop=True, max_chars=500) == ""
    fit = store.last_index_fit
    assert fit["status"] == "missing"
    assert "source_chars" not in fit, (
        "a missing index reported the previous build's byte counts as its own"
    )


def test_undecodable_index_is_unavailable_not_an_exception(tmp_path: Path):
    """One bad byte in an instance-owned file must not fail the cycle."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "index.md").write_bytes(b"\xff\xfe\x00not utf-8 \xc3\x28\n")

    store = MemoryStore(tmp_path)
    assert store.get_memory_context(loop=True, max_chars=500) == ""
    fit = store.last_index_fit
    assert fit["status"] == "unavailable"
    assert fit["reason"] == "UnicodeDecodeError"


def test_valid_empty_index_is_distinct_from_missing_and_unavailable(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "index.md").write_text("", encoding="utf-8")

    store = MemoryStore(tmp_path)
    assert store.get_memory_context(loop=True, max_chars=500) == ""
    assert store.last_index_fit["status"] == "empty"
    assert store.last_index_fit["source_chars"] == 0


def test_resident_fields_survive_into_the_builder_fit(tmp_path: Path):
    """The allowlist dropped exactly the fields #1443 added."""
    _index(tmp_path, RULES + [f"- [F{i}](f/{i}.md) filler" for i in range(200)])
    builder = ContextBuilder(tmp_path)
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda excluded_names=None: ""
    builder.build_system_prompt(loop_profile=True)

    memory_index = builder.last_fit["memory_index"]
    assert memory_index["status"] == "present"
    assert memory_index["resident_matched"] == 5
    assert memory_index["resident_missing"] == []
    assert memory_index["dropped_entries"] > 0


def test_a_renamed_rule_entry_reaches_the_builder_fit_by_name(tmp_path: Path):
    renamed = list(RULES)
    renamed[6] = "* [Do not modify these paths](facts/do-not-touch.md)"
    _index(tmp_path, renamed + [f"- [F{i}](f/{i}.md) filler" for i in range(200)])

    builder = ContextBuilder(tmp_path)
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda excluded_names=None: ""
    builder.build_system_prompt(loop_profile=True)

    memory_index = builder.last_fit["memory_index"]
    assert memory_index["resident_matched"] == 4
    assert memory_index["resident_missing"] == ["[DO NOT touch]"], (
        "the caller must learn WHICH rule stopped matching, not merely that one did"
    )


def test_sections_aggregate_is_unchanged_by_the_addition(tmp_path: Path):
    """`memory_index` is additive: existing readers of `sections` are untouched."""
    _index(tmp_path, RULES)
    builder = ContextBuilder(tmp_path)
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda excluded_names=None: ""
    prompt = builder.build_system_prompt(loop_profile=True)

    fit = builder.last_fit
    assert fit["sections"]["memory"] > 0
    assert sum(fit["sections"].values()) <= len(prompt)
    assert "memory_index" in fit and fit["sections"]["memory"] is not None
