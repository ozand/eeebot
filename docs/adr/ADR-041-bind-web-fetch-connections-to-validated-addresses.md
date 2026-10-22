---
title: Bind each web fetch connection to its validated address snapshot
status: proposed
date: 2026-10-22
authors: [eeebot contributors]
related: ["#2107"]
tags: [security, networking, web-fetch]
---

# ADR-041: Bind each web fetch connection to its validated address snapshot

## Status

Proposed — policy approved by owner; this record and its implementation remain unaccepted.

## Context

Issue #2107 identified that web_fetch follows redirects before checking the resulting URL. Current checks resolve names but do not bind the HTTP socket to the checked addresses, leaving resolution and connection as separate operations. This is a source-level ordering/control gap, not evidence of a successful exploit. Owner approved strict direct-only behavior on 2026-10-22.

HTTPX 0.28.1's public AsyncBaseTransport seam can adapt HTTPCore 1.0.9's AsyncConnectionPool with an explicit AsyncNetworkBackend. HTTPX's standard AsyncHTTPTransport does not expose that backend. Direct HTTPCore use therefore needs an explicit bounded dependency, rather than relying on a transitive dependency. The project currently declares HTTPX >=0.28,<1.0 and has no checked-in dependency lockfile. The direct HTTPCore range is bounded to >=1.0.9,<1.1.0 because the adapter uses the reviewed 1.0.9 public API; upgrades beyond that compatibility line need review and transport regression coverage.

## Decision

Each web_fetch hop resolves once, rejects empty, erroneous, malformed, or any blocked address set, then connects only to an address in that immutable validated snapshot. Preserve the URL hostname/authority for HTTP Host, TLS SNI, and certificate verification. Follow redirects manually, validating before the next connection. Use a fresh per-hop pool with retries disabled. Fail closed on explicit/configured proxy routing before DNS or request. Do not use Jina for web_fetch; web_search Jina remains unchanged.

### What this IS

A dedicated HTTPX AsyncBaseTransport adapter backed by explicitly declared, bounded HTTPCore APIs, for web_fetch only.

### What this IS NOT

No URL-to-IP rewriting, private HTTPX pool mutation, proxy support, automatic redirect following, Jina fetch fallback, broad policy change to other network consumers, or live-network test.

## Consequences

### What gets easier

Tests can prove the actual TCP destination is in the validated snapshot while preserving origin identity.

### What gets harder

A small transport adapter and direct HTTPCore dependency add compatibility and maintenance costs; every redirect is handled explicitly and consumes a fresh pool.

### What does not change

web_search provider routing and public web_fetch JSON/error and extraction behavior remain as compatible as possible.

## Alternatives Considered

- HTTPX automatic redirects and post-response validation: rejected because the destination has already been contacted.
- Rewrite URL hostname to a numeric IP: rejected because it changes origin identity and risks Host/SNI/certificate semantics.
- Mutate HTTPX's private pool: rejected because it relies on unsupported internals.
- Do nothing: leaves the ordering and DNS-to-connect gap unresolved.

## Test Contract

| Claim | Test | Status |
|---|---|---|
| Each TCP connection is limited to validated addresses while Host and TLS identity retain the URL origin | `tests/test_web_fetch_security.py::test_transport_pins_address_and_preserves_origin` | passing |
| Empty, failed, malformed, mixed blocked DNS answers fail before connection | `tests/test_web_fetch_security.py::test_resolver_rejects_mixed_empty_and_malformed` | passing |
| Every redirect is validated before connecting | `tests/test_web_fetch_security.py::test_redirect_private_target_is_never_requested` | passing |
| Explicit proxy configuration is rejected before resolution | `tests/test_web_fetch_security.py::test_web_fetch_rejects_proxy_before_dns` | passing |
| Jina fetch fallback cannot cause an outbound request | Not yet written | not yet written |
| Response streams close on success, redirects, and errors | Not yet written | not yet written |
| Resolver rejects prohibited addresses consistently | `tests/test_web_fetch_security.py::test_resolver_rejects_mixed_empty_and_malformed` | passing |

## References

Issue #2107 and owner policy/design comments 6348906705, 6349075771; HTTPX 0.28.1 and HTTPCore 1.0.9 public transport APIs.
