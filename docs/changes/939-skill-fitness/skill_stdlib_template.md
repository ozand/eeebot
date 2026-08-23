# SKILL Template: stdlib / no-uv / PEP 723 scripts

## Purpose

This document provides the canonical template and rules for SKILL.md files
that bundle **standalone Python scripts** using only the standard library.

## Rules for bundled scripts in `skills/<name>/scripts/`

### 1. No external build tooling (no uv, no pip, no venv)

Scripts bundled in a skill MUST NOT require `uv`, `pip install`, or any virtual
environment setup step.  The agent runs on a constrained host where uv may not
be available.  Use **only** the Python standard library.

### 2. PEP 723 inline metadata (informational)

If the script is intended as a standalone runnable, use
[PEP 723](https://peps.python.org/pep-0723/) `# /// script` inline metadata
at the top (comment block only — NOT executed by the default `python` invocation):

```python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
```

Kept empty — no dependencies.  This makes the intent explicit without implying
any dependency manager is required.

### 3. Noninteractive execution

Scripts MUST NOT prompt for user input (`input()`, `getpass`, interactive
`argparse` without defaults).  All parameters must have defaults or be
supplied via environment variables or command-line arguments with sensible
defaults.

### 4. Bounded output

Scripts MUST write results to **stdout** (for success) and **stderr** (for
diagnostics/errors).  They MUST NOT write to files unless explicitly instructed
via a command-line argument.  Output MUST be bounded (no infinite loops, no
unbounded accumulation in memory).

### 5. Exit codes

- `0` — success
- `1` — user/input error
- `2` — runtime/environment error

Never exit with `sys.exit()` inside library functions — only in `if __name__ ==
"__main__":` guards.

### 6. Size limit

Bundled scripts SHOULD be ≤300 lines.  Split into multiple files or use
`references/` for large data if needed.

---

## Minimal SKILL.md template

```markdown
---
name: my-skill
description: <what it does and when to use it — include triggering context>
---

# My Skill

## Quick start

\`\`\`bash
python skills/my-skill/scripts/main.py
\`\`\`

## Script: `scripts/main.py`

Does X by Y.  Pass `--flag` for Z.

## Output

Writes results to stdout as JSON lines.  Errors to stderr.
```

---

## Minimal `scripts/main.py` template

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""<One-line description>.

Usage:
    python main.py [--output-format json|text]

Writes results to stdout.  Errors to stderr.  Exit 0=ok, 1=user error, 2=runtime error.
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    try:
        result = {"status": "ok", "data": []}  # replace with real logic
        if args.output_format == "json":
            print(json.dumps(result))
        else:
            print(result["status"])
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
```
