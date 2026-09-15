---
title: An optimisation is measured against the fastest implementation this host can run, never against the loop's own previous code
status: proposed
date: 2026-09-15
authors: [eeebot maintainers]
related: ["#1599", "#1605", "#1613", "#1619"]
tags: [self-improvement, measurement, honesty, eeepc]
---

# Status

Proposed, from a live case on 2026-09-15: the first artifact produced under a newly seeded priority satisfied the charter's strictest sentence and still shipped something slower than what the machine could already do.

# Context

`goals.md` Vector 1 carries the project's hardest requirement: *every optimisation claim must come with a before/after measurement*. It is the clause that separates this loop from one that asserts improvement.

The loop wrote `scripts/fb_surface.py`, a palette expansion from an 8-bit index buffer to RGBA, and reported its own measurement honestly:

```text
per-pixel          1819.33 ms per frame
uint32 LUT          148.57 ms per frame
speedup              12.25x
```

Every number is true and was produced by the artifact's own self-test on the host. The clause was satisfied exactly as written.

The same operation, measured on the same machine a day earlier with `numpy 1.24.2` — which is installed, importable as the agent user, and already used elsewhere in the instance:

```text
numpy uint32 LUT      36.3 ms
numpy fancy index    107.5 ms
```

So the optimised artifact is **four times slower than the numpy LUT**, and slower than the *naive* numpy implementation it never considered. The module declares "stdlib only" in its own docstring, as a property rather than a decision with a cost.

The clause did its job and caught nothing, because the baseline was the loop's own previous revision. A ratio against yourself is always available and always flattering: the worse the starting point, the better the number. Twelve times faster than a bad implementation is a report about the bad implementation.

This matters beyond one module. Cost per delivered thing is the metric this project intends to run on — ADR-016 makes cost per finished minute an admissible target, ADR-013 requires every artifact to declare µs and peak RSS. All of that arithmetic is built on measurements whose baseline nobody has constrained.

# Decision

**The baseline is the fastest implementation reachable on this host with what is already installed.** Four rules.

## 1. The baseline is named, and it is not your own last commit

A before/after measurement states what the "before" *is*: a named implementation, not "the previous version". Where a faster implementation is reachable with the libraries present on the host, that is the baseline, and beating a slower one is not a result.

## 2. A ratio without an absolute figure is not a measurement

Report the absolute cost in the units that matter — milliseconds per frame, CPU-seconds per cycle, bytes — beside any speedup. A 12× improvement and 148 ms per frame are compatible facts, and only the second one says whether the thing is usable.

## 3. "Stdlib only" is a decision with a price, and the price is quoted

Avoiding a dependency is often right on a 2 GB machine: import cost, memory, install risk. It is a trade, so the record shows both sides — what the dependency-free path costs against the available one, measured, in the same units. Asserted as a virtue with no number, it is how a slower artifact gets called an optimisation.

## 4. Not using the fastest available implementation is allowed, and is stated

There are good reasons to refuse the faster path: it may not exist on the target surface, it may cost memory the cycle does not have, it may drag in a dependency the harness cannot rely on. Any of those is a legitimate answer. Silence is not — the artifact says which implementation is fastest here, which it used, and why.

# Consequences

## What gets easier

Cost-per-delivered-thing becomes trustworthy. Every figure ADR-013 and ADR-016 rest on gains a defined floor, so summing them means something.

The loop is pushed to look at what the machine already has before writing new code. `numpy`, `PIL`, `scipy`, `libopenmpt`, `ffmpeg` and a C and Rust toolchain are all installed and were all invisible to an artifact that reimplemented a lookup table in Python. That is a composition failure as much as a performance one (#1599): the fastest path was on the machine and unused.

The charter's strictest clause starts catching things. It has been exercised 20 times in 2 288 commits, and the first exercise under a seeded priority produced a regression reported as a win.

## What gets harder

Every optimisation now needs a survey before it needs code — what is installed, what would this cost with it. That is more work per change, and it is the work that was missing.

Some artifacts will now correctly report that they made something *slower* than the available alternative and shipped anyway for a stated reason. That is the intended outcome and it will read as a failure to anyone skimming.

## What does not change

The charter — Vector 1's sentence is unchanged, this record says how to satisfy it honestly. ADR-013's per-artifact cost declaration and ADR-016's cost-per-minute target, both of which this makes meaningful. `MUTATION_POLICY`, the gate, fitness, `_TARGETS`.

# Alternatives considered

- **Leave it: the measurement was honest and the clause was met.** Rejected. It was honest and it was useless, which is the worse failure of the two — a check that passes over a regression teaches everyone to trust it.

- **Require a specific library.** Rejected. Naming `numpy` in a rule dates immediately and answers the wrong question; the rule is about the baseline, not about a dependency.

- **Ban "stdlib only".** Rejected. It is frequently the right call on this hardware. What is forbidden is asserting it without the number that justifies it.

- **Make the loop benchmark every available implementation before every change.** Rejected as written — it would dominate the cycle budget. The rule applies to a change *claimed as an optimisation*, which is a small fraction of commits, and asks for the fastest reachable path rather than an exhaustive survey.

# Test Contract

- A commit whose subject or body claims an optimisation carries an absolute figure, a named baseline, and the units; a claim with only a ratio fails the check.
- A fixture with two implementations present asserts the recorded baseline is the faster one, not the artifact's own previous revision.
- An artifact that declines the fastest available implementation records which one it is and why it was not used; silence fails.
- The check runs on the commit, not on a reviewer's attention, since the case that prompted this record passed review-by-self-test cleanly.

# References

- `goals.md` Vector 1 — the before/after clause this record operationalises.
- ADR-013 — every artifact declares measured µs and peak RSS; this defines what those are measured against.
- ADR-016 — cost per finished minute as an admissible target, which depends on it.
- #1599 — composition depth; reimplementing an installed library is the same failure seen from another side.
- `scripts/fb_surface.py` in the instance — the live case: 12.25× faster than itself, 4× slower than the installed alternative.
