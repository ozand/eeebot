"""#1335: defer enhancement-shaped proposals aimed at scripts nothing runs.

Measured on #1208: 24 integrated cycles (2.9% of every integration since
2026-07-14) and 29 more attempts added ``--json``, ``--dry-run``, CLI options
and path filters to instance scripts that no runtime, unit or harness ever
executes. Each flag was exercised by exactly one thing — the script's own
self-test — which is what made the work look complete. The shape comes from
every demand lane, so per #1108 the gate sits downstream of all of them: in
the proposer, right where a futile-surface target is refused (#1184).

Two halves, only one of them gated here:

* a **harness candidate** (``scripts/(check|validate|audit|analyze|verify)_*.py``)
  has a consumer — the validator harness runs it with ``--json`` and reads the
  document (#1317) — so an enhancement there is never deferred;
* a script with **no runtime caller** is deferred with reason
  ``enhancement_without_caller``. "Caller" here means a literal
  ``scripts/<name>.py`` in a scanned tree: the product runtime (``nanobot/``),
  the host side (``host/eeepc/``: systemd units, deploy scripts, instance env),
  the instance repo outside its own tests, docs, lessons, memory and mirrored
  ``nanobot/``, and the instruction files the executor follows (``AGENTS.md``,
  ``skills/*/SKILL.md``). A reference by bare basename, through a variable, or
  from a file outside those trees (an operator crontab, say) is invisible to
  this index; the host was checked for those by hand when the gate shipped.

Conservative by construction, because misfiling a live script as dead costs
more than missing a dead one:

* the title must both *add* something (``Add``/``Extend``/``Support``/…) and
  name the shape (``--flag``, JSON output, dry-run, CLI option, path filter);
  a ``Fix …`` title never matches;
* *any* literal reference in a scanned file counts — docstring mentions
  included. That shields a few genuinely dead scripts; it never exposes a
  live one that the scan has read;
* an index that is not complete — zero files, a product dir missing, the file
  cap reached — is ``unavailable`` and never defers (trap: absence of data is
  not a zero);
* a target that does not exist in the workspace is not this gate's business
  (creating a new script is a different shape).

Never a hard block: the caller gives the model one retry with the feedback
text, and the deferral is a ``proposer_reject`` ledger row with its own reason
(counted by ``scripts/loop_metrics_report.py``'s ``by_reason`` breakdown), so a
gate that never fires is distinguishable from one that was never loaded. Like
``self_dedup``, the reason counts toward the demand item's exhaustion (#760),
so an item whose proposals keep taking this shape stops being presented after
two refusals in a day instead of burning a retry every cycle.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

REASON = "enhancement_without_caller"

# Same class as validator_harness._ALLOWLIST_RE — duplicated on purpose (this
# module must not import the harness); tests pin the two patterns equal.
_HARNESS_CANDIDATE_RE = re.compile(r"^(check|validate|audit|analyze|verify)_.*\.py$")
_TARGET_RE = re.compile(r"^scripts/([A-Za-z0-9_.-]{1,120}\.py)$")

# Title classifier (the #1208 measurement's shape, kept conservative): an
# additive verb up front AND a flag/JSON-output/dry-run/CLI-option/path-filter
# token. A fix-shaped title ("Fix …", "Repair …") fails the additive anchor.
_ADDITIVE_TITLE_RE = re.compile(
    r"^\W*(add|adds|adding|extend|extends|support|expose|introduce|implement|"
    r"enable|provide|include|integrate|allow|offer)\b",
    re.IGNORECASE,
)
_SHAPE_RE = re.compile(
    r"--[a-z][a-z0-9-]*"
    r"|\bjson\s+(?:output|report|reporting|format|formatting|mode|summary|summaries|results?)\b"
    r"|\b(?:structured|machine[- ]readable)\s+"
    r"(?:json|output|results?|report|reporting|summary|summaries|scan|logging)\b"
    r"|\bdry[- ]?run\b"
    r"|\bcli\s+(?:option|options|flag|flags|argument|arguments|arg|args|interface|switch|switches)\b"
    r"|\bcommand[- ]line\s+(?:option|options|flag|flags|argument|arguments|interface)\b"
    r"|\b(?:path|target|file|directory|dir)\s+filter(?:ing|s)?\b"
    r"|\bfilter(?:ing)?\s+(?:option|options|flag|flags|argument|arguments)\b"
    r"|\b(?:configurable|customizable|customisable)\s+(?:\w+\s+){0,2}"
    r"(?:option|options|flag|flags|argument|arguments|retention|threshold|thresholds|days)\b",
    re.IGNORECASE,
)

# Caller index bounds. The bridge is a oneshot process, so the index is built
# at most once per cycle; the caps keep a pathological tree from stalling it,
# and reaching one makes the index unavailable rather than silently partial.
_PRODUCT_DIRS = ("nanobot", "host/eeepc")  # runtime + units, deploy scripts, instance env
# Top-level instance dirs that are prose about scripts or the scripts' own
# tests, not callers; ``nanobot`` is the instance's stale mirror of the runtime.
_INSTANCE_SKIP_DIRS = frozenset(
    {"tests", "lessons", "memory", "docs", "images", "nanobot",
     ".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}
)
_SCAN_SUFFIXES = frozenset(
    {".py", ".sh", ".bash", ".service", ".timer", ".yml", ".yaml", ".toml",
     ".cfg", ".ini", ".conf", ".txt", ".json", ".env", ".mk", ""}
)
# Instruction files the executor follows: a script AGENTS.md or a skill tells
# the agent to run IS run, even though no code names it. Other Markdown (docs,
# lessons, history) is prose about scripts, not a caller, and stays out.
_INSTRUCTION_BASENAMES = frozenset({"AGENTS.md", "SKILL.md"})
_MAX_FILE_BYTES = 512_000
_MAX_FILES = 4000
_MAX_CALLERS_KEPT = 5
_REF_RE = re.compile(r"scripts/([A-Za-z0-9_.-]{1,120}\.py)")


def is_enhancement_shaped(title: str) -> bool:
    """True when ``title`` adds a flag / JSON output / dry-run / CLI option /
    path filter to something. A fix-shaped title never matches."""
    text = " ".join(str(title or "").split())
    return bool(text and _ADDITIVE_TITLE_RE.match(text) and _SHAPE_RE.search(text))


@dataclass
class CallerIndex:
    """Which ``scripts/<name>.py`` literals appear where, over the scanned roots."""

    roots: tuple[str, ...] = ()
    files_scanned: int = 0
    files_skipped_large: int = 0
    truncated: bool = False
    missing: tuple[str, ...] = ()
    references: dict[str, list[str]] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.files_scanned == 0 or self.truncated or self.missing:
            return "unavailable"
        return "ok"

    def callers_of(self, basename: str) -> list[str]:
        return list(self.references.get(basename, ()))

    def describe(self) -> str:
        text = f"caller index {self.status}: {self.files_scanned} files under {', '.join(self.roots) or '-'}"
        if self.missing:
            text += f"; missing {', '.join(self.missing)}"
        if self.truncated:
            text += f"; truncated at {_MAX_FILES} files"
        if self.files_skipped_large:
            text += f"; {self.files_skipped_large} skipped >{_MAX_FILE_BYTES}B"
        return text


def product_root() -> Path:
    """Root of the running product checkout/release (``nanobot/`` lives in it)."""
    return Path(__file__).resolve().parents[2]


def _iter_files(root: Path, top_level_skip: frozenset[str]) -> tuple[list[Path], bool]:
    """Files under ``root`` worth scanning, and whether the cap cut the walk
    short. ``top_level_skip`` applies to ``root``'s direct children only — a
    nested ``ops/scripts/`` or ``automation/docs/`` is still read."""
    out: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name == "__pycache__" or (current == root and entry.name in top_level_skip):
                        continue
                    stack.append(entry)
                elif entry.is_file() and (
                    entry.suffix in _SCAN_SUFFIXES or entry.name in _INSTRUCTION_BASENAMES
                ):
                    if len(out) >= _MAX_FILES:
                        return out, True
                    out.append(entry)
            except OSError:
                continue
    return out, False


def _scan(index: CallerIndex, label: str, files: list[Path], base: Path) -> None:
    for path in files:
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                index.files_skipped_large += 1
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        index.files_scanned += 1
        try:
            rel = f"{label}:{path.relative_to(base).as_posix()}"
        except ValueError:
            rel = f"{label}:{path.name}"
        for name in set(_REF_RE.findall(text.replace("\\", "/"))):
            if name == path.name:
                continue  # a script's own usage line is not a caller
            bucket = index.references.setdefault(name, [])
            if len(bucket) < _MAX_CALLERS_KEPT and rel not in bucket:
                bucket.append(rel)


_index_cache: dict[tuple[str, str], CallerIndex] = {}


def build_caller_index(root: Path | None, selfevo_repo: Path | None) -> CallerIndex:
    """Scan the product runtime + host side and the instance repo (outside its
    tests/docs/lessons/memory) for ``scripts/<name>.py`` references. Cached per
    process — one build per bridge invocation; a failed build caches as
    unavailable rather than re-walking the tree on every proposal."""
    key = (
        str(Path(root).resolve()) if root is not None else "",
        str(Path(selfevo_repo).resolve()) if selfevo_repo is not None else "",
    )
    cached = _index_cache.get(key)
    if cached is not None:
        return cached
    index = CallerIndex()
    try:
        roots: list[str] = []
        missing: list[str] = []
        if root is not None:
            for sub in _PRODUCT_DIRS:
                directory = Path(root) / sub
                if not directory.is_dir():
                    missing.append(sub + "/")
                    continue
                roots.append(sub + "/")
                files, truncated = _iter_files(directory, frozenset())
                index.truncated = index.truncated or truncated
                _scan(index, sub, files, Path(root))
        if selfevo_repo is not None and Path(selfevo_repo).is_dir():
            roots.append("instance/")
            files, truncated = _iter_files(Path(selfevo_repo), _INSTANCE_SKIP_DIRS)
            index.truncated = index.truncated or truncated
            _scan(index, "instance", files, Path(selfevo_repo))
        index.roots = tuple(roots)
        index.missing = tuple(missing)
    except Exception as exc:  # unavailable, never partial-and-ok
        _LOG.warning("%s: caller index build failed: %s: %s", REASON, type(exc).__name__, exc)
        index = CallerIndex(roots=index.roots, files_scanned=0)
    _index_cache[key] = index
    return index


def enhancement_without_caller(
    proposal: dict[str, Any] | None,
    selfevo_repo: Path | None,
    *,
    root: Path | None = None,
) -> tuple[str, str] | None:
    """Return ``(feedback_text, matched_against)`` when the proposal is an
    enhancement to a script nothing runs, else ``None``.

    ``feedback_text`` is the retry steer for the model; ``matched_against`` is
    ``enhancement_without_caller:<target>`` so the caller can record the ledger
    reason. Fail-open: any error means "not deferred".
    """
    try:
        if not isinstance(proposal, dict):
            return None
        title = str(proposal.get("task_title") or "").strip()
        target = str(proposal.get("target_path") or "").replace("\\", "/").strip()
        match = _TARGET_RE.match(target)
        if not match or not is_enhancement_shaped(title):
            return None
        basename = match.group(1)
        if _HARNESS_CANDIDATE_RE.match(basename):
            return None
        if selfevo_repo is None or not (Path(selfevo_repo) / target).is_file():
            return None
        index = build_caller_index(root if root is not None else product_root(), selfevo_repo)
        if index.status != "ok":
            _LOG.warning("%s: %s; not deferring '%s'", REASON, index.describe(), title)
            return None
        callers = index.callers_of(basename)
        if callers:
            _LOG.info("%s: '%s' has callers %s; not deferring", REASON, target, callers)
            return None
        # The index description leads so it survives the ledger row's 200-char
        # ``detail`` cap: a row must prove what was scanned, not only that
        # something was refused.
        feedback = (
            f"{index.describe()}; no reference to '{target}': your proposal adds a "
            "flag/JSON output/dry-run/CLI option to a script nothing runs (no runtime "
            "reference, no systemd unit, not a validator-harness candidate) — only its own "
            "self-test would ever exercise the new option. Propose a defect fix, or work on a "
            "script something executes"
        )
        return feedback, f"{REASON}:{target}"
    except Exception as exc:  # fail-open, like every other proposer heuristic
        _LOG.warning("%s: gate error %s: %s; not deferring", REASON, type(exc).__name__, exc)
        return None
