#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ozand/herkoot/Projects/nanobot"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
install -m 0644 "$ROOT/systemd/eeebot-local-cycle.service" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/eeebot-local-cycle.timer" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/eeebot-local-approval-keeper.service" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/eeebot-local-approval-keeper.timer" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/eeebot-guarded-evolution.service" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/eeebot-guarded-evolution.timer" "$UNIT_DIR/"
systemctl --user daemon-reload

echo "Installed units to $UNIT_DIR"
echo "Next: systemctl --user enable --now eeebot-local-approval-keeper.timer"
echo "Next: systemctl --user enable --now eeebot-local-cycle.timer"
echo "Optional: systemctl --user enable --now eeebot-guarded-evolution.timer"
