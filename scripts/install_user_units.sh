#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ozand/herkoot/Projects/nanobot-ops-dashboard"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
install -m 0644 "$ROOT/systemd/nanobot-ops-dashboard-web.service" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/nanobot-ops-dashboard-collector.service" "$UNIT_DIR/"
systemctl --user daemon-reload

echo "Installed units to $UNIT_DIR"
echo "Next: systemctl --user enable --now nanobot-ops-dashboard-web.service"
echo "Next: systemctl --user enable --now nanobot-ops-dashboard-collector.service"
