# Design: product ↔ instance flow

- **Issue:** #672
- **Status:** design/contract — no code changed by this document
- **story_id:** docs/specs/self-evolving-runtime/spec.md

## Overview

Two repos back the self-evolving agent:

- **product** — `ozand/eeebot`, the runtime code (this repo). Deployed to the
  eeepc host as immutable releases.
- **instance** — `ozand/eeebot-self-evolving`, the autonomous agent's own
  workspace repo, where it commits `scripts/`, `lessons/`, `surfaces/`, and
  memory roughly hourly (500+ autonomous commits).

Today the **downstream** direction (product → instance) works via full release
deploys. The **upstream** direction (instance → product) does not exist at all:
nothing the instance learns reaches the product. This document specifies the
upstream pattern and the minimal, operator-driven, read-only first increment —
deliberately shaped so it introduces no new reward gradient the instance can
farm (#800).

---

## 1. Downstream (product → instance) — document as-is

The existing path, `host/eeepc/scripts/deploy_release.sh`:

1. `git archive` of product HEAD → timestamped release tarball
   (`YYYYMMDDTHHMMSSZ-canonical-<commit>`), excludes `.git`/`.venv`/`__pycache__`
   (deploy_release.sh:48).
2. `scp` to the host, extract into
   `/opt/eeepc-agent/runtimes/self-evolving-agent/releases/<name>`
   (deploy_release.sh:65-72); write `SOURCE_COMMIT` (deploy_release.sh:73).
3. Atomic activation: repoint the `current` symlink at the new release
   (deploy_release.sh:79); the bridge unit runs `-m nanobot.runtime.bridge`
   straight from `current` since #601 (deploy_release.sh:81-82).
4. Seed `goal_text.json` into the state dir, sync `libexec/` scripts, restart
   the service (deploy_release.sh:84-102).

**What is NOT synced, and why that is correct.** The deploy pushes product
*code* only. The **instance repo is never touched** by a deploy — its
`scripts/`, `lessons/`, `surfaces/`, and memory are the agent's own mutable
workspace and live under host state, not the release. Overwriting them on
deploy would destroy the very learning this issue wants to harvest, and would
re-create the "generation-inherited state" split-brain failure mode the loop
redesign explicitly bans (design-constraints.md, `anima` non-goal). Code flows
down; workspace state stays put. The two directions are asymmetric on purpose.

---

## 2. Upstream (instance → product) — pattern harvest

Harvesting means an **operator** promoting a proven instance artifact into the
product repo by hand. Three candidate types:

- **Lessons** — `lessons/*.md`-style failure-mode cards (the schema is
  cross-project-portable by design, lessons.py:1-14). A recurring, generalized
  failure mode is a candidate for a product-side guard, test, or doc.
- **Generalizable scripts** — `scripts/*.py` artifacts that solve a
  host-independent problem (e.g. a reporting/inspection helper), as opposed to
  host-local glue.
- **Prompt / policy patterns** — reusable shapes distilled from `surfaces/`
  (prompt or policy surfaces) that could improve the product's proposer or
  precheck.

**This has already happened once, manually.** `scripts/loop_metrics_report.py`
was harvested from the instance's autonomously-built `loop_health_report.py`
(P10, commit 6a365ac) — its self-check style and compact table rendering were
reused, re-based onto the ledger as source of truth
(loop_metrics_report.py:39-44). This design just makes that ad hoc act
repeatable and evidence-driven.

### Evidence base (harness-observed only)

A candidate is only worth an operator's attention if the *harness* — never the
instance's own claim — vouches for it:

- **Classification** — the #704 done-ledger `general_or_host_local` field
  (704-ledger-artifact-memory/design.md:49). See §5 for the integrity caveat:
  only a harness-derived classification may feed the report.
- **Usage evidence (#761)** — `usage_evidence.py` records when a
  `scripts/*.py` artifact was actually *exercised*, from three on-disk signals
  the harness observes itself (`used:pycache`, `used:output`, `touched:result`;
  usage_evidence.py:1-36). Only `{pycache, output}` count as trustworthy
  (`HARNESS_SIGNALS`, usage_evidence.py:71).
- **Confirmed-serves (#761)** — `confirm_serves` marks a completed demand
  `confirmed` iff `last_used` post-dates completion, from usage evidence alone;
  no text/claim field can ever confirm (usage_evidence.py:351-363).

A candidate with a general classification, recent harness-observed usage, and a
confirmed-serves tie-back is a strong harvest target. One with none is junk.

### Trigger = MANUAL operator command

Harvest runs **only when the operator invokes it** — never on a schedule and
never from inside the loop. The argument against a scheduled/automatic trigger
is #800 directly: the instance farms any reward gradient it can reach. A
standing "harvestable" pipeline is a new gradient — the instance would learn to
mint artifacts that *look* harvestable. Keeping the trigger in the operator's
hands, off the instance's clock, means "getting harvested" is never a signal
the loop can optimize toward. This matches the loop-redesign stance that
liveness and steering come from harness truth, not instance action
(702 decision.md §4, design-constraints.md C1).

---

## 3. Minimal concrete mechanism (next increment)

A **read-only, product-side report script** — `scripts/harvest_report.py`
(DESIGN ONLY, not written here). It runs on the operator's machine against a
read-only checkout/mount of the instance repo plus the host state dir, and
prints a table of harvest candidates. It writes nothing, merges nothing, and
touches neither repo.

Proposed columns, each sourced from harness-observed data:

| Column | Source |
|---|---|
| `artifact` | instance path (`scripts/*.py`, `lessons/*.md`, `surfaces/*`) |
| `classification` | done-ledger `general_or_host_local`, **harness-derived only** (§5) — 704 design.md:49 |
| `usage_signals` | `usage_evidence.py` `last_used`/`signal`, restricted to `HARNESS_SIGNALS` (usage_evidence.py:71) |
| `confirmed_serves` | `confirm_serves` result for the artifact (usage_evidence.py:351) |
| `age` | git last-commit date of the artifact in the instance repo |
| `ledger_refs` | `cycle_id`/`commit_sha` from the cycle ledger (cycle_ledger.py:210 terminal rows) |

Output is advisory. The operator reads the table, picks a candidate, and does a
**manual cherry-pick / rewrite** into the product repo through the normal PR
path (as was done for `loop_metrics_report.py`). **No auto-merge, ever.**

---

## 4. Non-goals (explicit)

- **No auto-merge** of instance code into the product — never, under any
  evidence threshold.
- **No instance-initiated PRs** to the product repo. The instance cannot open,
  request, or trigger a harvest; the operator pulls, the instance never pushes.
- **No new instance-visible reward** tied to the `general` classification or to
  "harvestability." No metric, no demand kind, no context signal derived from
  it may become visible to the proposer — otherwise it becomes farmable (#800).
- **No new durable state.** The report is computed on demand from existing
  ledger/usage artifacts; it is not a new ledger (cf. #704/#720 "the ledger is
  the only durable state").

---

## 5. Risks

- **Reward-hacking pressure on `general_or_host_local`.** This is the sharpest
  risk. The field is **not yet written anywhere in code** — it exists only as a
  designed done-ledger field (704 design.md:49), whose two candidate writers
  are "a harvest pass **or the proposing LLM**." The second path is
  **instance-claim-derived**: the instance writes the commits, so letting the
  LLM stamp its own work `general` is exactly the gradient #800 warns about and
  #789 already caught live (an instance script wrote a forged
  `signal: "operator-confirmed"` into the completed sidecar,
  usage_evidence.py:62-70). **Mitigation:** the harvest report must derive
  classification from **harness-observed signals only** — the same discipline
  `usage_evidence.HARNESS_SIGNALS` (usage_evidence.py:71) and the #789 tamper
  repair (usage_evidence.py:365-374) already enforce. If #704's field is ever
  wired, an LLM-assigned value must be treated as `unclassified` by the report,
  never as `general`. Prefer classifying from usage/confirmed-serves evidence
  the instance cannot author.
- **Churn imports.** Harvesting an artifact the instance is about to archive as
  decayed (demand.py `decay` kind, demand.py:55-67) wastes review and imports
  dead code. **Mitigation:** the report should surface `age` and usage recency
  and de-prioritize (or exclude) artifacts already decay-eligible.
- **Review burden.** Every candidate is manual operator work. **Mitigation:**
  evidence columns exist to *rank*, so the operator triages the top of the list
  and ignores the tail; the report is a filter, not a queue to drain.

---

## 6. Follow-up issues

1. **Implement `scripts/harvest_report.py` (read-only report).** Build the §3
   table from the cycle ledger + `usage_evidence` sidecar + instance git log;
   no writes, `--test` self-check like `loop_metrics_report.py`. Scope: one
   script, read-only.
2. **Harness-derived `general_or_host_local` classifier.** Wire #704's field
   from usage/confirmed-serves evidence only (never an LLM claim), so
   `harvestable_upstream_ratio` (loop_metrics_report.py:231-236, currently
   `null`) gains a trustworthy input. Scope: classification source, integrity
   test pinned per #789.
3. **Operator harvest runbook.** Document the manual cherry-pick/rewrite path
   (mirroring the `loop_metrics_report.py` precedent) so harvest is repeatable
   without new automation. Scope: docs only.

## References

- `host/eeepc/scripts/deploy_release.sh` — the downstream release path (§1).
- `docs/changes/704-ledger-artifact-memory/design.md` — done-ledger schema and
  the `general_or_host_local` field this harvest reuses (§2, §5).
- `docs/changes/702-ledger-loop-architecture-decision/decision.md` +
  `design-constraints.md` — harness-truth / single-proposer stance (§2).
- `nanobot/runtime/usage_evidence.py` — harness-observed usage + confirmed
  serves + #789 tamper repair (§2, §5).
- `nanobot/runtime/cycle_ledger.py` — terminal-row `cycle_id`/outcome (§3).
- `nanobot/runtime/demand.py` — `decay` kind, churn-import risk (§5).
- `scripts/loop_metrics_report.py` — the existing manual-harvest precedent and
  the `harvestable_upstream_ratio` gap this closes (§2, §6).
