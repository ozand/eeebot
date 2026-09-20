"""The artifact dependency graph (#1769, ADR-024, ADR-025).

Nodes are the loop's own artifacts in the instance repository: top-level
`scripts/*.py`, `surfaces/*` files, and `skills/*/SKILL.md`. Edges are
"A uses B", resolved from what can be read statically — imports, a literal
`scripts/<name>.py` path in a subprocess/`os.system` call, a systemd unit
or timer naming a script, a surface or skill referencing one, or a
documentation instruction to run one.

ADR-024's typed-edge decision, restated once here (every consumer reads
this module, never restates it):

- `used_by`      — production use. Only this kind makes an artifact a
                    *component*; see :meth:`ArtifactGraph.is_component`.
- `tested_by`    — a test imports or executes it. Coverage, not use.
- `mentioned_in` — named in prose, a catalogue, a comment or a docstring.
                    Never promotes a leaf, by construction (rule 2 below).

Does `tested_by` raise the rung? **No — ADR-024 and ADR-025 agree, they do
not conflict.** ADR-024 names only `used_by` as promoting; ADR-025 decision
2 makes this explicit and gives the reason: readiness ("does anything need
this") and testedness ("does it work") are two independent axes, and 90 of
114 leaves measured tested-and-unused proves the signal does not
discriminate — treating it as a half-step was considered and rejected in
ADR-025's own alternatives section. `is_component` implements exactly this:
only `used_by` is ever inspected.

`used_by` itself excludes an artifact manufacturing its own promotion — a
self-loop, or a mutual pair with no anchor outside itself (a wrapper or
skill minted purely to hand its target an edge, and vice versa). See
:meth:`ArtifactGraph.is_component` for the mechanism and the incident that
motivated it (the validity ladder's rung 2 read literally by the proposer
within hours of shipping, 2026-09-20).

Resolution is conservative by design (ADR-024 rule 5): an edge that cannot
be proven from a literal path is not an edge. Ambiguous stems (the same
name resolves to more than one node), a subprocess/`os.system` argument
built from a variable or an f-string rather than a string literal, and any
other reference this module cannot classify all fall into
``unresolved_count`` — reported, never silently dropped and never counted
as an edge either way.

This module never touches the host. A systemd unit is read only when its
`.service`/`.timer` file is checked into `host/eeepc/systemd/` in THIS
(harness) repository — genuinely host-only units (created via `systemctl
edit`/a manual file drop, never committed anywhere) are a stated,
zero-count blind spot; see :attr:`ArtifactGraph.unit_scan_status` and the
module docstring's own PR notes. "Static resolution only" (the issue's own
non-goal) rules out live host reads as a design choice, not an oversight.

Per ADR-023, every input this module reads (`scripts/`, `tests/`,
`docs/`, `surfaces/`, `skills/` in the instance repository, plus
`host/eeepc/systemd/` in this repository) lies inside the loop's own
writable surface except the systemd class. The resulting figures are
therefore UNSAFE by ADR-023's provenance test and must never be presented
to the loop as fact — they are dashboard/demand-ranking input only. See
``docs/adr/ADR-023-a-fact-shown-to-the-loop-is-one-the-loop-cannot-reach.md``.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "artifact-graph-v1"

#: ADR-024's three edge kinds. Order matters nowhere; membership does.
EDGE_KINDS = ("used_by", "tested_by", "mentioned_in")

_SCRIPTS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])scripts/([A-Za-z_]\w*)\.py(?![A-Za-z0-9_])")
#: A load pattern in this codebase's own tests (43+ files, measured
#: 2026-09-19): `scripts/` has no `__init__.py`, so a test loads a
#: standalone script via `importlib.util.spec_from_file_location(name,
#: REPO_ROOT / 'scripts' / 'x.py')` — a `Path` built from two SEPARATE
#: string literals, which `_SCRIPTS_PATH_RE` (one literal) cannot see and a
#: plain `ast.Import` walk cannot see either (the argument is a variable,
#: not the literal). This is not prose and not ambiguous — it is a
#: deliberate path-construction expression — so it is resolved the same
#: way as a single-literal path, not routed through the unresolved count.
#:
#: An even more common form (89 files, plus two real production aggregator
#: scripts) is `from scripts.x import y` / `import scripts.x` / `from
#: scripts import x` -- `scripts/` has no `__init__.py` but is still
#: importable as an implicit namespace package. `node.module.split(".")[0]`
#: (the OLD resolver's own logic, `scorecard._retention_cost`) always
#: yields the literal string "scripts", never a real stem, and silently
#: drops the edge -- handled directly in the AST walk below, not by regex.
_SPLIT_SCRIPTS_PATH_RE = re.compile(r"""['"]scripts['"]\s*/\s*['"]([A-Za-z_]\w*)\.py['"]""")
#: A documented RUN instruction, not just a mention: the path appears as a
#: shell/python invocation (a fenced code line, optionally `$`-prefixed).
#: ADR-024's own sample (10 `.md` refs, 2 real instructions) is exactly this
#: narrow — matching more here would misclassify prose as use.
_RUN_INSTRUCTION_RE = re.compile(
    r"^\s*\$?\s*(?:python3?\s+)?\.?/?scripts/([A-Za-z_]\w*)\.py\b",
    re.MULTILINE,
)
#: A subprocess/os.system argument list containing a literal `scripts/x.py`
#: path -- resolved via AST below, this regex only extracts the stem once
#: an ast.Constant string has already been confirmed as the argument.


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # "script" | "surface" | "skill"
    path: str  # repo-relative


@dataclass(frozen=True)
class Edge:
    source: str    # repo-relative path of the referencing file
    target: str    # node id
    kind: str      # one of EDGE_KINDS
    evidence: str  # short machine-readable tag, e.g. "import", "exec_path"


@dataclass
class ArtifactGraph:
    """The resolved graph, or an explicit unavailable state.

    ``status == "unavailable"`` (missing/unreadable repo) is never
    presented as an empty graph — a reader must check ``status`` before
    trusting ``nodes``/``edges`` at all (AC: "a missing or unreadable input
    yields an explicit 'unavailable' graph, never an empty graph presented
    as 'no dependencies'").
    """

    status: str
    generated_at: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    unresolved_count: int = 0
    unresolved_examples: list[str] = field(default_factory=list)
    unit_scan_status: str = "unavailable"  # "scanned" | "unavailable"
    notes: list[str] = field(default_factory=list)

    # ─── the rung (ADR-024 decision 2, extended by #1801-adjacent hardening
    #     against self-reference; stated once) ───────────────────────────

    def _node_id_by_path(self) -> dict[str, str]:
        return {n.path: nid for nid, n in self.nodes.items()}

    def is_component(self, node_id: str) -> bool:
        """True iff at least one GENUINE ``used_by`` edge targets *node_id*.

        This is THE rung definition. Every consumer (scorecard, dashboard,
        demand pipeline) calls this — or :meth:`rung` — rather than
        re-deriving it from ``edges`` directly, so the definition changes
        in exactly one place if it ever needs to.

        "Genuine" excludes two shapes an artifact can manufacture for
        itself, measured live 2026-09-20 (the ladder's rung 2 read
        literally by the proposer within hours of shipping): a
        self-loop (a script importing/invoking itself — ``source`` and
        ``target`` resolve to the same node), and a mutual pair (node A's
        only edge into node B is reciprocated by B's only edge into A —
        "give a script self-invocation", a wrapper/skill minted purely to
        hand its target a `used_by` edge, and the target is the wrapper's
        or skill's only reason to exist). Neither is "something ELSE
        depends on it" (ADR-025's own rung sentence) — it is the artifact
        depending on itself through one extra hop.

        A source that is not itself a graph node at all — a doc's run
        instruction, a systemd unit, an operator procedure — cannot form
        either shape (it has no reciprocal edge to receive) and always
        promotes, unchanged from before this hardening.

        An edge excluded here as part of a mutual pair does not vanish —
        it is simply not counted as PROMOTING. If either side of the pair
        also has a genuine edge from outside the pair, that side still
        promotes on that edge; only the manufactured reciprocal link is
        inert. This is why a real two-way dependency between two
        independently-used modules is never wrongly stranded — only a
        pair with no anchor outside itself is.
        """
        node_by_path = self._node_id_by_path()
        for e in self.edges:
            if e.kind != "used_by" or e.target != node_id:
                continue
            source_node_id = node_by_path.get(e.source)
            if source_node_id is None:
                return True  # not a graph node -- cannot self-reference
            if source_node_id == node_id:
                continue  # self-loop
            if self._has_used_by_edge(node_id, source_node_id):
                continue  # mutual pair: target's own edge reaches back to source
            return True
        return False

    def _has_used_by_edge(self, source_node_id: str, target: str) -> bool:
        """True iff some ``used_by`` edge whose source resolves to
        *source_node_id* targets *target* -- used only for the mutual-pair
        check above, never as a public entry point (call :meth:`is_component`
        instead, which applies the self-reference exclusion this helper
        does not)."""
        node_by_path = self._node_id_by_path()
        return any(
            e.kind == "used_by" and e.target == target and node_by_path.get(e.source) == source_node_id
            for e in self.edges
        )

    def rung(self, node_id: str) -> str:
        return "component" if self.is_component(node_id) else "leaf"

    def leaves(self) -> list[str]:
        """Every node id with no ``used_by`` edge, in node-discovery order."""
        return [nid for nid in self.nodes if not self.is_component(nid)]

    def components(self) -> list[str]:
        return [nid for nid in self.nodes if self.is_component(nid)]

    def oldest_leaves(self, *, repo: Path | None = None, limit: int = 10) -> list[str]:
        """Leaf node ids sorted oldest-first by file mtime, when *repo* is
        given (the caller's own repo checkout — this module does not hold
        one). Falls back to discovery order (stable, not a fabricated age)
        when *repo* is absent or a path is unreadable."""
        leaf_ids = self.leaves()
        if repo is None:
            return leaf_ids[:limit]
        with_mtime: list[tuple[float, str]] = []
        for nid in leaf_ids:
            node = self.nodes.get(nid)
            if node is None:
                continue
            try:
                mtime = (repo / node.path).stat().st_mtime
            except OSError:
                continue
            with_mtime.append((mtime, nid))
        with_mtime.sort(key=lambda pair: pair[0])
        return [nid for _mtime, nid in with_mtime[:limit]]

    # ─── serialization ──────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "generated_at": self.generated_at,
            "nodes": {nid: {"type": n.type, "path": n.path} for nid, n in self.nodes.items()},
            "edges": [
                {"source": e.source, "target": e.target, "kind": e.kind, "evidence": e.evidence}
                for e in self.edges
            ],
            "counts": {
                "artifacts": len(self.nodes),
                "components": len(self.components()),
                "leaves": len(self.leaves()),
                "unresolved": self.unresolved_count,
            },
            "unresolved_examples": list(self.unresolved_examples[:20]),
            "unit_scan_status": self.unit_scan_status,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactGraph":
        nodes_raw = data.get("nodes")
        nodes = {
            nid: Node(id=nid, type=str(n.get("type") or ""), path=str(n.get("path") or ""))
            for nid, n in (nodes_raw.items() if isinstance(nodes_raw, dict) else [])
            if isinstance(n, dict)
        }
        edges_raw = data.get("edges")
        edges = [
            Edge(
                source=str(e.get("source") or ""),
                target=str(e.get("target") or ""),
                kind=str(e.get("kind") or ""),
                evidence=str(e.get("evidence") or ""),
            )
            for e in (edges_raw if isinstance(edges_raw, list) else [])
            if isinstance(e, dict)
        ]
        counts_raw = data.get("counts")
        counts: dict[str, Any] = counts_raw if isinstance(counts_raw, dict) else {}
        return cls(
            status=str(data.get("status") or "unavailable"),
            generated_at=str(data.get("generated_at") or ""),
            nodes=nodes,
            edges=edges,
            unresolved_count=int(counts.get("unresolved") or data.get("unresolved_count") or 0),
            unresolved_examples=list(data.get("unresolved_examples") or []),
            unit_scan_status=str(data.get("unit_scan_status") or "unavailable"),
            notes=list(data.get("notes") or []),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unavailable(reason: str) -> ArtifactGraph:
    return ArtifactGraph(status="unavailable", generated_at=_now(), notes=[reason])


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ─── node discovery ─────────────────────────────────────────────────────────


def _discover_nodes(repo: Path) -> dict[str, Node]:
    nodes: dict[str, Node] = {}
    scripts_dir = repo / "scripts"
    if scripts_dir.is_dir():
        for p in sorted(scripts_dir.glob("*.py")):
            nid = f"scripts/{p.stem}"
            nodes[nid] = Node(id=nid, type="script", path=f"scripts/{p.name}")
    surfaces_dir = repo / "surfaces"
    if surfaces_dir.is_dir():
        for p in sorted(surfaces_dir.iterdir()):
            if p.is_file():
                nid = f"surfaces/{p.name}"
                nodes[nid] = Node(id=nid, type="surface", path=f"surfaces/{p.name}")
    skills_dir = repo / "skills"
    if skills_dir.is_dir():
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").is_file():
                nid = f"skills/{d.name}"
                nodes[nid] = Node(id=nid, type="skill", path=f"skills/{d.name}/SKILL.md")
    return nodes


# ─── reference extraction from one source file ─────────────────────────────


def _script_stem_targets(stems_index: dict[str, list[str]], stem: str) -> tuple[str | None, bool]:
    """``(node_id, ambiguous)`` for a bare script stem against the node
    index. ``ambiguous`` is True when the stem resolves to more than one
    node id (ADR-024 rule 5's "name matching two files") -- the caller
    counts that as unresolved, never guesses which one was meant."""
    candidates = stems_index.get(stem) or []
    if len(candidates) == 1:
        return candidates[0], False
    if len(candidates) > 1:
        return None, True
    return None, False


def _python_source_references(
    text: str, stems_index: dict[str, list[str]],
) -> tuple[set[str], int, list[str]]:
    """From a `.py` file's text: ``(resolved node ids, unresolved count,
    unresolved examples)``. Covers plain imports, `scripts/<x>.py` path
    literals anywhere in the text (docstrings/comments included -- see the
    note below), and subprocess/os.system calls whose argument is a
    literal path vs. one built from a variable/f-string.
    """
    resolved: set[str] = set()
    unresolved = 0
    examples: list[str] = []

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    # `import scripts.x` -- the real target is the
                    # submodule, not the `scripts` package name.
                    stem = parts[1] if parts[0] == "scripts" and len(parts) > 1 else parts[0]
                    nid, ambiguous = _script_stem_targets(stems_index, stem)
                    if nid:
                        resolved.add(nid)
                    elif ambiguous:
                        unresolved += 1
                        examples.append(f"ambiguous import stem {stem!r}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if parts[0] == "scripts" and len(parts) > 1:
                    # `from scripts.x import ...` -- 89 of this codebase's
                    # own test files use exactly this form (measured
                    # 2026-09-19): `scripts/` has no `__init__.py` but is
                    # still importable as an implicit namespace package, so
                    # `node.module.split(".")[0]` (the old resolver's own
                    # logic) silently discards the actual target and always
                    # resolves to the literal string "scripts", which is
                    # never a stem. Take the submodule instead.
                    candidate_stems = [parts[1]]
                elif parts[0] == "scripts" and len(parts) == 1:
                    # `from scripts import x[, y]` -- each imported name is
                    # itself a submodule/stem.
                    candidate_stems = [alias.name for alias in node.names]
                else:
                    candidate_stems = [parts[0]]
                for stem in candidate_stems:
                    nid, ambiguous = _script_stem_targets(stems_index, stem)
                    if nid:
                        resolved.add(nid)
                    elif ambiguous:
                        unresolved += 1
                        examples.append(f"ambiguous import stem {stem!r}")
            elif isinstance(node, ast.Call):
                is_subprocess_ish = (
                    (isinstance(node.func, ast.Attribute) and node.func.attr in
                     {"run", "Popen", "call", "check_call", "check_output"})
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "system")
                )
                if not is_subprocess_ish:
                    continue
                for arg in ast.walk(node):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        for stem in _SCRIPTS_PATH_RE.findall(arg.value):
                            nid, ambiguous = _script_stem_targets(stems_index, stem)
                            if nid:
                                resolved.add(nid)
                            elif ambiguous:
                                unresolved += 1
                                examples.append(f"ambiguous exec path stem {stem!r}")
                    elif isinstance(arg, ast.JoinedStr):
                        # An f-string argument to subprocess/os.system: the
                        # path is built at runtime, not a literal -- this is
                        # exactly ADR-024 rule 5's "built from a variable
                        # rather than a literal". Only counted when the
                        # f-string mentions scripts/ at all (otherwise it is
                        # not a candidate reference in the first place).
                        static_parts = "".join(
                            v.value for v in arg.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)
                        )
                        if "scripts/" in static_parts:
                            unresolved += 1
                            examples.append("runtime-built exec path (f-string)")
                    elif isinstance(arg, ast.Name):
                        # A bare variable/name as the whole command arg
                        # (e.g. subprocess.run(cmd)) cannot be resolved
                        # statically at all; only counts if we have other
                        # evidence this call targets a script (skipped --
                        # no literal to even suspect scripts/ involvement,
                        # so this is not a reference, not an unresolved one).
                        continue

    # The split-literal `'scripts' / 'x.py'` Path-construction idiom (see
    # _SPLIT_SCRIPTS_PATH_RE) -- the dominant way this codebase's OWN tests
    # load a standalone script, and a deliberate expression rather than
    # prose, so it resolves the same as a single-literal path.
    for stem in _SPLIT_SCRIPTS_PATH_RE.findall(text):
        nid, ambiguous = _script_stem_targets(stems_index, stem)
        if nid:
            resolved.add(nid)
        elif ambiguous:
            unresolved += 1
            examples.append(f"ambiguous split-path stem {stem!r}")

    return resolved, unresolved, examples


# ─── the main resolver ──────────────────────────────────────────────────────


def build_artifact_graph(repo: Path, *, harness_root: Path | None = None) -> ArtifactGraph:
    """Build the graph from *repo* (the instance repository checkout).

    *harness_root* (defaults to this package's own repo root) is where
    `host/eeepc/systemd/*.service`/`*.timer` are read from for the
    systemd-unit `used_by` class — a THIS-repo file, never a live host
    read; see the module docstring.
    """
    repo = Path(repo)
    if not repo.is_dir():
        return _unavailable(f"instance repo not found: {repo}")

    nodes = _discover_nodes(repo)
    if not nodes:
        return _unavailable(f"no scripts/surfaces/skills discovered under {repo}")

    stems_index: dict[str, list[str]] = {}
    for nid, node in nodes.items():
        if node.type == "script":
            stems_index.setdefault(Path(node.path).stem, []).append(nid)

    edges: list[Edge] = []
    unresolved_count = 0
    unresolved_examples: list[str] = []

    def _record_unresolved(n: int, examples: list[str]) -> None:
        nonlocal unresolved_count
        unresolved_count += n
        unresolved_examples.extend(examples)

    # scripts/*.py referencing other scripts -> used_by
    scripts_dir = repo / "scripts"
    if scripts_dir.is_dir():
        for p in sorted(scripts_dir.glob("*.py")):
            text = _read_text(p)
            if text is None:
                continue
            resolved, n_unresolved, examples = _python_source_references(text, stems_index)
            self_id = f"scripts/{p.stem}"
            source_rel = f"scripts/{p.name}"
            for target in resolved:
                if target == self_id:
                    continue
                edges.append(Edge(source=source_rel, target=target, kind="used_by", evidence="import_or_exec_path"))
            _record_unresolved(n_unresolved, [f"{source_rel}: {e}" for e in examples])

    # tests/**/*.py referencing scripts -> tested_by
    tests_dir = repo / "tests"
    if tests_dir.is_dir():
        for p in sorted(tests_dir.rglob("*.py")):
            text = _read_text(p)
            if text is None:
                continue
            resolved, n_unresolved, examples = _python_source_references(text, stems_index)
            source_rel = p.relative_to(repo).as_posix()
            for target in resolved:
                edges.append(Edge(source=source_rel, target=target, kind="tested_by", evidence="import_or_exec_path"))
            _record_unresolved(n_unresolved, [f"{source_rel}: {e}" for e in examples])

    # surfaces/* and skills/*/SKILL.md referencing scripts -> used_by
    # (ADR-024 rule 1: "a surface or skill that invokes it").
    for node in nodes.values():
        if node.type not in ("surface", "skill"):
            continue
        text = _read_text(repo / node.path)
        if text is None:
            continue
        stems = set(_SCRIPTS_PATH_RE.findall(text))
        for stem in stems:
            nid, ambiguous = _script_stem_targets(stems_index, stem)
            if nid:
                edges.append(Edge(source=node.path, target=nid, kind="used_by", evidence=f"{node.type}_reference"))
            elif ambiguous:
                _record_unresolved(1, [f"{node.path}: ambiguous {node.type} reference stem {stem!r}"])

    # docs/**/*.md -> used_by (a run instruction) or mentioned_in (prose),
    # per ADR-024 rule 4's conservative default.
    docs_dir = repo / "docs"
    if docs_dir.is_dir():
        for p in sorted(docs_dir.rglob("*.md")):
            text = _read_text(p)
            if text is None:
                continue
            source_rel = p.relative_to(repo).as_posix()
            instructed = set(_RUN_INSTRUCTION_RE.findall(text))
            all_mentioned = set(_SCRIPTS_PATH_RE.findall(text))
            for stem in instructed:
                nid, ambiguous = _script_stem_targets(stems_index, stem)
                if nid:
                    edges.append(Edge(source=source_rel, target=nid, kind="used_by", evidence="run_instruction"))
                elif ambiguous:
                    _record_unresolved(1, [f"{source_rel}: ambiguous run-instruction stem {stem!r}"])
            for stem in all_mentioned - instructed:
                nid, ambiguous = _script_stem_targets(stems_index, stem)
                if nid:
                    edges.append(Edge(source=source_rel, target=nid, kind="mentioned_in", evidence="prose_mention"))
                elif ambiguous:
                    _record_unresolved(1, [f"{source_rel}: ambiguous prose-mention stem {stem!r}"])

    # host/eeepc/systemd/*.service|*.timer (THIS repo, never the live host)
    # -> used_by. See the module docstring for why this is not a host read.
    unit_scan_status = "unavailable"
    if harness_root is None:
        harness_root = Path(__file__).resolve().parents[2]
    systemd_dir = harness_root / "host" / "eeepc" / "systemd"
    if systemd_dir.is_dir():
        unit_scan_status = "scanned"
        for p in sorted(list(systemd_dir.glob("*.service")) + list(systemd_dir.glob("*.timer"))):
            text = _read_text(p)
            if text is None:
                continue
            source_rel = p.relative_to(harness_root).as_posix()
            for stem in set(_SCRIPTS_PATH_RE.findall(text)):
                nid, ambiguous = _script_stem_targets(stems_index, stem)
                if nid:
                    edges.append(Edge(source=source_rel, target=nid, kind="used_by", evidence="systemd_unit"))
                elif ambiguous:
                    _record_unresolved(1, [f"{source_rel}: ambiguous systemd-unit stem {stem!r}"])

    return ArtifactGraph(
        status="complete",
        generated_at=_now(),
        nodes=nodes,
        edges=edges,
        unresolved_count=unresolved_count,
        unresolved_examples=unresolved_examples,
        unit_scan_status=unit_scan_status,
        notes=[
            "Genuinely host-only systemd units (never committed to host/eeepc/systemd/ in this "
            "repo, e.g. created via systemctl edit) are a structural blind spot, not counted "
            "above -- static resolution only, per this issue's own non-goal.",
        ],
    )


# ─── harness-owned publication ──────────────────────────────────────────────
#
# Published to `<state_dir>/artifact_graph/latest.json`, the same shape
# `scorecard._latest_path` uses for `scorecard/latest.json` -- `state_dir`
# is the harness's OWN runtime state directory (e.g.
# `/var/lib/eeepc-agent/self-evolving-agent/state/` on eeepc), never a path
# inside the instance repository the loop commits to. The loop's writable
# surface (`mutation_policy._COMMIT_PATH_PREFIXES`:  scripts/, tests/,
# docs/, surfaces/, skills/, memory/, lessons/ -- all *inside* the repo
# checkout) has no member that resolves to any path under `state_dir`, so a
# loop-authored commit has no reachable path to this file at all -- not a
# permission check, a namespace one. See
# `tests/test_artifact_graph_provenance.py`.


def _graph_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "artifact_graph"


def _graph_latest_path(state_dir: Path) -> Path:
    return _graph_dir(state_dir) / "latest.json"


def publish_artifact_graph(
    state_dir: Path, repo: Path, *, harness_root: Path | None = None,
) -> Path:
    """Build the graph and overwrite `<state_dir>/artifact_graph/latest.json`.

    Harness-owned: this is the only function that writes this path. Nothing
    the loop can commit (see the module note above) is ever consulted to
    produce or alter its content.
    """
    graph = build_artifact_graph(repo, harness_root=harness_root)
    path = _graph_latest_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read_latest_artifact_graph(state_dir: Path) -> ArtifactGraph:
    """Read the last-published graph. Missing/corrupt is `status ==
    "unavailable"`, never an empty graph presented as "no dependencies"."""
    path = _graph_latest_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _unavailable(f"no published artifact graph at {path}")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return _unavailable(f"unreadable artifact graph at {path}")
    if not isinstance(data, dict):
        return _unavailable(f"malformed artifact graph at {path}")
    return ArtifactGraph.from_dict(data)
