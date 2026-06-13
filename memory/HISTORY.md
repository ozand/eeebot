# History

- 2026-06-13: Implemented the **exploitation loop** (exploitation lane). When a subagent successfully implements and pushes changes, the coordinator shifts to `exploit-successful-improvement-path` to continue building on this path.
- 2026-06-13: Fixed subagent summary regex to support YAML-like indentation in Plain-text.
- 2026-06-13: Fixed `_subagent_lane_health` to scan checkout directory telemetry.
- 2026-06-13: Integrated telemetry result scanning directly into `_derive_reward_signal` to prevent early discard of verification contracts.
