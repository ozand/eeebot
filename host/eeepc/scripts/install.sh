#!/usr/bin/env bash
# =============================================================================
# eeepc host deploy script
# Installs / updates the self-evolving agent runtime on a fresh or existing host.
#
# Usage:
#   sudo bash host/eeepc/scripts/install.sh [--dry-run]
#
# What it does:
#   1. Creates system user eeepc-agent
#   2. Creates directory layout (/opt, /var/lib, /etc/eeepc-agent)
#   3. Copies systemd units + drop-ins
#   4. Copies libexec scripts
#   5. Copies env files (skips litellm.env if it already has a real key)
#   6. Creates Python venv and installs the package
#   7. Enables and starts timers
#
# Prerequisites:
#   - Debian/Ubuntu host with systemd
#   - python3.11+ available
#   - git, curl installed
#   - LITELLM_API_KEY and LITELLM_BASE_URL set in environment OR
#     /etc/eeepc-agent/litellm.env already present with real values
#
# After first install:
#   the bridge + promotion-verifier timers enabled below start the loop;
#   no manual service start is needed.
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOST_DIR="$REPO_ROOT/host/eeepc"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

log()  { echo "[install] $*"; }
run()  { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY] $*"; else "$@"; fi; }
warn() { echo "[WARN] $*" >&2; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (sudo bash $0)" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# 1. System user
# ---------------------------------------------------------------------------
create_user() {
  if id eeepc-agent &>/dev/null; then
    log "user eeepc-agent already exists"
  else
    log "creating system user eeepc-agent"
    run useradd --system --no-create-home --shell /usr/sbin/nologin \
        --home-dir /opt/eeepc-agent eeepc-agent
  fi
}

# ---------------------------------------------------------------------------
# 2. Directory layout
# ---------------------------------------------------------------------------
DIRS=(
  /opt/eeepc-agent/runtimes/self-evolving-agent/releases
  /var/lib/eeepc-agent/self-evolving-agent/state
  /var/lib/eeepc-agent/self-evolving-agent/workspace/subagents
  /etc/eeepc-agent/instances
  /usr/local/libexec
)

create_dirs() {
  for d in "${DIRS[@]}"; do
    if [[ ! -d "$d" ]]; then
      log "mkdir -p $d"
      run mkdir -p "$d"
    fi
  done
  run chown -R eeepc-agent:eeepc-agent /opt/eeepc-agent /var/lib/eeepc-agent

  # #875: the root-verified promotion tree. Deliberately a SIBLING of
  # /var/lib/eeepc-agent, created AFTER (and outside) the recursive chown
  # above — it must stay root-owned so the eeepc-agent-uid bridge/loader can
  # read but never write it (filesystem permission IS the trust boundary
  # here; see nanobot.runtime.promoted_overlay's boundary self-check, which
  # REFUSES to load anything if this tree is ever not root-owned).
  log "creating root-owned promoted-runtime tree /var/lib/eeepc-promoted"
  run mkdir -p /var/lib/eeepc-promoted
  run chown root:eeepc-agent /var/lib/eeepc-promoted
  run chmod 0755 /var/lib/eeepc-promoted
}

# ---------------------------------------------------------------------------
# 3. Python venv + package install
# ---------------------------------------------------------------------------
VENV_DIR=/opt/eeepc-agent/runtimes/self-evolving-agent/venv

install_package() {
  local python
  python=$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)
  log "using python: $python ($($python --version))"

  if [[ ! -d "$VENV_DIR" ]]; then
    log "creating venv at $VENV_DIR"
    run "$python" -m venv "$VENV_DIR"
  fi

  log "installing eeebot package from $REPO_ROOT"
  run "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  run "$VENV_DIR/bin/pip" install --quiet -e "$REPO_ROOT[dev]"

  # Create/update 'current' symlink to a release dir
  local release_dir
  release_dir="/opt/eeepc-agent/runtimes/self-evolving-agent/releases/$(date -u +%Y%m%dT%H%M%SZ)-install"
  if [[ $DRY_RUN -eq 0 ]]; then
    mkdir -p "$release_dir"
    cp -r "$REPO_ROOT"/. "$release_dir/"
    ln -sfn "$release_dir/.venv" "$release_dir/../../../venv" 2>/dev/null || true
    # point venv into release for systemd ExecStart compatibility
    ln -sfn "$VENV_DIR" "$release_dir/.venv"
    ln -sfn "$release_dir" /opt/eeepc-agent/runtimes/self-evolving-agent/current
    log "current → $release_dir"
  else
    echo "[DRY] would create release at $release_dir and update current symlink"
  fi
  run chown -R eeepc-agent:eeepc-agent /opt/eeepc-agent
}

# ---------------------------------------------------------------------------
# 4. systemd units
# ---------------------------------------------------------------------------
install_units() {
  local src="$HOST_DIR/systemd"

  log "installing systemd unit files"
  for f in "$src"/*.service "$src"/*.timer; do
    [[ -f "$f" ]] || continue
    local name
    name=$(basename "$f")
    run cp "$f" "/etc/systemd/system/$name"
    log "  ✓ $name"
  done

  log "installing systemd drop-ins"
  for f in "$src"/drop-ins/**/*.conf; do
    [[ -f "$f" ]] || continue
    # path: drop-ins/eeepc-foo.service.d/bar.conf
    local rel="${f#"$src/drop-ins/"}"
    local dest="/etc/systemd/system/$rel"
    run mkdir -p "$(dirname "$dest")"
    run cp "$f" "$dest"
    log "  ✓ $rel"
  done

  run systemctl daemon-reload
}

