import ast
import inspect
import json
from collections import Counter
from pathlib import Path

from nanobot.runtime import knowledge_curator as curator

FIXTURE = Path(__file__).parent / "fixtures/reflector_clusters_1355.json"


def test_approach_hint_titles_are_derived_per_card():
    first = curator._reflector_card(
        card_id="first", detail="Use bounded reads", problem="Large files exhaust memory",
        cycles=["cycle-first"], days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
        kind="approach_hint",
    )
    second = curator._reflector_card(
        card_id="second", detail="Use a fallback route", problem="Provider requests return 404",
        cycles=["cycle-second"], days=["2026-09-02"], first_seen="2026-09-02", last_seen="2026-09-02",
        kind="approach_hint",
    )

    assert first and second
    assert first["title"] != second["title"]
    assert first["title"] == first["problem"]
    assert second["title"] == second["problem"]


def test_error_pattern_titles_are_derived_per_card():
    first = curator._reflector_card(
        card_id="first", detail="Retry once", problem="The gateway returns 502",
        cycles=["cycle-first"], days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
        kind="error_pattern",
    )
    second = curator._reflector_card(
        card_id="second", detail="Retry after the malformed JSON is detected", problem="The parser sees malformed JSON",
        cycles=["cycle-second"], days=["2026-09-02"], first_seen="2026-09-02", last_seen="2026-09-02",
        kind="error_pattern",
    )

    assert first and second
    assert first["title"] != second["title"]
    assert first["title"] == first["problem"]
    assert second["title"] == second["problem"]
    assert first["tags"] == ["config"]
    assert second["tags"] == ["runtime"]
    assert first["source"] == "reflector"
    assert second["source"] == "reflector"


def test_real_cluster_has_three_distinct_fields():
    cluster = json.loads(FIXTURE.read_text())[1]
    cluster["problem"] += " runtime"
    card = curator._reflector_card(card_id=cluster["cycles"][0], detail=cluster["detail"],
        problem=cluster["problem"], cycles=cluster["cycles"], days=cluster["days"],
        first_seen=cluster["first_seen"], last_seen=cluster["last_seen"], kind=cluster["kind"])
    assert card is not None
    assert card["title"] == card["problem"]
    assert card["title"] != "Reusable corrective approach"
    assert card["solution"]
    assert card["tags"] == ["runtime"]
    assert card["source"] == "reflector"


def test_reflector_tags_are_derived_per_subsystem():
    cards = [
        curator._reflector_card(
            card_id="config", detail="Configure the provider route",
            problem="The provider route returns 404", cycles=["cycle-config"],
            days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
        ),
        curator._reflector_card(
            card_id="prompt", detail="Trim optional context before sending the prompt",
            problem="The context window exceeds its budget", cycles=["cycle-prompt"],
            days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
        ),
        curator._reflector_card(
            card_id="git", detail="Keep the feature branch in the worktree",
            problem="A git branch reset discarded the commit", cycles=["cycle-git"],
            days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
        ),
    ]

    assert all(cards)
    tags = [card["tags"][0] for card in cards]
    assert tags == ["config", "prompt", "git"]
    assert len(set(tags)) == len(tags), "different subsystems must not collapse to one constant tag"
    assert Counter(tags) == {"config": 1, "prompt": 1, "git": 1}
    assert all(card["source"] == "reflector" for card in cards)


def test_reflector_card_without_a_vocabulary_term_is_declined():
    card = curator._reflector_card(
        card_id="no-topic", detail="Apply the orchard remedy",
        problem="The orchard fruit color was observed", cycles=["cycle-no-topic"],
        days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
    )
    assert card is None


def test_derived_tag_test_fails_against_constant_isolated_copy(tmp_path):
    source = Path(curator.__file__).read_text(encoding="utf-8")
    mutated = source.replace(
        "    return tags\n\n\ndef reflector_tag_drift(",
        '    return ["runtime"]\n\n\ndef reflector_tag_drift(',
        1,
    )
    assert mutated != source
    isolated_path = tmp_path / "isolated_knowledge_curator.py"
    isolated_path.write_text(mutated, encoding="utf-8")
    import sys
    import types

    module = types.ModuleType("isolated_knowledge_curator_tags")
    module.__file__ = str(isolated_path)
    sys.modules[module.__name__] = module
    exec(compile(mutated, str(isolated_path), "exec"), module.__dict__)

    first = module.__dict__["_reflector_card"](
        card_id="config", detail="Configure the provider route",
        problem="The provider route returns 404", cycles=["cycle-config"],
        days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
    )
    second = module.__dict__["_reflector_card"](
        card_id="prompt", detail="Trim optional context before sending the prompt",
        problem="The context window exceeds its budget", cycles=["cycle-prompt"],
        days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
    )
    assert first and second
    assert first["tags"] == ["runtime"]
    assert second["tags"] == ["runtime"]


def test_single_string_is_declined():
    assert curator._reflector_card(card_id="", detail="Run targeted verification before committing.",
        problem="", cycles=[], days=[], first_seen="", last_seen="") is None


def test_other_kinds_remain_declined():
    assert curator._reflector_card(
        card_id="other", detail="Some detail", problem="Some condition",
        cycles=["cycle-other"], days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
        kind="good_practice",
    ) is None


def test_constant_title_mutation_is_caught_on_an_isolated_copy(tmp_path):
    source = Path(curator.__file__).read_text(encoding="utf-8")
    mutated = source.replace(
        'title = problem.strip()',
        'title = "Reusable corrective approach"',
        1,
    )
    assert mutated != source
    isolated_path = tmp_path / "isolated_knowledge_curator.py"
    isolated_path.write_text(mutated, encoding="utf-8")
    import sys
    import types

    module = types.ModuleType("isolated_knowledge_curator")
    module.__file__ = str(isolated_path)
    sys.modules[module.__name__] = module
    exec(compile(mutated, str(isolated_path), "exec"), module.__dict__)

    first = module.__dict__["_reflector_card"](
        card_id="first", detail="Use bounded reads", problem="Large files exhaust memory",
        cycles=["cycle-first"], days=["2026-09-01"], first_seen="2026-09-01", last_seen="2026-09-01",
        kind="approach_hint",
    )
    second = module.__dict__["_reflector_card"](
        card_id="second", detail="Use a fallback route", problem="Provider requests return 404",
        cycles=["cycle-second"], days=["2026-09-02"], first_seen="2026-09-02", last_seen="2026-09-02",
        kind="approach_hint",
    )
    assert first and second
    assert first["title"] == second["title"]


def test_all_card_builders_use_distinct_sources():
    tree = ast.parse(inspect.getsource(curator))
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
        if not {"title", "problem", "solution"} <= fields.keys():
            continue
        found += 1
        sources = [{n.id for n in ast.walk(fields[key]) if isinstance(n, ast.Name)}
                   for key in ("title", "problem", "solution")]
        assert all(sources)
        assert not any(sources[i] & sources[j] for i in range(3) for j in range(i))
    assert found > 0
