"""Generalized guard for the undefined-name-at-runtime class (2026-09-01).

PR #1142 split the bridge cycle body into ``_evaluate_candidate`` while
leaving code in ``_main_impl_body`` that referenced names now assigned only
inside the new function's scope (``_cycle_tier``, ``_sp``). Python compiles
such references as LOAD_GLOBAL; module-importing tests stay green, and the
live loop dies with NameError only when the code path runs.

This test disassembles every code object reachable from the bridge module
and asserts every LOAD_GLOBAL target exists in the module namespace or in
builtins. It is red on the pre-fix code and catches the whole defect class,
not just the two known names.
"""

import builtins
import dis
import types

from nanobot.runtime import bridge

# Names legitimately absent at import time: created at runtime before use.
_ALLOWED_RUNTIME_NAMES: set[str] = {
    "__debug__",
    # `del`-then-rebind or conditional plugin-style globals would go here.
}


def _iter_code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _iter_code_objects(const)


def test_every_load_global_in_bridge_resolves() -> None:
    module_names = set(vars(bridge))
    builtin_names = set(vars(builtins))
    offenders: list[str] = []
    seen: set[tuple[str, str]] = set()
    for func in list(vars(bridge).values()):
        code = getattr(func, "__code__", None)
        if code is None or getattr(func, "__module__", None) != bridge.__name__:
            continue
        for co in _iter_code_objects(code):
            for ins in dis.get_instructions(co):
                if ins.opname != "LOAD_GLOBAL":
                    continue
                name = ins.argval
                if (
                    name in module_names
                    or name in builtin_names
                    or name in _ALLOWED_RUNTIME_NAMES
                ):
                    continue
                key = (co.co_name, name)
                if key not in seen:
                    seen.add(key)
                    offenders.append(f"{co.co_name}:{co.co_firstlineno} loads undefined global '{name}'")
    assert not offenders, (
        "bridge.py references names that exist in no scope at runtime "
        "(import-green, runtime-dead class): " + "; ".join(sorted(offenders))
    )
