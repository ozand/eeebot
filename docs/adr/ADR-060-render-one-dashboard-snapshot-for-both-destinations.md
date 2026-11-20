---
title: Render one selected dashboard snapshot for both destinations
status: proposed
date: 2026-11-20
authors: [ozand, architect]
related: ["ADR-036", "ADR-048", "ADR-049", "#2191"]
tags: [dashboard, observability, publication]
---

## Status

Proposed under [#2191](https://github.com/ozand/eeebot/issues/2191). This record does not change current behavior, authorize implementation or delivery to either destination, or supersede unrelated ADR-036 decisions.

## Context

The owner wants one dashboard without redundant publication checks and states that the agent does not access secrets. The publisher is a different process: it reads persisted inputs and has an authenticated GitHub transport. Agent permissions alone cannot establish what bytes the publisher may render.

Dashboard master `dfaf9181791cf15e54f35d1dc5272ee6d272882d` renders a common site, then adds private cycle pages and a router only for LAN. `publish_ordered` scans the versioned public pages; `publish_to_pages` also scans candidate pages twice, and checks inherited artifacts. Inherited bytes are not necessarily those just rendered. Neither the overlay nor the repeated checks are necessary to have two delivery destinations.

A local control on equivalent reviewed dashboard source `f0cad4372637f25f5dec1a15180662fbb1a6193f` used one synthetic lesson, changing only its `solution`. `split_render_inputs` retained both ordinary prose and an invented private-key header; `render_public_pages` put each in `lessons.html`. `scan_pages` accepted the prose and rejected the header. No key body, real record, or transport was used. This is not evidence of a real secret or of the producer in #430. It shows that field selection and length limits are not a content guarantee. A separate prompt-reader probe did not pass its positive control on Windows and is not evidence for the decision.

## Decision

Propose one selected dataset, one render, and one versioned artifact map for both destinations. Use the existing typed public projection as the ceiling, not the raw input dictionary or a union with the LAN overlay. Keep the LAN atomic swap first and the GitHub ref update second, with independent sink outcomes; equal artifacts for a snapshot do not imply equal freshness at both locations.

Remove duplicate checks of identical bytes, not the sole check covering them. Preserve effective validation of the exact bytes each destination receives, including retained GitHub blobs if still inherited. Validate the shared artifact map before using it for either destination. GitHub-only validation of inherited content must not hold up the LAN swap. If a path is absent from the shared manifest, it is not part of the snapshot: do not silently carry it forward. Blob reuse may change transfer cost, not the files or bytes served. A retained cycle archive must belong to that same manifest and be available to both sinks; otherwise mark that coverage unavailable rather than claim parity. Preserve version checks, regular-file-only writes and explicit relative output names. Do not rely on secrecy of a URL or on the scanner to enforce allowed paths.

### Selected inputs

| Source authority | Admitted fields or capability |
|---|---|
| Configured agent state | Existing typed projections of portfolio/tree, scorecard, hypotheses, ledger outcomes, demand, skill/CI/model/compaction/token/bridge/planning/strategist/drift observations, and source freshness. Use validated IDs, statuses, counters, times, titles and cycle file metadata; no raw diagnostic errors. |
| Bounded own records in state | Selected cycle-level prompt excerpts, subagent task/summary/result, reflections, and the release-authored charter in the derived public view. Preserve bounds and explicit provenance checks. Operator `goal_text` contributes metadata only. |
| Configured instance repository | Selected own lesson fields, bounded `AGENTS.md`, skills and memory, and existing cycle title/file metadata; no repository walk as a source of arbitrary file bodies. |
| Publisher configuration and CI | Validated display label, generator identity, snapshot metadata and typed CI freshness. The authenticated GitHub transport returns only projected observations to the generator; credentials are not data inputs. |

Keep source roots separate and preserve descriptor-relative, regular-file, bounded reads and explicit provenance checks for admitted own text. Exclude credentials, environment/auth files, and unrelated private operator or third-party data. This increment neither adds a per-call history reader nor copies raw LAN history. These are limits on this increment, not a publication ban on bounded, authorized own sessions, requests, responses or tool history under ADR-036 §2. A named path alone does not prove containment; any selected reader that cannot enforce its root must be constrained before shared delivery or remain unavailable. An unsupported safe-open platform must not fall back to following links. Keep source exclusions independent of content checks.

### LAN-only displays

`render_private_pages` creates `cycles/<id>.html` and `_add_private_cycle_navigation` overrides LAN `cycle.html`, including its missing-detail view. No other LAN-only page set is constructed by this writer.

| Current LAN-only display | Disposition in the shared snapshot |
|---|---|
| Per-cycle route and LAN detail unavailable page | Retire the routing override and standalone pages; both destinations use existing `cycle.html` with the same availability evidence. |
| Cycle summary and reflection counts | Reuse already projected cycle and reflection fields; no private merge needed. Private-derived model-call totals and completeness are not carried over unless supported by selected authoritative data. |
| Attempt classifications, outcomes, counts and completeness; attempt/session roles, token/duration totals and tool sequence; fallback session list | Preserve already supported public aggregates and selected subagent records. Retire the private reconstruction; any missing per-attempt or per-session value remains unavailable. Do not infer chronology, duration or causality; dashboard #443 remains separate. |
| Per-model and per-tool steps, including requests, responses, reasoning, arguments and results | Retire the LAN-only transcript route in this increment without exporting its data. Keep already selected bounded cycle prompts and subagent summaries; they are not reconstructed turns. |

Only the destination-specific display and routing are replaced by this proposal. ADR-036 §4's recorder and detail requirements remain applicable to separately authorized work: per-attempt rows, model/tool distinction, duration and completeness semantics, reconstruction attribution, and the tool-step recording and test obligations. A future authorized detail view uses the same shared dataset contract; this proposal neither adds it nor deletes its source records or collection requirements.

### When the last scan can go

Do not remove the remaining whole-output check for the dataset above. The control disproves the claim that the current projection already replaces it; it does not measure leak likelihood. To remove it later, show from reader/writer contracts and synthetic tests that every route to either destination excludes the disallowed sources and content. Verify that previously inherited bytes cannot reintroduce unchecked data. An explicit content boundary at ingestion or a smaller dataset with no uncontrolled text could meet that bar. Neither is demonstrated for the current dataset. Stop before delivery if source or output containment cannot be established. No bypass flag, scanner exception for the incident, or new collector is proposed.

## Alternatives considered

- **Keep two views and only deduplicate checks:** lower-cost and keeps private troubleshooting detail, but leaves the LAN-only pipeline. Dashboard #369 is an independent route for deduplication.
- **Publish the LAN overlay everywhere and remove checks:** retains all the views but admits raw-history-derived displays and drops the existing protection without a replacement. Rejected.
- **Strip all free text and remove scans:** may make the boundary easier to verify, but removes the bounded own context authorized by ADR-036 under #2185. Not chosen here.

## Consequences

One selected projection, one render, and no destination-specific content. Users lose the LAN-only call transcript and unsupported private detail. Until a replacement content boundary is proven, a shared artifact rejected by the check cannot update either destination; transport failures remain independent. This design does not restore or verify public freshness in dashboard #430, resolve #443, or complete #369. No live data access, implementation, publication, or rollout is authorized by #2191.

## Test Contract

| Decision claim | Check | Evidence |
|---|---|---|
| Existing selection does not replace the content check | Compare two synthetic lessons through projection, render and scan, as described above | Executed on `f0cad437`; both render, only the marker is rejected |
| Both destinations receive the same snapshot | With two recording sinks, assert one render and equal version, paths and bytes; simulate failure of each sink independently | Not implemented |
| Containment survives consolidation | Exercise disallowed fields and sources, out-of-root and non-regular inputs, invalid paths, and extra/stale inherited blobs; assert no publication of rejected artifacts | Not implemented for the shared pipeline |
| Shared cycle view does not copy the LAN overlay | Assert the common route has no LAN redirect or raw transcript export; currently selected own detail is retained and missing attempt facts remain unavailable. No change to recorder duties or future eligibility | Not implemented |

## References

- [ADR-036](ADR-036-one-dashboard-two-places.md): existing publication decision and bounded own-content amendment. [ADR-049](ADR-049-review-before-effective-ADR-acceptance.md): acceptance procedure.
- [Dashboard input and rendering](https://github.com/ozand/eeebot-ops-dashboard/blob/f0cad4372637f25f5dec1a15180662fbb1a6193f/scripts/techtree_viewer.py): `read_local_state`, `render_public_pages`, `publish_to_pages`.
- [Dashboard projection and sink routing](https://github.com/ozand/eeebot-ops-dashboard/blob/f0cad4372637f25f5dec1a15180662fbb1a6193f/scripts/two_sinks.py): `split_render_inputs`, `render_private_pages`, `_add_private_cycle_navigation`, `publish_ordered`.
- [Private detail builder](https://github.com/ozand/eeebot-ops-dashboard/blob/f0cad4372637f25f5dec1a15180662fbb1a6193f/scripts/cycle_detail.py): `build_cycle_index`, `render_cycle_page`.
- [#2191 experiment receipt](https://github.com/ozand/eeebot/issues/2191#issuecomment-7026511491); dashboard [#430](https://github.com/ozand/eeebot-ops-dashboard/issues/430) and [#369](https://github.com/ozand/eeebot-ops-dashboard/issues/369).
