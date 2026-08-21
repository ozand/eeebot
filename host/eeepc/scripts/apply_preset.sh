#!/usr/bin/env bash
# =============================================================================
# apply_preset.sh — activate a named operator preset (#906)
#
# Usage (on the eeepc host, as root):
#   sudo bash apply_preset.sh <name>
#
# What it does:
#   1. Validates /etc/eeepc-agent/presets/<name>.env exists AND sources it
#      successfully — exits 1 with a clear message and makes NO changes if
#      either check fails (validate-before-mutate: bad input, zero changes).
#   2. Points the /etc/eeepc-agent/preset.env symlink at that file. The
#      bridge service unit loads this file (optionally, `-` prefix) BEFORE
#      the per-instance env file, so instance-file values still win per
#      variable — see host/eeepc/systemd/eeepc-self-evolving-subagent-bridge.service.
#   3. Warns (never fails) if the instance env file already sets, uncommented,
#      any variable this preset also sets — that var silently overrides the
#      preset per systemd EnvironmentFile ordering, so the operator gets an
#      explicit heads-up per #906 review (previously the instance template
#      shipped SUBAGENT_BRIDGE_MODEL uncommented, quietly defeating any
#      preset's model choice).
#   4. Reads SELFEVO_CYCLE_PAUSE out of the preset and translates it into a
#      systemd timer drop-in: OnUnitActiveSec=<blank> (clears the stock 15m
#      value) + OnUnitInactiveSec=<pause>. OnUnitInactiveSec fires N after
#      the PREVIOUS run ENDS rather than N after it STARTED, which also
#      sidesteps the OnUnitActiveSec + NTP clock-adjustment timer-stagnation
#      failure mode recorded in lessons/errors.yaml (ERR-2026-2026-06-14-003:
#      OnUnitActiveSec lost its activation marker under NTP drift on this
#      same Eee PC hardware). If SELFEVO_CYCLE_PAUSE is empty/unset, the
#      drop-in is removed instead, restoring the unit's own stock 15m timer.
#   5. `systemctl daemon-reload` and prints the resulting effective profile.
#
# stdlib/bash only, fail-open by construction: an invalid preset name or an
# unsourceable preset file changes nothing (validated before any mutation);
# a missing/blank SELFEVO_CYCLE_PAUSE just means "no drop-in", never an
# error; the override-warning check can itself never abort the script.
# =============================================================================

set -euo pipefail

PRESETS_DIR=/etc/eeepc-agent/presets
PRESET_SYMLINK=/etc/eeepc-agent/preset.env
INSTANCE_ENV_FILE=/etc/eeepc-agent/instances/self-evolving-subagent-bridge.env
TIMER_DROPIN_DIR=/etc/systemd/system/eeepc-self-evolving-subagent-bridge.timer.d
TIMER_DROPIN_FILE="$TIMER_DROPIN_DIR/preset.conf"

log()  { echo "[apply_preset] $*"; }
err()  { echo "[apply_preset] ERROR: $*" >&2; }

