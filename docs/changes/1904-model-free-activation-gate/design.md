# Design: Model-free bridge activation gate

## Approach

`python -m nanobot.runtime.activation_check` is a deterministic self-check entry point. It imports the candidate bridge, strictly validates JSON with `Config.model_validate` (not `load_config`, which silently defaults after invalid config), creates a local tool registry, dispatches one no-op tool step, writes a `bridge_activation_self_check` ledger row, and verifies required state-write evidence. It never constructs a provider, manager, or model route. Failure exits nonzero.

A dedicated oneshot unit carries the bridge EnvironmentFiles, user, sandbox, resource ceilings, and state write path. Before `current` changes, the deploy supplies candidate `RELEASE_ROOT`/mode through systemd manager environment and starts this unit; systemd retains its EnvironmentFile parsing without shell re-serialization. Self-check failure flows into existing `die`/ERR rollback. Then `current` flips and the bridge unit starts the real cycle. `classify_bridge_run` distinguishes timeout: deployment invokes the same recorder module directly as `eeepc-agent` with candidate `PYTHONPATH` and the bridge's state environment; its ledger and marker say `inconclusive` / behavior not measured. Output says the release remains active. Other cycle failures hit rollback. The activation check runs pre-flip, so only the real cycle is in the post-flip health-gate window.

## Affected components

- `nanobot/runtime/activation_check.py` — model-free self-check and behavioral-timeout event recorder.
- `host/eeepc/scripts/deploy_release.sh` — pre-flip self-check, post-flip cycle classification and timeout recording.
- `host/eeepc/scripts/lib_bridge_exit.sh` — timeout outcome class beside existing run classification.
- `host/eeepc/systemd/eeepc-self-evolving-activation-check.service` — candidate self-check and timeout recorder unit.
- `tests/test_deploy_activation_exit_class.py` — rollback and no-model assertions.
- `docs/changes/1904-model-free-activation-gate/` and `docs/specs/host-runtime/spec.md` — capability decision and spec delta.

## Trade-offs / alternatives considered

Using a model-backed restart as the activation criterion is rejected because queue delay is not a release-health signal. `load_config()` is rejected for self-check because it logs validation errors then silently falls back to defaults. Merely importing modules is insufficient: it does not validate config, state writes, or tool dispatch. No real tool with external effects is invoked; a minimal deterministic no-op exercises registry dispatch. Starting the recorder synchronously as the timed-out oneshot's successor is rejected because systemd will wait for the 55-minute timer interval; it is queued immediately and asynchronously instead.

## Verification

Named tests cover invalid import, invalid config, healthy self-check with a fail-on-model stub, and deploy rollback/continue decision. Run `python -m pytest tests/ --timeout=300` for this shared deploy/runtime module.

## Spec delta

`docs/changes/1904-model-free-activation-gate/specs/host-runtime/spec.md` adds model-free activation verification and distinguishes it from post-flip real-cycle behavior classification.
