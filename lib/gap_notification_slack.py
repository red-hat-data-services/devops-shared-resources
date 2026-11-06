"""Slack delivery helper for the GAP notification prototype."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lib.slack_client import (
    SLACK_CHAT_POST_MESSAGE,
    SLACK_MAX_RETRY_AFTER,
    SlackAmbiguousError,
    SlackClient,
    SlackError,
    SlackPermanentError,
    SlackRateLimitError,
    SlackTransientError,
)


@dataclass
class SlackNotifier:
    token: str = field(repr=False)
    channel: str
    session: Any = None
    timeout: int = 20

    def __post_init__(self) -> None:
        self._validate_inputs()
        self.session = self._client().session

    def _validate_inputs(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("token must be a non-empty string")
        if not isinstance(self.channel, str) or not self.channel.strip():
            raise ValueError("channel must be a non-empty string")
        if self.session is not None and not hasattr(self.session, "post"):
            raise TypeError("session must provide post()")
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _client(self) -> SlackClient:
        return SlackClient(self.token, session=self.session, timeout=self.timeout)

    def send(self, text: str, *, thread_ts: str | None = None, client_msg_id: str | None = None) -> dict[str, str]:
        self._validate_inputs()
        return self._client().post_message(
            self.channel,
            text,
            thread_ts=thread_ts,
            client_msg_id=client_msg_id,
        )
