# Avoiding Repeated Task Failures by Checking Recent Cycle Outcomes

## Description
To maintain a high confirmation ratio and minimize wasted cycles, subagents and the coordinator must actively check recent cycle outcomes before proposing or implementing new tasks. Proposing tasks that match recent failures leads to a high repeat failure rate, which degrades system efficiency.

## Root Causes
1. **Lack of Context Awareness**: Subagents proposing tasks without checking the recent history of successes and failures in `memory/HISTORY.md`.
2. **Duplicate Backlog Seeding**: The backlog seeder or LLM proposer proposing tasks that address issues already attempted and failed recently, without resolving the underlying root cause first.
3. **Suppression Bypass**: Proposing tasks with slightly different titles or descriptions that bypass simple string-matching duplicate checks but attempt the same failing logic.

## Recovery Procedures
If the system gets stuck in a repeat failure loop:
1. **Identify the Repeating Pattern**: Run `python3 scripts/analyze_repeat_failures.py`.
2. **Prune the Backlog**: Remove the repeating task from the active backlog or mark it as deprecated.
3. **Clear Suppression State**: If the failure was transient and has been resolved, clear the suppression state to allow the task to run.

## Prevention Mechanisms
1. **Verify Recent Outcomes**: Always check if the task or a closely related task has been attempted in the last 7 days by inspecting `memory/HISTORY.md`.
2. **Do Not Re-propose Failed Tasks Directly**: If a task failed, do not simply re-propose it. First, create a lesson documenting the failure, and then propose a task that specifically addresses the root cause of the failure.
3. **Check for Blocked Files**: Ensure no safety lock files (like `blocked_file_present`) are in the workspace before starting.
4. **Use Candidate Matching**: When using `scripts/analyze_repeat_failures.py`, pass the candidate task title to check for recent matches:
   ```bash
   python3 scripts/analyze_repeat_failures.py --match-candidate "Your Task Title"
   ```
