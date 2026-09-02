"""Documented deterministic fixture redaction helper; no live paths are read."""
from pathlib import Path

REDACTED_FIELDS = ("task_title", "expected_outcome_claim")

def redact(row: dict) -> dict:
    return {key: ("REDACTED" if key in REDACTED_FIELDS else value) for key, value in row.items()}

if __name__ == "__main__":
    raise SystemExit("Use only with an explicitly supplied, disposable input/output; no default live path.")