if [[ $# -ne 1 || -z "${1:-}" ]]; then
  err "usage: apply_preset.sh <name>"
  exit 1
fi

NAME="$1"
PRESET_FILE="$PRESETS_DIR/$NAME.env"

# --- 1. validate existence, THEN source (both before any mutation) --------
if [[ ! -f "$PRESET_FILE" ]]; then
  err "preset '$NAME' not found at $PRESET_FILE — no changes made."
  err "available presets:"
  for f in "$PRESETS_DIR"/*.env; do
    [[ -f "$f" ]] || continue
    err "  - $(basename "${f%.env}")"
  done
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  err "must run as root (sudo bash $0 $NAME)"
  exit 1
fi

# Values default to empty so an unset var in the preset file reads as "".
SELFEVO_PRESET=""
SELFEVO_PROPOSER_MODEL=""
SUBAGENT_BRIDGE_MODEL=""
SELFEVO_CYCLE_PAUSE=""
SELFEVO_MAX_TOOL_ITERATIONS=""
# Sourcing runs the preset file as shell AS ROOT — unlike systemd's own
# EnvironmentFile=, which only parses KEY=VALUE assignments, `source` here
# executes arbitrary shell. The trust boundary is therefore the filesystem
# (root-owned /etc content), not the parser: only root-writable presets
# under $PRESETS_DIR must ever be sourced this way.
# shellcheck disable=SC1090
set -a
source "$PRESET_FILE"
set +a
# Reaching here means the preset file exists and sourced cleanly — every
# mutation below is safe to perform. (With `set -e`, a syntactically broken
# preset would have already aborted the script above, before any change.)

# --- 2. activate the symlink -----------------------------------------------
log "activating preset '$NAME' -> $PRESET_FILE"
ln -sfn "$PRESET_FILE" "$PRESET_SYMLINK"

# --- 3. warn (never fail) about instance-env vars that shadow the preset ---
# Fail-open: the whole block is `{ ... } || true`, so any error inside it
# (missing file, grep hiccup) is swallowed — a warning helper must never
# abort activation. Plain `^VAR=` grep, one line per preset-assigned var,
# against whatever the instance file currently has uncommented.
{
  if [[ -f "$INSTANCE_ENV_FILE" ]]; then
    for _var in $(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$PRESET_FILE" | sed 's/=$//'); do
      if grep -qE "^${_var}=" "$INSTANCE_ENV_FILE"; then
        log "WARNING: instance env overrides preset for: $_var"
      fi
    done
  fi
} || true

# --- 4. timer drop-in from SELFEVO_CYCLE_PAUSE ---------------------------
if [[ -n "$SELFEVO_CYCLE_PAUSE" ]]; then
  log "materializing timer drop-in: OnUnitInactiveSec=$SELFEVO_CYCLE_PAUSE"
  mkdir -p "$TIMER_DROPIN_DIR"
  cat > "$TIMER_DROPIN_FILE" <<EOF
# Generated by apply_preset.sh for preset '$NAME' — do not hand-edit, it will
# be overwritten (or removed) on the next apply_preset.sh run.
#
# OnUnitActiveSec is cleared (blank) because systemd only lets ONE of
# OnUnitActiveSec/OnUnitInactiveSec drive a given timer meaningfully once a
# drop-in adds the other — and OnUnitActiveSec is additionally documented as
# unreliable on this hardware: lessons/errors.yaml ERR-2026-2026-06-14-003
# records the stock 15m OnUnitActiveSec timer stagnating ("Trigger: n/a")
# after an NTP clock adjustment. OnUnitInactiveSec (N after the previous run
# ENDS) avoids that failure mode and self-adapts to cycle length.
[Timer]
OnUnitActiveSec=
OnUnitInactiveSec=$SELFEVO_CYCLE_PAUSE
EOF
else
  if [[ -f "$TIMER_DROPIN_FILE" ]]; then
    log "SELFEVO_CYCLE_PAUSE unset — removing timer drop-in (stock 15m timer)"
    rm -f "$TIMER_DROPIN_FILE"
  else
    log "SELFEVO_CYCLE_PAUSE unset — no timer drop-in to remove (stock 15m timer)"
  fi
fi

# --- 5. reload + report ----------------------------------------------------
systemctl daemon-reload

log "=== effective profile ==="
log "preset:            ${SELFEVO_PRESET:-$NAME}"
log "proposer model:    ${SELFEVO_PROPOSER_MODEL:-<unset — falls back to bridge/default>}"
log "executor model:    ${SUBAGENT_BRIDGE_MODEL:-<unset — falls back to default>}"
log "cycle pause:       ${SELFEVO_CYCLE_PAUSE:-<unset — stock 15m OnUnitActiveSec timer>}"
log "max tool iters:    ${SELFEVO_MAX_TOOL_ITERATIONS:-<unset — falls back to config default>}"
log "note: any variable also set (uncommented) in the instance env file"
log "      ($INSTANCE_ENV_FILE) overrides this preset — see WARNING lines above, if any."
