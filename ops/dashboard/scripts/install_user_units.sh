#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
install -m 0644 "$ROOT/systemd/nanobot-ops-dashboard-web.service" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/nanobot-ops-dashboard-collector.service" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/eeebot-ops-dashboard-web.service" "$UNIT_DIR/"
install -m 0644 "$ROOT/systemd/eeebot-ops-dashboard-collector.service" "$UNIT_DIR/"
systemctl --user daemon-reload

echo "Installed units to $UNIT_DIR"
echo "Compatibility units: nanobot-ops-dashboard-web.service / nanobot-ops-dashboard-collector.service"
echo "New identity units: eeebot-ops-dashboard-web.service / eeebot-ops-dashboard-collector.service"
echo "Next: systemctl --user enable --now eeebot-ops-dashboard-web.service"
echo "Next: systemctl --user enable --now eeebot-ops-dashboard-collector.service"
