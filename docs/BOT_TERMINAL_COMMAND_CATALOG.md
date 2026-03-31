# Bot Terminal Command Catalog

Last updated: 2026-03-30 UTC

## Purpose

This document is the authoritative catalog of terminal command groups that may be
exposed to the running bot under the current `eeepc` goals.

It is intentionally a nomenclature document, not a large policy matrix.

Anything not explicitly covered here is treated as disallowed by default.

## Scope

- Ubuntu/Debian weak-host runtime for `eeepc`
- terminal command access used by the running bot
- aligned with `workspace-only`, `no-root`, `truthful probing`, and bounded
  self-improvement

## Baseline Rules

- Prefer specialized tools and bounded `workspace.*` actions over raw shell.
- Prefer read-only commands first.
- Keep command execution inside the workspace when possible.
- No root, no destructive system commands, no blind network-admin actions.
- New commands should be added by command group, not as one-off exceptions.

## Allowed Command Groups

### 1. Repository and workspace inspection

Examples:

- `pwd`
- `ls`
- `ls -la`
- `find .`
- `rg ...`
- `fd ...` / `fdfind ...`
- `tree`
- `head`
- `tail`
- `sed -n ...`
- `awk ...`

Purpose:

- inspect the workspace,
- search files and text,
- support truthful diagnostics and bounded research.

### 2. Git read-only diagnostics

Examples:

- `git status`
- `git diff`
- `git log --oneline -n 20`
- `git show <rev>`
- `git branch --show-current`

Purpose:

- inspect repository state,
- explain current changes,
- support evidence-backed local work.

Note:

- these commands are only useful when the current working directory is an actual
  Git repository inside the allowed workspace boundary.

### 3. Runtime and toolchain version checks

Examples:

- `python --version`
- `python3 --version`
- `git --version`
- `node -v`
- `npm -v`
- `pytest --version`
- `ruff --version`
- `uv --version`
- `jq --version`
- `sqlite3 --version`

Purpose:

- truthful capability probing,
- operator diagnostics,
- environment validation.

### 4. Safe host diagnostics

Examples:

- `ps`
- `ps aux`
- `ss -lntp`
- `lsof`
- `df -h`
- `du -sh .`
- `free -m`
- `uptime`
- `htop`

Purpose:

- inspect process/memory/disk/network state,
- support host embodiment without mutation.

### 5. Archive and transfer helpers inside operator workflows

Examples:

- `unzip`
- `zip`
- `rsync`

Purpose:

- bounded packaging and synchronization of local artifacts,
- no privilege escalation.

## Command Groups That Stay Disallowed

### 1. Privilege escalation and system administration

- `sudo`
- `su`
- `apt`
- `apt-get`
- `dpkg`
- `snap`
- `systemctl`
- `journalctl`

### 2. Destructive filesystem and disk operations

- `rm -rf`
- `dd`
- `mkfs`
- `format`
- disk-partitioning tools

### 3. Power and host shutdown controls

- `shutdown`
- `reboot`
- `poweroff`

### 4. Lateral movement and unrestricted remote access

- `ssh`
- `scp`
- unrestricted `curl`/`wget` against internal or private endpoints
- raw Docker or network-admin commands outside bounded operator workflows

## Prefer Bounded Actions Instead Of Raw Shell

Use these instead of opening a wider shell lane:

- `workspace.runtime.probe`
- `workspace.repo_status`
- `workspace.venv.ensure`
- `workspace.python.run`
- `workspace.pip.install_local`
- `workspace.experiment.tiny_runtime_check`

## Host Install Set Worth Having

These are safe and useful to have on the Ubuntu host for the current goals:

- `ripgrep`
- `jq`
- `fd-find`
- `tree`
- `rsync`
- `unzip`
- `shellcheck`
- `htop`
- `lsof`

Already useful if present:

- `python3`
- `git`
- `sqlite3`
- `iproute2` (`ss`)
- `procps` (`ps`)

## Host Status (eeepc, 2026-03-30)

Installed during the current rollout:

- `ripgrep`
- `jq`
- `fd-find`
- `tree`
- `rsync`
- `unzip`
- `zip`
- `shellcheck`
- `htop`
- `lsof`

Verified at runtime:

- `exec` is registered and available in `/cap_status`
- `pwd` executes successfully through the live host runtime
- `/workspace repo-status` returns truthful git status for the configured source repo

Known limitation:

- natural-language requests for some allowed commands such as `git status` are not
  yet consistently chosen by the model, even though the host exec lane is enabled.
  This is a prompting/agent-choice issue, not proof that the host allowlist is absent.

## Update Rule

- Update this document whenever a new command group is intentionally exposed.
- Keep the matching backlog item in `todo.md` linked to this file.
- If a command cannot be safely grouped, do not expose it yet.
