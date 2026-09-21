# Verification of Skill Portability & Reuse Metrics (#1827)

Independent second-source verification of the skill reuse audit performed on `eeepc-lan`.

## 1. Verified Claims Summary

| Claim | Measured Value | Match? | Details |
|---|---|---|---|
| **1. 164 reads across 35 skills** | 164 reads / 35 skills | **COINCIDES** | Snapshot at `2026-09-20T23:31:13Z` has exactly 164 read rows across 35 distinct skills (total current is 170 / 36 skills). |
| **2. 155 of 156 cycle-task mapping; 0 title collisions** | 155 of 156 resolved | **COINCIDES** | Joining 156 (skill, cycle) pairs against cycle titles in ledger resolved 155 pairs (1 unresolved: `verification-939`). 0 task title collisions found for any skill. Distinct cycle count equals distinct task count. |
| **3. `run-tests`: 50 tasks (32% of applications)** | 50 tasks / 156 = 32.05% | **COINCIDES** | `run-tests` was applied across exactly 50 distinct cycle tasks out of 156 total skill applications (32.05%). |
| **4. Distribution of applications across skills** | 50, 12, 7, 5, 5, 4 (x9), 3 (x3), 2 (x14), 1 (x4) | **COINCIDES** | Grouping by distinct cycles per skill matches the exact distribution: 1 skill at 50, 1 at 12, 1 at 7, 2 at 5, 9 at 4, 3 at 3, 14 at 2, 4 at 1. Sum = 156 applications across 35 skills. |
| **5. 96% vs 0% split is age artefact; 21% vs 0% age-matched at 24h** | Pre: 21.9% at 24h (7/32), Post: 0 eligible skills | **COINCIDES** | All 3 post-ladder skills were under 21 hours old at measurement cutoff. Pre-ladder skills had only a 21.9% reuse rate within their first 24h. |

## 2. Age-Matched Cohort Analysis Detail

- **Cutoff time**: `2026-09-20T23:31:13Z`
- **Ladder deploy threshold**: `2026-09-19T00:00:00Z`
- **Pre-ladder cohort (first read before 2026-09-19)**:
  - 32 skills
  - Carried to 2nd distinct cycle eventually: 31 / 32 = 96.9%
  - Carried to 2nd distinct cycle within 24h: 7 / 32 = 21.9%
  - Carried to 2nd distinct cycle within 48h: 8 / 32 = 25.0%
- **Post-ladder cohort (first read >= 2026-09-19)**:
  - 3 skills (`inspect-verify-and-proof`, `targeted-file-slice`, `raw-json-final-turn`)
  - Ages at cutoff: 20.59h, 9.76h, 8.19h
  - None reached 24 hours of age at the time of evaluation.
  - Carried to 2nd distinct cycle within 24h: 0 / 0 (no eligible skills).
- **Conclusion**: The comparison of 96% vs 0% is entirely an age artefact.
