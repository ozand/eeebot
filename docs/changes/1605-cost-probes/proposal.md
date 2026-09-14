# Change: measured cost probes for the eeepc cycle

- **change-id:** 1605-cost-probes
- **issue:** #1605
- **capability:** `docs/specs/host-runtime/spec.md`, `docs/specs/observability/spec.md`
- **role / workstream:** runtime / eeepc observability

## Problem

The daily host capability inventory records whether hardware is present, but it
cannot say what one eeebot cycle costs its own process, whether the framebuffer
transfer is affordable, or whether native builds fit the machine. Host-wide
metrics answer a different question and cannot be substituted for process
attribution.

## Intended change

Add a stdlib-only `scripts/cycle_cost_probe.py` used by the existing
`refresh_host_capabilities()` writer. It provides:

- own-process CPU tick delta and peak `VmHWM`, with cycle ID and sampling window;
- thermal-zone and kernel thermal-throttle counter snapshots explicitly marked
  as sampled during a cycle;
- permanent `probe_unavailable` battery result with the host-observed AC-only
  reason;
- framebuffer geometry-derived full-surface byte cost and an injectable/device
  push timer;
- explicitly opt-in C hello-world peak-RSS and Cargo completion probes;
- four-state records with `value: null` for unavailable measurements.

The daily timer does not run a build, cargo build, framebuffer write, or loaded
cycle. Those operations require an explicit caller and are not part of this
implementation run.

## Acceptance

- [x] New records use `present`, `absent`, `present_uninitialized`, or
      `probe_unavailable` and retain a numeric `value` only when measured.
- [x] Body readings are process-attributed and include the cycle ID.
- [x] Thermal and throttle readings identify an active-cycle sampling window.
- [x] Battery is permanently unavailable with a recorded reason, never zero.
- [x] Framebuffer cost is computed from 1024×600×32bpp as 2,457,600 bytes.
- [x] Toolchain load probes are explicit opt-in operations and are unavailable
      in the non-authorized daily refresh.
- [x] Existing `host_capabilities.json` writer is extended without a new sink.

## Out of scope

- Running a loaded cycle, C build, Cargo build, or framebuffer write on the host.
- Re-checking the operator's already-recorded compiler, battery, thermal, and
  framebuffer readings.
- Changing `collect_host_metrics` or adding consumers, tech-tree logic, or
  on-screen rendering.
