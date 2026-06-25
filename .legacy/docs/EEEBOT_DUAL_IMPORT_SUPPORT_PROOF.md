# eeebot Dual Import Support Proof

Last updated: 2026-04-21 UTC

Goal:
Add the first hard internal migration slice beyond public identity and compatibility aliases:
- allow `eeebot` package imports alongside `nanobot`
- keep existing `nanobot` package/runtime intact
- avoid unsafe bulk package rename

Implemented:
- added `eeebot/` compatibility package
- added `eeebot/__main__.py` for `python -m eeebot`
- added explicit compatibility subpackages:
  - `eeebot.cli`
  - `eeebot.cli.commands`
  - `eeebot.config`
  - `eeebot.config.paths`
- updated packaging config so wheels/sdists include `eeebot/`

Verified:
- `import eeebot`
- `import eeebot.cli.commands`
- `import eeebot.config.paths`
- `python -m eeebot --help`

Important scope note:
This is a bounded dual-import slice, not a full internal rename. The canonical runtime package remains `nanobot` for compatibility. The `eeebot` package currently provides safe aliases for the highest-value user-facing import surfaces first.
