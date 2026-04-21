"""Compatibility package alias for the eeebot public project name."""

from __future__ import annotations

from pathlib import Path
import importlib
import sys

import nanobot as _nanobot
from nanobot import __logo__, __version__

# Extend the eeebot package search path so imports like eeebot.agent.loop and
# eeebot.runtime.state resolve to the existing nanobot package tree unless an
# explicit eeebot compatibility shim exists locally.
__path__ = [str(Path(__file__).parent), *list(_nanobot.__path__)]

# Explicitly alias the highest-value runtime modules so they map to the exact
# same module objects instead of being imported twice under a second package
# name.
for _alias, _target in {
    'eeebot.agent': 'nanobot.agent',
    'eeebot.agent.loop': 'nanobot.agent.loop',
    'eeebot.agent.context': 'nanobot.agent.context',
    'eeebot.agent.memory': 'nanobot.agent.memory',
    'eeebot.agent.skills': 'nanobot.agent.skills',
    'eeebot.agent.subagent': 'nanobot.agent.subagent',
    'eeebot.bus': 'nanobot.bus',
    'eeebot.bus.events': 'nanobot.bus.events',
    'eeebot.bus.queue': 'nanobot.bus.queue',
    'eeebot.channels': 'nanobot.channels',
    'eeebot.channels.base': 'nanobot.channels.base',
    'eeebot.channels.manager': 'nanobot.channels.manager',
    'eeebot.channels.registry': 'nanobot.channels.registry',
    'eeebot.config': 'nanobot.config',
    'eeebot.config.loader': 'nanobot.config.loader',
    'eeebot.config.paths': 'nanobot.config.paths',
    'eeebot.cron': 'nanobot.cron',
    'eeebot.cron.service': 'nanobot.cron.service',
    'eeebot.cron.types': 'nanobot.cron.types',
    'eeebot.heartbeat': 'nanobot.heartbeat',
    'eeebot.heartbeat.service': 'nanobot.heartbeat.service',
    'eeebot.providers': 'nanobot.providers',
    'eeebot.providers.base': 'nanobot.providers.base',
    'eeebot.providers.registry': 'nanobot.providers.registry',
    'eeebot.utils': 'nanobot.utils',
    'eeebot.utils.helpers': 'nanobot.utils.helpers',
    'eeebot.utils.evaluator': 'nanobot.utils.evaluator',
}.items():
    sys.modules.setdefault(_alias, importlib.import_module(_target))
