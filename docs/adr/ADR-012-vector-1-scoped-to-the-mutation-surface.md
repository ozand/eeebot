---
title: Vector 1 names only what the loop is permitted to change
status: proposed
date: 2026-09-14
authors: [eeebot maintainers]
related: ["#1598", "#1596", "#1599", "#1482", "#800", "#1335", "#1208"]
tags: [goals, governance, self-improvement]
---

# Status

Proposed — filed with #1598, ahead of any charter edit. ADR-011 governs how the charter reaches demand; this record governs what the charter may ask for.

# Context

`goals.md` is the immutable operator charter. It is read from `RELEASE_ROOT` by `goal_review.read_charter_text`, appended to every executor prompt after the prompt-fit pass, and the loop is forbidden to edit it. Vector 1 — the primary goal — asks the loop to

> make the agent system itself more effective and higher-quality at running its own improvement cycles … finding and applying optimizations to its own code and workflows … proposing dedicated modules in more efficient languages (Rust, C++, C) with benchmarks proving the gain.

The agent system is the harness in `ozand/eeebot`: prompt assembly, compaction, the gate, demand collection, the proposer. The loop cannot change a line of it.

```text
nanobot/runtime/mutation_policy.py:24
_COMMIT_PATH_PREFIXES = ("surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/", "skills/")
_COMMIT_EXACT_PATHS = frozenset({"AGENTS.md"})
```

`nanobot/`, `systemd/`, `ops/` and `state/` sit outside the commit surface deliberately; `state/` and `ops/` are the policy's `forbidden_dirs`, and `goals.md`, `IDENTITY.md`, `SOUL.md`, `USER.md`, `OPERATING.md` are its `immutable_files` (PR #1731). `AGENTS.md` is the single exact-path allowance (`commit_exact_paths`), for repository layout only (ADR-022 decision 6). The loop's reach is the scaffolding around the executor, never the executor.

What that produces is visible in 2 288 non-merge commits on `eeebot-self-evolving@origin/main` since 2026-07-01:

```text
feat 594 · chore 1076 · docs 120 · fix 90 · test 74 · perf 20
197 of 266 recent feat subjects begin with "add"
144 of 266 are detect / check / validate / assert / guard / report
 20 perf commits (0.9%), each optimising a validator the loop wrote itself
  0 files in Rust, C or C++
154 scripts/*.py across 104 distinct commit scopes
```

The shape is not a model failure. When the only writable surface is `scripts/`, "become more effective at running improvement cycles" has one available expression, and it is "add another checker". The optimisation clause makes the point sharpest: it carries the strictest requirement in the charter — every optimisation claim needs a before/after measurement — and where exercised it is honoured (`perf(validate_deprecation_status): ... cut runtime from 23s to 12s`). But what gets optimised is the loop's own overhead, because the loop's own overhead is the only thing in reach. The genuine candidate, 1.5M tokens per integration, lives in harness code.

A charter that asks for an object the permissions withhold makes every measurement of progress against it dishonest in both directions: a satisfied V1 metric overstates, and an unsatisfied one blames the loop for a boundary the operator drew.

# Decision

**The charter names objects the loop can actually change, and says plainly which parts of a vector the operator executes.**

## 1. Every named object of improvement intersects the mutation surface

A vector that directs the loop names something inside `_COMMIT_PATH_PREFIXES` or `commit_exact_paths` (`AGENTS.md`, repository layout only). Vector 1's object becomes the instance's own tooling, knowledge and workflows — the surface the loop actually holds.

## 2. Operator-executed work is labelled as operator-executed

Improving the harness remains a real goal of the project; it stops being phrased as an instruction to the loop. The charter states which part of a vector the operator carries, so a reader of either the charter or a V1 metric knows whose work is being measured.

## 3. An unsatisfiable clause is either given a route or moved

The Rust/C/C++ clause has produced 0 native files in 2 288 commits, and there is no path by which a loop confined to `scripts/` and `tests/` could produce a compiled module the harness would load. It is either given an explicit route or it moves to the operator-executed part under rule 2. A standing clause nobody can satisfy trains the reader to skip clauses.

## 4. The charter and the mutation policy are checked against each other

A test asserts that the surfaces the charter names and `MUTATION_POLICY` do not contradict each other, so the two cannot drift apart again silently. They drifted this far because nothing compared them.

