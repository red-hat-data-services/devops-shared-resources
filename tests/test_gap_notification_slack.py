"""Offline tests for the GAP Slack notifier."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from lib.gap_notification_slack import (
    SLACK_CHAT_POST_MESSAGE,
    SLACK_MAX_RETRY_AFTER,
    SlackAmbiguousError,
    SlackNotifier,
    SlackPermanentError,
    SlackRateLimitError,
    SlackTransientError,
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


class SlackNotifierTests(unittest.TestCase):
    def test_send_success_with_thread_and_client_message_id(self):
        session = FakeSession(response=FakeResponse({"ok": True, "channel": "C123", "ts": "1700000000.123"}))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session, timeout=11)

        receipt = notifier.send("hello", thread_ts="1700000000.111", client_msg_id="msg-1")

        self.assertEqual(receipt, {"channel": "C123", "ts": "1700000000.123"})
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, SLACK_CHAT_POST_MESSAGE)
        self.assertEqual(kwargs["timeout"], 11)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer xoxb-secret-token")
        self.assertEqual(kwargs["json"]["channel"], "C123")
        self.assertEqual(kwargs["json"]["text"], "hello")
        self.assertFalse(kwargs["json"]["unfurl_links"])
        self.assertFalse(kwargs["json"]["unfurl_media"])
        self.assertEqual(kwargs["json"]["thread_ts"], "1700000000.111")
        self.assertEqual(kwargs["json"]["client_msg_id"], "msg-1")
        self.assertNotIn("token", repr(notifier))

    def test_invalid_channel_is_rejected_before_session_factory_runs(self):
        for channel in (None, "", "   ", 123):
            with self.subTest(channel=channel):
                with patch("requests.Session") as factory:
                    with self.assertRaises((TypeError, ValueError)):
                        SlackNotifier("xoxb-secret-token", channel)
                    factory.assert_not_called()

    def test_constructor_initializes_public_session_once_and_reuses_it_for_multiple_sends(self):
        with patch("requests.Session") as factory:
            session = SimpleNamespace(post=Mock(return_value=FakeResponse({"ok": True, "channel": "C123", "ts": "1700000000.123"})))
            factory.return_value = session
            notifier = SlackNotifier("xoxb-secret-token", "C123")

            self.assertIs(notifier.session, session)
            self.assertFalse(session.trust_env)
            first = notifier.send("hello")
            second = notifier.send("world")

        factory.assert_called_once()
        self.assertEqual(first, {"channel": "C123", "ts": "1700000000.123"})
        self.assertEqual(second, {"channel": "C123", "ts": "1700000000.123"})
        self.assertEqual(len(session.post.mock_calls), 2)

    def test_send_uses_updated_public_fields_and_does_not_touch_original_session(self):
        original_session = FakeSession(response=FakeResponse({"ok": True, "channel": "C111", "ts": "1700000000.111"}))
        replacement_session = FakeSession(response=FakeResponse({"ok": True, "channel": "C123", "ts": "1700000000.123"}))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=original_session, timeout=11)

        notifier.token = "xoxb-updated-token"
        notifier.session = replacement_session
        notifier.timeout = 17

        receipt = notifier.send("hello")

        self.assertEqual(receipt, {"channel": "C123", "ts": "1700000000.123"})
        self.assertEqual(original_session.calls, [])
        self.assertEqual(len(replacement_session.calls), 1)
        url, kwargs = replacement_session.calls[0]
        self.assertEqual(url, SLACK_CHAT_POST_MESSAGE)
        self.assertEqual(kwargs["timeout"], 17)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer xoxb-updated-token")
        self.assertEqual(kwargs["json"]["channel"], "C123")

    def test_ok_false_known_permanent_codes_on_http_200_are_permanent(self):
        for error in ("missing_scope", "not_in_channel", "invalid_auth"):
            with self.subTest(error=error):
                session = FakeSession(response=FakeResponse({"ok": False, "error": error}, 200))
                notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)

                with self.assertRaises(SlackPermanentError) as raised:
                    notifier.send("hello")

                self.assertEqual(len(session.calls), 1)
                self.assertEqual(raised.exception.error, error)
                self.assertEqual(raised.exception.status_code, 200)
                self.assertNotIn("xoxb-secret-token", str(raised.exception))

    def test_http_500_is_ambiguous_and_redacted(self):
        session = FakeSession(response=FakeResponse({"ok": False, "error": "internal_error", "token": "secret"}, 500))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)

        with self.assertRaises(SlackAmbiguousError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.error, "unknown_error")
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("xoxb-secret-token", str(raised.exception))

    def test_known_rate_limit_on_http_500_is_ambiguous(self):
        session = FakeSession(response=FakeResponse({"ok": False, "error": "ratelimited"}, 500, {"Retry-After": "2"}))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)

        with self.assertRaises(SlackAmbiguousError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.error, "ratelimited")
        self.assertEqual(raised.exception.status_code, 500)

    def test_non_json_http_429_is_rate_limited(self):
        session = FakeSession(response=FakeResponse(None, 429, {"Retry-After": "3"}))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)
        with self.assertRaises(SlackRateLimitError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.retry_after, 3.0)
        self.assertEqual(raised.exception.error, "unknown_error")

    def test_timeout_is_ambiguous(self):
        session = FakeSession(side_effect=requests.Timeout("xoxb-secret-token"))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)

        with self.assertRaises(SlackAmbiguousError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertIsNone(raised.exception.retry_after)
        self.assertNotIn("xoxb-secret-token", str(raised.exception))

    def test_request_exception_is_ambiguous_and_redacted(self):
        session = FakeSession(side_effect=requests.ConnectionError("xoxb-secret-token"))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)

        with self.assertRaises(SlackAmbiguousError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertIsNone(raised.exception.retry_after)
        self.assertNotIn("xoxb-secret-token", str(raised.exception))

    def test_rate_limit_exposes_retry_after(self):
        session = FakeSession(
            response=FakeResponse({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "2.5"})
        )
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)

        with self.assertRaises(SlackRateLimitError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.retry_after, 2.5)
        self.assertEqual(raised.exception.error, "ratelimited")
        self.assertEqual(raised.exception.status_code, 429)

    def test_rate_limit_retry_after_is_capped(self):
        session = FakeSession(response=FakeResponse({"ok": False, "error": "rate_limited"}, 429, {"Retry-After": "7200"}))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)

        with self.assertRaises(SlackRateLimitError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.retry_after, SLACK_MAX_RETRY_AFTER)

    def test_rate_limit_without_header_is_still_rate_limited(self):
        session = FakeSession(response=FakeResponse({"ok": False, "error": "rate_limited"}, 200, {}))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)

        with self.assertRaises(SlackRateLimitError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        # Missing server guidance gets a bounded fallback, never a tight retry.
        self.assertEqual(raised.exception.retry_after, 1.0)

    def test_malformed_success_is_ambiguous(self):
        cases = (
            {"ok": True, "channel": "C999", "ts": "1700000000.123"},
            {"ok": True, "channel": "C123"},
            {"ok": True, "channel": "C123", "ts": 123},
        )
        for data in cases:
            with self.subTest(data=data):
                session = FakeSession(response=FakeResponse(data, 200))
                notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)
                with self.assertRaises(SlackAmbiguousError):
                    notifier.send("hello")
                self.assertEqual(len(session.calls), 1)

    def test_success_on_non_2xx_is_ambiguous(self):
        session = FakeSession(response=FakeResponse({"ok": True, "channel": "C123", "ts": "1700000000.123"}, 302))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)
        with self.assertRaises(SlackAmbiguousError):
            notifier.send("hello")
        self.assertEqual(len(session.calls), 1)

    def test_unexpected_body_is_ambiguous(self):
        session = FakeSession(response=FakeResponse(["not", "a", "dict"], 200))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)
        with self.assertRaises(SlackAmbiguousError):
            notifier.send("hello")
        self.assertEqual(len(session.calls), 1)

    def test_missing_ok_with_known_error_code_is_ambiguous(self):
        session = FakeSession(response=FakeResponse({"error": "missing_scope"}, 200))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)
        with self.assertRaises(SlackAmbiguousError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.error, "missing_scope")
        self.assertEqual(raised.exception.status_code, 200)

    def test_unknown_error_is_sanitized_to_constant(self):
        session = FakeSession(response=FakeResponse({"ok": False, "error": "super-secret-body"}, 400))
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=session)
        with self.assertRaises(SlackAmbiguousError) as raised:
            notifier.send("hello")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.error, "unknown_error")
        self.assertNotIn("super-secret-body", str(raised.exception))
        self.assertNotIn("xoxb-secret-token", str(raised.exception))

    def test_rejections_do_not_leak_request_or_response_content(self):
        response = FakeResponse({"ok": False, "error": "internal_error", "body": "super-secret-body"}, 500)
        notifier = SlackNotifier("xoxb-secret-token", "C123", session=FakeSession(response=response))

        with self.assertRaises(SlackAmbiguousError) as raised:
            notifier.send("hello")

        self.assertEqual(len(notifier.session.calls), 1)
        self.assertNotIn("super-secret-body", str(raised.exception))
        self.assertNotIn("Authorization", str(raised.exception))
        self.assertNotIn("xoxb-secret-token", str(raised.exception))

    def test_defaults_use_a_real_session_only_when_needed(self):
        with patch("requests.Session") as factory:
            session = SimpleNamespace(post=Mock(return_value=FakeResponse({"ok": True, "channel": "C123", "ts": "1.2"})))
            factory.return_value = session
            notifier = SlackNotifier("xoxb-secret-token", "C123")
            receipt = notifier.send("hello")

        factory.assert_called_once()
        self.assertFalse(session.trust_env)
        self.assertEqual(receipt, {"channel": "C123", "ts": "1.2"})

    def test_exported_retry_cap_constant_remains_available(self):
        self.assertEqual(SLACK_MAX_RETRY_AFTER, 3600.0)


if __name__ == "__main__":
    unittest.main()
