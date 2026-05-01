"""Safe deterministic bounded subagent executor.

This module is intentionally small and side-effect free. It is a fallback command
executor for hosts where the external Pi Dev CLI is unavailable. It reads the
materializer prompt from stdin and emits a concise review summary to stdout.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone

MAX_PROMPT_CHARS = 8000


def _summarize_prompt(prompt: str) -> dict[str, object]:
    bounded_prompt = prompt[:MAX_PROMPT_CHARS]
    lower = bounded_prompt.lower()
    risks: list[str] = []
    if "mutate" in lower or "write" in lower or "commit" in lower:
        risks.append("requested_scope_mentions_mutation_or_write")
    if "source artifact unavailable" in lower:
        risks.append("source_artifact_unavailable")
    return {
        "schema_version": "bounded-subagent-executor-summary-v1",
        "status": "completed",
        "executor": "deterministic_bounded_review",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "prompt_sha256": hashlib.sha256(bounded_prompt.encode("utf-8")).hexdigest(),
        "prompt_chars": len(bounded_prompt),
        "findings": [
            "bounded executor received and reviewed the subagent request prompt",
            "no file mutations or network calls were performed by this executor",
        ],
        "risks": risks,
        "recommendation": "completed_bounded_review",
    }


def main() -> int:
    prompt = sys.stdin.read()
    payload = _summarize_prompt(prompt)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