**Widening the loop's reach is explicitly not part of this decision.** Letting the loop open pull requests against `ozand/eeebot` — containment by review and CI rather than by path prefix — is a genuine capability change with its own failure modes, and it needs its own issue, gate, held-out contract and rollback story. Shipping it inside a wording change would be a permissions change under cover of a documentation edit.

# Consequences

## What gets easier

V1 metrics start meaning what they say. Today `repeat_failure_rate` at 0.298 is read as "the agent system is getting better at running its cycles"; after this it is read as "the instance's tooling is getting better", which is the claim the data actually supports.

The output shape stops looking like a defect. A loop that adds checkers to `scripts/` is doing exactly what its permissions allow; once the charter says so, the question becomes the useful one — is this the best use of that surface — instead of the unanswerable one about why it is not rewriting the harness.

Rule 4 makes the next drift a test failure. The charter is operator-owned and edited rarely; the mutation policy changes with the runtime. Without the check, the next divergence is found by an audit, as this one was.

## What gets harder

Rescoping V1 narrows what the loop is nominally aiming at, and there is a real risk of encoding the current limits as the permanent ambition. ADR-011's Direction mechanism is the offsetting pressure: the aim can be raised without editing the charter, and it is visible on the front page.

Rule 2 puts harness improvement on the operator's plate explicitly. That is honest, and it is also more work that is now visibly unassigned rather than invisibly unassigned.

## What does not change

`MUTATION_POLICY`, the gate, the forbidden-path list, and the executor's confinement. Vector 2 and the operator-transparency work, which is served largely by the sibling dashboard repository (#1482). The charter's immutability with respect to the loop — this is an operator edit, and the gate keeps rejecting any cycle that touches `goals.md`.

# Alternatives considered

- **Leave the charter and widen the mutation surface to `nanobot/` by commit.** Rejected. The path-prefix confinement is the property that makes an unreviewed autonomous cycle safe to run every 15 minutes; replacing it with nothing is not a trade, and replacing it with review is option (b) below, which is a separate decision.

- **Widen the reach by pull request instead of commit, in this record.** Rejected as scope, not as idea. It may be the right next capability, and it deserves its own ADR with a held-out contract and a rollback story. Bundling it here would mean a permissions change lands with the approval given to a wording fix.

- **Leave the charter as aspiration and treat the mismatch as harmless.** Rejected. It is not harmless: it makes V1 measurement ambiguous, and #1599 shows the cost compounds — the loop's output shape and inventory are unmeasured precisely because nobody could say what the output was supposed to be.

- **Delete the optimisation clause entirely.** Rejected. The before/after-measurement requirement is the strongest sentence in the charter and it is being honoured where exercised. The clause needs a reachable object, not removal.

- **Rescope by narrowing to the metrics instead of the charter.** Rejected. Metrics are already proxies; redefining the goal as its proxies is the shortest route to optimising the proxy, and #800 and #1335 are both records of the loop finding exactly that kind of shortcut when one existed.

# Test Contract

- A check asserts every surface named as a loop-directed object in `goals.md` is covered by `MUTATION_POLICY._COMMIT_PATH_PREFIXES`, and fails when a charter edit or a policy edit breaks that.
- The check distinguishes loop-directed text from operator-executed text, so labelling a paragraph operator-executed is a supported outcome and not a way to silence the check by accident.
- A test asserts `MUTATION_POLICY` itself is unchanged by this work.
- The gate continues to reject any cycle that stages `goals.md` or `IDENTITY.md`; a test covers it, because this record is the first thing in a while to touch the charter's wording and the protection must be shown still live.

# References

- #1598 — the implementing issue.
- ADR-011 — how the charter reaches demand once it is satisfiable.
- #1596 — the demand-path half of the same problem.
- #1599 — output shape and inventory are unmeasured; the cost of not knowing what the loop is for.
- #1482 — two dashboard products; where Vector 2 is actually served.
- #800 — the create-then-archive reward-farming vector.
- #1335 — enhancement-shaped cycles on scripts nothing runs.
- #1208 — a previous dead-script list held 5 live files; measure before retiring.
- `nanobot/runtime/mutation_policy.py` — the commit surface.
- `nanobot/runtime/goal_review.py` — `read_charter_text`, and where the charter enters the loop.
