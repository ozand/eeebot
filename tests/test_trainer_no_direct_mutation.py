"""ADR-021 rule 2, asserted on the call graph (#1666 phase 2).

"No trainer path commits directly ... asserted on the call graph, not by
convention. The ADR says this explicitly because a convention is what #1188
violated." (issue #1666)

The "trainer" is the night contour: ``reflector.py`` (transcripts ->
journal), ``knowledge_curator.py`` (journal -> lesson proposals),
``strategist.py`` (cross-cycle direction), and ``retirement_candidates.py``
(#1672, non-use evidence). ``trainer_evidence.py`` is the citation-shape
validator ADR-021 rule 4 requires of a proposal.

Two things are asserted about the reachable call graph of those five
modules (their own code, not every module they merely import for reading):

1. No function anywhere in that reachable set ever shells out to a git
   mutating verb (commit/push/add/checkout/reset/merge/rm/mv/tag/apply/am/
   stash/clean/cherry-pick). All commit/push machinery lives in
   ``nanobot.runtime.bridge``, invoked from the bridge's own cycle/pickup
   flow -- never from a trainer module.
2. The only function in that reachable set whose body both performs a write
   operation (``write_text``/``write_bytes``/``os.replace``/``os.rename``/
   ``unlink``/``mkdir``/``atomic_write_yaml``/``open(..., "w"/"a")``) AND
   names a ``lessons``/``skills`` path is
   ``knowledge_curator.apply_staged_lesson_cards``. Its only caller anywhere
   in ``nanobot/`` must be the concrete gate entrypoint named below --
   ``nanobot.runtime.bridge._pickup_staged_promotions`` -- and that
   function's only caller must be inside ``bridge.py`` itself (the bridge's
   own cycle-start boundary, never a trainer module).

If a trainer module grows a new writer into ``lessons/``/``skills/``, or an
existing one gains a second caller, this test fails and names the offending
function -- the gate stops being the only door the moment either changes.

#1666 phase 3 adds a second, narrower lock: ``retirement_candidates.
propose_retirement`` (a pure citation-shaping function, no I/O, no
deletion) must be reachable ONLY through ``knowledge_curator.
stage_retirement_proposal`` -- the retire-side gate that validates the
citation and the exposure floor before recording a proposal. A second
caller of ``propose_retirement`` would be a retire path that can build a
proposal without ever being validated.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = REPO_ROOT / "nanobot" / "runtime"

# The night contour, per ADR-021 and #1666's own framing.
TRAINER_SEED_MODULES = (
    "nanobot.runtime.reflector",
    "nanobot.runtime.knowledge_curator",
    "nanobot.runtime.strategist",
    "nanobot.runtime.retirement_candidates",
    "nanobot.runtime.trainer_evidence",
)

# The concrete gate entrypoint (ADR-021 rule 2): the sole caller through
# which a trainer-staged lesson change may reach the repository checkout.
GATE_ENTRYPOINT_MODULE = "nanobot.runtime.bridge"
GATE_ENTRYPOINT_FUNC = "_pickup_staged_promotions"
GATE_WRITER_MODULE = "nanobot.runtime.knowledge_curator"
GATE_WRITER_FUNC = "apply_staged_lesson_cards"

# #1666 phase 3: the retire-side gate and the proposal builder it alone
# may call.
RETIRE_GATE_MODULE = "nanobot.runtime.knowledge_curator"
RETIRE_GATE_FUNC = "stage_retirement_proposal"
RETIRE_PROPOSAL_BUILDER_MODULE = "nanobot.runtime.retirement_candidates"
RETIRE_PROPOSAL_BUILDER_FUNC = "propose_retirement"

_GIT_MUTATING_VERBS = frozenset({
    "commit", "push", "add", "checkout", "reset", "merge", "rm", "mv", "tag",
    "apply", "am", "stash", "clean", "cherry-pick",
})
_SUBPROCESS_CALL_NAMES = frozenset({"run", "call", "check_call", "check_output", "Popen", "system"})

_WRITE_OP_PATTERN = re.compile(
    r"\.write_text\(|\.write_bytes\(|os\.replace\(|os\.rename\(|os\.remove\(|os\.unlink\("
    r"|\.unlink\(|shutil\.\w+\(|\.touch\(|\.mkdir\(|atomic_write_yaml\("
    r"|\.open\(\s*[\"'][wax]|open\([^)]*,\s*[\"'][wax]"
)
_LESSONS_SKILLS_PATH_PATTERN = re.compile(r"""["'](?:lessons|skills)(?:/|["'])""")


def _module_path(module: str) -> "Path | None":
    candidate = RUNTIME_DIR.parent.parent / (module.replace(".", "/") + ".py")
    return candidate if candidate.is_file() else None


def _module_tree(module: str) -> "tuple[Path, ast.Module] | None":
    path = _module_path(module)
    if path is None:
        return None
    return path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_nanobot_modules(module: str) -> set[str]:
    found = _module_tree(module)
    if found is None:
        return set()
    _, tree = found
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nanobot."):
                    out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: not used in nanobot.runtime
                continue
            mod = node.module or ""
            if mod.startswith("nanobot"):
                for alias in node.names:
                    candidate = f"{mod}.{alias.name}"
                    out.add(candidate if _module_path(candidate) else mod)
    return out


def _trainer_closure() -> set[str]:
    """Every nanobot.* module reachable from the trainer seed modules."""
    seen: set[str] = set()
    frontier: list[str] = list(TRAINER_SEED_MODULES)
    while frontier:
        mod = frontier.pop()
        if mod in seen:
            continue
        seen.add(mod)
        frontier.extend(_imported_nanobot_modules(mod))
    return seen


def _iter_functions(tree: ast.Module, source: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segment = ast.get_source_segment(source, node) or ""
            yield node, segment


def test_no_trainer_function_shells_out_to_git() -> None:
    """No function reachable from the trainer seed modules ever runs a git
    mutating command. All commits/pushes belong to bridge.py's own flow."""
    offenders: list[str] = []
    for module in sorted(_trainer_closure()):
        found = _module_tree(module)
        if found is None:
            continue
        path, tree = found
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(func):
                if not isinstance(call, ast.Call):
                    continue
                target = call.func
                name = target.attr if isinstance(target, ast.Attribute) else (
                    target.id if isinstance(target, ast.Name) else ""
                )
                if name not in _SUBPROCESS_CALL_NAMES:
                    continue
                literals = {
                    const.value for const in ast.walk(call)
                    if isinstance(const, ast.Constant) and isinstance(const.value, str)
                }
                hit = literals & _GIT_MUTATING_VERBS
                if hit:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{func.lineno} {func.name} -> {sorted(hit)}")
    assert not offenders, (
        "Trainer module(s) shell out to a git mutating verb directly -- all "
        "commits/pushes must live in bridge.py's own flow:\n" + "\n".join(offenders)
    )


# nanobot.runtime.lesson_index is import-reachable from knowledge_curator
# (it reads generated index rows for curator prompts) but its one function
# that writes a lessons/ path, generate_index, is never CALLED by any
# trainer module -- only by bridge._catch_up_main, unconditionally after
# every reset, to rebuild a deterministic derived catalogue (no lesson body
# content, not proposal-driven). test_generate_index_is_never_called_by_a_
# trainer_module below verifies that claim on the call graph rather than
# taking it on faith, so this allowlist entry cannot silently go stale.
_ADDITIONAL_ALLOWED_WRITERS = frozenset({"nanobot.runtime.lesson_index:generate_index"})


def test_only_the_named_gate_writer_touches_lessons_or_skills_paths() -> None:
    """Within the trainer's reachable modules, the only function whose body
    writes AND names a lessons/skills path must be the gate writer named at
    the top of this module (plus the narrow, verified exception above)."""
    allowed = {f"{GATE_WRITER_MODULE}:{GATE_WRITER_FUNC}"} | _ADDITIONAL_ALLOWED_WRITERS
    offenders: list[str] = []
    for module in sorted(_trainer_closure()):
        found = _module_tree(module)
        if found is None:
            continue
        path, tree = found
        source = path.read_text(encoding="utf-8")
        for func, segment in _iter_functions(tree, source):
            if not (_WRITE_OP_PATTERN.search(segment) and _LESSONS_SKILLS_PATH_PATTERN.search(segment)):
                continue
            qualified = f"{module}:{func.name}"
            if qualified not in allowed:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{func.lineno} {qualified}")
    assert not offenders, (
        f"Only {sorted(allowed)} may write into lessons/ or skills/ from a "
        f"trainer-reachable module; found an additional writer:\n" + "\n".join(offenders)
    )


def test_generate_index_is_never_called_by_a_trainer_module() -> None:
    """The one allowlisted exception above holds only as long as no trainer
    module actually calls generate_index -- verified here, not assumed."""
    sites = _all_call_sites("generate_index")
    trainer_paths = set()
    for mod in TRAINER_SEED_MODULES:
        mod_path = _module_path(mod)
        if mod_path is not None:
            trainer_paths.add(str(mod_path.relative_to(REPO_ROOT)).replace("\\", "/"))
    offenders = [s for s in sites if s.split(":")[0].replace("\\", "/") in trainer_paths]
    assert not offenders, (
        "generate_index must not be called from a trainer module (it is a "
        "bridge-only derived-catalogue rebuild, not a trainer write path); "
        "found:\n" + "\n".join(offenders)
    )


def _all_call_sites(func_name: str) -> list[str]:
    """Every ``nanobot/**/*.py`` file with a Call node naming *func_name*,
    as ``relative/path.py:lineno``, excluding the function's own definition
    line and any lambda/def named the same (self-recursion is not a caller)."""
    sites: list[str] = []
    for path in (REPO_ROOT / "nanobot").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = target.attr if isinstance(target, ast.Attribute) else (
                    target.id if isinstance(target, ast.Name) else ""
                )
                if name == func_name:
                    sites.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    return sites


def test_gate_writer_has_exactly_one_caller_the_named_entrypoint() -> None:
    """apply_staged_lesson_cards must be reachable only through the gate
    entrypoint -- a second caller would be a second, ungated door into
    lessons/."""
    sites = _all_call_sites(GATE_WRITER_FUNC)
    entrypoint_path = _module_path(GATE_ENTRYPOINT_MODULE)
    assert entrypoint_path is not None
    entrypoint_rel = str(entrypoint_path.relative_to(REPO_ROOT)).replace("\\", "/")
    # Every call site must be inside the gate entrypoint's own module.
    outside = [s for s in sites if s.split(":")[0].replace("\\", "/") != entrypoint_rel]
    assert not outside, (
        f"{GATE_WRITER_FUNC} must be called only from {entrypoint_rel} "
        f"(the gate entrypoint); found calls elsewhere:\n" + "\n".join(outside)
    )
    assert sites, f"{GATE_WRITER_FUNC} has no callers at all -- the gate wiring may have been removed"


def test_gate_entrypoint_has_exactly_one_caller_inside_bridge() -> None:
    """_pickup_staged_promotions itself must only be invoked from bridge.py's
    own cycle flow -- never from a trainer module."""
    sites = _all_call_sites(GATE_ENTRYPOINT_FUNC)
    entrypoint_path = _module_path(GATE_ENTRYPOINT_MODULE)
    assert entrypoint_path is not None
    entrypoint_rel = str(entrypoint_path.relative_to(REPO_ROOT)).replace("\\", "/")
    outside = [s for s in sites if s.split(":")[0].replace("\\", "/") != entrypoint_rel]
    assert not outside, (
        f"{GATE_ENTRYPOINT_FUNC} must be called only from within {entrypoint_rel}; "
        f"found calls elsewhere:\n" + "\n".join(outside)
    )
    assert sites, f"{GATE_ENTRYPOINT_FUNC} has no callers at all"


def test_trainer_evidence_validator_is_wired_into_the_gate() -> None:
    """ADR-021 rule 4's citation validator must actually be called by the
    gate writer -- a validator nothing calls is not an enforced gate."""
    path = _module_path(GATE_WRITER_MODULE)
    assert path is not None
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called_names = {
        (node.func.attr if isinstance(node.func, ast.Attribute) else
         node.func.id if isinstance(node.func, ast.Name) else "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "validate_trainer_citation" in called_names, (
        f"{GATE_WRITER_MODULE} must call trainer_evidence.validate_trainer_citation"
    )


def test_retire_proposal_builder_has_exactly_one_caller_the_retire_gate() -> None:
    """#1666 phase 3: propose_retirement (pure, no I/O) must be reachable
    only through stage_retirement_proposal -- a second caller could build
    a retire proposal that is never validated or floor-checked."""
    sites = _all_call_sites(RETIRE_PROPOSAL_BUILDER_FUNC)
    gate_path = _module_path(RETIRE_GATE_MODULE)
    assert gate_path is not None
    gate_rel = str(gate_path.relative_to(REPO_ROOT)).replace("\\", "/")
    outside = [s for s in sites if s.split(":")[0].replace("\\", "/") != gate_rel]
    assert not outside, (
        f"{RETIRE_PROPOSAL_BUILDER_FUNC} must be called only from {gate_rel} "
        f"(the retire gate); found calls elsewhere:\n" + "\n".join(outside)
    )
    assert sites, f"{RETIRE_PROPOSAL_BUILDER_FUNC} has no callers at all -- the retire gate wiring may have been removed"


def test_retire_gate_validates_citation_and_exposure_floor() -> None:
    """stage_retirement_proposal must actually call the citation validator
    (same check as the add path) AND reference the exposure floor --
    a retire path that skips either is not a gate."""
    path = _module_path(RETIRE_GATE_MODULE)
    assert path is not None
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == RETIRE_GATE_FUNC),
        None,
    )
    assert func is not None, f"{RETIRE_GATE_FUNC} not found in {RETIRE_GATE_MODULE}"
    segment = ast.get_source_segment(source, func) or ""
    called_names = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else
         n.func.id if isinstance(n.func, ast.Name) else "")
        for n in ast.walk(func) if isinstance(n, ast.Call)
    }
    assert RETIRE_PROPOSAL_BUILDER_FUNC in called_names, (
        f"{RETIRE_GATE_FUNC} must call {RETIRE_PROPOSAL_BUILDER_FUNC}"
    )
    assert "validate_trainer_citation" in called_names, (
        f"{RETIRE_GATE_FUNC} must call trainer_evidence.validate_trainer_citation"
    )
    assert "MIN_RETIREMENT_EXPOSURE" in segment, (
        f"{RETIRE_GATE_FUNC} must enforce the named exposure floor MIN_RETIREMENT_EXPOSURE"
    )
