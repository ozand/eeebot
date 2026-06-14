# Lesson: SOURCE_COMMIT Handshake in Release Promotion

## Context
The self-evolving runtime coordinates improvements on the host, bundles them, runs health checks, and registers promotion candidates. The promoter service then evaluates the promotion candidate against the running system to determine if it should be deployed.

## Problem
The self-evolving loop suddenly went idle. Subagents stopped receiving tasks.
Checking the logs via `journalctl -u eeepc-self-evolving-agent-health.service` revealed the coordinator was blocked at the promotion stage (`blocked_not_ready` state).
It was unable to derive the parent version of the active runtime, stalling candidate verification.

## Root Cause
The coordinator promotion logic expects to read a file named `SOURCE_COMMIT` in the active production release directory (e.g. `/opt/eeepc-agent/runtimes/self-evolving-agent/releases/<release-id>/SOURCE_COMMIT`) to establish the base git hash of the running code.
During manual release creation or automated in-place patching, this metadata file was missing or deleted. Without the base hash, the coordinator could not determine the diff boundary for the new candidate.

## Resolution
1. Locate the active release commit hash via git logs.
2. Manually write the hash to the `SOURCE_COMMIT` file inside the active release directory:
   ```bash
   echo "3b056385db49b1ff1461ff399e4b789a5839ceb8" > /opt/eeepc-agent/runtimes/self-evolving-agent/releases/20260609T203743Z-canonical-3b05638/SOURCE_COMMIT
   ```
3. Restart the health service to trigger verification:
   ```bash
   sudo systemctl start eeepc-self-evolving-agent-health.service
   ```
4. Manually approve the candidate once it transitions to `ready_for_policy_review` using the promoter API.

## Key Takeaway
Any deployment script, packaging routine, or manual patch application MUST preserve and write the `SOURCE_COMMIT` metadata file containing the exact git hash of the release. Without this handshake, the promotion pipeline will enter a silent blocking state to prevent unverified drift.
