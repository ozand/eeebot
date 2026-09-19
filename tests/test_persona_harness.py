"""Persona self-consistency harness — #1771.

Every test here runs against a fake client. The harness talks to one local
model on one workstation; the suite must never need it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.runtime import persona_harness as ph

# ─── the statement set is data, and it is valid ─────────────────────────────


def test_shipped_statement_set_loads_and_is_within_the_stated_size() -> None:
    """20-40 behavioural statements plus 2-3 controls, as the issue specifies."""
    st = ph.load_statements()
    assert 20 <= len(st.behaviours) <= 40, len(st.behaviours)
    assert 2 <= len(st.controls) <= 3, len(st.controls)
    assert st.fiction


def test_shipped_statements_name_their_source_and_trait() -> None:
    """A gap is only actionable if it points at the file and the trait that
    produced it — an unattributed failure cannot be fixed."""
    for statement in ph.load_statements().behaviours:
        assert statement.source, statement.id
        assert statement.trait, statement.id


def test_shipped_statements_cover_every_persona_file() -> None:
    sources = {s.source for s in ph.load_statements().behaviours}
    assert {"IDENTITY.md", "SOUL.md", "goals.md"} <= sources


def test_shipped_statements_are_not_all_expected_yes() -> None:
    """A set answerable by always saying yes measures agreeableness, not the
    character."""
    expected = [s.expected for s in ph.load_statements().behaviours]
    assert expected.count("yes") >= 5
    assert expected.count("no") >= 5


def _write_set(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "statements.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


_MINIMAL = {
    "fiction": "A checklist.",
    "statements": [
        {"id": "C01", "kind": "control", "expected": "yes", "text": "Two plus two is four."},
        {"id": "S01", "kind": "behaviour", "expected": "yes", "text": "I report honestly.",
         "source": "SOUL.md", "trait": "honesty"},
        {"id": "S02", "kind": "behaviour", "expected": "no", "text": "I flatter.",
         "source": "SOUL.md", "trait": "honesty"},
    ],
}


@pytest.mark.parametrize("mutate,fragment", [
    (lambda p: p.update(fiction=""), "fiction"),
    (lambda p: p.update(statements=[]), "statements"),
    (lambda p: p["statements"].append(dict(p["statements"][0])), "duplicated"),
    (lambda p: p["statements"][1].update(expected="maybe"), "expected"),
    (lambda p: p["statements"][1].update(kind="vibe"), "kind"),
    (lambda p: p["statements"][1].update(text="  "), "empty text"),
    (lambda p: p.update(statements=[s for s in p["statements"] if s["kind"] != "control"]), "control"),
])
def test_invalid_statement_sets_are_refused_with_a_reason(tmp_path: Path, mutate, fragment) -> None:
    payload = json.loads(json.dumps(_MINIMAL))
    mutate(payload)
    with pytest.raises(ph.PersonaHarnessError) as exc:
        ph.load_statements(_write_set(tmp_path, payload))
    assert fragment in str(exc.value)


def test_missing_statement_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(ph.PersonaHarnessError, match="not found"):
        ph.load_statements(tmp_path / "nope.json")


# ─── answer parsing ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("yes", "yes"),
    ("No", "no"),
    ("  YES.\n", "yes"),
    ("<think>the definition says I must not flatter</think>no", "no"),
    ("<think>yes would be wrong here</think>\n\nNo", "no"),
    ("<think>still reasoning and then the reply was cut", ph.UNPARSED),
    ("I would rather not answer", ph.UNPARSED),
    ("", ph.UNPARSED),
])
def test_parse_answer(raw: str, expected: str) -> None:
    assert ph.parse_answer(raw) == expected


def test_parse_answer_ignores_a_yes_that_only_appears_inside_thinking() -> None:
    """A model that reasons 'yes, but actually no' must be graded on the
    reply, not on the thinking that preceded it."""
    assert ph.parse_answer("<think>yes at first glance</think>no") == "no"


# ─── the prompt comes from production, not from a copy here ────────────────


def test_ablation_candidates_are_derived_from_the_production_block_list() -> None:
    expected = tuple(
        name for kind, name, _cap, _req in ContextBuilder.BOOTSTRAP_FILES if kind == "release"
    )
    assert ph.ablation_candidates() == expected


def test_module_holds_no_private_copy_of_the_block_list_or_order() -> None:
    """The one invariant that makes this harness worth running: it measures
    the prompt production builds. A literal block name in this module would be
    a second list, free to drift from ADR-022's."""
    source = Path(ph.__file__).read_text(encoding="utf-8")
    for kind, filename, _cap, _required in ContextBuilder.BOOTSTRAP_FILES:
        assert filename not in source, f"{filename} is hard-coded in persona_harness.py ({kind})"


