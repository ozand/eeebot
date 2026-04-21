# eeebot Budget and Reward Model

Last updated: 2026-04-21 UTC

## Purpose

This document defines how the self-improving runtime should think about budget, credits, reward, and cost discipline.

The system must not only improve things.
It must improve them efficiently and in bounded fashion.

## Budget hierarchy

The primary budget is the hourly cycle budget.

Each cycle may include:
- one main bounded task
- optional supporting micro-tasks
- optional verification/documentation work

All of them must fit within the same cycle budget envelope.

### Required budget dimensions

A cycle budget should include at minimum:
- max requests
- max tool calls
- max subagents
- max wall-clock time
- optional credits/cost budget

### Why the hourly budget matters

The budget is not just an implementation detail.
It is what keeps the self-improving system from becoming an unbounded, chaotic process.

The system must always be able to say:
- what budget it had
- what budget it used
- whether the improvement justified the cost

## Experiment budget model

Each selected task should inherit a run budget inside the cycle.

That budget belongs in the experiment contract.

The ideal model is:
- cycle budget at the top
- one or more experiment budgets underneath
- all experiment budgets still constrained by the same hourly envelope

## Reward model

Reward is not only “did code change”.
Reward should capture whether the system improved in a way that matters.

### Reward inputs may include
- PASS rate
- blocker reduction
- artifact completeness
- improved visibility/truthfulness
- successful subagent completion
- lower operator confusion
- lower response size or latency
- successful closure of bounded issues

### Reward should not be purely narrative

The reward signal should be tied to measurable or at least auditable evidence.

## Baseline / current / frontier

For every experiment lane, the runtime should track:
- baseline
- current
- frontier

### Baseline
The prior accepted state or prior measurable reference point.

### Current
The result of the current experiment.

### Frontier
The best known result so far.

## Outcome rules

Every experiment must end with one of:
- keep
- discard
- crash
- blocked

### keep
Use when:
- current >= baseline according to the chosen rule
- or the first valid baseline is being established

### discard
Use when:
- the experiment completed but did not beat the baseline
- or the change is not worth the added complexity

### crash
Use when:
- the execution path failed technically
- the experiment cannot be evaluated because it did not complete correctly

### blocked
Use when:
- the experiment could not legally or procedurally proceed
- for example approval gate missing, dependency unavailable, or authority denied

## Simplicity penalty

Reward should not ignore complexity.

Even if a change improves a metric slightly, it should be penalized if it adds disproportionate complexity.

Recommended supporting fields:
- complexity_delta
- simplicity_judgment
- keep_reason
- discard_reason

## Credits

Credits are the operator-facing budget accounting layer.
They should track:
- balance
- delta for the current experiment/cycle
- reason for spend/reward
- source file/artifact

Credits should never be decorative.
They should explain how much bounded work was consumed by a cycle.

## Ideal operator interpretation

A good cycle:
- uses modest budget
- produces a clear measurable outcome
- improves the frontier or clarifies a blocker
- does not introduce ugly complexity

A bad cycle:
- burns lots of budget
- creates ambiguous outcomes
- adds complexity without meaningful gain
- leaves no durable evidence

## Recommended dashboard surfaces

The dashboard should show:
- cycle budget
- experiment budget
- budget used
- credits balance / delta
- reward signal
- baseline/current/frontier
- outcome
- complexity_delta
- simplicity_judgment
- revert_required when discard happens
