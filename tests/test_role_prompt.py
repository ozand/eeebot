"""#1729 (ADR-022 rule 2): role prompts are files, not string literals.

Every test here fails against the pre-#1729 tree for the reason in its name:
the eight side roles carried their system prompt as a Python literal, so there
was no file to be missing, no budget to exceed, and no way for the operator to
read or review the text without a harness PR.

The parity fixture (``tests/fixtures/role_literals_pre_1729.json``) holds the
eight literals exactly as they stood on ``origin/main`` at ``fa5b9926``. It is
the evidence that this migration preserved meaning: rewording a role is a
later change with its own replay (ADR-011 rule 3), and it must update this
fixture deliberately rather than by accident.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nanobot.runtime import role_prompt
from nanobot.runtime.mutation_policy import MUTATION_POLICY
from nanobot.runtime.role_prompt import (
    IDENTITY_SHORT_CAP,
    ROLE_FLAGS,
    ROLE_NAMES,
    build_role_system_prompt,
    load_role_text,
    resolve_release_root,
    role_budget,
    system_chars,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "role_literals_pre_1729.json"
PRE_MIGRATION_LITERALS: dict[str, str] = json.loads(FIXTURE.read_text(encoding="utf-8"))
#: The fixture is a frozen snapshot of the eight roles that existed as
#: Python literals at #1729's own commit -- it does not grow when a later
#: role (e.g. "planner", #1852) is born directly as a file with no prior
#: literal to have parity with. Parity is checked against this frozen set,
#: never against the live, growing ``ROLE_NAMES``.
PRE_1729_ROLE_NAMES: tuple[str, ...] = tuple(PRE_MIGRATION_LITERALS)


def _normalise(text: str) -> str:
    """Whitespace-normalised comparison: the role files are hard-wrapped for a
    human reader, the literals were wrapped by the Python source. Only the
    words are the contract."""
    return " ".join(text.split())


def _release_root(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / "release"
    (root / "roles").mkdir(parents=True)
    for name, body in files.items():
        path = root / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


# ─── the eight files exist and say what the literals said ──────────────────

class TestLiteralParity:
    def test_fixture_covers_every_pre_1729_role(self):
        """Every role ROLE_NAMES carried at #1729's own commit has a
        literal in the fixture -- a role born later (planner, #1852) is
        not expected to (see PRE_1729_ROLE_NAMES)."""
        assert set(PRE_MIGRATION_LITERALS) <= set(ROLE_NAMES)

    @pytest.mark.parametrize("role", ROLE_NAMES)
    def test_role_file_exists_with_front_matter_and_heading(self, role):
        path = REPO_ROOT / "roles" / f"{role}.md"
        assert path.is_file(), f"roles/{role}.md is missing from the release tree"
        raw = path.read_text(encoding="utf-8")
        assert raw.startswith("---\n"), "role files carry front matter"
        assert f"role: {role}\n" in raw
        assert re.search(rf"^# Role: {re.escape(role)}$", raw, flags=re.M)

    @pytest.mark.parametrize("role", PRE_1729_ROLE_NAMES)
    def test_loaded_role_text_equals_the_pre_migration_literal(self, role):
        text, meta = load_role_text(role, release_root=REPO_ROOT)
        assert meta["missing"] is False and meta["truncated"] is False
        assert _normalise(text) == _normalise(PRE_MIGRATION_LITERALS[role])

    @pytest.mark.parametrize("role", ROLE_NAMES)
    def test_role_body_is_within_its_declared_budget(self, role):
        text, meta = load_role_text(role, release_root=REPO_ROOT)
        assert meta["budget"] is not None
        assert len(text) <= meta["budget"], (
            f"roles/{role}.md is {len(text)} chars, over its declared "
            f"budget_chars: {meta['budget']}"
        )

    def test_proposer_roles_render_the_authoritative_commit_surfaces(self):
        """The placeholder is filled from ``mutation_policy``, so the two
        proposer prompts cannot drift from the policy the way a copied literal
        could (the drift this repo already fixed once, in #1731)."""
        for role in ("proposer", "demand-proposer"):
            text, _ = load_role_text(role, release_root=REPO_ROOT)
            assert MUTATION_POLICY.render_commit_surfaces() in text
            MUTATION_POLICY.validate_rendered_surfaces(text)
            assert "{commit_surfaces}" not in text


# ─── missing and truncated files degrade, never raise ──────────────────────

class TestCharterIntegrity:
    def test_current_size_charter_reaches_proposer_and_planner_whole(self, tmp_path):
        charter = "A" * 3865
        root = _release_root(
            tmp_path,
            **{
                "IDENTITY.md": "# Identity\n\nTest identity.\n",
                "SOUL.md": "# Soul\n\nTest soul.\n",
                "goals.md": charter,
                "roles__proposer.md": "---\nrole: proposer\n---\n# Role: proposer\n\nPropose.\n",
                "roles__planner.md": "---\nrole: planner\n---\n# Role: planner\n\nPlan.\n",
            },
        )
        expected = "## goals.md\n\n" + charter
        for role in ("proposer", "planner"):
            prompt, fit = build_role_system_prompt(role, release_root=root)
            assert expected in prompt
            assert fit["blocks"]["goals"] == len(expected)
            assert fit["truncated"] == []

    def test_oversized_charter_refuses_prompt_build_with_reason(self, tmp_path):
        root = _release_root(
            tmp_path,
            **{
                "IDENTITY.md": "# Identity\
\
Test identity.\
",
                "SOUL.md": "# Soul\
\
Test soul.\
",
                "goals.md": "X" * (role_prompt.CHARTER_MAX_CHARS + 1),
                "roles__proposer.md": "---\
role: proposer\
---\
# Role: proposer\
\
Propose.\
",
            },
        )
        with pytest.raises(role_prompt.CharterTooLargeError, match="role prompt refused.*goals.md.*maximum is 8000"):
            build_role_system_prompt("proposer", release_root=root)


class TestMissingAndTruncated:
    def test_missing_role_file_leaves_the_caller_running_on_identity(self, tmp_path):
        root = _release_root(
            tmp_path,
            **{"IDENTITY.md": "# id\n\nI am the agent under test.\n", "SOUL.md": "# soul\n\nBe honest.\n"},
        )
        text, fit = build_role_system_prompt("curator", release_root=root)
        assert "[missing: roles/curator.md]" in text
        assert fit["missing"] == ["roles/curator.md"]
        assert "I am the agent under test." in text
        assert "Be honest." in text
        assert fit["chars"] == len(text)

    def test_missing_identity_and_soul_are_named_individually(self, tmp_path):
        root = _release_root(tmp_path, **{"roles__curator.md": "# Role: curator\n\nCurate.\n"})
        text, fit = build_role_system_prompt("curator", release_root=root)
        assert fit["missing"] == ["IDENTITY.md", "SOUL.md"]
        assert "[missing: IDENTITY.md]" in text and "[missing: SOUL.md]" in text
        assert "Curate." in text

    def test_unresolvable_release_root_refuses_charter_roles(self, tmp_path, monkeypatch):
        monkeypatch.setattr(role_prompt, "resolve_release_root", lambda explicit=None: None)
        with pytest.raises(role_prompt.RolePromptBuildError, match="goals.md is unavailable"):
            build_role_system_prompt("proposer")

    def test_over_budget_role_body_is_cut_with_the_loader_notice(self, tmp_path):
        body = "\n".join(f"line {n} of a very long role description" for n in range(200))
        root = _release_root(
            tmp_path,
            **{
                "IDENTITY.md": "# id\n\nAgent.\n",
                "roles__strategist.md": f"---\nrole: strategist\nbudget_chars: 400\n---\n# Role: strategist\n\n{body}\n",
            },
        )
        text, meta = load_role_text("strategist", release_root=root)
        assert meta["truncated"] is True
        assert len(text) <= 400
        assert "[roles/strategist.md truncated at 400 chars; read the file for the rest]" in text
        _, fit = build_role_system_prompt("strategist", release_root=root)
        assert fit["truncated"] == ["roles/strategist.md"]

    def test_role_file_without_budget_front_matter_gets_the_default(self):
        assert role_budget({}) == role_prompt.DEFAULT_ROLE_BUDGET
        assert role_budget({"budget_chars": "not a number"}) == role_prompt.DEFAULT_ROLE_BUDGET
        assert role_budget({"budget_chars": "900"}) == 900


# ─── per-role flags: the issue's revision, asserted ────────────────────────

class TestPerRoleFlags:
    @pytest.mark.parametrize(
        "role,with_charter,with_soul",
        [
            ("proposer", True, True),
            ("demand-proposer", True, True),
            ("goal-review", True, True),
            ("curator", False, True),
            ("narrator", False, True),
            ("strategist", False, False),
            ("reflector", False, False),
            ("skill-eval", False, False),
        ],
    )
    def test_flags_match_the_issue_revision(self, role, with_charter, with_soul):
        assert ROLE_FLAGS[role] == (with_charter, with_soul)
        _, fit = build_role_system_prompt(role, release_root=REPO_ROOT)
        assert fit["with_charter"] is with_charter
        assert fit["with_soul"] is with_soul
        assert ("goals" in fit["blocks"]) is with_charter
        assert ("soul" in fit["blocks"]) is with_soul

    def test_tight_cap_roles_grow_only_by_the_short_identity(self):
        """strategist, reflector and skill-eval run under tight input caps, so
        the issue's revision bounds their growth by the first paragraph of
        IDENTITY.md rather than by identity + soul."""
        for role in ("strategist", "reflector", "skill-eval"):
            _, fit = build_role_system_prompt(role, release_root=REPO_ROOT)
            growth = fit["chars"] - len(PRE_MIGRATION_LITERALS[role])
            assert growth <= IDENTITY_SHORT_CAP + len(role_prompt.SECTION_SEPARATOR), (
                f"{role} grew by {growth} chars, past the short-identity bound"
            )

    def test_short_identity_is_the_first_paragraph_under_its_cap(self):
        text, _ = build_role_system_prompt("skill-eval", release_root=REPO_ROOT)
        identity = text.split(role_prompt.SECTION_SEPARATOR)[0]
        assert len(identity) <= IDENTITY_SHORT_CAP
        full = (REPO_ROOT / "IDENTITY.md").read_text(encoding="utf-8")
        assert len(identity) < len(full)
        # The paragraph, not the heading: a heading-only identity says nothing.
        assert identity.splitlines()[0] == "## IDENTITY.md"
        assert any(line.strip() for line in identity.splitlines()[1:])

    def test_full_identity_form_is_available_and_longer(self):
        short, _ = build_role_system_prompt("skill-eval", release_root=REPO_ROOT)
        full, _ = build_role_system_prompt("skill-eval", release_root=REPO_ROOT, identity_short=False)
        assert len(full) > len(short)


# ─── the narrator keeps the ADR-016 barrier ────────────────────────────────

class TestNarratorBarrier:
    def test_narrator_prompt_names_no_channel_figure(self):
        text, _ = build_role_system_prompt("narrator", release_root=REPO_ROOT)
        lowered = text.lower()
        for figure in ("views", "watch time", "subscriber", "impressions", "click-through", "returning viewer"):
            assert figure not in lowered, f"ADR-016 rule 1: {figure!r} reached the narrator prompt"

    def test_role_prompt_module_has_no_channel_reader(self):
        source = (REPO_ROOT / "nanobot" / "runtime" / "role_prompt.py").read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]  # skip the module docstring, which names the rule
        for token in ("youtube", "analytics", "subscriber", "views"):
            assert token not in body.lower()


# ─── no role prose survives as a literal ───────────────────────────────────

class TestNoLiteralsLeft:
    SOURCE_DIRS = ("nanobot", "scripts")
    # The opening words of the eight literals: if any of them is still in
    # Python source, a role is being authored in code again (ADR-022 rule 2).
    FORBIDDEN = (
        'role": "system", "content": "You are',
        "You are proposing exactly ONE small",
        "You are selecting exactly ONE demand item",
        "You are performing a periodic goal review",
        "You are the eeebot knowledge curator",
        "You are the eeebot strategist role",
        "You are the eeebot skill-eval executor",
        "Analyze every message, skill, command",
        "You write plain, warm narration",
    )

    #: Single-purpose tool-call prompts, not roles of the agent: each is one
    #: sentence telling a model what shape to return for one mechanical call,
    #: carries no persona, procedure or values, and has no operator-reviewable
    #: content to move. Named here so the ratchet stays honest — a NEW literal
    #: role prompt fails this test, these two do not silently pass unnamed.
    #: Their disposition (leave as code vs. move to roles/) is #1726's to take
    #: with the rest of the fingerprint table.
    TECHNICAL_PROMPT_ALLOWLIST = {
        "nanobot/agent/memory.py",  # "You are a memory consolidation agent." (96 chars)
        "nanobot/heartbeat/service.py",  # heartbeat tick prompt (73 chars)
    }

    def _python_sources(self):
        for directory in self.SOURCE_DIRS:
            for path in (REPO_ROOT / directory).rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                yield path

    @pytest.mark.parametrize("needle", FORBIDDEN)
    def test_role_prose_is_not_in_python_source(self, needle):
        offenders = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in self._python_sources()
            if needle in path.read_text(encoding="utf-8", errors="ignore")
        )
        offenders = [path for path in offenders if path not in self.TECHNICAL_PROMPT_ALLOWLIST]
        assert offenders == [], (
            f"role prose {needle!r} is authored in code again: {offenders}. "
            "It belongs in roles/<role>.md at the release root (ADR-022 rule 2)."
        )

    def test_the_allowlisted_prompts_are_still_short_and_still_there(self):
        """An allowlist entry that grew into a real role, or that vanished, must
        be re-decided rather than carried forward on trust."""
        for relative in sorted(self.TECHNICAL_PROMPT_ALLOWLIST):
            path = REPO_ROOT / relative
            assert path.is_file(), f"{relative} is allowlisted but absent; re-take the decision"
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r'"role": "system", "content": "([^"]*)"', source):
                assert len(match.group(1)) <= 120, (
                    f"{relative} now carries a {len(match.group(1))}-char system prompt; "
                    "that is a role, and it belongs in roles/<role>.md"
                )


