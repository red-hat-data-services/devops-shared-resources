"""Offline tests for the reusable Slack client."""

from __future__ import annotations
from service_client_fakes import FakeResponse

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lib.slack_client import (
    SlackClient,
)


class FakeSession:
    def __init__(self, *, response=None, responses=None, side_effect=None):
        self.response = response
        self.responses = list(responses or [])
        self.side_effect = side_effect
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.side_effect is not None:
            raise self.side_effect
        if self.responses:
            return self.responses.pop(0)
        return self.response


class SlackClientTests(unittest.TestCase):
    # A reusable Slack client must serve multiple channels while preserving each returned
    # receipt.
    def test_client_can_be_reused_for_multiple_channels(self):
        session = FakeSession(
            responses=[
                FakeResponse({"ok": True, "channel": "C111", "ts": "1700000000.111"}),
                FakeResponse({"ok": True, "channel": "C222", "ts": "1700000000.222"}),
            ]
        )
        client = SlackClient("xoxb-reusable-token", session=session, timeout=11)

        self.assertNotIn("token", repr(client))
        first = client.post_message("C111", "hello")
        second = client.post_message("C222", "world")

        self.assertEqual(first, {"channel": "C111", "ts": "1700000000.111"})
        self.assertEqual(second, {"channel": "C222", "ts": "1700000000.222"})
        self.assertEqual([kwargs["json"]["channel"] for _, kwargs in session.calls], ["C111", "C222"])

    # Invalid channels or message text must fail without sending a request.
    def test_post_message_rejects_invalid_channel_or_text(self):
        client = SlackClient("xoxb-reusable-token", session=FakeSession(response=FakeResponse({"ok": True, "channel": "C321", "ts": "1.2"})))
        with self.assertRaises(ValueError):
            client.post_message("", "hello")
        with self.assertRaises(ValueError):
            client.post_message("C321", "")
        self.assertEqual(client.session.calls, [])

    # The default Slack session must be created once without sending during construction.
    def test_default_session_is_created_only_when_needed(self):
        with patch("requests.Session") as factory:
            session = SimpleNamespace(post=Mock(return_value=FakeResponse({"ok": True, "channel": "C321", "ts": "1.2"})))
            factory.return_value = session
            client = SlackClient("xoxb-reusable-token")
            session.post.assert_not_called()
            receipt = client.post_message("C321", "hello")

        factory.assert_called_once()
        self.assertFalse(session.trust_env)
        self.assertEqual(receipt, {"channel": "C321", "ts": "1.2"})
