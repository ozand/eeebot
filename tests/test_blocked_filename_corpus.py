from __future__ import annotations

from pathlib import Path

from nanobot.runtime import llm_proposer


def test_live_instance_names_and_secret_corpus():
    instance = Path("/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving")
    live_names = []
    if instance.exists():
        live_names = [p.relative_to(instance).as_posix() for p in instance.rglob("*") if p.is_file()]
    allowed = [p for p in live_names if not any(x in p for x in (".env", ".git/", "package-lock", "yarn.lock", ".npmrc", "id_rsa")) and Path(p).name not in {".gitignore", "token_usage.py", "test_loop_consolidation_tokens.py"}]
    allowed.extend(["scripts/analyze_token_usage.py", "scripts/check_token_budget.py", "scripts/validate_no_secrets.py"])
    blocked = [
        ".env", ".env.local", "api_token.json", "token.txt", "secrets.yaml",
        "my_credentials.json", "id_rsa", ".git/config", "package-lock.json",
        "yarn.lock", ".npmrc", "private_key.pem",
    ]
    assert all(llm_proposer._is_blocked_filename(path) is False for path in allowed)
    assert all(llm_proposer._is_blocked_filename(path) is True for path in blocked)
    base = {"task_title": "x", "rationale": "x", "serves": "priority 1"}
    assert all(llm_proposer.validate_sizing({**base, "target_path": path})[0] is False for path in blocked)
