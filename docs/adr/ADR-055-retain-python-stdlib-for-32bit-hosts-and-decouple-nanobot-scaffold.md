---
title: Prefer demonstrated portability over framework or language allegiance
status: proposed
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2177", "#619"]
tags: [architecture, portability]
---

# Status

Proposed; no platform migration or permanent Python commitment is authorized.

# Context

The owner selected nanobot as a quick prototype scaffold, not a permanent architecture, and explained that Python fit the then-considered weak/legacy 32-bit target better than the TypeScript option under consideration. This records the selection rationale, not a verified compatibility matrix, a claim about all JavaScript runtimes, or comparative performance evidence for Rust or any other platform.

# Decision

Keep the current implementation while evaluating the bounded durability proposals; this is not a permanent language/framework selection. Python and nanobot remain replaceable. Any proposed alternative must identify the actual target(s), verify installation and compatibility there, and compare operational simplicity, resource use, migration cost, and measured benefit against the current baseline before a separate decision authorizes migration. Preserve #619 import and dual-CLI compatibility unless a separate authorized decision changes it. Durability invariants should be specified independently of framework-specific APIs.

# Consequences

Existing delivery remains simple; alternative platforms require evidence rather than allegiance. Maintaining compatibility costs effort. Security, host activation and runtime authority remain unchanged.

# Alternatives considered

Permanent Python lock-in and immediate wholesale replacement are both rejected: neither follows from the owner's explanation or demonstrated need.

# Test Contract

This proposal alone authorizes no platform migration and imposes no runtime gate. Any future replacement issue must define target-specific installation/compatibility evidence and a measured comparison before migration is considered. Existing import tests do not prove portability. The proposed durability work may use the current Python implementation as an experiment without making Python a permanent requirement. This ADR remains Proposed; acceptance is deferred.

# References

- #2177; #619; docs/specs/migration/spec.md.
