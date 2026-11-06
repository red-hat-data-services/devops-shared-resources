"""Offline tests for the reusable Slack client."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from lib.slack_client import (
    SLACK_CHAT_POST_MESSAGE,
    SlackAmbiguousError,
    SlackClient,
    SlackPermanentError,
    SlackRateLimitError,
)


class FakeResponse:
    def __init__(self, data=None, status_code=200, headers=None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._data


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
    def test_post_message_success_with_thread_and_client_message_id(self):
        session = FakeSession(response=FakeResponse({"ok": True, "channel": "C321", "ts": "1700000000.123"}))
        client = SlackClient("xoxb-reusable-token", session=session, timeout=11)

        receipt = client.post_message("C321", "hello", thread_ts="1700000000.111", client_msg_id="msg-1")

        self.assertEqual(receipt, {"channel": "C321", "ts": "1700000000.123"})
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, SLACK_CHAT_POST_MESSAGE)
        self.assertEqual(kwargs["timeout"], 11)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer xoxb-reusable-token")
        self.assertEqual(kwargs["json"]["channel"], "C321")
        self.assertEqual(kwargs["json"]["text"], "hello")
        self.assertFalse(kwargs["json"]["unfurl_links"])
        self.assertFalse(kwargs["json"]["unfurl_media"])
        self.assertEqual(kwargs["json"]["thread_ts"], "1700000000.111")
        self.assertEqual(kwargs["json"]["client_msg_id"], "msg-1")
        self.assertNotIn("token", repr(client))

    def test_client_can_be_reused_for_multiple_channels(self):
        session = FakeSession(
            responses=[
                FakeResponse({"ok": True, "channel": "C111", "ts": "1700000000.111"}),
                FakeResponse({"ok": True, "channel": "C222", "ts": "1700000000.222"}),
            ]
        )
        client = SlackClient("xoxb-reusable-token", session=session, timeout=11)

        first = client.post_message("C111", "hello")
        second = client.post_message("C222", "world")

        self.assertEqual(first, {"channel": "C111", "ts": "1700000000.111"})
        self.assertEqual(second, {"channel": "C222", "ts": "1700000000.222"})
        self.assertEqual([kwargs["json"]["channel"] for _, kwargs in session.calls], ["C111", "C222"])

    def test_post_message_rejects_invalid_channel_or_text(self):
        client = SlackClient("xoxb-reusable-token", session=FakeSession(response=FakeResponse({"ok": True, "channel": "C321", "ts": "1.2"})))
        with self.assertRaises(ValueError):
            client.post_message("", "hello")
        with self.assertRaises(ValueError):
            client.post_message("C321", "")

    def test_post_message_permanent_error_is_sanitized(self):
        session = FakeSession(response=FakeResponse({"ok": False, "error": "not_in_channel"}, 403))
        client = SlackClient("xoxb-reusable-token", session=session)

        with self.assertRaises(SlackPermanentError) as raised:
            client.post_message("C321", "hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.error, "not_in_channel")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertNotIn("xoxb-reusable-token", str(raised.exception))

    def test_post_message_rate_limit_exposes_retry_after(self):
        session = FakeSession(response=FakeResponse({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "2.5"}))
        client = SlackClient("xoxb-reusable-token", session=session)

        with self.assertRaises(SlackRateLimitError) as raised:
            client.post_message("C321", "hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.retry_after, 2.5)
        self.assertEqual(raised.exception.error, "ratelimited")
        self.assertEqual(raised.exception.status_code, 429)

    def test_post_message_ambiguous_on_unverified_or_failed_transport(self):
        bad_body_session = FakeSession(response=FakeResponse(["not", "a", "dict"], 200))
        bad_body = SlackClient("xoxb-reusable-token", session=bad_body_session)
        with self.assertRaises(SlackAmbiguousError):
            bad_body.post_message("C321", "hello")
        self.assertEqual(len(bad_body_session.calls), 1)

        timeout_session = FakeSession(side_effect=requests.Timeout("xoxb-reusable-token"))
        timeout = SlackClient("xoxb-reusable-token", session=timeout_session)
        with self.assertRaises(SlackAmbiguousError):
            timeout.post_message("C321", "hello")
        self.assertEqual(len(timeout_session.calls), 1)

    def test_default_session_is_created_only_when_needed(self):
        with patch("requests.Session") as factory:
            session = SimpleNamespace(post=Mock(return_value=FakeResponse({"ok": True, "channel": "C321", "ts": "1.2"})))
            factory.return_value = session
            client = SlackClient("xoxb-reusable-token")
            receipt = client.post_message("C321", "hello")

        factory.assert_called_once()
        self.assertFalse(session.trust_env)
        self.assertEqual(receipt, {"channel": "C321", "ts": "1.2"})
