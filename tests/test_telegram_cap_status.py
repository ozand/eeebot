import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
pytest.importorskip('telegram')

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import TelegramChannel, TelegramConfig


class _DummyMessage:
    def __init__(self):
        self.calls = []

    async def reply_text(self, text):
        self.calls.append(text)


def test_cap_status_command_is_registered():
    assert any(cmd.command == 'cap_status' for cmd in TelegramChannel.BOT_COMMANDS)
    assert any(cmd.command == 'workspace' for cmd in TelegramChannel.BOT_COMMANDS)
    assert any(cmd.command == 'sub_run' for cmd in TelegramChannel.BOT_COMMANDS)


def test_help_mentions_cap_status():
    channel = TelegramChannel(TelegramConfig(enabled=False, token='x', allow_from=['*']), MessageBus())
    msg = _DummyMessage()
    update = SimpleNamespace(message=msg)
    asyncio.run(channel._on_help(update, None))
    assert msg.calls
    assert '/cap_status' in msg.calls[0]
    assert '/workspace' in msg.calls[0]
    assert '/sub_run' in msg.calls[0]