def test_assemble_prompt_calls_the_production_assembler(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_build(self, *, skill_names=None, excluded_skill_names=None,
                   loop_profile=False, strict=None, degrade_on_overflow=False):
        seen["loop_profile"] = loop_profile
        seen["release_root"] = self.release_root
        self.last_fit = {"sections": {"identity": 7}}
        return "SENTINEL-PROMPT"

    monkeypatch.setattr(ContextBuilder, "build_system_prompt", fake_build, raising=True)
    result = ph.assemble_prompt(tmp_path / "release", tmp_path / "ws")
    assert result.text == "SENTINEL-PROMPT"
    assert seen["loop_profile"] is True
    assert result.fit["sections"] == {"identity": 7}


def test_assemble_prompt_rejects_an_unknown_ablation_block(tmp_path: Path) -> None:
    with pytest.raises(ph.PersonaHarnessError, match="unknown block"):
        ph.assemble_prompt(tmp_path, tmp_path, ablate="NOPE.md")


def test_ablation_removes_the_block_through_the_real_missing_path(tmp_path: Path) -> None:
    """The ablated block must go missing the way an absent file goes missing —
    through the loader's own degradation, not by patching the builder."""
    release = tmp_path / "release"
    release.mkdir()
    for name in ph.ablation_candidates():
        (release / name).write_text(f"# {name}\n\nunique-body-of-{name}\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    target = ph.ablation_candidates()[0]
    baseline = ph.assemble_prompt(release, workspace)
    ablated = ph.assemble_prompt(release, workspace, ablate=target, _tmp_root=tmp_path / "shadow")

    assert f"unique-body-of-{target}" in baseline.text
    assert f"unique-body-of-{target}" not in ablated.text
    assert f"[missing: {target}]" in ablated.text
    assert ablated.ablated == target
    # every other block survived
    for name in ph.ablation_candidates()[1:]:
        assert f"unique-body-of-{name}" in ablated.text


def test_ablation_can_replace_a_block_instead_of_removing_it(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    for name in ph.ablation_candidates():
        (release / name).write_text(f"body-{name}", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = ph.ablation_candidates()[0]
    out = ph.assemble_prompt(
        release, workspace, ablate=target, replacement="REPLACEMENT-BODY",
        _tmp_root=tmp_path / "shadow",
    )
    assert "REPLACEMENT-BODY" in out.text
    assert f"body-{target}" not in out.text


def test_ablation_does_not_touch_the_real_release_files(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    for name in ph.ablation_candidates():
        (release / name).write_text(f"body-{name}", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = ph.ablation_candidates()[0]
    ph.assemble_prompt(release, workspace, ablate=target, _tmp_root=tmp_path / "shadow")
    assert (release / target).read_text(encoding="utf-8") == f"body-{target}"


# ─── fake clients ───────────────────────────────────────────────────────────


class FakeClient:
    """Answers from a per-statement script; counts what it was asked."""

    def __init__(self, answers: dict[str, list[str]] | None = None, default: str = "yes",
                 error: str = "", model: str = "fake-model") -> None:
        self.answers = answers or {}
        self.default = default
        self.error = error
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> dict[str, object]:
        self.calls.append((system, user))
        if self.error:
            return {"text": "", "tokens": 0, "error": self.error}
        key = self._match(user)
        if key is not None:
            queue = self.answers[key]
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
            return {"text": reply, "tokens": 11}
        return {"text": self.default, "tokens": 11}

    def _match(self, user: str) -> str | None:
        for key in self.answers:
            if key in user:
                return key
        return None


def _set_from(payload: dict, tmp_path: Path) -> ph.StatementSet:
    return ph.load_statements(_write_set(tmp_path, payload))


def _prompt(text: str = "SYSTEM") -> ph.AssembledPrompt:
    return ph.AssembledPrompt(text=text, fit={"sections": {"identity": 3}})


# ─── controls gate the run ──────────────────────────────────────────────────


def test_a_control_miss_aborts_with_plumbing_broken_and_no_character_verdict(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient(answers={"Two plus two is four.": ["no"]})
    report = ph.run_once(client, st, _prompt(), repeats=2)

    assert report.plumbing_broken is True
    assert "C01" in report.plumbing_reason
    assert report.behaviours == [], "behavioural statements must not be asked after a control miss"
    text = ph.format_report(report)
    assert "PLUMBING BROKEN" in text
    assert "Definition gaps" not in text
    assert "## Statements" not in text


def test_unparseable_controls_are_also_plumbing_broken(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    report = ph.run_once(FakeClient(default="I decline to answer"), st, _prompt(), repeats=2)
    assert report.plumbing_broken is True
    assert "no parseable answer" in report.plumbing_reason


def test_client_errors_on_every_call_are_plumbing_broken_not_a_character_finding(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    report = ph.run_once(FakeClient(error="runner_not_configured"), st, _prompt(), repeats=1)
    assert report.plumbing_broken is True
    assert ph.format_report(report).count("PLUMBING BROKEN") == 1


def test_passing_controls_let_the_behavioural_pass_run(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient(answers={
        "Two plus two is four.": ["yes"],
        "I report honestly.": ["yes"],
        "I flatter.": ["no"],
    })
    report = ph.run_once(client, st, _prompt(), repeats=2)
    assert report.plumbing_broken is False
    assert {r.statement.id for r in report.behaviours} == {"S01", "S02"}
    assert all(r.verdict == "holds" for r in report.behaviours)
    assert report.gaps == []


# ─── verdicts ───────────────────────────────────────────────────────────────


def test_a_written_trait_that_does_not_work_is_reported_as_a_gap(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient(answers={
        "Two plus two is four.": ["yes"],
        "I report honestly.": ["yes"],
        "I flatter.": ["yes", "yes", "yes"],   # expected "no" — the trait does not hold
    })
    report = ph.run_once(client, st, _prompt(), repeats=3)
    gaps = {r.statement.id: r.verdict for r in report.gaps}
    assert gaps == {"S02": "gap"}
    assert "S02" in ph.format_report(report)


def test_partial_agreement_is_weak_not_a_pass(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient(answers={
        "Two plus two is four.": ["yes"],
        "I report honestly.": ["yes"],
        "I flatter.": ["no", "yes", "no", "yes"],
    })
    report = ph.run_once(client, st, _prompt(), repeats=4)
    by_id = {r.statement.id: r.verdict for r in report.behaviours}
    assert by_id["S02"] == "weak"


def test_a_statement_with_no_parseable_answer_is_no_signal_not_zero(tmp_path: Path) -> None:
    """Never measured and measured-as-wrong are different findings."""
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient(answers={
        "Two plus two is four.": ["yes"],
        "I report honestly.": ["mumble"],
        "I flatter.": ["no"],
    })
    report = ph.run_once(client, st, _prompt(), repeats=2)
    by_id = {r.statement.id: r for r in report.behaviours}
    assert by_id["S01"].agreement is None
    assert by_id["S01"].verdict == "no-signal"


# ─── ablation ───────────────────────────────────────────────────────────────


def _report_with(rates: dict[str, float | None], label: str = "x") -> ph.RunReport:
    report = ph.RunReport(label=label, model="fake", repeats=2)
    for sid, rate in rates.items():
        statement = ph.Statement(id=sid, kind="behaviour", text=sid, expected="yes")
        result = ph.StatementResult(statement=statement)
        if rate is None:
            result.answers = [ph.UNPARSED, ph.UNPARSED]
        else:
            hits = round(rate * 10)
            result.answers = ["yes"] * hits + ["no"] * (10 - hits)
        report.behaviours.append(result)
    return report


def test_ablation_delta_is_per_statement_against_baseline() -> None:
    baseline = _report_with({"S01": 1.0, "S02": 0.9})
    ablated = _report_with({"S01": 0.4, "S02": 0.9})
    delta = ph.diff_reports(baseline, ablated, "BLOCK")
    assert delta.per_statement["S01"] == pytest.approx(-0.6)
    assert delta.per_statement["S02"] == pytest.approx(0.0)
    assert set(delta.moved) == {"S01"}
    assert delta.verdict == "changes behaviour"


def test_a_block_whose_removal_moves_nothing_is_named_as_such() -> None:
    baseline = _report_with({"S01": 1.0, "S02": 0.9})
    ablated = _report_with({"S01": 1.0, "S02": 0.9})
    delta = ph.diff_reports(baseline, ablated, "BLOCK")
    assert delta.moved == {}
    assert delta.verdict == "no measurable effect"
    assert "no measurable effect" in ph.format_report(baseline, [delta])


def test_unmeasured_statements_are_omitted_from_the_delta_not_scored_zero() -> None:
    baseline = _report_with({"S01": 1.0, "S02": None})
    ablated = _report_with({"S01": 1.0, "S02": 0.5})
    delta = ph.diff_reports(baseline, ablated, "BLOCK")
    assert "S02" not in delta.per_statement


def test_an_ablation_pass_with_broken_plumbing_is_not_read_as_an_effect() -> None:
    baseline = _report_with({"S01": 1.0})
    ablated = _report_with({})
    ablated.plumbing_broken = True
    delta = ph.diff_reports(baseline, ablated, "BLOCK")
    assert delta.verdict == "plumbing-broken"


# ─── budget ─────────────────────────────────────────────────────────────────


def test_plan_calls_counts_every_pass(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    assert ph.plan_calls(st, repeats=3) == 9
    assert ph.plan_calls(st, repeats=3, ablations=["a", "b"]) == 27


def test_an_over_budget_run_refuses_before_spending_a_single_call(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient()
    with pytest.raises(ph.PersonaHarnessError, match="exceeds the budget"):
        ph.run(
            client, release_root=tmp_path, workspace=tmp_path,
            statement_set=st, repeats=5, max_calls=4,
        )
    assert client.calls == [], "budget must be checked before the first call"


def test_an_unknown_ablation_block_is_refused_before_spending_a_call(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient()
    with pytest.raises(ph.PersonaHarnessError, match="unknown block"):
        ph.run(
            client, release_root=tmp_path, workspace=tmp_path,
            statement_set=st, repeats=1, ablations=["NOPE.md"], max_calls=999,
        )
    assert client.calls == []


def test_the_report_always_states_the_call_count_and_tokens(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient(answers={
        "Two plus two is four.": ["yes"], "I report honestly.": ["yes"], "I flatter.": ["no"],
    })
    report = ph.run_once(client, st, _prompt(), repeats=2)
    text = ph.format_report(report)
    assert f"model calls      {report.calls}" in text
    assert report.calls == 6
    assert "tokens" in text


# ─── end to end, still with a fake client ───────────────────────────────────


def test_run_produces_a_baseline_and_one_delta_per_ablation(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    for name in ph.ablation_candidates():
        (release / name).write_text(f"# {name}\nbody\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    st = _set_from(_MINIMAL, tmp_path)
    target = ph.ablation_candidates()[0]

    client = FakeClient(answers={
        "Two plus two is four.": ["yes"], "I report honestly.": ["yes"], "I flatter.": ["no"],
    })
    baseline, deltas = ph.run(
        client, release_root=release, workspace=workspace,
        statement_set=st, repeats=2, ablations=[target], max_calls=100,
    )
    assert baseline.plumbing_broken is False
    assert len(deltas) == 1 and deltas[0].block == target
    assert baseline.calls == 6 and deltas[0].report.calls == 6


def test_a_broken_baseline_skips_the_ablations_entirely(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    for name in ph.ablation_candidates():
        (release / name).write_text("body", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient(answers={"Two plus two is four.": ["no"]})

    baseline, deltas = ph.run(
        client, release_root=release, workspace=workspace, statement_set=st,
        repeats=1, ablations=list(ph.ablation_candidates()), max_calls=999,
    )
    assert baseline.plumbing_broken is True
    assert deltas == [], "no ablation is worth running once the plumbing is suspect"


def test_report_to_dict_is_json_serialisable_and_names_the_gaps(tmp_path: Path) -> None:
    st = _set_from(_MINIMAL, tmp_path)
    client = FakeClient(answers={
        "Two plus two is four.": ["yes"], "I report honestly.": ["yes"], "I flatter.": ["yes"],
    })
    report = ph.run_once(client, st, _prompt(), repeats=2)
    payload = ph.report_to_dict(report)
    assert payload["schema"] == "persona-harness-v1"
    assert payload["gaps"] == ["S02"]
    json.dumps(payload)


def test_the_run_never_reads_credentials_from_disk() -> None:
    """Keys come from the environment. A path to a secrets file in this module
    would be a new place for one to leak from."""
    source = Path(ph.__file__).read_text(encoding="utf-8")
    assert "/etc/" not in source
    assert "litellm.env" not in source


def test_the_default_ablation_shadow_root_is_removed_after_the_prompt_is_built(tmp_path: Path) -> None:
    """Without an explicit shadow root the copy is temporary: the prompt is a
    string by the time the call returns, so nothing needs the directory."""
    import tempfile as _tempfile

    release = tmp_path / "release"
    release.mkdir()
    for name in ph.ablation_candidates():
        (release / name).write_text(f"unique-body-of-{name}", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = ph.ablation_candidates()[0]

    before = set(Path(_tempfile.gettempdir()).glob("persona-ablate-*"))
    out = ph.assemble_prompt(release, workspace, ablate=target)
    after = set(Path(_tempfile.gettempdir()).glob("persona-ablate-*"))

    assert f"[missing: {target}]" in out.text
    assert after == before, "the shadow release root outlived the call"
