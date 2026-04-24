#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
for path in (SRC, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from nanobot_ops_dashboard.config import load_config
from nanobot_ops_dashboard.reachability import probe_eeepc_reachability


def main() -> None:
    cfg = load_config()
    result = probe_eeepc_reachability(cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
