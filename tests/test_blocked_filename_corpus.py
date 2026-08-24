"""#947 fix-pass: host-independent corpus test for _is_blocked_filename.

Validates the two-tier gate rule (structural hard-blocks + sensitive-word last-
segment rule) against a fixed corpus of ALLOWED and BLOCKED basenames/paths.
No host filesystem reads; runs cleanly in CI.
"""
from __future__ import annotations

from nanobot.runtime import bridge, llm_proposer

# Filenames/paths that must be ALLOWED (gate returns False).
_ALLOWED: list[str] = [
    # Original false-positive from #947 live failure:
    "scripts/analyze_token_usage.py",
    # All variants the issue mentions / operator tooling:
    "scripts/token_report.py",
    "scripts/summarize_token_costs.py",
    "scripts/token_budget_check.py",
    "scripts/check_token_budget.py",
    "scripts/validate_no_secrets.py",
    # Named-exception constant:
    "scripts/count_tokens.py",
    # Innocent names with 'token'/'secret' in non-last position:
    "surfaces/token_usage_report.md",
    "scripts/cycle_logger.py",
    "memory/MEMORY.md",
    "docs/spec.md",
    # Paths that look structural but are not blocked patterns:
    "scripts/git_helper.py",       # 'git' not as a .git/ path component
    "scripts/environment_check.py",  # 'env' substring but not .env file
]

# Filenames/paths that must be BLOCKED (gate returns True).
_BLOCKED: list[str] = [
    # Sensitive-word last-segment (singular):
    "token.txt",
    "api_token.json",
    "my_token.yaml",
    # Sensitive-word last-segment (plural, singularized):
    "tokens.json",
    "api_tokens.json",
    "my_tokens.yaml",
    # secret / credential forms:
    "secrets.yaml",
    "my_credentials.json",
    "credentials.json",
    "secret.txt",
    # Structural hard-blocks:
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    "package-lock.json",
    "yarn.lock",
    ".git/config",
    "some/path/.git/objects/abc",
    "id_rsa",
    "id_rsa_backup",
    # private_key in stem:
    "private_key.pem",
    "my_private_key.pem",
    "private_key_backup.pem",
]


def _check_module(mod) -> None:
    for path in _ALLOWED:
        result = mod._is_blocked_filename(path)
        assert result is False, (
            f"{mod.__name__}._is_blocked_filename({path!r}) returned True "
            f"(false positive — should be ALLOWED)"
        )
    for path in _BLOCKED:
        result = mod._is_blocked_filename(path)
        assert result is True, (
            f"{mod.__name__}._is_blocked_filename({path!r}) returned False "
            f"(false negative — should be BLOCKED)"
        )


def test_bridge_corpus() -> None:
    """bridge._is_blocked_filename passes fixed corpus."""
    _check_module(bridge)


def test_proposer_corpus() -> None:
    """llm_proposer._is_blocked_filename passes fixed corpus (mirror gate)."""
    _check_module(llm_proposer)


def test_proposer_validate_sizing_rejects_blocked() -> None:
    """validate_sizing rejects all blocked paths before acceptance."""
    base = {"task_title": "x", "rationale": "x", "serves": "priority 1"}
    for path in _BLOCKED:
        ok, reason = llm_proposer.validate_sizing({**base, "target_path": path})
        assert ok is False, (
            f"validate_sizing accepted blocked path {path!r} (reason={reason!r})"
        )


def test_proposer_validate_sizing_allows_innocent() -> None:
    """validate_sizing accepts (or at least does not block on filename) innocent paths."""
    base = {"task_title": "x", "rationale": "x", "serves": "priority 1"}
    for path in _ALLOWED:
        ok, reason = llm_proposer.validate_sizing({**base, "target_path": path})
        # The only reason for rejection should NOT be a blocked-filename rejection.
        assert "blocked filename" not in (reason or ""), (
            f"validate_sizing gave blocked-filename rejection for innocent "
            f"path {path!r}: {reason!r}"
        )
