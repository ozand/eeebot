from nanobot.utils import helpers


def test_estimate_prompt_tokens_falls_back_without_tiktoken(monkeypatch):
    monkeypatch.setattr(helpers, "_get_tiktoken_encoding", lambda: None)
    tokens = helpers.estimate_prompt_tokens([
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "response"},
    ])
    assert tokens >= 1


def test_estimate_message_tokens_falls_back_without_tiktoken(monkeypatch):
    monkeypatch.setattr(helpers, "_get_tiktoken_encoding", lambda: None)
    tokens = helpers.estimate_message_tokens({"role": "user", "content": "hello world"})
    assert tokens >= 1
