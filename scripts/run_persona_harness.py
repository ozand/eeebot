#!/usr/bin/env python3
"""Run the persona self-consistency harness — #1771.

On demand, by the operator. Never per cycle, never in a deploy gate: this
spends real calls on the one local model and answers a question about the
character definition, not about a release's health.

    # what a run would cost, without spending it
    python3 scripts/run_persona_harness.py --dry-run

    # baseline against the current release files
    python3 scripts/run_persona_harness.py

    # baseline plus one ablation per persona block
    python3 scripts/run_persona_harness.py --ablate-all --max-calls 800

Credentials come from the environment, as everywhere else in this repository:

    LITELLM_BASE_URL   the operator's gateway, e.g. http://host:4001/v1
    LITELLM_API_KEY    the gateway key

Neither is read from disk and neither is printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanobot.runtime.persona_harness import (  # noqa: E402
    DEFAULT_MAX_CALLS,
    DEFAULT_REPEATS,
    DEFAULT_STATEMENTS_PATH,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    PersonaHarnessError,
    ablation_candidates,
    assemble_prompt,
    format_report,
    litellm_client,
    load_statements,
    plan_calls,
    report_to_dict,
    run,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persona self-consistency regression set and ablation (#1771).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--statements", default=str(DEFAULT_STATEMENTS_PATH),
        help="statement set JSON (default: persona/statements.json)",
    )
    parser.add_argument(
        "--release-root", default=str(REPO_ROOT),
        help="directory holding the release-owned context files (default: this repository)",
    )
    parser.add_argument(
        "--workspace", default="",
        help="loop workspace for the workspace-owned block; default is an empty "
             "directory, which renders that block as missing (reported in the output)",
    )
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                        help=f"answers sampled per statement (default: {DEFAULT_REPEATS})")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE,
                        help=f"sampling temperature (default: {DEFAULT_TEMPERATURE}); repeats at "
                             "temperature 0 measure nothing about sampling noise")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-call timeout in seconds")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="completion cap per answer (default: the harness role's, 8192). The answer is one "
                             "word, but this model reasons before it: a cap too low truncates the thinking and "
                             "the reply parses as no-signal.")
    parser.add_argument("--model", default="",
                        help="model to ask (default: the executor role's). Off the host, where the preset "
                             "environment is not loaded, that role falls back to a built-in default which is "
                             "NOT the model the loop runs — name it explicitly and check the model line in "
                             "the report.")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                        help=f"refuse to start above this many model calls (default: {DEFAULT_MAX_CALLS})")
    parser.add_argument("--ablate", action="append", default=[], metavar="BLOCK",
                        help="remove one release block and re-run; repeatable")
    parser.add_argument("--ablate-all", action="store_true",
                        help="ablate every release block in turn")
    parser.add_argument("--dry-run", action="store_true",
                        help="assemble the prompt, print the plan and the call count, make no calls")
    parser.add_argument("--json", default="", metavar="PATH", help="also write the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        statement_set = load_statements(args.statements)
    except PersonaHarnessError as exc:
        print(f"persona-harness: {exc}", file=sys.stderr)
        return 2

    ablations = list(dict.fromkeys(list(ablation_candidates()) if args.ablate_all else args.ablate))
    unknown = [b for b in ablations if b not in ablation_candidates()]
    if unknown:
        print(
            f"persona-harness: unknown block(s) {', '.join(unknown)}; "
            f"ablatable blocks: {', '.join(ablation_candidates())}",
            file=sys.stderr,
        )
        return 2

    # Default workspace: an empty temp directory, so the workspace-owned block
    # renders as missing and the run is reproducible on any checkout. The
    # prompt's `sections` map in the output says which blocks were present.
    workspace = Path(args.workspace) if args.workspace else Path(tempfile.mkdtemp(prefix="persona-ws-"))

    planned = plan_calls(statement_set, args.repeats, ablations)

    if args.dry_run:
        prompt = assemble_prompt(args.release_root, workspace)
        sections = prompt.fit.get("sections") or {}
        print("# Persona harness — dry run (no model calls)")
        print()
        print(f"statements       {len(statement_set.behaviours)} behavioural + {len(statement_set.controls)} controls")
        print(f"repeats          {args.repeats}")
        print(f"ablations        {', '.join(ablations) if ablations else 'none'}")
        print(f"planned calls    {planned}  (budget {args.max_calls})")
        print(f"prompt           {prompt.chars} chars")
        print("sections         " + ", ".join(f"{k}={v}" for k, v in sorted(sections.items())))
        if planned > args.max_calls:
            print()
            print("OVER BUDGET — the run would refuse to start.", file=sys.stderr)
            return 1
        return 0

    client = litellm_client(
        model=args.model, temperature=args.temperature,
        max_tokens=args.max_tokens, timeout=args.timeout,
    )

    try:
        baseline, deltas = run(
            client,
            release_root=args.release_root,
            workspace=workspace,
            statement_set=statement_set,
            repeats=args.repeats,
            ablations=ablations,
            max_calls=args.max_calls,
        )
    except PersonaHarnessError as exc:
        print(f"persona-harness: {exc}", file=sys.stderr)
        return 2

    print(format_report(baseline, deltas))

    if args.json:
        Path(args.json).write_text(
            json.dumps(report_to_dict(baseline, deltas), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\njson written to {args.json}")

    return 1 if baseline.plumbing_broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
