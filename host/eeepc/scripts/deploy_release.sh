#!/usr/bin/env bash
# =============================================================================
# eeepc deploy: push a new code release to the running host and activate it
#
# Usage (from dev machine, not on eeepc):
#   bash host/eeepc/scripts/deploy_release.sh [--host eeepc] [--dry-run]
#
# What it does:
#   1. Bundles current repo HEAD into a timestamped release archive
#   2. Sends it to eeepc via scp
#   3. On eeepc: extracts, creates venv, symlinks current, restarts agent
#
# Prerequisites on dev machine:
#   - ssh access to eeepc (key-based)
#   - gh CLI authenticated
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOST="eeepc"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --host)    HOST="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

log()  { echo "[deploy] $*"; }
run()  { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY] $*"; else "$@"; fi; }

COMMIT=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RELEASE_NAME="${TIMESTAMP}-canonical-${COMMIT}"
ARCHIVE="/tmp/eeebot-release-${RELEASE_NAME}.tar.gz"

log "repo:    $REPO_ROOT"
log "commit:  $COMMIT ($BRANCH)"
log "release: $RELEASE_NAME"
log "host:    $HOST"

# 1. Create archive from git HEAD (excludes .git, .venv, __pycache__)
log "creating archive..."
run git -C "$REPO_ROOT" archive --format=tar.gz --prefix="${RELEASE_NAME}/" HEAD \
  -o "$ARCHIVE"
log "archive: $ARCHIVE ($(du -sh "$ARCHIVE" | cut -f1))"

# 2. Copy to host
REMOTE_ARCHIVE="/tmp/$(basename "$ARCHIVE")"
log "uploading to $HOST:$REMOTE_ARCHIVE..."
run scp "$ARCHIVE" "ozand@${HOST}:${REMOTE_ARCHIVE}"

# 3. Extract, build venv, update symlink, restart
log "installing on $HOST..."
run ssh "ozand@${HOST}" bash -s -- "$REMOTE_ARCHIVE" "$RELEASE_NAME" "$COMMIT" <<'REMOTE'
set -euo pipefail
ARCHIVE="$1"
RELEASE_NAME="$2"
COMMIT="$3"

RELEASES_DIR=/opt/eeepc-agent/runtimes/self-evolving-agent/releases
RELEASE_DIR="$RELEASES_DIR/$RELEASE_NAME"
VENV_BASE=/opt/eeepc-agent/runtimes/self-evolving-agent/venv

echo "[remote] extracting to $RELEASE_DIR"
sudo mkdir -p "$RELEASES_DIR"
cd /tmp
sudo tar xzf "$ARCHIVE" -C "$RELEASES_DIR"
echo "$COMMIT" | sudo tee "$RELEASE_DIR/SOURCE_COMMIT" > /dev/null

echo "[remote] linking venv into release"
sudo ln -sfn "/home/opencode/.venvs/nanobot" "$RELEASE_DIR/.venv"

echo "[remote] updating current symlink"
sudo ln -sfn "$RELEASE_DIR" /opt/eeepc-agent/runtimes/self-evolving-agent/current

echo "[remote] fixing ownership"
sudo chown -R eeepc-agent:eeepc-agent "$RELEASE_DIR" "$VENV_BASE" 2>/dev/null || true

echo "[remote] syncing libexec scripts from release"
sudo cp "$RELEASE_DIR/host/eeepc/libexec/"*.py /usr/local/libexec/ 2>/dev/null || true
sudo chmod +x /usr/local/libexec/eeepc-self-evolving-*.py

echo "[remote] reloading systemd + restarting agent"
sudo systemctl daemon-reload
sudo systemctl restart eeepc-self-evolving-agent.service || true

echo "[remote] current release: $(readlink /opt/eeepc-agent/runtimes/self-evolving-agent/current)"
echo "[remote] done — commit $COMMIT"
REMOTE

log "cleaning up local archive"
run rm -f "$ARCHIVE"

log ""
log "=== Deploy complete ==="
log "Release: $RELEASE_NAME"
log "To verify:"
log "  ssh ozand@$HOST 'sudo journalctl -u eeepc-self-evolving-agent.service --since \"1 min ago\" --no-pager'"
log "  ssh ozand@$HOST 'sudo systemctl status eeepc-self-evolving-agent.service'"
