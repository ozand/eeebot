"""Role prompts as files (#1729, ADR-022 rule 2: code assembles, code does not author).

Every non-executor caller of the LLM gateway used to carry its system prompt as
a Python string literal. This module replaces those literals with files under
``<release root>/roles/<role>.md`` and assembles each caller's system prompt as

    IDENTITY.md (short form) -> SOUL.md (optional) -> goals.md (optional) -> roles/<role>.md

with the same ``[missing: <name>]`` marker and per-block telemetry semantics as
the executor loader (:func:`nanobot.agent.block_loader.load_block`).
There is one convention for a block that is absent or over its cap, not two.

Per-role flags (issue #1729, revision 2026-09-18):

* proposer, demand-proposer, goal-review: identity (short) + soul + charter;
* curator, narrator: identity (short) + soul;
* strategist, reflector, skill-eval: identity (short) only — their input caps
  are tight, so growth is bounded by the first paragraph of ``IDENTITY.md``.

The narrator keeps ADR-016 rule 1 by construction: this module reads only the
release-root files named above; it has no reader for any channel figure.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from nanobot.agent.block_loader import load_block, trim_lines
from nanobot.observability.llm_telemetry import system_chars as _system_chars

#: Default deployed release tree (mirrors ``bridge.RELEASE_ROOT`` /
#: ``llm_proposer._RELEASE_ROOT_DEFAULT``); the systemd units for the side
#: roles do not all export ``RELEASE_ROOT``, so the package's own tree and
#: this path are the fallbacks.
RELEASE_ROOT_DEFAULT = "/opt/eeepc-agent/runtimes/self-evolving-agent/current"
ROLES_DIRNAME = "roles"
#: The short identity form is the first paragraph of ``IDENTITY.md``
#: (issue #1729 revision): at most this many chars.
IDENTITY_SHORT_CAP = 300
IDENTITY_FULL_CAP = 1500
SOUL_CAP = 1800
CHARTER_CAP = 3200
#: A role file with no ``budget_chars`` front-matter key gets this cap.
DEFAULT_ROLE_BUDGET = 2600
SECTION_SEPARATOR = "\n\n---\n\n"

#: role -> (with_charter, with_soul). The single place the per-role expectation lives.
ROLE_FLAGS: dict[str, tuple[bool, bool]] = {
    "proposer": (True, True),
    "demand-proposer": (True, True),
    "goal-review": (True, True),
    "curator": (False, True),
    "narrator": (False, True),
    "strategist": (False, False),
    "reflector": (False, False),
    "skill-eval": (False, False),
}
ROLE_NAMES: tuple[str, ...] = tuple(ROLE_FLAGS)

_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_ROLE_HEADING_RE = re.compile(r"^# Role: [^\n]*\n+", re.M)


def resolve_release_root(explicit: "str | Path | None" = None) -> Path | None:
    """The release tree the role and identity files are read from.

    An explicit argument wins outright: the caller named the tree.

    Otherwise the candidates are, in order, ``RELEASE_ROOT``, the tree this
    package is installed in (the release tarball *is* the repository, so the
    package's grandparent carries ``roles/`` on the host and in a dev checkout
    alike), and the deployed default path. The first candidate that actually
    carries a ``roles/`` directory is the release tree; a path that does not is
    not one, and reading roles out of it would turn a stale or narrowly-scoped
    ``RELEASE_ROOT`` into eight ``[missing: ...]`` markers that look like a
    broken deploy. When no candidate carries ``roles/``, the first that exists
    is returned anyway, so ``IDENTITY.md``/``SOUL.md``/``goals.md`` still load
    from it and only the genuinely absent role file renders its marker.

    ``None`` when nothing resolves — every block then renders its marker,
    never raises.
    """
    if explicit:
        return Path(explicit)
    candidates: list[Path] = []
    configured = os.environ.get("RELEASE_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path(__file__).resolve().parents[2])
    candidates.append(Path(RELEASE_ROOT_DEFAULT))
    for candidate in candidates:
        if (candidate / ROLES_DIRNAME).is_dir():
            return candidate
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """Split ``---`` front matter from a role file. Values are kept as strings;
    the body is what reaches the prompt."""
    match = _FRONT_MATTER_RE.match(raw)
    if not match:
        return {}, raw
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, raw[match.end():]


def role_budget(meta: dict[str, str]) -> int:
    try:
        value = int(meta.get("budget_chars", "") or 0)
    except ValueError:
        value = 0
    return value if value > 0 else DEFAULT_ROLE_BUDGET


def _substitute(text: str, substitutions: dict[str, str] | None) -> str:
    """Fill the named placeholders a role file declares (e.g. ``{commit_surfaces}``).
    Only known keys are replaced; any other brace text is left as written."""
    for key, value in (substitutions or {}).items():
        text = text.replace("{" + key + "}", value)
    return text


def default_substitutions() -> dict[str, str]:
    """The values role files may reference. Today: the authoritative commit
    surfaces, so the proposer prompts keep agreeing with ``mutation_policy``."""
    from nanobot.runtime.mutation_policy import MUTATION_POLICY

    return {"commit_surfaces": MUTATION_POLICY.render_commit_surfaces()}


def load_role_text(
    role: str,
    *,
    release_root: "str | Path | None" = None,
    substitutions: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The body of ``roles/<role>.md`` with front matter and the ``# Role:``
    heading stripped and placeholders filled: the text the literal used to be.

    Returns ``(text, meta)`` with the loader's marker semantics: an absent
    file yields ``"[missing: roles/<role>.md]"`` and ``meta["missing"]``;
    a body over its ``budget_chars`` is cut at a line boundary with the
    loader's own truncation notice and ``meta["truncated"]``.
    """
    name = f"{ROLES_DIRNAME}/{role}.md"
    root = resolve_release_root(release_root)
    path = Path(root) / ROLES_DIRNAME / f"{role}.md" if root is not None else None
    if path is None or not path.is_file():
        marker = f"[missing: {name}]"
        return marker, {"missing": True, "truncated": False, "chars": len(marker), "budget": None}
    raw = path.read_text(encoding="utf-8")
    front, body = _parse_front_matter(raw)
    body = _ROLE_HEADING_RE.sub("", body, count=1).strip()
    body = _substitute(body, substitutions if substitutions is not None else default_substitutions())
    budget = role_budget(front)
    if len(body) <= budget:
        return body, {"missing": False, "truncated": False, "chars": len(body), "budget": budget}
    notice = f"\n\n[{name} truncated at {budget} chars; read the file for the rest]"
    kept = trim_lines(body, max(0, budget - len(notice)))
    text = kept + notice
    return text, {"missing": False, "truncated": True, "chars": len(text), "budget": budget}


