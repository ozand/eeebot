"""eeebot channels compatibility package."""

from __future__ import annotations

from pathlib import Path

import nanobot.channels as _channels
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager

__path__ = [str(Path(__file__).parent), *list(_channels.__path__)]

__all__ = ['BaseChannel', 'ChannelManager']
