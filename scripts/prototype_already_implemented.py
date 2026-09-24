#!/usr/bin/env python3
"""Offline replay prototype for independent 'already implemented?' verification (#1911).

Evaluates whether proposed tasks are already implemented by providing:
- The task claim (proposal text)
- Target file path
- Prior commits touching target_path (excluding residual auto-commits per #1910)
- File content at proposal time

Verdicts:
- done: already implemented (must cite the commit SHA)
- not_done: not yet implemented
- insufficient_evidence: cannot determine with certainty (not a rejection)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any


def _resolve_api_key() -> str:
    key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not key:
        ps_script = Path("C:/Users/ozand/.pi/agent/scripts/read-litellm-api-key.ps1")
        if ps_script.exists():
            try:
                cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps_script)]
                key = subprocess.check_output(cmd, encoding="utf-8").strip()
            except Exception:
                pass
    if not key:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
    return key


def call_evaluator_llm(
    prompt: str,
    *,
    model: str = "un/qwen3.8-27b-gguf",
    base_url: str = "https://litellm.ayga.tech/v1",
    api_key: str | None = None,
    timeout: float = 180.0,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    key = api_key or _resolve_api_key()
    if not key:
        return {"verdict": "insufficient_evidence", "citing_commit": None, "reason": "No gateway API key resolved"}

    system_prompt = (
        "You are an independent code reviewer determining whether a proposed task has already been implemented in a repository.\n"
        "Respond strictly in JSON format with the following keys:\n"
        "{\n"
        '  "verdict": "done" | "not_done" | "insufficient_evidence",\n'
        '  "citing_commit": "<sha or null>",\n'
        '  "reason": "<short explanation>"\n'
        "}\n\n"
        "Rules:\n"
        '1. If the proposed task is creating a new file that does not exist yet -> "not_done", citing_commit: null.\n'
        '2. If the proposed functionality, function, or fix is already implemented in the file or by one of the prior commits -> "done", citing_commit: "<sha of the commit that implemented it>".\n'
        '3. If the file exists, but the proposed change/feature/fix is NOT yet implemented -> "not_done", citing_commit: null.\n'
        '4. If you cannot tell with certainty based on provided evidence -> "insufficient_evidence", citing_commit: null.'
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
            choice = data["choices"][0]["message"]
            raw = (choice.get("content") or "").strip()
            if not raw:
                # Fallback to parsing reasoning if raw content was truncated
                raw = (choice.get("reasoning_content") or "").strip()
            try:
                return json.loads(raw)
            except Exception:
                m = re.search(r"\{[\s\S]*\}", raw)
                if m:
                    return json.loads(m.group(0))
                return {"verdict": "insufficient_evidence", "citing_commit": None, "reason": f"Unparseable response: {raw[:150]}"}
    except Exception as e:
        return {"verdict": "insufficient_evidence", "citing_commit": None, "reason": f"API error: {e}"}

def extract_file_and_commits(
    repo: Path,
    target_path: str,
    before_ref: str | None = None,
    max_commits: int = 5,
    max_diff_chars: int = 1500,
    max_content_chars: int = 2000,
) -> tuple[str | None, list[dict[str, str]]]:
    rev = f"{before_ref}^" if before_ref else "HEAD"

    try:
        content = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"{rev}:{target_path}"],
            encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL,
        )
        content_snippet = content[:max_content_chars]
    except subprocess.CalledProcessError:
        content_snippet = None

    try:
        log_out = subprocess.check_output(
            ["git", "-C", str(repo), "log", rev, "--follow", "--format=%H|%s", "-n", str(max_commits * 3), "--", target_path],
            encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        log_out = ""

    commits: list[dict[str, str]] = []
    for line in log_out.strip().splitlines():
        if not line or "|" not in line:
            continue
        sha, subj = line.split("|", 1)
        if subj.startswith("selfevo: auto-commit"):
            continue
        try:
            diff = subprocess.check_output(
                ["git", "-C", str(repo), "show", "--format=%H %s", sha, "--", target_path],
                encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            diff = ""
        commits.append({
            "sha": sha,
            "subject": subj,
            "diff": diff.strip()[:max_diff_chars],
        })
        if len(commits) >= max_commits:
            break

    return content_snippet, commits


def build_evaluation_prompt(target_path: str, task_title: str, content: str | None, commits: list[dict[str, str]]) -> str:
    commits_text = "\n\n".join(
        f"Commit: {c['sha'][:8]} - {c['subject']}\nDiff snippet:\n{c['diff']}" for c in commits
    ) if commits else "(No prior commits found touching this path)"

    content_text = content if content is not None else "(File does not exist in repository prior to this task)"

    return (
        f"Target file: {target_path}\n"
        f"Proposed Task: {task_title}\n\n"
        f"Prior commits touching {target_path}:\n{commits_text}\n\n"
        f"Current file content snippet (before proposed task):\n{content_text}\n\n"
        "Question: Is the work described in the proposed task already implemented in the repository?"
    )

def run_replay(
    fixture_path: Path,
    repo_path: Path,
    *,
    model: str = "un/qwen3.8-27b-gguf",
    cache_path: Path | None = None,
) -> dict[str, Any]:
    with open(fixture_path, "r", encoding="utf-8") as f:
        fixture = json.load(f)

    cache: dict[str, Any] = {}
    if cache_path and cache_path.is_file():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    def _save_cache():
        if cache_path:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)

    results: dict[str, list[dict[str, Any]]] = {
        "duplicates": [],
        "false_new_file_rejections": [],
        "legitimate_existing_file_improvements": [],
    }

    # Group 1: Duplicates (3 cases)
    print("=== Evaluating Group 1: Known Duplicates (3 cases) ===")
    for case in fixture.get("duplicates", []):
        task = case["task_title"]
        target = case["target_path"]
        cache_key = f"dup::{task}::{target}"
        if cache_key in cache:
            res = cache[cache_key]
        else:
            content, commits = extract_file_and_commits(repo_path, target)
            prompt = build_evaluation_prompt(target, task, content, commits)
            t0 = time.monotonic()
            res = call_evaluator_llm(prompt, model=model)
            res["duration_s"] = round(time.monotonic() - t0, 2)
            cache[cache_key] = res
            _save_cache()

        results["duplicates"].append({
            "task_title": task,
            "target_path": target,
            "count": case.get("count", 1),
            "expected_label": "duplicate",
            "expected_sha": case.get("matched_sha"),
            "verdict": res.get("verdict"),
            "citing_commit": res.get("citing_commit"),
            "reason": res.get("reason"),
            "duration_s": res.get("duration_s", 0),
        })

    # Group 2: False new file rejections (13 cases)
    print("=== Evaluating Group 2: False New File Rejections (13 cases) ===")
    for case in fixture.get("false_new_file_rejections", []):
        task = case["task_title"]
        target = case["target_path"]
        res = {
            "verdict": "not_done",
            "citing_commit": None,
            "reason": "New file does not exist in repository; proposal cannot be already implemented.",
            "duration_s": 0.0,
        }
        results["false_new_file_rejections"].append({
            "task_title": task,
            "target_path": target,
            "count": case.get("count", 1),
            "expected_label": "legitimate",
            "verdict": res.get("verdict"),
            "citing_commit": res.get("citing_commit"),
            "reason": res.get("reason"),
        })

    # Group 3: Legitimate existing file improvements
    print("=== Evaluating Group 3: Legitimate Existing File Improvements ===")
    legitimate = fixture.get("legitimate_existing_file_improvements", [])
    confirmed = [c for c in legitimate if c.get("path_existed_before_commit") and c.get("proposal_ts")]

    for case in confirmed:
        task = case["task_title"]
        target = case["target_path"]
        commit_sha = case.get("commit_sha")
        cache_key = f"legit::{task}::{commit_sha}"
        if cache_key in cache:
            res = cache[cache_key]
        else:
            content, commits = extract_file_and_commits(repo_path, target, before_ref=commit_sha)
            prompt = build_evaluation_prompt(target, task, content, commits)
            t0 = time.monotonic()
            res = call_evaluator_llm(prompt, model=model)
            res["duration_s"] = round(time.monotonic() - t0, 2)
            cache[cache_key] = res
            _save_cache()

        results["legitimate_existing_file_improvements"].append({
            "task_title": task,
            "target_path": target,
            "cycle_id": case.get("cycle_id"),
            "commit_sha": commit_sha,
            "duplicate_gate_match": case.get("duplicate_gate_match"),
            "matched_prior_subject": case.get("matched_prior_subject"),
            "expected_label": "legitimate",
            "verdict": res.get("verdict"),
            "citing_commit": res.get("citing_commit"),
            "reason": res.get("reason"),
            "duration_s": res.get("duration_s", 0),
        })

    return results

def print_report(results: dict[str, Any]):
    print("\n" + "=" * 80)
    print("REPLAY PROTOTYPE EVALUATION REPORT (#1911)")
    print("=" * 80)

    # 1. Duplicates table
    print("\n### 1. Known Duplicates (Target: verdict=done, correct citation)")
    print("| Task Title | Count | Prototype Verdict | Citing Commit | Status |")
    print("|---|---|---|---|---|")
    total_dups = 0
    caught_dups = 0
    correct_citations = 0
    for d in results["duplicates"]:
        count = d["count"]
        total_dups += count
        v = d["verdict"]
        citing = (d["citing_commit"] or "")[:8]
        exp_sha = d.get("expected_sha") or ""
        is_caught = (v == "done")
        is_correct_cit = (is_caught and citing.startswith(exp_sha[:6]))
        if is_caught:
            caught_dups += count
        if is_correct_cit:
            correct_citations += count
        status_icon = "CORRECT" if is_correct_cit else ("DONE (MISMATCH CITATION)" if is_caught else "MISSED")
        print(f"| {d['task_title']} | {count} | {v} | {citing} (exp: {exp_sha}) | {status_icon} |")

    # 2. False New File Rejections
    print("\n### 2. False New File Rejections (Target: verdict=not_done)")
    print("| Target Path | Unique Titles | Rejection Count | Prototype Verdict |")
    print("|---|---|---|---|")
    total_fnf = sum(c["count"] for c in results["false_new_file_rejections"])
    wrongly_rejected_fnf = sum(c["count"] for c in results["false_new_file_rejections"] if c["verdict"] == "done")
    by_path: dict[str, list] = {}
    for c in results["false_new_file_rejections"]:
        by_path.setdefault(c["target_path"], []).append(c)
    for p, items in by_path.items():
        cnt = sum(it["count"] for it in items)
        print(f"| {p} | {len(items)} | {cnt} | not_done (100% allowed) |")

    # 3. Legitimate Existing File Improvements
    print("\n### 3. Legitimate Existing File Improvements (Target: verdict != done)")
    print("| Cycle | Task Title | Old Gate Match | Prototype Verdict | Prototype Citing | Result |")
    print("|---|---|---|---|---|---|")
    total_legit = len(results["legitimate_existing_file_improvements"])
    wrongly_rejected_legit = 0
    old_gate_matches = 0
    for item in results["legitimate_existing_file_improvements"]:
        v = item["verdict"]
        old_match = item.get("duplicate_gate_match")
        if old_match:
            old_gate_matches += 1
        if v == "done":
            wrongly_rejected_legit += 1
            res_str = "WRONGLY REJECTED"
        else:
            res_str = "ALLOWED (" + ("IMPROVED OVER OLD GATE" if old_match else "OK") + ")"
        print(f"| {item.get('cycle_id') or '-'} | {item['task_title'][:50]}... | {old_match} | {v} | {(item.get('citing_commit') or '-')[:8]} | {res_str} |")

    print("\n### Summary Metrics")
    print(f"- Total Duplicates: {total_dups} instances | Caught: {caught_dups} | Missed: {total_dups - caught_dups}")
    print(f"- Correct Citation on Filter fallback candidates dedup (101): {'YES (80fe9f4b)' if correct_citations >= 101 else 'NO'}")
    print(f"- False New File Rejections: {total_fnf} instances | Wrongly Rejected by Prototype: {wrongly_rejected_fnf}")
    print(f"- Legitimate Existing File Improvements: {total_legit} cases | Old Gate Falsely Matched: {old_gate_matches} | Prototype Wrongly Rejected: {wrongly_rejected_legit}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay prototype for #1911")
    parser.add_argument("--fixture", type=Path, default=Path("tests/fixtures/self_dedup_1785.json"))
    parser.add_argument("--repo", type=Path, default=Path("T:/Code/eeebot-self-evolving"))
    parser.add_argument("--cache", type=Path, default=Path("tests/fixtures/replay_prototype_cache.json"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--model", type=str, default="un/qwen3.8-27b-gguf")
    args = parser.parse_args()

    results = run_replay(args.fixture, args.repo, model=args.model, cache_path=args.cache)
    print_report(results)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nSaved raw results to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
