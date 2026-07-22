"""Channel abstraction: extra doors into the same single conversation.

A channel connects an external messenger (Telegram today; Matrix, Discord,
Signal bridges, … tomorrow) to the one shared brain. To add a messenger,
subclass Channel, implement start/send/stop, and register it in the app —
the brain, tasks and reminders need no changes because every channel funnels
into the same handle_message callback and receives the same proactive nudges.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

# incoming text from the user → the assistant's reply
HandleMessage = Callable[[str], Awaitable[str]]


class Channel(ABC):
    name: str = "channel"

    def __init__(self, handle_message: HandleMessage) -> None:
        self.handle_message = handle_message

    @abstractmethod
    async def start(self) -> None:
        """Connect and begin receiving messages."""

    @abstractmethod
    async def send(self, text: str) -> None:
        """Proactively push a message (reminders, nudges) to the user."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect cleanly."""
