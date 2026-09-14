# Architecture

## Boundary

The project is a protocol and artifact layer above `surf-cli`:

```text
Question
   |
   v
Council protocol (this project)
   |-- stage/status/manifest
   |-- anonymization and bounded evidence
   |-- synthesis and reporting
   |
   +--> surf-cli transport
          |-- ChatGPT browser session
          |-- Gemini browser session
          +-- Claude browser UI session
```

`surf-cli` owns browser connectivity, provider-specific interaction, and
authentication cookies. `surf_consilium` must not inspect or persist cookies or
provider credentials.

## Stages

- Stage 1 creates independent first opinions.
- Stage 2 reviews anonymized successful Stage 1 artifacts.
- Stage 3 synthesizes the available evidence and explicitly reports degradation.

The coordinator must not silently substitute a helper error for a provider answer.
Every provider/stage result is recorded in `manifest.json`.

## Artifact and transport design

Full answers are file-backed. Bounded review material is a derived artifact and
must identify omissions. Large evidence must never be embedded in a Windows process
command line. Provider adapters should use argv arrays, avoid `shell=True`, and use
stdin/file transport only after verifying that the installed Surf version supports it.

## Browser routing

Windows installations commonly expose one shared Surf native-messaging endpoint.
The runtime therefore treats browser/profile selection as a precondition, not an
implementation detail. `surf doctor` validates host health but not semantic routing;
`surf tab.list --json` must be checked for the expected browser/profile inventory.

## Future adapter contract

A live adapter should expose:

```python
class ProviderAdapter(Protocol):
    name: str
    def preflight(self) -> None: ...
    def send(self, prompt: str) -> str: ...
    def extract(self) -> str: ...
```

It must distinguish `not_configured`, `transport_failed`, `generation_incomplete`,
`extraction_failed`, `empty_response`, and `success`. Retries are bounded and only
allowed for known transient failures.
