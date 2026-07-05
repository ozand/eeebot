# Model Routing — spec

_Status: current. Last updated: 2026-06-25._

## Purpose

Model routing is the minimal task-type routing layer in front of the main agent
runtime. It classifies each inbound turn into one of three task types and selects
the model from that type's fallback list, keeping a single provider instance and
switching only the `model` parameter per turn. It exists so the runtime can use a
cheap general model for chat, a vision model for media, and the mandatory local
code model for engineering work — without a separate provider per task.

## Requirements

### Task types and routing
- R1. The router SHALL classify every turn into exactly one task type:
  `general`, `code`, or `vision`.
- R2. Each task type SHALL resolve a model from an ordered fallback list, and the
  router SHALL keep one provider instance, switching only the `model` parameter
  per turn (no per-task provider split).
- R3. Every routed model name SHALL carry a gateway prefix (`cl/`, `an/`, or
  `un/`); a bare model name SHALL NOT be routed.
- R4. The `code` task type SHALL route to the mandatory local executor model
  `un/qwen3.6-27b-mtp`, and `un/qwen3.6-27b-mtp` SHALL be the only model in the
  `code` fallback list. The same model SHALL be used as the `code` executor
  override after the first tool-calling response.
- R5. The `general` fallback order SHALL be `gemini-3.5-flash-low` →
  `gemini-3-flash` → `gpt-5.4`; the `vision` fallback SHALL be
  `gemini-3.1-flash-image`. The `general` executor override SHALL be
  `gemini-3.5-flash-low`; the `vision` executor override SHALL be
  `gemini-3.1-flash-image`.

### Detection
- R6. A turn SHALL be classified `vision` when the inbound message has media
  attachments.
- R7. A turn SHALL be classified `code` when the text strongly suggests coding
  work (e.g. `pytest`, `pip`, `npm`, `git`, code fences, file extensions);
  otherwise it SHALL be classified `general`.

### Fallback
- R8. Fallback to the next model SHALL trigger only on model-availability-style
  failures: key not allowed, model not found, unsupported model, access denied,
  invalid model name.
- R9. The router SHALL NOT fall back on arbitrary errors, so prompt/runtime bugs
  are not hidden behind silent model swaps.

### Live constraint
- R10. A model SHALL NOT be added to active routing without a fresh successful
  `/chat/completions` probe for the live key; presence in `/v1/models` alone SHALL
  NOT qualify a model. (`qwen3-coder-flash` and `coder-model` were rejected at
  chat-completion time on the live Telegram key and SHALL NOT be re-added without a
  fresh successful probe.)

## Scenarios

### Scenario: coding request routes to the mandatory local model
- Given an inbound text turn containing `pytest` and a code fence
- When the router classifies and routes the turn
- Then the task type is `code` and the model is `un/qwen3.6-27b-mtp`.

### Scenario: media attachment routes to vision
- Given an inbound message with an image attachment
- When the router classifies the turn
- Then the task type is `vision` and the model is `gemini-3.1-flash-image`.

### Scenario: model-availability failure falls back
- Given a `general` turn where `gemini-3.5-flash-low` returns "model not found"
- When the router handles the failure
- Then it retries with `gemini-3-flash`, and if that also fails on availability,
  with `gpt-5.4`.

### Scenario: prompt bug does not trigger fallback
- Given a `general` turn that fails with a non-availability runtime error
- When the router handles the failure
- Then it does not fall back to the next model and the error surfaces.

## References

- Reference doc: `MODEL_ROUTING_FALLBACK_V1.md` was folded into this spec and
  removed 2026-07-05 (#613; recoverable from git history).
- LiteLLM single-source-of-truth config: `/etc/eeepc-agent/litellm.env`
  (see README "LiteLLM configuration").
- Code: `nanobot/config/schema.py` (`modelRouting` config),
  `nanobot/providers/registry.py`, `nanobot/providers/litellm_provider.py`.
- Related specs: `chat-agent-framework`, `host-runtime`.
