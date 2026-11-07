"""Thin GAP Slack facade guarantees; transport policy lives in SDK contracts."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lib import slack_client
from lib.gap_notifications import notify_tools
from lib.gap_notifications.notify_tools import SlackNotifier


def session_for(channel="C123", ts="1700000000.123"):
    response = SimpleNamespace(
        status_code=200, headers={},
        json=lambda: {"ok": True, "channel": channel, "ts": ts},
    )
    return SimpleNamespace(post=Mock(return_value=response))


class SlackNotifierTests(unittest.TestCase):
    def test_send_delegates_arguments_receipt_and_error_instance(self):
        session = session_for()
        receipt = {"channel": "C123", "ts": "1700000000.123"}
        client = Mock(session=session)
        client.post_message.return_value = receipt
        with patch.object(notify_tools, "SlackClient", return_value=client) as factory:
            notifier = SlackNotifier("dummy-token", "C123", session=session, timeout=11)
            client.post_message.assert_not_called()
            factory.reset_mock()
            self.assertIs(notifier.send("hello", thread_ts="1.1", client_msg_id="msg-1"), receipt)
            factory.assert_called_once_with("dummy-token", session=session, timeout=11)
            client.post_message.assert_called_once_with("C123", "hello", thread_ts="1.1", client_msg_id="msg-1")
            self.assertNotIn("token", repr(notifier))

            # Facade must not swallow/reclassify the underlying error instance.
            error = slack_client.SlackAmbiguousError("safe ambiguity", status_code=503, error="unknown_error")
            client.post_message.side_effect = error
            with self.assertRaises(slack_client.SlackAmbiguousError) as raised:
                notifier.send("again")
            self.assertIs(raised.exception, error)
            self.assertEqual(client.post_message.call_count, 2)

    def test_invalid_channel_is_rejected_before_session_factory_runs(self):
        for channel in (None, "", "   ", 123):
            with self.subTest(channel=channel):
                with patch("requests.Session") as factory:
                    with self.assertRaises((TypeError, ValueError)):
                        SlackNotifier("xoxb-secret-token", channel)
                    factory.assert_not_called()

    def test_constructor_initializes_public_session_once_and_reuses_it_for_multiple_sends(self):
        with patch("requests.Session") as factory:
            session = session_for()
            factory.return_value = session
            notifier = SlackNotifier("dummy-token", "C123")
            self.assertIs(notifier.session, session)
            self.assertFalse(session.trust_env)
            session.post.assert_not_called()
            first = notifier.send("hello")
            second = notifier.send("world")
        factory.assert_called_once()
        self.assertEqual(first, {"channel": "C123", "ts": "1700000000.123"})
        self.assertEqual(second, first)
        self.assertEqual(session.post.call_count, 2)

    def test_send_uses_updated_public_fields_and_does_not_touch_original_session(self):
        original_session = session_for("C111")
        replacement_session = session_for("C456")
        notifier = SlackNotifier("dummy-first", "C123", session=original_session, timeout=11)
        notifier.token = "dummy-updated"
        notifier.channel = "C456"
        notifier.session = replacement_session
        notifier.timeout = 17
        receipt = notifier.send("hello")
        self.assertEqual(receipt, {"channel": "C456", "ts": "1700000000.123"})
        original_session.post.assert_not_called()
        replacement_session.post.assert_called_once()
        args, kwargs = replacement_session.post.call_args
        self.assertEqual(args, (slack_client.SLACK_CHAT_POST_MESSAGE,))
        self.assertEqual(kwargs["timeout"], 17)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer dummy-updated")
        self.assertEqual(kwargs["json"]["channel"], "C456")

    def test_shared_exception_and_constant_exports(self):
        for name in ("SlackAmbiguousError", "SlackPermanentError", "SlackRateLimitError", "SlackTransientError"):
            self.assertIs(getattr(notify_tools, name), getattr(slack_client, name))
        self.assertEqual(notify_tools.SLACK_MAX_RETRY_AFTER, 3600.0)
        self.assertEqual(notify_tools.SLACK_MAX_RETRY_AFTER, slack_client.SLACK_MAX_RETRY_AFTER)
        self.assertEqual(notify_tools.SLACK_CHAT_POST_MESSAGE, slack_client.SLACK_CHAT_POST_MESSAGE)
