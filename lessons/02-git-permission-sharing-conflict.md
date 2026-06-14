# Lesson: Multi-User Git Write Access & Shared Object Database Permissions

## Context
On the Eee PC host, multiple actors interact with the `/home/opencode/servers_team/repo_research/eeebot-self-evolving` workspace:
1. The subagent bridge running inside systemd under the `eeepc-agent` user.
2. Human operators or coordinator test commands executing under the `opencode` user.
3. Systemd maintenance scripts or administrative commands sometimes run as `root`.

## Problem
Subagents began failing to commit their changes, resulting in empty commit attempts or silent completions without git logs.
Manual git commands like `git add` or `git commit` failed with the following error:
```
error: insufficient permission for adding an object to repository database .git/objects
error: scripts/eeebot_dashboard.py: failed to insert into database
error: unable to index file 'scripts/eeebot_dashboard.py'
```

## Root Cause
When git commands are run by different users (specifically `root` or `opencode`), new Git object directories and packfiles are created under `.git/objects/` with that user's default ownership and `umask`.
If a subsequent git command run by `eeepc-agent` needs to write to a directory owned by `root` (e.g. `.git/objects/07/`), it fails due to lack of write permission.

## Resolution
1. Change the ownership of the entire repository to the shared user/group:
   ```bash
   sudo chown -R opencode:eeepc-agent /home/opencode/servers_team/repo_research/eeebot-self-evolving
   ```
2. Force shared write permissions (both user and group) recursively:
   ```bash
   sudo chmod -R g+rw /home/opencode/servers_team/repo_research/eeebot-self-evolving
   ```
3. Configure the git repository to respect shared access permissions by default:
   ```bash
   git -C /home/opencode/servers_team/repo_research/eeebot-self-evolving config core.sharedRepository group
   ```

## Key Takeaway
In a multi-user local engineering workspace, never mix direct `root` git writes with unprivileged user git writes. Group access must be explicitly enabled using `core.sharedRepository group`, and recursive ownership must be verified whenever access errors occur.
