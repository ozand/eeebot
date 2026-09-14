---
name: surf-consilium
description: >
  Run a bounded three-stage advisory council across ChatGPT, Gemini, and Claude
  through surf-cli browser sessions. Use for difficult architecture, product,
  technology, business, risk, or strategy questions requiring independent opinions,
  anonymized peer review, and a final synthesis. This skill is advisory only and
  never performs unapproved state-changing actions.
compatibility: Requires surf-cli on PATH, a Chromium browser with the Surf extension, and authenticated provider sessions.
---

# Surf Consilium

`surf-consilium` is a protocol skill layered on top of `surf-cli`. It does not
replace Surf: `surf-cli` supplies browser automation and provider transport; this
skill supplies the council workflow, artifact discipline, status model, and safety
rules.

## Prerequisites

Before starting:

```bash
surf --version
surf doctor
surf --help-full
surf chatgpt --help
surf gemini --help
```

A live run also requires authenticated browser sessions for the selected providers.
There may be no dedicated Claude subcommand; inspect the installed Surf interface
and use verified browser UI operations for Claude.

On Windows, verify the intended browser/profile with:

```bash
surf tab.list --json
```

`surf doctor` alone does not prove that the correct browser owns the shared native
messaging endpoint. If unrelated tabs appear, stop and repair browser/profile
routing before sending data.

## Three-stage protocol

### Stage 1 — independent opinions

Send the same neutral question independently to each selected provider. Do not
include peer answers. Store each result and status separately.

### Stage 2 — blind peer review

Anonymize successful Stage 1 answers as Response A/B/C. Ask each available provider
to assess strengths, weaknesses, unsupported assumptions, trade-offs, and risks.
Preserve full answers; if evidence must be bounded, create an explicit summarized
copy and mark omissions.

### Stage 3 — chairman synthesis

Give the chairman the original question, available opinions, peer reviews, missing
stage/provider statuses, and evidence limits. The final memo must report consensus,
disagreement, confidence, unresolved questions, and actionable next steps.

## Completion states

Use only these states:

- `COMPLETE` — all required providers completed every required stage.
- `PARTIAL_STAGE1` — one or more required first opinions failed or are missing.
- `PARTIAL_STAGE2` — Stage 1 completed, but one or more peer reviews failed or are missing.
- `CHAIRMAN_ONLY` — a synthesis exists without sufficient council evidence.
- `FAILED` — no usable council result.

Never upgrade a partial run to complete because a model acknowledged the prompt.
A helper error, timeout, Cloudflare page, extraction failure, or browser UI text is
not a model opinion.

## Artifact contract

Each run should be isolated under `.council/<run-id>/` or an explicitly supplied
output directory:

```text
manifest.json
question.md
stage1_<provider>.md
stage2_<provider>.md
stage3_final.md
```

`manifest.json` records provider/stage status, sanitized error categories, truncation
flags, completion state, and artifact paths. Do not store credentials, cookies,
private keys, `.env` contents, or unnecessary personal data.

## Transport constraints

- Never put full peer transcripts into a Windows command-line argument.
- Prefer file-backed input, stdin only when verified for the installed Surf version,
  or bounded browser-native input.
- Do not use `shell=True` in adapters.
- Check `surf <command> --help` before relying on a subcommand.
- After every send, inspect the actual page and tab state. Save the response only
  after confirming a substantive new assistant message and completed generation.
- Use bounded retries only for transient failures; do not repeatedly resend a prompt
  when the page state is unknown.

## Safety boundary

Treat public chat services as an external data boundary. Use sanitized or synthetic
inputs by default. Do not send confidential business, personal, patient, financial,
contract, or regulated data without an approved provider and data-processing route.

The council is advisory. It must not autonomously:

- write ERP/WMS/CRM data;
- approve prices, discounts, orders, shipments, or financial postings;
- submit regulatory transactions;
- publish medical or regulated advertising content;
- make legal, medical, credit, or compliance decisions.

## Reporting

Report:

1. the question and scope;
2. roster and provider availability;
3. stage-by-stage completion status;
4. consensus and disagreements;
5. evidence gaps and confidence;
6. output artifact paths;
7. any failed or degraded steps.

Do not claim that the council independently verified external facts unless an
appropriate source-checking step was actually performed.
