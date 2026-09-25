---
title: One dashboard, two places — the published generator also serves the LAN, and only the LAN sees what the agents said
status: proposed
date: 2026-09-25
authors: [ozand, architect]
related: ["ADR-013", "ADR-018", "ADR-034", "#1899", "#1903", "#1917", "ozand/eeebot-ops-dashboard#311", "ozand/eeebot-ops-dashboard#313"]
tags: [dashboard, observability, privacy, deploy, decommission]
---

# Status

Proposed 2026-09-25, on the operator's decision the same day. Becomes accepted
when its named tests land and the operator flips the status, per
`docs/adr/README.md`. Implementation starts after the #1903 window closes.

# Context

Two dashboards exist, and they have drifted into two products.

| | published | host |
|---|---|---|
| where | `ozand.github.io/eeebot-ops-dashboard` | `http://192.168.1.189:8080` (LAN) |
| code | `ozand/eeebot-ops-dashboard`, `techtree_viewer.py` | `ozand/eeebot`, `scripts/eeebot_dashboard.py` |
| built | `techtree_autopublish.py` renders every page in memory (`render_pages`) and PUTs it to gh-pages via the contents API, about every 15 min, as `eeebot-publish` | a live server, 15 s refresh, as `eeepc-agent` |
| ships | `deploy/sync-manifest.txt` of the dashboard repo | inside every agent release |
| readers | the operator | no human reader found; the deploy health gate |

All operator-facing work of the past month went to the published one. The host
one stayed, because four things depend on it that have nothing to do with a
human looking at it:

- **deploy gate** — `deploy_release.sh:622–662` requires exactly one listener on
  `:8080` and a response from `/api/health` and `/api/metrics`, or the deploy dies;
- **host capability probe** — `eeebot-host-capabilities.service` runs
  `eeebot_dashboard.py --refresh-host-caps` daily (ADR-013);
- **held-out benchmark** — `heldout/checkers.py:190` `check_eeebot_dashboard` is
  one of the four held-out checks;
- **decay protection** — `SELFEVO_DECAY_PROTECT=scripts/eeebot_dashboard.py`.

The operator stated what the dashboard is for (2026-09-25): observability of the
agents' processes. Every cycle is its own entity, with which agent and which
model, ideally down to every call — the request and the answer in readable form,
not JSON, and never keys or secrets; a tool step such as publishing a video shows
what it did, not tokens.

A concrete failure shows why: `cycle-77ca3cf3580c` appears as *184 calls, 7.97M
tokens, 2h43m*. It was four attempts — three killed by the unit timeout, one
success — each within the 80-tick box (at most 36 executor and 16 planner calls).
The page summed them, and the reader concluded the box was broken.

The data to do this already exists on the host:

- `state/llm_calls/prompts/<day>.jsonl` — per model call: `cycle_id`, `component`,
  `model`, `seq`, the request `messages` with roles, the response `content`,
  `reasoning_content`, token counts, `finish_reason` (about 30 MB a day, older days
  gzipped);
- `state/llm_calls/<day>.jsonl` — per call duration and served model;
- `state/bridge/runs.jsonl` — one row per attempt (`run_id`, start, end,
  classification, cause, `cycle_id`);
- `state/subagents/*.json` — sessions: label, task, status, result;
- the cycle ledger — outcomes and verdicts.

Tool calls are not recorded separately; each request carries the conversation so
far, so the tool calls and their results are reconstructable from the next
request's messages.

# Decision

### 1. One generator, two sinks

The published generator is the only dashboard code. Each run writes the same
rendered pages to two places:

- **gh-pages**, as today;
- **a site directory on the host**, readable by the server in rule 2.

**A site is a snapshot, swapped whole.** Each run renders into a new directory
named by its snapshot version, then switches a `current` link to it in one
rename; the previous snapshot stays until the next swap succeeds. A reader never
sees a new page beside an old data file, or a link to a page not yet written.

**The two sinks are ordered and fail independently.** The host snapshot is
written first; the gh-pages update follows. If the GitHub update fails, the host
keeps the new snapshot and the next run retries the publish. Every page shows its
snapshot version and generation time, so a lagging or failed publish is visible
instead of assumed away.

### 2. Port 8080 serves the host site, on the LAN, without a password

