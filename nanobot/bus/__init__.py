"""Message bus module for decoupled channel-agent communication."""

from eeebot.bus.events import InboundMessage, OutboundMessage
from eeebot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
