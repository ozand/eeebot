# Agent Instructions — `surf_consilium`

## Scope

`surf_consilium/` is a standalone advisory council project layered on top of
`surf-cli`. It coordinates independent opinions, bounded peer review, and a final
synthesis across browser-based AI chats. It is not part of the eeebot runtime and
must not modify `nanobot/`, runtime state, ERP/WMS systems, or production data.

## Required dependency

**`surf-cli` must be installed and available on `PATH` before this project can run.**
A Chromium browser with the Surf extension and authenticated provider sessions are
also required for live calls. Check the actual installed interface before use:

```bash
surf --version
surf doctor
surf --help-full
surf chatgpt --help
surf gemini --help
```

Do not assume a `surf claude` command or a generic extraction command exists. Claude
must use the supported browser UI operations or a verified provider adapter.

## Browser and proxy safety

- On Windows, the default Surf native-messaging endpoint is shared by Chromium
  browsers. Verify `surf tab.list --json` after startup and before every council run.
- If unrelated tabs appear, stop: Surf may be attached to the wrong browser/profile.
- Do not run the Surf extension concurrently in ordinary Chrome, dedicated proxy
  Chrome, Yandex Browser, or another Chromium profile when using the same default
  endpoint.
- Never print or persist proxy credentials, cookies, API keys, raw IP evidence, or
  `.env` contents.
- A successful `surf doctor` proves host health, not correct browser/profile routing.

## Council protocol

The runner must model these states explicitly:

```text
COMPLETE
PARTIAL_STAGE1
PARTIAL_STAGE2
CHAIRMAN_ONLY
FAILED
```

Never claim a complete council when one required provider or stage is missing. A
provider/helper error is not an opinion and must remain an error in the manifest.
Preserve full provider outputs, but create bounded review inputs separately.

### Stage 1

- Send the neutral question independently to each selected provider.
- Do not include other providers' answers.
- Record provider status, timestamps, and sanitized error details.

### Stage 2

- Anonymize answers as Response A/B/C.
- Mark truncation explicitly if bounded summaries are needed.
- Ask each available provider to critique the same evidence.
- Do not infer completion from a confirmation message; require a substantive answer.

### Stage 3

- Include consensus, disagreements, evidence gaps, confidence, and owner decisions.
- State missing provider reviews and downgrade the run status accordingly.
- Do not present unverified ROI, legal, medical, security, or regulatory claims as
  established facts.

## Data handling

- Public chat is an external data boundary. Default to synthetic or sanitized data.
- Do not send confidential customer, patient, employee, financial, contract, or
  regulated data without explicit approval of the data-processing route.
- Keep the LLM advisory: ERP/WMS/CRM remain systems of record.
- Any future write-capable integration requires a separate issue, design review,
  approval gate, audit trail, and tests.

## Tool and transport rules

- Never pass large prompts or peer transcripts as Windows command-line arguments.
- Prefer file-backed input, stdin if verified by the installed CLI, or bounded browser
  input. Keep each tool call and process argument below the environment's limit.
- Avoid `shell=True`; use argv arrays and explicit encoding.
- Check provider-specific help before writing an adapter.
- After every send, inspect the actual tab/page state and save the extracted response.
- Keep browser automation and protocol orchestration as separate modules.
- Unit tests must be deterministic and must not require live login sessions.

## Verification

Run project-scoped checks only:

```bash
python -m pytest surf_consilium/tests -q
python -m py_compile surf_consilium/scripts/*.py
```

For live checks, record the exact provider, browser/profile verification, command
receipt, stage statuses, and output artifact paths. Do not include secrets or raw
session data in the receipt.

## Change discipline

- Keep changes confined to `surf_consilium/` unless the governing issue explicitly
  authorizes another path.
- Use one branch per task and preserve unrelated working-tree changes.
- Inspect `git diff` and project-scoped test results before reporting completion.
- User-facing explanations should be in Russian; code, agent instructions, and
  documentation in this directory should remain in English.
