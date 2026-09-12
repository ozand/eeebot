import ast
import inspect
import json
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
        card_id="second", detail="Use a fallback", problem="The parser sees malformed JSON",
        cycles=["cycle-second"], days=["2026-09-02"], first_seen="2026-09-02", last_seen="2026-09-02",
        kind="error_pattern",
    )

    assert first and second
    assert first["title"] != second["title"]
    assert first["title"] == "The gateway returns 502: Retry once"
    assert second["title"] == "The parser sees malformed JSON: Use a fallback"


def test_real_cluster_has_three_distinct_fields():
    cluster = json.loads(FIXTURE.read_text())[1]
    card = curator._reflector_card(card_id=cluster["cycles"][0], detail=cluster["detail"],
        problem=cluster["problem"], cycles=cluster["cycles"], days=cluster["days"],
        first_seen=cluster["first_seen"], last_seen=cluster["last_seen"], kind=cluster["kind"])
    assert card is not None
    assert card["title"] == card["problem"]
    assert card["title"] != "Reusable corrective approach"
    assert card["solution"]


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
        'elif kind == "approach_hint":\n        title = problem.strip()',
        'elif kind == "approach_hint":\n        title = "Reusable corrective approach"',
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
