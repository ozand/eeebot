# surf_consilium

`surf_consilium` — standalone orchestration project for a bounded multi-model
council. It is a skill layered on top of [`surf-cli`](https://github.com/ozand/surf-cli):
Surf provides browser automation and authenticated web-chat transport, while this
project defines the council protocol, artifacts, status tracking, and synthesis
workflow.

> The project is intended for advisory analysis. It does not autonomously change
> ERP/WMS data, publish content, approve transactions, or make regulated decisions.

## Prerequisites

Install and configure **`surf-cli` before using this project**. A browser-based
council also requires:

- a Chromium-based browser with the Surf extension enabled;
- active authenticated sessions for the chat providers you plan to use;
- a single, verified Surf browser/profile per Windows automation session;
- a working proxy/browser route when the target provider requires one.

Verify the installation:

```bash
surf --version
surf doctor
surf --help-full
surf chatgpt --help
surf gemini --help
```

Claude is handled through browser UI automation because Surf does not necessarily
provide a dedicated `surf claude` command. Verify the installed Surf version and
available commands instead of assuming a subcommand exists.

### Windows profile routing

Surf uses a shared native-messaging endpoint by default. Before automation, verify
that Surf controls the intended dedicated browser profile:

```bash
surf doctor
surf tab.list --json
```

`surf doctor` being successful is not sufficient: inspect the actual tab URLs and
titles. Do not run the same Surf extension concurrently in Yandex Browser and
Chrome when using the default Windows pipe. For a proxy session, launch the
project's dedicated Chrome profile using the approved proxy launcher, then verify
the tab inventory and route.

## Quick start

The current implementation is deliberately conservative and file-oriented:

```bash
python -m surf_consilium.council --question "Compare approaches to a business problem"
```

If using the scripts directly:

```bash
python scripts/run_council.py --question-file question.txt --output-dir .council/run-001
```

The runner creates an artifact directory containing:

```text
manifest.json
question.md
stage1_<provider>.md
stage2_<provider>.md
stage3_final.md
```

A run is `COMPLETE` only when every required stage has a verified provider result.
Otherwise the manifest must report `PARTIAL_*` or `FAILED`; a helper error is not a
model response.

## Protocol

1. **Stage 1 — independent opinions.** Send the same neutral question to each
   selected provider without exposing peer answers.
2. **Stage 2 — blind peer review.** Anonymize the first-stage answers as A/B/C and
   ask each provider to identify strengths, weaknesses, unsupported claims, and
   trade-offs.
3. **Stage 3 — chairman synthesis.** Produce a final memo that distinguishes
   consensus, disagreement, evidence gaps, owner decisions, and actionable next
   steps.

The coordinator must preserve full raw answers, record bounded summaries separately,
and identify missing/failed providers. Large evidence must not be inserted into a
Windows command-line argument. Use files, stdin where supported, or bounded browser
input. Validate prompt and output sizes before dispatch.

## Safety boundaries

- Never put credentials, cookies, API keys, private keys, or `.env` contents into
  prompts, artifacts, logs, or commits.
- Treat public chat sessions as an external data boundary. Do not send confidential
  business, personal, patient, regulated, or commercially sensitive data without an
  explicitly approved provider and data-processing route.
- Keep ERP/WMS/CRM integrations read-only until separately reviewed. State-changing
  actions require an explicit human approval gate.
- Do not use an LLM as the system of record for prices, stock, batches, FEFO, marking,
  shipment state, financial postings, or regulatory transactions.
- Treat model output as advisory and verify legal, financial, medical, and regulatory
  claims against authoritative sources.

## Project layout

- `SKILL.md` — agent-facing usage and protocol instructions.
- `AGENTS.md` — local development and agent safety rules.
- `scripts/` — executable orchestration and provider adapters.
- `tests/` — deterministic tests that do not require live browser sessions.

## Development

From the repository root:

```bash
python -m pytest surf_consilium/tests -q
python -m py_compile surf_consilium/scripts/*.py
```

Live provider checks are manual/integration checks and require authenticated browser
sessions. They must not run in unit-test or CI jobs by default.
