# Lesson: Systemd Timer Stagnation under NTP & Clock Drift

## Context
Subagent tasks are dispatched by the coordinator to a local filesystem queue, and a periodically triggered systemd timer (`eeepc-self-evolving-subagent-bridge.timer`) executes the bridge script to pop tasks from the queue and run them.

## Problem
The self-evolving loop stopped processing tasks for over 2.5 hours, even though the queue contained pending requests.
Checking `systemctl status eeepc-self-evolving-subagent-bridge.timer` showed:
```
Active: active (running)
Trigger: n/a
Triggers: ● eeepc-self-evolving-subagent-bridge.service
```
The timer trigger was dead (`n/a`), and it was no longer scheduling future executions of the bridge.

## Root Cause
The timer was configured using `OnUnitActiveSec=10m`.
`OnUnitActiveSec` instructs systemd to schedule the next run relative to the *start time* of the previous successful service invocation.
On low-resource hardware like Eee PC, system clock adjustments (via `chrony` or `systemd-timesyncd` NTP sync) or service process crashes can cause the systemd scheduler to lose track of relative active timestamps. When this happens, the timer enters an indefinite `n/a` sleep state.

## Resolution
1. Reset the timer unit state manually to force rescheduling:
   ```bash
   sudo systemctl restart eeepc-self-evolving-subagent-bridge.timer
   ```
2. For long-term robustness, shift periodic systemd timers to either:
   - `OnUnitInactiveSec=10m` (run relative to the service *exit* time, which resets the state machine cleanly upon deactivation).
   - `OnCalendar=*:0/10` (run on a fixed clock interval, e.g. every 10 minutes, which is immune to unit state-machine drift).

## Key Takeaway
Avoid using `OnUnitActiveSec` for critical periodic timers on devices prone to clock synchronization drift or heavy CPU constraint. Prefer `OnUnitInactiveSec` or explicit calendar-based `OnCalendar` patterns to guarantee self-healing execution schedules.
