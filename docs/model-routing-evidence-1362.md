# Model routing measurement (#1362)

## Scope and topology

No routing, token, thinking-budget, cadence or suppression changes. Operator-verified topology: eeepc is an Atom N270/2 GB machine, not the inference host. Both owned-GPU `un/` and vendor `an/`/`cl/` traffic traverse the same LAN gateway. Owned 3090Ti inference versus metered vendor inference is the relevant distinction, NOT local versus gateway. Gateway failure can affect both.

Operator baseline (earlier snapshot, not recollected): 998 calls (784 Qwen/214 Gemini), Qwen 125.6 busy minutes in 1248 minutes, 10.1%; median gap .1 min, max 193.9 min, ten gaps >30 min. 50-day bridge token share 99.4%. Cycle/pause medians 1.6/3.1 min. These are historical scope, not today's newly measured full-day denominators.

## UTC 2026-09-05 retained telemetry

Snapshot now contains 1004 calls (784 owned Qwen, 220 vendor Gemini), first 00:00:29.587382Z, last 23:58:21.678982Z. Counts below are per this UTC day, not extrapolated hourly rates. Nearest-rank p95, median p50; token totals are recorded usage, not billing estimates.

| Component | Calls/day | p50 ms | p95 ms | Prompt tokens | Completion tokens | Finish reasons | Max observed prompt tokens |
|---|---:|---:|---:|---:|---:|---|---:|
| proposer | 170 | 8374.6165 | 40763.177 | 667093 | 26928 | stop 170 | 4166 |
| reflector | 35 | 8347.822 | 37529.526 | 454729 | 14479 | stop 35 | 18115 |
| curator | 2 | 22940.631 | 30074.209 | 29252 | 5579 | stop 2 | 14670 |
| bridge | 796 | 6284.9305 | 33145.789 | 30566265 | 183175 | tool_calls 758; stop 24; error 13; length 1 | 72695 |

One additional strategist call is outside the four requested components. Bridge is a component, not a synonym for Qwen: 796 bridge calls exceed the 784 Qwen calls.

## Suppression attribution: unavailable, not zero

Day ledger: 135 proposer_skip, 44 proposer_reject, 37 proposed. All 179 skip/reject rows lack cycle_id. All 170 proposer telemetry calls have cycle_id equal to the empty string. No exact per-attempt join exists in these stored fields. Timestamp proximity is not proof of association (multiple retries and non-LLM skips exist).

Therefore exact purchased calls attributable to suppressed attempts are **unavailable calls/day and unavailable share**, not 0. An intentionally loose upper bound for proposer calls is 170/day (100% of proposer calls; 77.3% of 220 vendor calls); lower bound 0 is only a mathematical bound, not a measurement. Do not multiply suppression events by one call. This acceptance criterion cannot be completed from these rows without another reliable attempt identity or execution trace. No instrumentation change made here.

## Executor time

For 29 cycle IDs with both started/outcome rows and recorded bridge calls on this day: summed start-to-outcome spans 14545.98 seconds; summed call duration 7750.41 seconds (53.28%); median cycle span 378.925 seconds; median per-cycle call/span ratio 65.43%. Residual 6795.57 seconds includes tools, setup, validation, integration, waits and potentially missing telemetry: it is NOT measured model idle time. Retries can widen a cycle-ID span. Calls crossing UTC boundaries are not fully represented.

Qwen recorded completion throughput: 183175 completion tokens / 7536.97 request-duration seconds = **24.30 completion tokens per request-wall second**. This includes prefill/network/queue and is not server decode throughput. Vendor aggregate equivalent is 15.29, not a controlled performance comparison. The executor spends material time in model requests when running, but telemetry cannot locate time inside those requests or assign residual cycle time to one bottleneck.

## Served configuration: evidence missing

The checked eeepc service list has no llama/ollama/vllm/litellm inference service. That matches the supplied remote-GPU topology; it does not identify the served 3090Ti process/config. An authorized config path or management endpoint on the GPU host was not available in this task context. No port scan, credentials inspection, environment-file values, model load or synthetic request was performed.

Actual context capacity and server prefill/decode throughput remain **unavailable**. Neither the model name nor the 72695-token bridge maximum proves configured proposer capacity: different model routing and tokenizer accounting must be resolved. 4166 proposer prompt tokens is an observed demand, not proof that the owned route accepts it with unchanged output/thinking budget.

## Per-component decision

| Component | Verdict | Decisive evidence |
|---|---|---|
| proposer | Needs evidence; leave vendor routing unchanged | 170/day, max 4166 prompt tokens; owned served capacity and decision-quality replay absent |
| reflector | Needs evidence; leave vendor routing unchanged | 35/day, max 18115 prompt tokens; no owned-route reflection-quality comparison |
| curator | Needs evidence; leave vendor routing unchanged | only 2/day, max 14670 prompt tokens; sample too small and capacity unknown |
| bridge | Retain current owned route; no routing change | 784 Qwen calls, 24.30 completion tokens/request-wall second; 53.28% aggregate measured cycle time in all bridge calls |

**No additional component is established as movable by this measurement.** This is lack of evidence, not proof of impossibility. No migration issue or second suppression gate is proposed. Do not shrink prompts or thinking budgets to manufacture fit.

## Reproduction

All state reads used `ssh ozand@eeepc-lan sudo -u eeepc-agent python3 -` with stdlib JSON/gzip aggregation. Inputs: `state/llm_calls/2026-09-05.jsonl` and every `state/ledger/**/cycles*.jsonl*`, filtering ledger `ts` to 2026-09-05. Read JSON objects; group calls by component; sorted duration values give median and index ceil(.95*n)-1; sum prompt_tokens/completion_tokens; Counter(finish_reason). Group ledger by cycle_id; take earliest started and latest outcome only for IDs with bridge calls. Count missing IDs before attempting suppression joins. No state files were written.

Minimal table command (run under the SSH/service-account prefix above):

```python
import json, math, statistics, collections
from pathlib import Path
rows = [json.loads(x) for x in Path('/var/lib/eeepc-agent/self-evolving-agent/state/llm_calls/2026-09-05.jsonl').read_text().splitlines() if x.strip()]
for component in ('proposer', 'reflector', 'curator', 'bridge'):
    selected = [r for r in rows if r.get('component') == component]
    durations = sorted(r['duration_ms'] for r in selected)
    print(component, len(selected), statistics.median(durations) if durations else None,
          durations[math.ceil(.95 * len(durations))-1] if durations else None,
          sum(r['prompt_tokens'] for r in selected), sum(r['completion_tokens'] for r in selected),
          dict(collections.Counter(r.get('finish_reason') for r in selected)))
```

Outstanding acceptance: exact suppression rate/share and served GPU config/capacity. This document must not be presented as completed routing qualification.
