import ast
import inspect
import json
from pathlib import Path

from nanobot.runtime import knowledge_curator as curator

FIXTURE = Path(__file__).parent / "fixtures/reflector_clusters_1355.json"


def test_real_cluster_has_three_distinct_fields():
    cluster = json.loads(FIXTURE.read_text())[1]
    card = curator._reflector_card(card_id=cluster["cycles"][0], detail=cluster["detail"],
        problem=cluster["problem"], cycles=cluster["cycles"], days=cluster["days"],
        first_seen=cluster["first_seen"], last_seen=cluster["last_seen"], kind=cluster["kind"])
    assert card is not None
    assert len({card["title"], card["problem"], card["solution"]}) == 3
    assert card["title"] != card["solution"][:200]


def test_single_string_is_declined():
    assert curator._reflector_card(card_id="", detail="Run targeted verification before committing.",
        problem="", cycles=[], days=[], first_seen="", last_seen="") is None


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
