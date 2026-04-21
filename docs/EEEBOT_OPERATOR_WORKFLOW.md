# eeebot Operator Workflow

Last updated: 2026-04-21 UTC

## Purpose

This document explains how a human operator should interact with the self-improving runtime without micromanaging every action.

## Operator responsibilities

The operator should:
- define or update high-level goals
- define constraints and acceptable scope
- open approval windows when bounded apply actions are allowed
- inspect dashboard state and proofs
- intervene when policy or direction must change

The operator should not have to manually drive each micro-step.

## Typical operator loop

### 1. Check overview
Read:
- current goal
- current blocker
- current outcome
- current frontier

### 2. Check plan and experiments
Read:
- selected task
- experiment contract
- budget used
- keep/discard/crash/blocked outcome

### 3. Check subagents
Read:
- what subagents are doing
- how they map to current goal/task/cycle

### 4. Check blockers or revert requirements
If the system is blocked or discard happened, inspect:
- blocker class
- revert status
- next bounded step

## Approval workflow

When changes require apply authorization, the operator should:
- open a bounded approval window
- verify it is reflected in state
- allow one or more bounded cycles to use that window
- close or let the approval expire naturally

## What an operator should expect from a healthy system

A healthy runtime should always provide:
- a clear active goal
- a clear selected task
- measurable experiment state
- explicit outcome
- visible budget/reward state
- durable history

## When the operator should intervene

The operator should intervene when:
- the active goal is wrong
- the system keeps burning budget with weak outcomes
- complexity grows without clear gains
- the same blocker repeats without learning
- a policy/safety decision must be changed

## What the operator should not have to ask repeatedly

The operator should not need to repeatedly ask:
- what the bot is doing
- which task is active
- why a task was selected
- whether an experiment passed or failed
- which subagent is responsible

These should already be visible in durable state and the dashboard.
