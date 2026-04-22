from __future__ import annotations

from typing import Any

from nanobot.runtime.state import format_runtime_state, load_runtime_state


def build_action_registry_snapshot(workspace) -> dict[str, Any]:
    runtime = load_runtime_state(workspace)
    return {
        'version': 'action-registry-v1',
        'actions': {
            'capability.truth-check': {
                'kind': 'read_only',
                'renderer': 'runtime_state',
                'status': 'available',
                'description': 'Render canonical runtime truth from the selected runtime-state authority.',
            }
        },
        'default_action': 'capability.truth-check',
        'truth_check_preview': format_runtime_state(runtime)[:8],
    }
