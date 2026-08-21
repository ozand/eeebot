# eeepc host configuration — source of truth
#
# This directory contains everything needed to reproduce the eeepc
# self-evolving agent runtime on a fresh host.
#
# Directory layout:
#
#   host/eeepc/
#   ├── scripts/
#   │   ├── install.sh          — first-time host setup (run once as root)
#   │   └── deploy_release.sh   — push a new code release from dev machine
#   ├── systemd/
#   │   ├── *.service / *.timer — systemd unit files → /etc/systemd/system/
#   │   └── drop-ins/           — override drop-ins → /etc/systemd/system/*.d/
#   ├── libexec/
#   │   └── eeepc-self-evolving-subagent-bridge.py  → /usr/local/libexec/
#   └── etc/
#       ├── instances/          → /etc/eeepc-agent/instances/*.env
#       ├── litellm.env.example → /etc/eeepc-agent/litellm.env  (fill key!)
#       └── nanobot-config.template.json → /home/opencode/.nanobot-eeepc/config.template.json
#
# ## Fresh host setup
#
# 1. Clone the repo:
#    git clone https://github.com/ozand/eeebot /opt/eeebot-src
#    cd /opt/eeebot-src
#
# 2. Set credentials:
#    export LITELLM_API_KEY=sk-...
#    export LITELLM_BASE_URL=https://<your-litellm-proxy>/v1
#
# 3. Run install:
#    sudo bash host/eeepc/scripts/install.sh
#
# 4. Set nanobot gateway config key:
#    sudo nano /home/opencode/.nanobot-eeepc/config.template.json
#    # replace sk-REDACTED with real key in providers.custom.apiKey
#
# 5. Start the agent:
#    sudo systemctl start eeepc-self-evolving-agent.service
#    sudo journalctl -u eeepc-self-evolving-agent.service -f
#
# ## Deploying a code update (from dev machine)
#
#    bash host/eeepc/scripts/deploy_release.sh --host eeepc
#
# ## Host-specific things NOT in this repo (secrets, runtime state)
#
# These must be created manually or restored from backup:
# - /etc/eeepc-agent/litellm.env           (real LITELLM_API_KEY)
# - /home/opencode/.nanobot-eeepc/config.template.json (real apiKey in providers.custom)
# - /var/lib/eeepc-agent/self-evolving-agent/state/    (runtime state — not in git)
# - /home/opencode/.venvs/nanobot/                     (opencode user venv for gateway)
#
# ## Runtime architecture
#
# Component                  | Managed by
# ---------------------------|------------------------------------------
# eeepc-self-evolving-agent  | systemd timer (continuous evolution loop)
# eeepc-self-evolving-subagent-bridge | systemd timer (every 15min, real LLM review)
# eeepc-self-evolving-agent-health    | systemd timer (dry-run health check)
# eeepc-strong-reflection    | systemd timer (every 6h, deep review)
# nanobot gateway            | opencode user systemd (WebSocket/Telegram bridge)
#
# Full operational runbook was EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md (folded into
# docs/specs/host-runtime/spec.md, removed 2026-07-05, #613; recoverable from git history).
