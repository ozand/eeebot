# Design

The complete security decision, profile fields, preserved path behavior, alternatives, rollback, and falsifiable tests are in `docs/adr/ADR-044-planner-tool-capabilities-are-explicit-and-minimal.md` (Proposed). This change folder does not duplicate those details.

Implementation uses two immutable built-in profiles at `nanobot/agent/tools/toolsets.py`; `SubagentManager` resolves and validates them before spawn. Its registration branches only over that profile's declared tool set, derives `allowed_dir` from the selected profile (legacy `restrict_to_workspace` applies only to the executor compatibility profile), and appends profile-generated allowed-actions text for explicit role prompts. The planner bridge selects `planner`; executor callers default to `executor`.

No data-path widening or restriction is introduced. Planner absolute paths remain accepted exactly as before; this is not an OS sandbox. Plan output remains final JSON and the bridge remains the diary/ledger writer.
