# Issue #53 — OpenSpace Local-Only Sidecar Fit Audit

Date: 2026-04-22 UTC

## Bottom line

OpenSpace is a fit only as an external, bounded MCP sidecar that supplies specialist tool execution / skill work.
It is not a fit as a replacement for eeebot’s runtime, control plane, policy layer, or source-of-truth surfaces.

The current repo docs already define eeebot as the place for:
- stable startup and restart
- bounded resource usage
- truthful capability reporting
- durable follow-up and reporting
- canonical/evidence separation
- portable baseline assumptions
- simulator-backed regression coverage

So the architectural boundary should be:
- OpenSpace = specialist sidecar, producing candidate outputs over MCP
- eeebot = authority, validation, budgeting, persistence, promotion, dashboard, and operator control

## Bounded fit / gap matrix

| Area | OpenSpace as local-only sidecar | Keep in eeebot | Do not delegate to OpenSpace | Fit / gap note |
|---|---|---:|---:|---|
| MCP tool execution | Yes, for bounded specialist tasks | No | No | Strong fit if OpenSpace is treated as a separate bounded tool provider, not a core runtime. |
| Skill / artifact generation | Yes, for candidate artifacts and helper outputs | Partially | Yes, for acceptance and publication | Good fit for generating outputs that eeebot can evaluate. |
| Control-plane authority | No | Yes | Yes | eeebot must remain the authority for approvals, task selection, and state transitions. |
| Validation / truthfulness | No | Yes | Yes | eeebot already owns truthful capability reporting and should keep that role. |
| Budget / memory / prompt discipline | No | Yes | Yes | Weak-host discipline belongs in eeebot; sidecar should only consume bounded requests. |
| Source pinning / canonical state | No | Yes | Yes | Promotion, evidence, and source-of-truth handling stay in eeebot. |
| Dashboard / operator surfaces | No | Yes | Yes | UI, reporting, and operator-facing state should remain in eeebot. |
| Heavy compute / long-running work | Limited, only if externally hosted | Prefer eeebot orchestration | Yes, if it impacts host reliability | On the weak host, this is a gap/risk; do not make eeebot depend on it. |
| Cloud/community OpenSpace path | No | No | Yes | Explicitly out of scope for this issue and should remain excluded. |
| Replacing eeebot runtime | No | Yes | Yes | Not a fit; this would violate the current architecture and policy boundaries. |

## What OpenSpace should do

OpenSpace should be used only for:
- bounded MCP-served specialist tasks
- tool/skill execution that returns candidate outputs
- optional external compute that eeebot can review
- non-authoritative assistance that can fail without breaking eeebot

## What must stay in eeebot

eeebot must retain:
- runtime and control-plane authority
- task orchestration and scheduling
- validation hooks and policy gates
- capability reporting
- source pinning / canonical-vs-evidence separation
- prompt, memory, and budget discipline
- owner utility logic and dashboard surfaces
- promotion / rollback / audit trails

## What must not be delegated

Do not delegate to OpenSpace:
- approval decisions
- promotion decisions
- authoritative state writes
- truth claims about eeebot’s current capabilities
- weak-host safety enforcement
- budget accounting
- dashboard publication
- canonical artifact selection
- runtime replacement or bootstrapping of eeebot itself

## Risk / gap summary

The main gap is not conceptual; it is operational:
- the weak-host runtime currently lacks a working local `mcp` dependency path
- the documented blocker is dependency-chain failure around `cryptography` on the i386 host
- therefore a direct local-host OpenSpace MCP integration is blocked unless a host-compatible dependency strategy or stronger-sidecar bridge exists

## Recommended next action for issue #53

Conditional go, not full go.

Next step:
1. freeze the boundary as a narrow sidecar contract,
2. keep all authority and verification in eeebot,
3. choose one transport strategy for the sidecar path:
   - host-compatible `mcp`, or
   - a stronger external bridge,
4. then implement only one read-only / low-risk OpenSpace-backed capability as a proof point.

If the transport cannot be made reliable on the weak host, close the issue as no-go for local-only execution and keep OpenSpace as an external optional dependency only.
