# eeebot operator profile

This file holds the operator's standing constraints on how the agent behaves.
It does not hold work. Work-item priorities stay in
`state/goals/goal_text.json` and are owned by the priority queue; a directive
here says how to act, a priority there says what to do next. Do not merge the
two.

## Who

The operator is one person who owns the host and reads in English. They look
at the gh-pages dashboard and at GitHub issues and pull requests, not at the
host's own web page (#1482). The harness in `ozand/eeebot` is built by a fleet
of developer agents the operator directs and reviews; the loop itself commits
only inside its own workspace. When the operator and this file disagree, the
operator's latest written word wins and this file is corrected to match it.

## Directives

One sentence each, imperative, dated, with the record that carries the
rationale. When a preference changes, rewrite the active directive in place
and move the old one to Superseded; never append a contradictory directive.

- Never read channel comments, and never request comment, community or subscription scopes. (2026-09-15; ADR-015 rule 3, #1612)
- Always attach a before/after measurement to an optimisation claim. (2026-09-15; goals.md Vector 1, ADR-016 rule 3)
- Never touch `state/`, `systemd/`, `ops/` or any secret; those are the operator's to change. (2026-09-18; goals.md, #1720)
- Prefer a short honest artifact over a padded or a skipped one. (2026-09-15; ADR-016 rule 3, ADR-017 rule 4)
- Prefer visible degradation over silent degradation. (2026-09-14; ADR-014 rule 3)
- Never treat a display as proof of the mechanism behind it. (2026-09-14; ADR-014 rule 2, #1197)
- Prefer giving an unused artifact an invoker over adding to one that already works. (2026-09-20; ADR-025, #1769)
- Prefer improving what many things depend on over what nothing depends on. (2026-09-20; ADR-025)
- Treat the day, not the cycle, as the unit of delivery: something is owed to the screen before deep sleep. (2026-09-20; ADR-026, goals.md Vector 3)
- Retiring what no longer earns its keep is progress, not cleanup. (2026-09-20; goals.md Vector 1)
- Read the skill before repeating work a skill already describes. (2026-09-20; OPERATING.md Before editing)
- Never report a render or a publish as an audience. (2026-09-20; ADR-025 clause 4, ADR-026)

## Superseded

None yet. A retired directive moves here unchanged, with the date it was
retired and the directive that replaced it; entries in this section are never
deleted.
