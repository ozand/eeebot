---
title: An artifact graph has typed edges, and only production use makes a component
status: accepted
date: 2026-09-19
authors: [eeebot maintainers]
related: ["#1769", "#1766", "ADR-022", "ADR-023"]
tags: [architecture, runtime, observability, scorecard]
---

# Status

**Accepted, 2026-09-19.** Nothing in the tree satisfies this record yet. #1769 (the artifact dependency graph) is its implementation and is open. The resolver this record replaces — `_retention_cost` in `nanobot/runtime/scorecard.py` — is live and publishing today.

# Context

#1769 was filed on one number from the 7-day scorecard:

```json
"quality": {"script_count": 181, "retention_cost": {"zero_static_consumers": 122}}
```

read as *122 of 181 scripts have no consumer — the loop produces leaves, not layers*.

## What the number actually counts

`nanobot/runtime/scorecard.py:946-996`, verified in tree. The resolver's candidate set is `scripts/*.py`, and its **consumer** set is the same list:

```python
scripts = sorted(scripts_dir.glob("*.py"))
stems = {path.stem for path in scripts}
...
for consumer in scripts:            # ← only a script can be a consumer
```

An artifact scores a consumer when another *script* imports its stem or names `scripts/<name>.py`. Tests, documents, surfaces, skills and systemd units are never consumers, by construction. So `zero_static_consumers` means **"no other script references it"**, and it has been read as "nothing uses it". The figure is not wrong; its name is wider than its definition.

One further defect, found while verifying the above: the path regex is applied to the whole file text, including docstrings and comments —

```python
scripts_path_re = re.compile(r"(?<![A-Za-z0-9_])scripts/([A-Za-z_]\w*)\.py(?![A-Za-z0-9_])")
...
mentioned.update(scripts_path_re.findall(text))
```

— so a script named in a comment already counts as an edge today. The prose problem the taxonomy below fixes is not hypothetical; it is in the production resolver.

## What the other reference classes contain

Sampled 2026-09-19 against the 122 artifacts the current resolver calls consumerless:

| referenced from | of the 122 |
|---|---|
| `tests/` | 96 |
| some `.md` | 81 |
| a surface or a skill | 4 |
| a systemd unit | 1 |

Two samples then split those classes apart, and they split in opposite directions:

- **10 `.md` references: 2 were real instructions to run the script, 8 were catalogue entries or prose.** Four fifths of the documentation class is not use at all. A prose mention must never become an edge.
- **10 `tests/` references: 10 were real imports or executions.** Every one is a genuine reference — and none of them is production use. A script with a green suite and nothing running it is still dead weight.

So neither "count every reference" nor "count only script imports" produces the number the charter cares about. The classes have to be kept apart.

# Decision

**Edges in the artifact graph are typed, and only one type bears on whether an artifact is a component.**

1. **Three edge kinds.**
   - `used_by` — production use: an import or execution on a path the system actually runs, a surface or skill that invokes it, a systemd unit or timer that names it, a documented instruction to run it.
   - `tested_by` — a test imports or executes it.
   - `mentioned_in` — named in prose, a catalogue, a comment or a docstring.

2. **The rung.** An artifact is a **component** when at least one `used_by` edge points at it. Otherwise it is a **leaf**. `tested_by` and `mentioned_in` never promote a leaf. The definition is stated once, in the graph module, and every consumer reads it from there rather than restating it.

3. **A test is not a consumer.** Coverage is a property worth reporting on its own axis; it is not evidence that anything needs the artifact. `tested_by` is published because "leaf with tests" and "leaf without tests" are different retirement decisions, not because either is a component.

4. **Prose is not an edge.** A `.md` reference is `used_by` only when it is an instruction to run the artifact; everything else is `mentioned_in`. Where the resolver cannot tell the two apart it chooses `mentioned_in` and reports the count it could not classify — a documentation reference is 4-to-1 prose, so the conservative default is the accurate one.

5. **Unresolvable references are their own count.** Not dropped, and not counted as an edge.

6. **One definition of "consumer" in the codebase.** The graph replaces `_retention_cost`'s private resolver; the two must not coexist. Two definitions of the same word, one of them private to a scorecard field, is how the 122 got misread in the first place.

# Consequences

**The new number is not comparable to the old one, and the PR must say so instead of presenting a delta.** It moves in both directions at once: `used_by` widens to surfaces, skills, units and run-instructions, which turns some of the 122 into components; and `tests/` and prose stop counting entirely, which turns some of the other 59 into leaves. A single before/after figure would describe neither change. Report the three classes and the reclassification counts, not a difference.

**The graph's figures cannot enter the executor's prompt as fact.** Per ADR-023 a figure is presentable to the loop only when every input in its derivation lies outside the loop's writable surface. This graph resolves edges from `scripts/`, `tests/`, `docs/`, `surfaces/` and `skills/` — all inside it. Only the systemd class is outside. The graph belongs on the dashboard and in demand ranking; it reaches the prompt only labelled as self-reported, if at all.

**Judgement is required for one class and must be bounded.** Distinguishing an instruction from a mention is the only classification in this taxonomy that is not mechanical. It is bounded by clause 4's default and made visible by its unclassified count; it is not resolved by a cleverer regex, and a resolver that claims to have classified every `.md` reference is over-claiming.

**Retirement gets a defensible input.** "Leaf, no tests, oldest" is an argument for retirement. "No other script imports it" never was, and 96 of the 122 had a test importing them.

**`_retention_cost` keeps publishing until the graph replaces it.** During the overlap two numbers will disagree. The field's current meaning must be documented where it is published so the disagreement reads as two questions rather than a bug.

# Alternatives considered

**Untyped edges with a weight.** Rejected. A weight would put "a test imports it" and "a timer runs it" on one scale, and the whole finding is that they are categorically different — one is coverage, the other is use.

**Keep counting tests as consumers.** Rejected on the sample: all 10 `tests/` references were real, so this is not a precision problem that better resolution fixes. It is a definition problem. Counting them would declare 96 of the 122 artifacts alive without a single one of them being run in production.

**Runtime call tracing instead of static resolution.** Out of scope for #1769 and does not settle this question: a trace would show what ran during the window, which is a fourth signal to classify, not a replacement for the taxonomy. Worth revisiting once the static graph exists and its `used_by` set can be checked against what actually executes.

**Fix the name instead of the resolver** — rename the field to `scripts_with_no_script_importer` and stop there. Tempting, and it would remove the misreading. Rejected because the charter's question ("is this a component others stand on?") would then have no answer at all, and because clause 6's two-definitions problem would survive.

# References

- #1769 — the artifact dependency graph (implementation).
- ADR-023 — provenance rule; why this graph's figures cannot be shown to the loop as fact.
- ADR-022 — context ontology; the "one question, one owner" principle this record applies to the word "consumer".
- `nanobot/runtime/scorecard.py:946-996` — `_retention_cost`, the resolver being replaced; the `for consumer in scripts` loop and the docstring/comment-matching regex are both quoted above.
- Sampling of the 122, 2026-09-19: class counts and the two 10-item samples, in Context.
