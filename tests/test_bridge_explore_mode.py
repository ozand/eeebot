import json
import pytest
from pathlib import Path
from nanobot.runtime.bridge import _parse_explore_mode

def test_parse_explore_mode():
    req = {"task": "fix something"}
    assert _parse_explore_mode(req) == (1, "")
    
    req = {"task": "explore: 3\nfix this"}
    assert _parse_explore_mode(req) == (3, "")

    req = {"task": "explore: 2\nmeasurement: causal_gap"}
    assert _parse_explore_mode(req) == (2, "causal_gap")
