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
    r'|connect(?:ion)? timed? ?out.{0,20}(?:\berror\b|\bfailed\b)'
    r'|\berror code:\s*(?:429|500|502|503|504)\b'
    r'|\b(?:429|500|502|503|504)\b.{0,30}\b(?:error|service unavailable|bad gateway)\b'
    r'|http\s+(?:429|500|502|503|504)\b'
    r'|status code\s+(?:429|500|502|503|504)\b.{0,30}\bupstream\b'
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


def classify_llm_error(error_text: str, *, model_call_failure: dict | None = None) -> str:
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
    supplier_error_types = {
        'ratelimiterror', 'internalservererror', 'serviceunavailableerror',
        'apiconnectionerror', 'timeout', 'timeouterror', 'connecttimeout',
        'readtimeout', 'remoteprotocolerror',
    }
    if SUPPLIER_UNAVAILABLE_RX.search(error_text):
        if not model_call_failure:
            return 'paused-supplier'
    if isinstance(model_call_failure, dict) and model_call_failure.get('stage') in {'model_call', 'response_handling'} and (
        model_call_failure.get('stage') == 'model_call' or model_call_failure.get('call_stage') == 'model_call'
    ):
        error_type = str(model_call_failure.get('error_type') or '').rsplit('.', 1)[-1].lower()
        call_stage = str(model_call_failure.get('call_stage') or model_call_failure.get('stage') or '')
        message = str(model_call_failure.get('message') or error_text).lower()
        if call_stage == 'model_call' and error_type in supplier_error_types:
            return 'paused-supplier'
        if call_stage == 'model_call' and error_type == 'notfounderror' and 'model route' in message:
            return 'paused-supplier'
        definitive_request_errors = {
            'badrequesterror', 'contextwindowexceedederror',
            'invalidrequesterror', 'unprocessableentityerror',
            'authenticationerror', 'permissiondeniederror',
        }
        request_rejection_markers = (
            'invalid parameter', 'invalid temperature', 'unsupported parameter',
            'maximum context length', 'context length exceeded',
            'invalid tool call arguments', 'malformed tool-call',
            'invalid api key', 'incorrect api key', 'unauthorized',
            'authentication failed', 'permission denied', 'deployment not found',
            'model not found', 'invalid model',
        )
        has_client_http_status = bool(re.search(r"(?:api error|http(?: status)?|status code|error code)\s*[:=]?\s*(?:400|401|403|404|409|413|422)\b", message))
        if error_type in definitive_request_errors or has_client_http_status or any(marker in message for marker in request_rejection_markers):
            return 'failed'
        return 'model_call_incomplete'
    return 'failed'