`:8080` becomes a static file server of the site directory, bound to all
interfaces as today, with no authentication. This is the operator's decision of
2026-09-25, recorded as an accepted risk: anyone who can reach the host on `:8080`
can read the private pages of rule 3. "All interfaces" includes the tailnet, not
only the home LAN; which networks actually reach the port is a fact of the host's
network rules, checked and recorded when this ships and whenever they change. The
decision is revisited if any reachable network stops being trusted.
Checked 2026-09-26: `:8080` is reachable from the LAN, the tailnet and loopback.
The operator accepted the LAN and the tailnet as trusted (#1969).

The interface of `scripts/eeebot_dashboard.py` is retired (rule 6).

### 3. The boundary: a publish allowlist, and private pages that only the host has

The generator produces two sets:

- **public pages** — cycles, attempts, sessions, roles, models, counts, causes,
  durations, verdicts; never the text of a request or an answer;
  task and commit titles are public by the operator's decision of 2026-09-26;
  the text of calls is not;
- **private pages** — the per-call detail of rule 4.

The boundary is enforced on **every file** of the publish tree, not only on HTML
pages: data files, archives, search indexes, manifests and anything with an
unexpected extension.

- **Separate inputs.** Public pages are rendered from a data object that never
  contains request, answer, reasoning or tool-output text; the private renderer
  is the only code that reads `state/llm_calls/prompts/`. Private text cannot leak
  through a public page because the public renderer never holds it.
- **An allowlist of published paths.** The publisher sends only paths on an
  explicit list and fails loudly on any other, whatever its name or origin.
- **A scan of the built tree before it is sent.** The complete gh-pages tree is
  checked for private markers (secret patterns and fingerprints of call content)
  and the publish is refused on a hit.

The host site gets both sets. A public page that has a private counterpart links
to it as a host-relative path labelled *LAN only*; the link carries no data, and
on GitHub it simply does not open.

### 4. The entity model

```text
cycle ─┬─ attempt (run_id)          start, end, how it ended (completed | unit timeout | killed | …)
       │    └─ session              role (proposer | planner | executor | reflector | …),
       │         │                  model and served model, ticks used / limit, end reason
       │         ├─ model step      what was asked (the messages new since the previous step),
       │         │                  what was answered, tool calls requested, reasoning (collapsed),
       │         │                  prompt / completion tokens, duration
       │         └─ tool step       tool, arguments in readable form, result excerpt, status, duration
       └─ outcome                   verdict, files, commit — from the ledger
```

- **An attempt is its own row.** Counts are shown per attempt and totalled per
  cycle, never only totalled: *4 attempts · 3 killed by timeout · 185 calls*.
- **A tool step carries no tokens.** Tokens belong to model steps only.
- **Readable, not raw.** Messages render as text blocks by role; tool calls as
  `tool(argument: value, …)`; long outputs are cut *for display* with an explicit
  *"… N characters not shown"*; the source records are never changed.
- **Queue and generation are separate** when #1899 provides the split; until then
  the duration is labelled *queue + generation, not separable*.

**Tool steps: reconstructed for the past, recorded from now on.** Today tool calls
exist only inside the next request's message history, so reconstruction is
incomplete by construction: the last tool calls before a session ends, is
cancelled or is killed have no next request; compaction can remove earlier
history; retries and several tool calls in one answer blur which attempt they
belong to. Therefore:

- a reconstructed step names its source (`reconstructed from request seq N`);
- a session whose history is known to be incomplete says so, *history
  incomplete*, and missing steps are never shown as "no tools were used";
- tool duration is **unknown** for reconstructed steps, never computed from the
  gap between requests;
- the agent runtime gains a **tool-step recorder** (tool, arguments, result
  excerpt, status, start and end) keyed by cycle, attempt, session and seq, so
  new cycles need no reconstruction. The recorder lives in the agent repo and
  ships after the #1903 window like any behaviour-adjacent change.

### 5. Nothing secret is rendered, anywhere

Redaction runs on render, for both sets:

- values matching secret patterns — API keys, bearer tokens, `Authorization`
  headers, private-key blocks, passwords in URLs and `KEY=value` lines — are
  replaced by `[redacted: kind]`;
- no page ever renders the contents of environment files
  (`/etc/eeepc-agent/*.env`) or of the publish credential, even when a tool
  output happens to contain them;
- the operator's priority document is private: its text may appear on private
  pages (it is the operator's own LAN), never on a public page.

Redaction is a second line, not the boundary. The boundary is rule 3: call
content never leaves the host.

### 6. Retiring the old interface by responsibilities

| responsibility | today | after |
|---|---|---|
| human interface on `:8080` | `eeebot_dashboard.py --serve` | **replaced** by the static server of rule 2 |
| deploy health gate | `:8080` listener + `/api/health` + `/api/metrics` | **replaced** by a model-free activation check in the shape of #1917; the gate stops depending on any dashboard |
| host capability probe | `--refresh-host-caps`, daily unit | **moved** to its own module and unit; its output path and readers unchanged (ADR-013) |
| held-out check | `check_eeebot_dashboard` | **replaced, as a named change to the control set**, not a silent shrink. The new check asserts what the old one existed for, on the new dashboard: the current snapshot is served, it is younger than a bound, and a known page renders. If a property of the old check has no equivalent, the loss of coverage is written down in the change, not absorbed into a new baseline |
| decay protection | `SELFEVO_DECAY_PROTECT` entry | **dropped** with the file |
| skill and test references | `eeebot-agent-work-review` skill, tests | **updated** in the same change |

Before implementation this table is re-checked against every reader of
`eeebot_dashboard.py`, of `:8080`, and of each of its routes (`/api/*`,
`/health`, the root page) in both repositories, host units, external probes and
skill texts, the way the #1699 and #1941 censuses were. Each reader gets a
replacement or an explicit `unavailable`. Nothing is removed until its
responsibility has a new owner or is dropped on purpose.

# Consequences

- **One code base for the operator's view.** Dashboard work no longer splits
  between two repos, and none of it ships inside an agent release, so measurement
  freezes such as #1903 do not block it — except the deploy-gate change, which is
  part of the agent repo.
- **The operator gets the view he asked for** — attempts, roles, models, every
  call — on the LAN, and the public page stops misleading by summing attempts.
- **The LAN exposure is explicit.** Private pages carry the agents' requests,
  answers and tool outputs; the operator accepted this for the home LAN.
- **The site directory grows.** Private pages are rendered for a bounded window
  (default: the last 7 days of cycles), older ones removed by the generator.

# Alternatives considered

**Keep both dashboards.** Rejected: the host one has no human reader, and keeping
it alive keeps four unrelated duties tied to a UI nobody opens.

**Publish call detail on the public page with redaction.** Rejected: redaction is
never complete, and the page is public.

**Private repository with Pages.** Rejected: GitHub Pages for private
repositories needs a paid plan, and the content would still leave the host.

**Password on the LAN pages.** Offered; the operator chose no password.

## Test Contract

| Decision claim | Test | Currently |
|---|---|---|
| One run writes the same public page set to both sinks, host first, as one snapshot version | `tests/test_two_sinks.py::test_public_pages_same_snapshot_in_both_sinks` | not written |
| The publisher sends only allowlisted pages and fails loudly on any other | `tests/test_two_sinks.py::test_publish_allowlist_refuses_unlisted_pages` | not written |
| Private pages exist only in the host site | `tests/test_two_sinks.py::test_private_pages_never_reach_gh_pages` | not written |
| Public pages contain no request or answer text | `tests/test_two_sinks.py::test_public_pages_carry_no_call_content` | not written |
| An attempt is its own row; counts are per attempt and totalled | `tests/test_cycle_detail.py::test_attempts_are_rows_with_per_attempt_counts` | not written |
| Model steps render messages, answer, tool calls, collapsed reasoning, tokens and duration; tool steps carry no tokens | `tests/test_cycle_detail.py::test_model_and_tool_steps_render_readably` | not written |
| Secret patterns and env-file contents never render, on either set | `tests/test_cycle_detail.py::test_redaction_covers_secret_patterns_and_env_files` | not written |
| An attempt to publish a private file of any kind (JSON, archive, index, unexpected extension) is refused on every publish path | `tests/test_two_sinks.py::test_no_private_file_of_any_kind_is_published` | not written |
| The built gh-pages tree is scanned and a private marker refuses the publish | `tests/test_two_sinks.py::test_built_tree_scan_refuses_private_markers` | not written |
| The public renderer's input never contains call text | `tests/test_two_sinks.py::test_public_renderer_input_has_no_call_text` | not written |
| A failure of either sink mid-update leaves a consistent snapshot; the next run retries | `tests/test_two_sinks.py::test_sink_failure_leaves_consistent_snapshot` | not written |
| A concurrent reader sees one snapshot, never a mix of old and new files | `tests/test_two_sinks.py::test_snapshot_swap_is_atomic_for_readers` | not written |
| A final tool call without a next request, a killed attempt, a retry and a compaction each render as *history incomplete*, never as "no tools" | `tests/test_cycle_detail.py::test_incomplete_history_is_marked` | not written |
| Reconstructed tool steps name their source and carry unknown duration | `tests/test_cycle_detail.py::test_reconstructed_steps_have_source_and_unknown_duration` | not written |
| The tool-step recorder writes tool, arguments, result excerpt, status and times keyed by cycle, attempt, session and seq | `tests/test_tool_step_recorder.py::test_recorder_keys_and_fields` | not written |
| Every former `:8080` route reader receives a replacement or an explicit `unavailable` | `tests/test_deploy_gate_without_dashboard.py::test_every_former_route_reader_is_served_or_unavailable` | not written |
| The replacement held-out check covers the old check's purpose, or the change records the coverage lost | `tests/test_heldout_dashboard_check.py::test_replacement_check_covers_or_records_loss` | not written |
| Display truncation is marked and never changes source records | `tests/test_cycle_detail.py::test_truncation_is_marked_and_display_only` | not written |
| The deploy gate passes without any dashboard listener | `tests/test_deploy_gate_without_dashboard.py::test_gate_is_model_free_and_dashboard_free` | not written |
| The host capability probe runs from its own unit and writes the same output | `tests/test_host_capabilities_unit.py::test_probe_moved_output_unchanged` | not written |

# References

ADR-013 (capability probe); ADR-018; ADR-034 (the operator's priority document is
private); #1899 (queue vs generation); #1903; #1917 (model-free activation check);
`ozand/eeebot-ops-dashboard#311`, `#313`; `cycle-77ca3cf3580c` (four attempts summed
as one); `scripts/eeebot_dashboard.py`; `host/eeepc/scripts/deploy_release.sh:622–662`;
`host/eeepc/systemd/eeebot-host-capabilities.service`;
`nanobot/runtime/heldout/checkers.py:190`; `state/llm_calls/prompts/`.