# ---------------------------------------------------------------------------
# 5. libexec scripts
# ---------------------------------------------------------------------------
install_libexec() {
  log "installing /usr/local/libexec scripts"
  for f in "$HOST_DIR/libexec"/*.py; do
    [[ -f "$f" ]] || continue
    local name
    name=$(basename "$f")
    run cp "$f" "/usr/local/libexec/$name"
    run chmod +x "/usr/local/libexec/$name"
    log "  ✓ $name"
  done

  # Subagent bridge: nothing to copy since #601 — the unit's ExecStart runs
  # `python -m nanobot.runtime.bridge` directly from the release on PYTHONPATH,
  # so libexec holds no bridge file at all (single code authority: the release).
  run rm -f /usr/local/libexec/eeepc-self-evolving-subagent-bridge.py
}

# ---------------------------------------------------------------------------
# 6. /etc/eeepc-agent env files
# ---------------------------------------------------------------------------
install_etc() {
  log "installing /etc/eeepc-agent config files"

  # instances/ env files (always overwrite — no secrets inside)
  for f in "$HOST_DIR/etc/instances"/*.env; do
    [[ -f "$f" ]] || continue
    local name
    name=$(basename "$f")
    run cp "$f" "/etc/eeepc-agent/instances/$name"
    run chmod 640 "/etc/eeepc-agent/instances/$name"
    log "  ✓ instances/$name"
  done

  # presets/ (#906) — non-secret operator profile templates; always
  # overwrite, same as instances/ above. No symlink is created here:
  # activation (pointing /etc/eeepc-agent/preset.env at one of these) is
  # apply_preset.sh's job, not install's.
  run mkdir -p /etc/eeepc-agent/presets
  for f in "$HOST_DIR/etc/presets"/*.env; do
    [[ -f "$f" ]] || continue
    local name
    name=$(basename "$f")
    run cp "$f" "/etc/eeepc-agent/presets/$name"
    log "  ✓ presets/$name"
  done

  # litellm.env — only install example if real file absent
  if [[ ! -f /etc/eeepc-agent/litellm.env ]] || grep -q 'sk-YOUR_KEY_HERE' /etc/eeepc-agent/litellm.env 2>/dev/null; then
    run cp "$HOST_DIR/etc/litellm.env.example" /etc/eeepc-agent/litellm.env
    run chmod 600 /etc/eeepc-agent/litellm.env
    warn "IMPORTANT: edit /etc/eeepc-agent/litellm.env and set LITELLM_API_KEY before starting the agent!"
  else
    log "  ✓ litellm.env already configured (skipping)"
  fi

  # Inject real key from env if provided
  if [[ -n "${LITELLM_API_KEY:-}" && -n "${LITELLM_BASE_URL:-}" ]]; then
    log "  Injecting LITELLM_API_KEY and LITELLM_BASE_URL from environment"
    run sed -i "s|sk-YOUR_KEY_HERE|${LITELLM_API_KEY}|g" /etc/eeepc-agent/litellm.env
    run sed -i "s|https://litellm.example.invalid/v1|${LITELLM_BASE_URL}|g" /etc/eeepc-agent/litellm.env
  fi

  # nanobot gateway config template
  local nanobot_dir=/home/opencode/.nanobot-eeepc
  if [[ -d "$nanobot_dir" ]]; then
    if [[ ! -f "$nanobot_dir/config.template.json" ]]; then
      run cp "$HOST_DIR/etc/nanobot-config.template.json" "$nanobot_dir/config.template.json"
      warn "Installed nanobot config template — set real LITELLM_API_KEY inside it!"
      log "  ✓ $nanobot_dir/config.template.json"
    else
      log "  ✓ $nanobot_dir/config.template.json already exists (skipping)"
    fi
  else
    warn "opencode home $nanobot_dir not found — skipping nanobot config template"
  fi
}

# ---------------------------------------------------------------------------
# 7. State dir initial structure
# ---------------------------------------------------------------------------
init_state() {
  local state=/var/lib/eeepc-agent/self-evolving-agent/state
  log "initialising state directory structure"
  for subdir in reports subagents/requests subagents/results improvements approvals goals outbox subagent_bridge workspace/subagents; do
    run mkdir -p "$state/$subdir"
  done
  run chown -R eeepc-agent:eeepc-agent /var/lib/eeepc-agent
}

# ---------------------------------------------------------------------------
# 8. Enable timers
# ---------------------------------------------------------------------------
enable_timers() {
  log "enabling systemd timers"
  local timers=(
    eeepc-self-evolving-subagent-bridge.timer
    eeepc-promotion-verifier.timer
    eeebot-archive-subagent-requests.timer
  )
  for t in "${timers[@]}"; do
    run systemctl enable "$t"
    log "  ✓ enabled $t"
  done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
require_root

log "=== eeepc host deploy ==="
log "REPO_ROOT: $REPO_ROOT"
log "DRY_RUN:   $DRY_RUN"
echo

create_user
create_dirs
install_package
install_units
install_libexec
install_etc
init_state
enable_timers

echo
log "=== Deploy complete ==="
echo
if grep -q 'sk-YOUR_KEY_HERE' /etc/eeepc-agent/litellm.env 2>/dev/null; then
  warn "litellm.env still has placeholder key — agent will NOT start until you set LITELLM_API_KEY"
  warn "  sudo nano /etc/eeepc-agent/litellm.env"
else
  log "Loop is driven by the enabled timers (bridge + promotion-verifier)."
  log "  sudo journalctl -u eeepc-self-evolving-subagent-bridge.service -f"
fi
