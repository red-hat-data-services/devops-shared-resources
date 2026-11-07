"""Real pinned SDK flow with in-memory transport and blocked outbound sockets."""

import logging
import socket
from http.client import RemoteDisconnected
from unittest.mock import Mock

import pytest
import requests
from slack_sdk import WebClient
from slack_sdk.version import __version__

from lib import slack_client as module
from lib.slack_client import SlackAmbiguousError, SlackClient, SlackPermanentError, SlackRateLimitError


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


@pytest.fixture(autouse=True)
def no_network_or_discovery(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network/discovery forbidden in SDK contract tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr("slack_sdk.web.base_client.urlopen", forbidden)
    monkeypatch.setattr("slack_sdk.web.base_client.load_http_proxy_from_env", forbidden)
    monkeypatch.setattr(WebClient, "__init__", forbidden)
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(key, "http://synthetic-user:proxy-secret@example.invalid:8080")


def session_for(data, status=200, headers=None):
    return Mock(post=Mock(return_value=Response(data, status, headers)))


# The pinned Slack SDK must preserve message arguments, receipts and explicit no-retry transport
# settings.
def test_real_sdk_post_message_request_and_receipt(monkeypatch):
    assert __version__ == "3.46.0"
    original = WebClient.chat_postMessage
    called = []

    def call_through(self, **kwargs):
        # These are our bridge settings, not an upstream call-order contract.
        assert self.retry_handlers == []
        assert self.proxy is None
        assert self._logger.disabled and not self._logger.propagate
        called.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(WebClient, "chat_postMessage", call_through)
    session = session_for({"ok": True, "channel": "C123", "ts": "1.2"})
    client = SlackClient("dummy-token", session=session, timeout=7)
    assert client.post_message("C123", "hello", thread_ts="1.1", client_msg_id="d7a85bb7-329a-49fd-9a16-e03c53b80656") == {"channel": "C123", "ts": "1.2"}
    expected = {"channel": "C123", "text": "hello", "thread_ts": "1.1", "client_msg_id": "d7a85bb7-329a-49fd-9a16-e03c53b80656", "unfurl_links": False, "unfurl_media": False}
    assert called == [expected]
    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args == (module.SLACK_CHAT_POST_MESSAGE,)
    assert kwargs["timeout"] == 7 and kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Authorization"] == "Bearer dummy-token"
    assert kwargs["json"] == expected


# Slack redirects must remain ambiguous and must never be followed automatically.
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_never_followed(status):
    session = session_for({"ok": True, "channel": "C123", "ts": "1.2"}, status,
                          {"Location": "https://elsewhere.invalid/credential-target"})
    with pytest.raises(SlackAmbiguousError):
        SlackClient("dummy", session).post_message("C123", "hello")
    session.post.assert_called_once()
    assert session.post.call_args.kwargs["allow_redirects"] is False


# Connection loss must produce one sanitized ambiguous send outcome without SDK log leakage.
@pytest.mark.parametrize("error", [RemoteDisconnected("secret-after-send"), ConnectionResetError("secret-after-send"), TimeoutError("secret-after-send"), requests.Timeout("secret-after-send"), requests.ConnectionError("secret-after-send")])
def test_connection_loss_is_one_attempt_without_sdk_logs(error, caplog):
    caplog.set_level(logging.DEBUG)
    session = Mock(post=Mock(side_effect=error))
    with pytest.raises(SlackAmbiguousError) as raised:
        SlackClient("secret-token", session).post_message("C123", "secret-message")
    session.post.assert_called_once()
    assert "secret" not in str(raised.value)
    assert raised.value.__suppress_context__
    assert raised.value.retry_after is None
    assert raised.value.__cause__ is None
    assert "secret" not in caplog.text


# Slack response errors must preserve safe classification and metadata without exposing response
# secrets.
@pytest.mark.parametrize("data,status,kind,safe_error", [
    ({"ok": 1, "channel": "C123", "ts": "1.2"}, 200, SlackAmbiguousError, "unknown_error"),
    ({"ok": "true", "channel": "C123", "ts": "1.2"}, 200, SlackAmbiguousError, "unknown_error"),
    ({"ok": 0, "error": "missing_scope"}, 200, SlackAmbiguousError, "missing_scope"),
    ({"error": "missing_scope"}, 400, SlackAmbiguousError, "missing_scope"),
    ({"error": "missing_scope"}, 200, SlackAmbiguousError, "missing_scope"),
    ({"ok": True, "channel": "COTHER", "ts": "1.2"}, 200, SlackAmbiguousError, None),
    ({"ok": True, "channel": "C123", "ts": " "}, 200, SlackAmbiguousError, None),
    ({"ok": True, "channel": "C123", "ts": 1.2}, 200, SlackAmbiguousError, None),
    ({"ok": True, "channel": "C123"}, 200, SlackAmbiguousError, None),
    (["secret-body"], 200, SlackAmbiguousError, None),
    (ValueError("secret-malformed-json"), 200, SlackAmbiguousError, None),
    (ValueError("secret-malformed-json"), 429, SlackRateLimitError, "unknown_error"),
    (None, 429, SlackRateLimitError, "unknown_error"),
    ({"ok": False, "error": "secret-error"}, 400, SlackAmbiguousError, "unknown_error"),
    ({"ok": False, "error": "internal_error", "token": "secret-token", "body": "secret-body"}, 500, SlackAmbiguousError, "unknown_error"),
    ({"ok": False, "error": "missing_scope"}, 500, SlackAmbiguousError, "missing_scope"),
    ({"ok": False, "error": "missing_scope"}, 403, SlackPermanentError, "missing_scope"),
    ({"ok": False, "error": "not_in_channel"}, 403, SlackPermanentError, "not_in_channel"),
    ({"ok": False, "error": "missing_scope"}, 200, SlackPermanentError, "missing_scope"),
    ({"ok": False, "error": "not_in_channel"}, 200, SlackPermanentError, "not_in_channel"),
    ({"ok": False, "error": "invalid_auth"}, 200, SlackPermanentError, "invalid_auth"),
    ({"ok": False, "error": "ratelimited"}, 200, SlackRateLimitError, "ratelimited"),
    ({"ok": False, "error": "ratelimited"}, 500, SlackAmbiguousError, "ratelimited"),
])
def test_strict_outer_policy_and_no_error_leaks(data, status, kind, safe_error, caplog):
    caplog.set_level(logging.DEBUG)
    session = session_for(data, status, {"Retry-After": "2.5"})
    with pytest.raises(kind) as raised:
        SlackClient("secret-token", session).post_message("C123", "secret-text")
    session.post.assert_called_once()
    assert raised.value.status_code == status
    assert raised.value.error == safe_error
    assert raised.value.retry_after == (2.5 if kind is SlackRateLimitError else None)
    assert "secret" not in str(raised.value) and "secret" not in caplog.text
    assert "Authorization" not in str(raised.value)
    assert raised.value.__context__ is None and raised.value.__cause__ is None


# Valid receipts from supported successful HTTP statuses must survive the SDK's narrower status
# check.
@pytest.mark.parametrize("status", [200, 201, 202, 299])
def test_valid_2xx_receipt_preserved_despite_sdk_200_only(status):
    session = session_for({"ok": True, "channel": "C123", "ts": "1.2"}, status)
    assert SlackClient("dummy", session).post_message("C123", "hello") == {"channel": "C123", "ts": "1.2"}
    session.post.assert_called_once()


# Rate-limit metadata must be bounded and returned to the caller without sleeping or retrying.
@pytest.mark.parametrize("headers,status,error,expected", [
    ({"Retry-After": "2.5"}, 429, "ratelimited", 2.5),
    ({"Retry-After": "7200"}, 429, "ratelimited", 3600),
    ({"Retry-After": "7200"}, 429, "rate_limited", 3600),
    ({"Retry-After": "-1"}, 429, "ratelimited", 1),
    ({"Retry-After": "NaN"}, 429, "ratelimited", 1),
    ({"Retry-After": "inf"}, 429, "ratelimited", 1),
    ({"Retry-After": "invalid"}, 429, "ratelimited", 1),
    ({"Retry-After": None}, 429, "ratelimited", 1),
    ({}, 429, "ratelimited", 1),
    ({}, 200, "rate_limited", 1),
])
def test_rate_limit_metadata_is_bounded_not_slept(headers, status, error, expected):
    session = session_for({"ok": False, "error": error}, status, headers)
    with pytest.raises(SlackRateLimitError) as raised:
        SlackClient("dummy", session).post_message("C123", "hello")
    assert raised.value.retry_after == expected
    assert raised.value.status_code == status
    assert raised.value.error == error
    session.post.assert_called_once()


# Unsupported Slack SDK versions must fail before any send.
def test_pin_drift_fails_before_send(monkeypatch):
    monkeypatch.setattr(module, "_sdk_version", "future-version")
    session = session_for({"ok": True})
    with pytest.raises(RuntimeError, match="Unsupported Slack SDK version"):
        SlackClient("dummy", session).post_message("C123", "hello")
    session.post.assert_not_called()
