# Host Measurement Evidence: ADR-021 Rule 5 (Reflection Coverage & Truncation)

Date: 2026-09-16
Measured on: eeepc (read-only, via sudo -u eeepc-agent)
Location: /var/lib/eeepc-agent/self-evolving-agent/state/reflector

## 1. Population and Truncation Frequency
- Total reflections across history (active reflections.jsonl + 4 archive .jsonl.gz files): 1460
- Reflections with input_fit telemetry: 405 (the remaining 1055 predate input_fit tracking)
- Overrun / recorder-truncated reflections (>32 KiB cap): 305 of 405 (75.3%)
- On active reflections.jsonl: 60 of 67 (89.6%)
- Reflector-dropped turns: 0 of 405 (0.0%, because recorder truncates down to ~32.7 KiB before the reflector scans it)

## 2. Transcript Coverage Distribution
Formula:
  transcript_coverage = kept_chars / (kept_chars + recorder_truncated_chars + dropped_chars)

Where:
- kept_chars = input_fit.transcript.chars
- recorder_truncated_chars = input_fit.transcript.recorder_truncated_chars
- dropped_chars = input_fit.transcript.dropped_chars

Measured distribution across all n=405 records with telemetry:
- Min coverage: 0.0038 (0.38% — the reflector saw only 0.38% of the match; ~32.7 KiB kept out of ~8.6 MB total)
- 25th percentile (p25): 0.2202 (22.0% seen)
- Median coverage: 0.3582 (35.8% seen)
- 75th percentile (p75): 0.9028 (90.3% seen)
- Max coverage: 1.0000 (100.0% seen, in 100 of 405 records, 24.7%)

Measured distribution among truncated records only (n=305):
- Min: 0.0038 (0.38%)
- Median: 0.2796 (28.0%)
- Max: 0.9028 (90.3%)

Sample live record:
- kept_chars = 32,697
- recorder_truncated_chars = 28,705
- total_chars = 61,402
- transcript_coverage = 0.5325 (53.2%)
