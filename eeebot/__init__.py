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
    'eeebot.config': 'nanobot.config',
    'eeebot.config.loader': 'nanobot.config.loader',
    'eeebot.config.paths': 'nanobot.config.paths',
    'eeebot.utils': 'nanobot.utils',
    'eeebot.utils.helpers': 'nanobot.utils.helpers',
}.items():
    sys.modules.setdefault(_alias, importlib.import_module(_target))
