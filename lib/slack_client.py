"""Reusable explicit Slack chat.postMessage transport and receipt validation.

Send caller-selected channel/text with optional thread timestamp and message ID,
disabled unfurls, explicit bearer token/session/timeout and no redirects. Verify
the service acknowledgement and channel/timestamp before returning a receipt;
sanitize errors into rate-limit, permanent, transient or ambiguous categories
without exposing raw response bodies or tokens.

There is no credential discovery, channel policy, history lookup, persistence,
automatic retry or exactly-once guarantee. A supplied client message ID does
not resolve uncertain outcomes by itself. Applications choose destinations,
record confirmed receipts and decide how to reconcile or hold failed sends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any

import requests
from slack_sdk import WebClient
from slack_sdk.version import __version__ as _sdk_version

SLACK_CHAT_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
SLACK_MAX_RETRY_AFTER = 3600.0
_UNKNOWN_SLACK_ERROR = "unknown_error"
_RATE_LIMIT_ERRORS = {"rate_limited", "ratelimited"}
_PERMANENT_ERRORS = {
    "channel_not_found",
    "not_in_channel",
    "invalid_auth",
    "account_inactive",
    "token_revoked",
    "missing_scope",
    "no_permission",
    "is_archived",
    "invalid_arguments",
    "msg_too_long",
    "restricted_action",
}
_SAFE_SLACK_ERRORS = _PERMANENT_ERRORS | _RATE_LIMIT_ERRORS | {_UNKNOWN_SLACK_ERROR}


class SlackError(RuntimeError):
    """Base class for sanitized Slack delivery errors."""

    def __init__(self, message: str, *, retry_after: float | None = None, status_code: int | None = None,
                 error: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code
        self.error = error


class SlackTransientError(SlackError):
    """A retryable or temporarily unavailable response."""


class SlackRateLimitError(SlackTransientError):
    """Slack asked the caller to back off."""


class SlackAmbiguousError(SlackError):
    """The send outcome could not be established safely."""


class SlackPermanentError(SlackError):
    """The request was rejected in a non-retryable way."""


class _SessionWebClient(WebClient):
    """Bounded slack_sdk 3.46.0 bridge, only for chat.postMessage.

    Initialize the BaseClient fields directly: its public constructor discovers
    environment proxies even for proxy="". Keep SDK method/payload construction,
    JSON serialization, retry loop (disabled), and response validation, replacing
    only the final HTTP boundary with the caller's requests-style session.
    Review this private coupling before changing the exact dependency pin.
    """

    def __init__(self, token, session, timeout):
        if _sdk_version != "3.46.0":
            raise RuntimeError("Unsupported Slack SDK version")
        self.token, self.timeout = token, timeout
        self.base_url = "https://slack.com/api/"
        self.ssl = self.proxy = None
        self.headers = {"User-Agent": "gap-slack/slack_sdk-3.46.0"}
        self.default_params = {}
        self.retry_handlers = []
        self._logger = logging.Logger("gap.slack.transport")
        self._logger.disabled = True
        self._logger.propagate = False
        self._session = session
        self.response = None

    def _perform_urllib_http_request_internal(self, url, req):
        if url != SLACK_CHAT_POST_MESSAGE or req.get_method() != "POST":
            raise ValueError("Unsupported Slack operation")
        # SDK has serialized the JSON already. Decode only to preserve the public
        # injected session.post(json=...) contract; requests does final encoding.
        self.response = self._session.post(
            url, headers=dict(req.header_items()), json=json.loads(req.data),
            timeout=self.timeout, allow_redirects=False,
        )
        return {
            "status": getattr(self.response, "status_code", None),
            "headers": getattr(self.response, "headers", {}),
            "body": json.dumps(SlackClient._response_json(self.response)),
        }


@dataclass
class SlackClient:
    token: str = field(repr=False)
    session: Any = None
    timeout: int = 20

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("token must be a non-empty string")
        if self.session is None:
            self.session = requests.Session()
            self.session.trust_env = False
        if not hasattr(self.session, "post"):
            raise TypeError("session must provide post()")
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def post_message(self, channel: str, text: str, *, thread_ts: str | None = None,
                     client_msg_id: str | None = None, blocks=None) -> dict[str, str]:
        if not isinstance(channel, str) or not channel.strip():
            raise ValueError("channel must be a non-empty string")
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")

        sdk = _SessionWebClient(self.token, self.session, self.timeout)
        try:
            sdk.chat_postMessage(
                channel=channel, text=text, thread_ts=thread_ts,
                client_msg_id=client_msg_id, unfurl_links=False, unfurl_media=False,
                **({"blocks": blocks} if blocks is not None else {}),
            )
        except Exception:
            # SDK validates only HTTP 200 and truthy ok, and can reject malformed
            # response shapes itself. Classify a captured response independently;
            # no response means uncertainty, never permission to retry.
            if sdk.response is None:
                raise SlackAmbiguousError("Slack delivery outcome could not be established") from None
        response = sdk.response
        status_code = getattr(response, "status_code", None)
        data = self._response_json(response)
        if status_code == 429:
            raise SlackRateLimitError(
                "Slack rate limit reached",
                retry_after=self._retry_after(getattr(response, "headers", {})),
                status_code=status_code,
                error=self._slack_error(data),
            )

        if not isinstance(data, dict):
            raise SlackAmbiguousError("Slack confirmation could not be verified", status_code=status_code)

        ok = data.get("ok")
        if ok is True:
            if not self._is_success_status(status_code):
                raise SlackAmbiguousError("Slack confirmation could not be verified", status_code=status_code)
            confirmed_channel = data.get("channel")
            ts = data.get("ts")
            if confirmed_channel != channel or not isinstance(ts, str) or not ts.strip():
                raise SlackAmbiguousError("Slack confirmation could not be verified", status_code=status_code)
            return {"channel": confirmed_channel, "ts": ts}

        if ok is not False:
            raise SlackAmbiguousError(
                "Slack delivery outcome could not be established",
                status_code=status_code,
                error=self._slack_error(data),
            )

        error = self._slack_error(data)
        if error in _RATE_LIMIT_ERRORS and self._is_success_status(status_code):
            raise SlackRateLimitError(
                "Slack rate limit reached",
                retry_after=self._retry_after(getattr(response, "headers", {})),
                status_code=status_code,
                error=error,
            )
        if error in _PERMANENT_ERRORS and self._is_definitive_error_status(status_code):
            raise SlackPermanentError(
                "Slack rejected the message delivery",
                status_code=status_code,
                error=error,
            )
        raise SlackAmbiguousError(
            "Slack delivery outcome could not be established",
            status_code=status_code,
            error=error,
        )

    @staticmethod
    def _response_json(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            return None

    @staticmethod
    def _slack_error(data: Any) -> str:
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, str) and error.strip() and error in _SAFE_SLACK_ERRORS:
                return error
        return _UNKNOWN_SLACK_ERROR

    @staticmethod
    def _is_rate_limit(data: dict[str, Any]) -> bool:
        return SlackClient._slack_error(data) in _RATE_LIMIT_ERRORS

    @staticmethod
    def _is_success_status(status_code: Any) -> bool:
        return isinstance(status_code, int) and 200 <= status_code < 300

    @staticmethod
    def _is_definitive_error_status(status_code: Any) -> bool:
        return isinstance(status_code, int) and (200 <= status_code < 300 or 400 <= status_code < 500)

    @staticmethod
    def _retry_after(headers: Any) -> float | None:
        if not hasattr(headers, "get"):
            return None
        value = headers.get("Retry-After", headers.get("retry-after"))
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else None
        try:
            retry_after = float(value)
        except (TypeError, ValueError):
            return 1.0
        if retry_after < 0 or not (retry_after < float("inf")):
            return 1.0
        return min(retry_after, SLACK_MAX_RETRY_AFTER)