def _identity_block(root: Path | None, *, short: bool) -> tuple[str, dict[str, Any]]:
    """``IDENTITY.md`` as the executor loader renders it, or its first
    paragraph (``short=True``) under :data:`IDENTITY_SHORT_CAP`."""
    if root is None:
        marker = "[missing: IDENTITY.md]"
        return marker, {"missing": True, "truncated": False, "chars": len(marker)}
    path = Path(root) / "IDENTITY.md"
    if not short:
        return load_block("IDENTITY.md", path, IDENTITY_FULL_CAP, True)
    if not path.is_file():
        marker = "[missing: IDENTITY.md]"
        return marker, {"missing": True, "truncated": False, "chars": len(marker)}
    raw = path.read_text(encoding="utf-8")
    paragraph = _first_paragraph(raw)
    heading = "## IDENTITY.md\n\n"
    text = heading + paragraph
    if len(text) <= IDENTITY_SHORT_CAP:
        return text, {"missing": False, "truncated": False, "chars": len(text)}
    notice = f"\n\n[IDENTITY.md truncated at {IDENTITY_SHORT_CAP} chars; read the file for the rest]"
    budget = max(0, IDENTITY_SHORT_CAP - len(heading) - len(notice))
    text = heading + paragraph[:budget].rsplit(" ", 1)[0] + notice
    return text, {"missing": False, "truncated": True, "chars": len(text)}


def _first_paragraph(raw: str) -> str:
    """First paragraph that is not a heading, joined to one line."""
    for chunk in re.split(r"\n\s*\n", raw.strip()):
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines or lines[0].startswith("#"):
            continue
        return " ".join(lines)
    return ""


def build_role_system_prompt(
    role: str,
    *,
    with_charter: bool | None = None,
    with_soul: bool | None = None,
    identity_short: bool = True,
    release_root: "str | Path | None" = None,
    role_text: str | None = None,
    substitutions: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Assemble one side role's system prompt from release files.

    ``with_charter`` / ``with_soul`` default to :data:`ROLE_FLAGS` for the
    role. ``role_text`` overrides reading ``roles/<role>.md`` (a caller that
    already holds the role body — the proposer's ``system_prompt`` argument —
    passes it through so identity and soul are still prepended once).

    Returns ``(text, telemetry)`` where telemetry is
    ``{"role", "blocks": {name: chars}, "missing": [...], "truncated": [...],
    "chars", "with_charter", "with_soul"}``. Never raises: a missing role
    file leaves the caller running on identity (+ soul) plus the marker.
    """
    flags = ROLE_FLAGS.get(role, (False, False))
    charter = flags[0] if with_charter is None else with_charter
    soul = flags[1] if with_soul is None else with_soul
    root = resolve_release_root(release_root)

    blocks: list[tuple[str, str, dict[str, Any]]] = []
    text, meta = _identity_block(root, short=identity_short)
    blocks.append(("identity", text, meta))
    if soul:
        text, meta = _load_release_file(root, "SOUL.md", SOUL_CAP)
        blocks.append(("soul", text, meta))
    if charter:
        text, meta = _load_release_file(root, "goals.md", CHARTER_CAP)
        blocks.append(("goals", text, meta))
    if role_text is not None:
        blocks.append(("role", role_text, {"missing": False, "truncated": False, "chars": len(role_text)}))
    else:
        text, meta = load_role_text(role, release_root=root, substitutions=substitutions)
        blocks.append(("role", text, meta))

    parts = [text for _, text, _ in blocks if text]
    assembled = SECTION_SEPARATOR.join(parts)
    files = {"identity": "IDENTITY.md", "soul": "SOUL.md", "goals": "goals.md", "role": f"{ROLES_DIRNAME}/{role}.md"}
    telemetry = {
        "role": role,
        "blocks": {name: len(text) for name, text, _ in blocks},
        "missing": [files[name] for name, _, meta in blocks if meta.get("missing")],
        "truncated": [files[name] for name, _, meta in blocks if meta.get("truncated")],
        "chars": len(assembled),
        "with_charter": charter,
        "with_soul": soul,
    }
    return assembled, telemetry


def _load_release_file(root: Path | None, filename: str, cap: int) -> tuple[str, dict[str, Any]]:
    if root is None:
        marker = f"[missing: {filename}]"
        return marker, {"missing": True, "truncated": False, "chars": len(marker)}
    return load_block(filename, Path(root) / filename, cap, True)


#: Re-export (#1784). The definition moved to
#: :mod:`nanobot.observability.llm_telemetry`, beside the field it feeds, so
#: ``providers.base`` can measure the executor's own system prompt without a
#: provider importing a runtime module -- the dependency inversion that broke
#: ``test_trainer_no_direct_mutation`` in #1743. This name stays for the four
#: callers that already import it from here.
system_chars = _system_chars