# ─── telemetry: system_prompt_chars is measured from the built string ──────

class TestSystemPromptChars:
    def test_system_chars_reads_the_system_message(self):
        messages = [{"role": "system", "content": "abcde"}, {"role": "user", "content": "x" * 99}]
        assert system_chars(messages) == 5

    def test_system_chars_is_none_without_a_system_message(self):
        assert system_chars([]) is None
        assert system_chars(None) is None
        assert system_chars([{"role": "user", "content": "hi"}]) is None

    def test_recorded_row_carries_system_prompt_chars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
        from nanobot.observability import llm_telemetry

        monkeypatch.setattr(llm_telemetry, "_llm_calls_dir", lambda: tmp_path / "llm_calls")
        with llm_telemetry.call_context("cycle-test", "strategist"):
            llm_telemetry.record_llm_call(
                model="m", duration_ms=1.0, usage={"prompt_tokens": 1},
                finish_reason="stop", retries=0, system_prompt_chars=703,
            )
            llm_telemetry.record_llm_call(
                model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0,
            )
        rows = [
            json.loads(line)
            for path in sorted((tmp_path / "llm_calls").glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert [row["system_prompt_chars"] for row in rows] == [703, None]

    def test_measured_size_is_the_built_string_not_the_capped_payload(self):
        """`state/llm_calls/prompts` truncates around 6.5 KB; the proposer's
        assembled prompt is larger than that, so a size read back from the
        payload would understate it. The row must carry the built length."""
        text, fit = build_role_system_prompt("proposer", release_root=REPO_ROOT)
        assert fit["chars"] == len(text)
        assert system_chars([{"role": "system", "content": text}]) == len(text)


# ─── release-root resolution ───────────────────────────────────────────────

class TestReleaseRootResolution:
    def test_explicit_argument_wins(self, tmp_path):
        assert resolve_release_root(tmp_path) == tmp_path

    def test_env_var_wins_when_it_names_a_release_tree(self, tmp_path, monkeypatch):
        (tmp_path / "roles").mkdir()
        monkeypatch.setenv("RELEASE_ROOT", str(tmp_path))
        assert resolve_release_root() == tmp_path

    def test_env_var_without_roles_falls_through_to_the_package_tree(self, tmp_path, monkeypatch):
        """A ``RELEASE_ROOT`` carrying only ``goals.md`` (the shape several
        existing fixtures build) is not a release tree for roles. Reading roles
        out of it would report eight missing files and look like a broken
        deploy; the package tree is the honest answer."""
        (tmp_path / "goals.md").write_text("# Goals\n", encoding="utf-8")
        monkeypatch.setenv("RELEASE_ROOT", str(tmp_path))
        assert resolve_release_root() == REPO_ROOT

    def test_package_tree_is_the_fallback(self, monkeypatch):
        monkeypatch.delenv("RELEASE_ROOT", raising=False)
        assert resolve_release_root() == REPO_ROOT
