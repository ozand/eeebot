"""Ontology block loading, shared by the executor prompt and the role prompts.

#1725 (ADR-022) put block loading in :class:`nanobot.agent.context.ContextBuilder`,
where it was the only part the role-prompt builder (#1729) needed. Importing the
whole builder for it pulled the memory store, the skills loader and the tool
registry into every trainer module's import closure -- which the trainer
mutation guard reads as "the reflector can now write into lessons/" (see
tests/test_trainer_no_direct_mutation.py). The two primitives live here instead:
no state, no I/O beyond the one file each call is given.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def trim_lines(text: str, max_chars: int) -> str:
    """Keep only complete lines that fit, never slicing a line or token."""
    if len(text) <= max_chars:
        return text
    if max_chars <= 0:
        return ""
    kept: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        if used + len(line) > max_chars:
            break
        kept.append(line)
        used += len(line)
    return "".join(kept)


def load_block(name: str, path: Path, cap: int, required: bool) -> tuple[str, dict[str, Any]]:
    """#1725 (ADR-022): load one ontology block, never raising.

    Returns ``(text, meta)``:

    * File absent, ``required``: ``text`` is the marker
      ``"[missing: <name>]"``; ``meta["missing"] is True`` -- the ledger
      flag this absence is seen through, never a silent empty section.
    * File absent, not required: ``text`` is ``""``; ``meta["missing"]
      is False`` (nothing was owed).
    * File present, under cap: the full ``## <name>`` heading plus
      content; ``meta["truncated"] is False``.
    * File present, over cap: cut at a line boundary with a built-in
      notice ("<name> truncated at N chars; read the file for the
      rest"); ``meta["truncated"] is True``. The heading, kept body, and
      notice together never exceed ``cap``.
    """
    heading = f"## {name}\n\n"
    if not path.is_file():
        if required:
            return f"[missing: {name}]", {"missing": True, "truncated": False, "chars": len(f"[missing: {name}]")}
        return "", {"missing": False, "truncated": False, "chars": 0}
    raw = path.read_text(encoding="utf-8")
    text = heading + raw
    if len(text) <= cap:
        return text, {"missing": False, "truncated": False, "chars": len(text)}
    notice = f"\n\n[{name} truncated at {cap} chars; read the file for the rest]"
    budget = max(0, cap - len(heading) - len(notice))
    trimmed_body = trim_lines(raw, budget)
    text = heading + trimmed_body + notice
    return text, {"missing": False, "truncated": True, "chars": len(text)}
