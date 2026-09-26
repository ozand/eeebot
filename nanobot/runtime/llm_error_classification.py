"""#1765: classify a raw LLM-call error string as a supplier-side outage
(``'paused-supplier'``) or our own defect (``'failed'``).

A standalone leaf module (no other ``nanobot.runtime`` imports) so both
:mod:`nanobot.runtime.bridge` (the executor/planner error paths) and
:mod:`nanobot.runtime.open_increment` (D2 round 2, external re-check item
2: a stale ``running`` registration with a telemetry-confirmed ``error``
must be classified immediately, not left for a classifier that may never
run again if the process that would have run it already died) can import
it directly. ``open_increment.py`` must never import ``bridge.py`` --
that would make bridge's entire git-mutating call graph transitively
"reachable" from every trainer module that merely reads an open increment
(``tests/test_trainer_no_direct_mutation.py``'s reachability check).
"""
from __future__ import annotations

import re

# #1765: patterns that mean "the supplier could not serve us" — connection
# refused/reset, a timeout with no response, HTTP 429/500/502/503/504, "no
# deployments available", or a route missing for a configured model. Matched
# case-insensitively against the raw LLM-call error text. Deliberately an
# ALLOWLIST, not a denylist: only these positive signals promote a cycle to
# 'paused-supplier'; everything else (context length exceeded, malformed
# tool-call payload, invalid model parameters, our own schema errors) stays
# 'failed' by falling through to the default — the conservative direction
# the issue asks for, and the only way this stays a real distinction instead
# of a catch-all.
SUPPLIER_UNAVAILABLE_RX = re.compile(
    r'connection (?:refused|reset|error)'
    r'|connect(?:ion)? timed? ?out'
    r'|\btimed? ?out\b'
    r'|\btimeout\b'
    r'|\bread timeout\b'
    r'|\berror code:\s*(?:429|500|502|503|504)\b'
    r'|\b(?:429|500|502|503|504)\b.{0,20}\berror\b'
    r'|ratelimiterror'
    r'|internalservererror'
    r'|serviceunavailableerror'
    r'|apiconnectionerror'
    r'|notfounderror'
    r'|no deployments available'
    r'|no healthy deployment'
    r'|try again in \d'
    # #1765 review: a genuine supplier-side unavailability observed in the
    # wild carries HTTP 400 (litellm.BadRequestError) — the local gateway's
    # own model daemon has no model loaded yet, not a malformed request of
    # ours. Classified on the MESSAGE, never on the status code alone (a
    # bare "400" stays unmatched — see the client-defect test fixtures) —
    # this is exactly why the allowlist below matches phrasing, not codes.
    r'|model.{0,20}not loaded'
    r'|/inference/load\b'
    r'|load first\b',
    re.IGNORECASE,
)


def classify_llm_error(error_text: str) -> str:
    """#1765: 'paused-supplier' (the gateway/model provider could not serve
    us) or 'failed' (the supplier rejected OUR request — our own defect,
    must keep failing loudly), from the raw executor LLM-call error text.

    Positive-match only (see :data:`SUPPLIER_UNAVAILABLE_RX`) — when the
    text does not clearly say "supplier unavailable", this returns 'failed',
    the conservative default the issue specifies. The raw text is recorded
    alongside this decision on the ledger row (see
    ``cycle_ledger.record_cycle_outcome``'s ``llm_error_classification``)
    so a misclassification is auditable after the fact, never silently lost.
    """
    if not error_text:
        return 'failed'
    if SUPPLIER_UNAVAILABLE_RX.search(error_text):
        return 'paused-supplier'
    return 'failed'
