"""Safe, file-oriented council runner skeleton.

Provider transport is intentionally injected so unit tests do not require live
browser sessions. A production adapter must implement the Provider protocol and
must preserve provider errors as errors rather than model responses.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol


PROVIDERS = ("chatgpt", "gemini", "claude")


class Provider(Protocol):
    def __call__(self, prompt: str) -> str: ...


@dataclass
class Result:
    provider: str
    stage: str
    status: str
    artifact: str | None = None
    error: str | None = None


def _safe_slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in value).strip("-")[:80]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _call(provider: str, stage: str, prompt: str, output: Path, adapters: Mapping[str, Provider]) -> Result:
    adapter = adapters.get(provider)
    if adapter is None:
        return Result(provider, stage, "not_configured", error="provider adapter unavailable")
    try:
        answer = adapter(prompt)
    except Exception as exc:  # adapters must not make failures look like answers
        return Result(provider, stage, "failed", error=f"adapter_error: {type(exc).__name__}")
    if not answer.strip():
        return Result(provider, stage, "failed", error="empty_response")
    _write(output, answer)
    return Result(provider, stage, "success", artifact=str(output))


def run_council(
    question: str,
    output_dir: Path,
    adapters: Mapping[str, Provider],
    providers: tuple[str, ...] = PROVIDERS,
) -> dict:
    """Run a protocol skeleton with injected provider adapters.

    The default adapters are empty by design. Live Surf transport belongs in a
    separate adapter module and must be explicitly configured by the caller.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "question.md", question)
    results: list[Result] = []

    for provider in providers:
        results.append(_call(provider, "stage1", question, output_dir / f"stage1_{provider}.md", adapters))

    stage1_ok = [r for r in results if r.stage == "stage1" and r.status == "success"]
    if not stage1_ok:
        completion = "FAILED"
    else:
        # Keep the first implementation conservative: peer-review orchestration
        # is explicit and bounded; no raw prompt construction is hidden here.
        labels = {chr(65 + i): Path(r.artifact).read_text(encoding="utf-8") for i, r in enumerate(stage1_ok)}
        review_prompt = "\n\n".join(f"Response {label}:\n{text}" for label, text in labels.items())
        review_prompt = (
            "Review the anonymized responses. Identify agreement, disagreement, "
            "unsupported claims, risks, and a corrected recommendation.\n\n" + review_prompt
        )
        for provider in providers:
            results.append(_call(provider, "stage2", review_prompt, output_dir / f"stage2_{provider}.md", adapters))
        stage2_ok = [r for r in results if r.stage == "stage2" and r.status == "success"]
        completion = "COMPLETE" if len(stage2_ok) == len(providers) else "PARTIAL_STAGE2"

    manifest = {
        "version": 1,
        "created_at": int(time.time()),
        "providers": list(providers),
        "completion": completion,
        "results": [asdict(r) for r in results],
    }
    _write(output_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded surf-consilium protocol skeleton")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--question")
    source.add_argument("--question-file", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".council/run"))
    args = parser.parse_args()
    question = args.question if args.question is not None else args.question_file.read_text(encoding="utf-8")
    manifest = run_council(question, args.output_dir, adapters={})
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["completion"] != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
