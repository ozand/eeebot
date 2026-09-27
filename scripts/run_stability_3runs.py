#!/usr/bin/env python3
"""Run 3-pass stability evaluation across 15 decisive cases (#1911)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from scripts.prototype_already_implemented import (
    build_evaluation_prompt,
    call_evaluator_llm,
    extract_file_and_commits,
)

FIXTURE_PATH = Path("tests/fixtures/self_dedup_1785.json")
REPO_PATH = Path("T:/Code/eeebot-self-evolving")
CACHE_PATH = Path("tests/fixtures/stability_3runs_cache.json")
RESULTS_PATH = Path("tests/fixtures/stability_3runs_results.json")

def load_cache() -> dict[str, dict]:
    if CACHE_PATH.is_file():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def collect_15_cases() -> list[dict]:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    cases = []
    # 3 duplicates
    for c in data.get("duplicates", []):
        cases.append({
            "group": "duplicate",
            "task_title": c["task_title"],
            "target_path": c["target_path"],
            "before_ref": None,
            "expected_sha": c.get("matched_sha"),
            "cycle_id": None,
            "count": c.get("count", 1),
        })

    # 12 measured legitimate
    for c in data.get("legitimate_existing_file_improvements", []):
        if c.get("path_existed_before_commit") and c.get("proposal_ts"):
            cases.append({
                "group": "legitimate",
                "task_title": c["task_title"],
                "target_path": c["target_path"],
                "before_ref": c.get("commit_sha"),
                "expected_sha": None,
                "cycle_id": c.get("cycle_id"),
                "duplicate_gate_match": c.get("duplicate_gate_match"),
                "count": 1,
            })

    return cases

def run_stability():
    cases = collect_15_cases()
    cache = load_cache()
    print(f"Loaded {len(cases)} cases. Running 3 passes each...")

    results = []

    for idx, case in enumerate(cases):
        task = case["task_title"]
        target = case["target_path"]
        ref = case["before_ref"]
        print(f"\n[{idx+1}/{len(cases)}] {case['group'].upper()}: {task[:50]}... ({target})")

        content, commits = extract_file_and_commits(REPO_PATH, target, before_ref=ref)
        prompt = build_evaluation_prompt(target, task, content, commits)

        case_runs = []
        for r in range(1, 4):
            key = f"case_{idx+1}_run_{r}::{task}::{ref}"
            if key in cache:
                res = cache[key]
                print(f"  Run {r}: [CACHED] {res.get('verdict')} (citing {str(res.get('citing_commit'))[:8]}) in {res.get('duration_s')}s")
            else:
                t0 = time.monotonic()
                res = call_evaluator_llm(prompt)
                dur = round(time.monotonic() - t0, 2)
                res["duration_s"] = dur
                cache[key] = res
                save_cache(cache)
                print(f"  Run {r}: {res.get('verdict')} (citing {str(res.get('citing_commit'))[:8]}) in {dur}s")

            case_runs.append(res)

        verdicts = [cr.get("verdict") for cr in case_runs]
        citings = [str(cr.get("citing_commit") or "")[:8] for cr in case_runs]
        durations = [cr.get("duration_s", 0) for cr in case_runs]
        tokens = [cr.get("usage", {}).get("total_tokens", 0) for cr in case_runs]
        completion_tokens = [cr.get("usage", {}).get("completion_tokens", 0) for cr in case_runs]

        is_stable = len(set(verdicts)) == 1

        results.append({
            "index": idx + 1,
            "group": case["group"],
            "task_title": task,
            "target_path": target,
            "cycle_id": case.get("cycle_id"),
            "old_gate_match": case.get("duplicate_gate_match"),
            "expected_sha": case.get("expected_sha"),
            "runs": case_runs,
            "verdicts": verdicts,
            "citings": citings,
            "durations": durations,
            "tokens": tokens,
            "completion_tokens": completion_tokens,
            "stable": is_stable,
        })

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print_markdown_table(results)

def print_markdown_table(results: list[dict]):
    print("\n" + "=" * 100)
    print("STABILITY & COVARIATE TABLE ACROSS 3 RUNS (15 DECISIVE CASES)")
    print("=" * 100)
    print("\n| # | Группа | Задача / Файл | Вердикт 1 | Вердикт 2 | Вердикт 3 | Стабильность | Ср. время (с) | Ср. completion токены |")
    print("|---|---|---|---|---|---|---|---|---|")

    stable_count = 0
    all_durations = []
    all_tokens = []

    for r in results:
        v1, v2, v3 = r["verdicts"]
        c1, c2, c3 = r["citings"]
        v1_s = f"{v1} ({c1})" if c1 else v1
        v2_s = f"{v2} ({c2})" if c2 else v2
        v3_s = f"{v3} ({c3})" if c3 else v3

        avg_dur = round(sum(r["durations"]) / len(r["durations"]), 1)
        avg_comp = round(sum(r["completion_tokens"]) / len(r["completion_tokens"]), 0)

        all_durations.extend(r["durations"])
        all_tokens.extend(r["completion_tokens"])

        stable_marker = " 3/3" if r["stable"] else "⚠️ СМЕШАННЫЙ"
        if r["stable"]:
            stable_count += 1

        label_prefix = f"**{r['group'].upper()}**"
        title_snippet = f"`{r['target_path']}`: {r['task_title'][:40]}..."

        print(f"| {r['index']} | {label_prefix} | {title_snippet} | {v1_s} | {v2_s} | {v3_s} | {stable_marker} | {avg_dur}s | {int(avg_comp)} |")

    overall_avg_dur = round(sum(all_durations) / len(all_durations), 1)
    overall_avg_comp = round(sum(all_tokens) / len(all_tokens), 0)

    print("\n### Статистика стабильности и нагрузки очереди")
    print(f"- Всего случаев: {len(results)}")
    print(f"- Полностью стабильные (3 из 3 совпадают): {stable_count} / {len(results)} ({round(stable_count / len(results) * 100, 1)}%)")
    print(f"- Среднее время на вызов: {overall_avg_dur} секунд (разброс: {round(min(all_durations), 1)}s .. {round(max(all_durations), 1)}s)")
    print(f"- Средний объём генерации (completion tokens): {int(overall_avg_comp)} токенов")


if __name__ == "__main__":
    run_stability()
