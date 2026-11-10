"""ADR-057 bounded prompt provenance contracts."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

import pytest

from nanobot.observability.llm_telemetry import call_context
from nanobot.observability.prompt_provenance import (
    PromptRequest,
    PromptSection,
    observe_prompt,
    prompt_request_context,
    provider_call_context,
    role_request_for_system,
)
from nanobot.providers.base import LLMProvider, LLMResponse


def _section(name: str, text: str, safe: bool = True) -> PromptSection:
    return PromptSection(name, text, safe)


def test_pure_section_projection_matches_known_legacy_bytes():
    from nanobot.agent.context import ContextBuilder

    separator = ContextBuilder.SECTION_SEPARATOR
    sections = [("identity", "heading\nbody"), ("empty", ""), ("memory", "low entropy: 0")]
    expected = "heading\nbody" + separator + "low entropy: 0"
    assert ContextBuilder._join_sections(sections) == expected
    assert ContextBuilder._join_sections(sections).encode("utf-8") == expected.encode("utf-8")


def test_context_builder_known_fixture_bytes_order_and_budgets(tmp_path):
    from nanobot.agent.context import ContextBuilder

    # The expected prompt strings below intentionally render legacy section
    # lists with the public separator, independently of provenance metadata.
    interactive = ContextBuilder(tmp_path / "interactive")
    interactive._get_identity = lambda loop_profile=False: "# Identity\n\nFixture identity."
    interactive._load_bootstrap_files = lambda: "## AGENTS.md\n\nFixture bootstrap."
    interactive.skills.get_always_skills = lambda: []
    interactive.skills.load_skills_for_context = lambda names: ""
    interactive.skills.build_skills_summary = lambda excluded_names=None, compact=False: "<skills>fixed</skills>"
    interactive.memory.get_memory_context = lambda *, loop=False, max_chars=4000: "Fixed memory."
    prompt = interactive.build_system_prompt(loop_profile=False)
    expected_interactive = (
        "# Identity\n\nFixture identity." + ContextBuilder.SECTION_SEPARATOR
        + "## AGENTS.md\n\nFixture bootstrap." + ContextBuilder.SECTION_SEPARATOR
        + "# Skills\n\nThe following skills extend your capabilities. To use a skill, read the skill's SKILL.md file using the read_file tool.\n"
        "Skills with available=\"false\" need dependencies installed first - you can try installing them with apt/brew.\n\n"
        "<skills>fixed</skills>" + ContextBuilder.SECTION_SEPARATOR + "# Memory\n\nFixed memory."
    )
    assert prompt.encode("utf-8") == expected_interactive.encode("utf-8")
    assert [section.name for section in interactive.last_prompt_sections] == [
        "identity", "bootstrap", "active_skills", "skills_catalogue", "memory",
    ]
    assert interactive.last_fit["cap"] == ContextBuilder.MAX_SYSTEM_PROMPT_CHARS
    assert interactive.last_fit["chars"] == len(expected_interactive)
    assert interactive.last_fit["rung"] == "full"

    loop = ContextBuilder(tmp_path / "loop", release_root=tmp_path / "loop", state_dir=tmp_path / "loop-state")
    loop._load_ontology_blocks = lambda: ([
        ("identity", "## IDENTITY.md\n\nFixture identity."),
        ("soul", "## SOUL.md\n\nFixture soul."),
        ("goals", "## goals.md\n\nFixture charter."),
        ("user", "## USER.md\n\nFixture user."),
        ("operating", "## OPERATING.md\n\nFixture contract."),
        ("agents", "## AGENTS.md\n\nFixture bootstrap."),
    ], [], [])
    loop._get_identity = lambda loop_profile=False: "## Runtime\nFixture runtime."
    loop._load_position_block = lambda **kwargs: "## Position\nStep 2 of 7."
    loop._load_scorecard_block = lambda: "[missing: scorecard]"
    loop._load_priorities_block = lambda: "## Operator priorities\n(none)\n\n## Derived priorities\n(none)"
    loop.skills.get_always_skills = lambda: []
    loop.skills.load_skills_for_context = lambda names: ""
    loop.skills.build_skills_summary = lambda excluded_names=None, compact=False: ""
    loop.memory.get_memory_context = lambda *, loop=False, max_chars=4000: "Fixture memory."
    prompt = loop.build_system_prompt(
        loop_profile=True, iteration=2, max_iterations=7, cycle_id="fixture-cycle",
    )
    expected_loop_sections = [
        ("identity", "## IDENTITY.md\n\nFixture identity."),
        ("soul", "## SOUL.md\n\nFixture soul."),
        ("goals", "## goals.md\n\nFixture charter."),
        ("user", "## USER.md\n\nFixture user."),
        ("operating", "## OPERATING.md\n\nFixture contract."),
        ("agents", "## AGENTS.md\n\nFixture bootstrap."),
        ("priorities", "## Operator priorities\n(none)\n\n## Derived priorities\n(none)"),
        ("skills_catalogue", ""),
        ("memory", "# Memory\n\nFixture memory."),
        ("runtime", "## Runtime\nFixture runtime."),
        ("scorecard", "[missing: scorecard]"),
        ("position", "## Position\nStep 2 of 7."),
    ]
    expected_loop = ContextBuilder.SECTION_SEPARATOR.join(
        value for _, value in expected_loop_sections if value
    )
    assert prompt.encode("utf-8") == expected_loop.encode("utf-8")
    assert [section.name for section in loop.last_prompt_sections] == [
        "identity", "soul", "goals", "user", "operating", "agents", "priorities",
        "skills_catalogue", "memory", "runtime", "scorecard", "position",
    ]
    assert loop.last_fit["cap"] == ContextBuilder.MAX_SYSTEM_PROMPT_CHARS
    assert loop.last_fit["chars"] == len(expected_loop)
    assert loop.last_fit["budget"]["release_pool_chars"] == {
        "limit": ContextBuilder._RELEASE_POOL_CHARS,
        "used": None,
    }


def test_context_builder_transient_sections_project_to_exact_legacy_output(tmp_path):
    from pathlib import Path

    from nanobot.agent.context import ContextBuilder

    root = Path(__file__).resolve().parents[1]
    builder = ContextBuilder(tmp_path, release_root=root)
    for loop_profile in (False, True):
        rendered = builder.build_system_prompt(
            loop_profile=loop_profile,
            degrade_on_overflow=True,
            iteration=2,
            max_iterations=7,
            cycle_id="projection-test",
        )
        projection = builder._join_sections([
            (section.name, section.content) for section in builder.last_prompt_sections
        ])
        assert projection.encode("utf-8") == rendered.encode("utf-8")
        assert all(not section.evidence_safe for section in builder.last_prompt_sections)


def test_snapshots_deltas_removals_and_sensitive_omission():
    owner = "test-snapshot-delta-privacy"
    first = observe_prompt(
        owner=owner, producer="proposer", schema="role-v1",
        sections=(_section("role", "Fixed role body"), _section("secret", "1234", False)),
    ).provenance
    unchanged = observe_prompt(
        owner=owner, producer="proposer", schema="role-v1",
        sections=(_section("role", "Fixed role body"), _section("secret", "1234", False)),
    ).provenance
    changed = observe_prompt(
        owner=owner, producer="proposer", schema="role-v1",
        sections=(_section("role", "Changed fixed role"), _section("secret", "5678", False)),
    ).provenance
    removed = observe_prompt(
        owner=owner, producer="proposer", schema="role-v1", sections=(),
    ).provenance
    assert first["kind"] == "snapshot"
    assert unchanged["kind"] == "delta" and unchanged["sections"] == []
    assert changed["previous_call_id"] == unchanged["call_id"]
    assert changed["sections"][0]["name"] == "role"
    assert removed["sections"] == [{"name": "role", "removed": True}]
    for record in (first, unchanged, changed, removed):
        encoded = json.dumps(record)
        assert "secret" not in encoded and "1234" not in encoded and "5678" not in encoded
    assert first["order"] == ["role"]
    assert unchanged["order"] == ["role"]
    assert changed["order"] == ["role"]
    assert removed["order"] == []


def test_missing_baseline_and_release_change_start_full_snapshot():
    from nanobot.observability import prompt_provenance as module

    module._BASELINES.clear()
    owner = "test-baseline-reset"
    section = (_section("role", "fixed"),)
    first = observe_prompt(owner=owner, producer="curator", schema="v1", sections=section).provenance
    repeated_absence = observe_prompt(owner=owner, producer="curator", schema="v1", sections=section).provenance
    identified = observe_prompt(
        owner=owner, producer="curator", schema="v1", sections=section, release_id="r1",
    ).provenance
    changed_release = observe_prompt(
        owner=owner, producer="curator", schema="v1", sections=section, release_id="r2",
    ).provenance
    module._BASELINES.clear()  # process restart/cache loss is an explicit reset
    restarted = observe_prompt(
        owner=owner, producer="curator", schema="v1", sections=section, release_id="r2",
    ).provenance
    assert first["kind"] == "snapshot"
    assert repeated_absence["kind"] == "delta"
    assert repeated_absence["baseline_id"] == first["baseline_id"]
    assert repeated_absence["previous_call_id"] == first["call_id"]
    assert repeated_absence["sections"] == []
    assert identified["kind"] == "snapshot"  # unidentified -> identified transition
    assert changed_release["kind"] == "snapshot"
    assert restarted["kind"] == "snapshot"


def test_fixed_role_producer_matrix_hashes_only_the_approved_role_body():
    from pathlib import Path

    from nanobot.observability.prompt_provenance import SAFE_ROLE_PRODUCERS
    from nanobot.runtime.role_prompt import build_role_system_prompt

    assert SAFE_ROLE_PRODUCERS == {
        "proposer", "demand-proposer", "goal-review", "planner",
        "reflector", "strategist", "curator", "narrator",
    }
    release_root = Path(__file__).resolve().parents[1]
    for producer in sorted(SAFE_ROLE_PRODUCERS):
        system, metadata = build_role_system_prompt(producer, release_root=release_root)
        sections = tuple(metadata["sections"])
        role_sections = [section for section in sections if section.name == "role"]
        assert len(role_sections) == 1 and role_sections[0].evidence_safe
        request = role_request_for_system(system, owner=f"matrix:{producer}")
        with prompt_request_context(request):
            with provider_call_context([{"role": "system", "content": system}]) as call:
                assert call is not None
                assert call.provenance["producer"] == producer
                assert call.provenance["order"] == ["role"]
                assert call.provenance["sections"] == [{
                    "name": "role",
                    "sha256": hashlib.sha256(role_sections[0].content.encode("utf-8")).hexdigest(),
                    "chars": len(role_sections[0].content),
                }]
                assert call.provenance.get("release_id") == metadata.get("release_id")

    excluded, _ = build_role_system_prompt("skill-eval", release_root=release_root)
    request = role_request_for_system(excluded, owner="matrix:skill-eval")
    with prompt_request_context(request):
        with provider_call_context([{"role": "system", "content": excluded}]) as call:
            assert call is not None
            assert call.provenance["provenance_state"] == "unavailable"
            assert call.provenance["order"] == []
            assert call.provenance["sections"] == []


def test_role_prompt_bytes_match_explicit_legacy_assembly_for_production_flag_branches(tmp_path):
    from nanobot.runtime.role_prompt import build_role_system_prompt

    release = tmp_path / "release"
    (release / "roles").mkdir(parents=True)
    (release / "IDENTITY.md").write_text("# Identity\n\nFixture identity.\n", encoding="utf-8")
    (release / "SOUL.md").write_text("# Soul\n\nFixture soul.\n", encoding="utf-8")
    (release / "goals.md").write_text("Fixture charter.\n", encoding="utf-8")
    (release / "roles" / "proposer.md").write_text(
        "---\nrole: proposer\n---\n# Role: proposer\n\nFixture proposer role.\n",
        encoding="utf-8",
    )
    (release / "roles" / "planner.md").write_text(
        "---\nrole: planner\n---\n# Role: planner\n\nFixture planner role.\n",
        encoding="utf-8",
    )

    for role, expected_names, expected_prefixes in (
        ("proposer", ["identity", "soul", "goals", "role"], [
            "## IDENTITY.md\n\nFixture identity.",
            "## SOUL.md\n\n# Soul\n\nFixture soul.\n",
            "## goals.md\n\nFixture charter.",
            "Fixture proposer role.",
        ]),
        ("planner", ["identity", "goals", "role"], [
            "## IDENTITY.md\n\nFixture identity.",
            "## goals.md\n\nFixture charter.",
            "Fixture planner role.",
        ]),
    ):
        rendered, metadata = build_role_system_prompt(role, release_root=release)
        assert [section.name for section in metadata["sections"]] == expected_names
        expected = "\n\n---\n\n".join(expected_prefixes)
        assert rendered.encode("utf-8") == expected.encode("utf-8")
        assert metadata["chars"] == len(expected)
        assert metadata["with_charter"] is True
        assert metadata["with_soul"] is (role == "proposer")


def test_dispatch_matches_actual_system_and_mints_call_id():
    sections = (_section("role", "fixed role"),)
    request = PromptRequest("dispatch-owner", "narrator", "role-v1", sections, "exact")
    with prompt_request_context(request):
        with provider_call_context([{"role": "system", "content": "different"}]) as mismatched:
            assert mismatched is not None
            assert mismatched.provenance["provenance_state"] == "unavailable"
            assert "sections" not in mismatched.provenance
    with prompt_request_context(request):
        with provider_call_context([{"role": "system", "content": "exact"}]) as call:
            assert call is not None
            assert call.provenance["call_id"] == call.call_id
            assert call.provenance["producer"] == "narrator"


def test_fake_provider_call_correlates_existing_telemetry(tmp_path, monkeypatch):
    class LocalProvider(LLMProvider):
        async def chat(self, *args, **kwargs):
            return LLMResponse(content="local fake response")

        def get_default_model(self) -> str:
            return "test-model"

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "llm_calls"))
    system = "Fixed approved role body"
    request = PromptRequest(
        "local-fake-provider-owner", "proposer", "role-v1",
        (_section("role", system),), expected_system=system,
    )

    async def invoke():
        with prompt_request_context(request), call_context("local-cycle", "proposer"):
            return await LocalProvider().chat_with_retry(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": "synthetic"},
                ],
                model="test-model",
            )

    assert asyncio.run(invoke()).content == "local fake response"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    call_rows = [json.loads(line) for line in (tmp_path / "llm_calls" / f"{day}.jsonl").read_text(encoding="utf-8").splitlines()]
    prompt_rows = [json.loads(line) for line in (tmp_path / "llm_calls" / "prompts" / f"{day}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(call_rows) == len(prompt_rows) == 1
    call_provenance = call_rows[0]["prompt_provenance"]
    prompt_provenance = prompt_rows[0]["prompt_provenance"]
    assert call_provenance["call_id"] == prompt_provenance["call_id"]
    assert call_provenance["sections"][0]["name"] == "role"
    assert call_provenance["producer"] == "proposer"


def test_provider_failure_cancellation_and_followup_call_have_correct_identity(tmp_path, monkeypatch):
    class LocalProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.dispatches = 0
            self.outcomes = [RuntimeError("synthetic provider failure"), asyncio.CancelledError(), LLMResponse(content="ok")]

        async def chat(self, *args, **kwargs):
            self.dispatches += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def get_default_model(self) -> str:
            return "test-model"

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "llm_calls"))
    system = "fixed failure fixture"
    request = PromptRequest(
        "failure-owner", "proposer", "role-v1", (_section("role", system),),
        expected_system=system,
    )
    provider = LocalProvider()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "synthetic"}]

    async def invoke_once():
        with prompt_request_context(request), call_context("failure-cycle", "proposer"):
            return await provider.chat_with_retry(messages=messages, model="test-model")

    first_error = asyncio.run(invoke_once())
    assert first_error.finish_reason == "error"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(invoke_once())
    from nanobot.observability.prompt_provenance import current_provenance, take_failed_provenance
    assert current_provenance() is None
    assert take_failed_provenance() is None
    assert asyncio.run(invoke_once()).content == "ok"
    assert provider.dispatches == 3

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    calls_path = tmp_path / "llm_calls" / f"{day}.jsonl"
    prompts_path = tmp_path / "llm_calls" / "prompts" / f"{day}.jsonl"
    failures_path = tmp_path / "llm_calls" / "failures" / f"{day}.jsonl"
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    prompts = [json.loads(line) for line in prompts_path.read_text(encoding="utf-8").splitlines()]
    failures = [json.loads(line) for line in failures_path.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == len(prompts) == len(failures) == 1
    failure_id = failures[0]["prompt_provenance"]["call_id"]
    success_id = calls[0]["prompt_provenance"]["call_id"]
    assert failure_id != success_id
    assert prompts[0]["prompt_provenance"]["call_id"] == success_id
    assert all(
        row["prompt_provenance"]["call_id"] != failure_id
        for row in calls + prompts
    )
    assert failures[0]["error_type"] == "ProviderError"
    assert calls[0]["prompt_provenance"]["producer"] == "proposer"


def test_lru_evicts_at_64_owner_producer_baselines():
    from nanobot.observability import prompt_provenance as module

    module._BASELINES.clear()
    section = (_section("role", "fixed"),)
    for index in range(module.MAX_BASELINES):
        observe_prompt(
            owner=f"evict-owner-{index}", producer="planner", schema="v1", sections=section,
        )
    assert len(module._BASELINES) == module.MAX_BASELINES
    observe_prompt(owner="evict-owner-overflow", producer="planner", schema="v1", sections=section)
    assert len(module._BASELINES) == module.MAX_BASELINES
    assert ("evict-owner-0", "planner:v1") not in module._BASELINES
