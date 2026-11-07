# Slack client

Reusable Slack delivery operations live in `lib/slack_client.py`, using the
exactly pinned official `slack_sdk==3.46.0` WebClient through a bounded private
session bridge.

## API

- `SlackClient(token, session=None, timeout=20)`
- `post_message(channel, text, thread_ts=None, client_msg_id=None) -> {"channel": ..., "ts": ...}`

## Behavior

- Uses `chat.postMessage` with `unfurl_links=False` and `unfurl_media=False`.
- Requires an explicit token and an injected session for tests.
- Returns a confirmed receipt only when Slack responds with a verified matching channel and timestamp.
- Preserves sanitized error handling for ambiguous, rate-limited, and permanent failures.
- Does not add history, edit, upload, discovery, or retry orchestration.
- Managed default sessions have `trust_env=False`; injected sessions remain the
  caller's responsibility. Sends explicitly pass `allow_redirects=False`.
- SDK retries are disabled and its isolated logger is disabled/nonpropagating;
  raw SDK exceptions and responses never become public error messages.

## SDK boundary and upgrade constraints

`_SessionWebClient` retains the actual SDK `chat_postMessage`, `api_call`, JSON
serialization, request construction, empty retry loop and response validation.
It overrides only the final `_perform_urllib_http_request_internal` HTTP hook to
call the existing injected `session.post`. The serialized SDK JSON is decoded
back into the public session's `json=` argument, then requests performs final
wire encoding. This preserves existing fakes and mutable notifier configuration.

The bridge initializes the small set of BaseClient fields directly rather than
calling its environment-discovering constructor. It does not modify process-wide
proxy settings, urllib openers or SDK classes. This is **private, version-pinned
coupling**, not a public SDK transport feature: a different SDK version fails
before sending and requires review of initialization and HTTP-hook contracts.

The original captured response is classified independently even if SDK validation
raises: SDK accepts truthy `ok` and only HTTP 200, whereas our API requires strict
Boolean acknowledgement and accepts verified 2xx receipts. Transport failures
without a response remain ambiguous. `client_msg_id` is passed through; it is not
an exactly-once guarantee or permission to retry an uncertain write.

The adapter deliberately retains requests, receipt/error policy, and the existing
session API. Adoption standardizes message operation construction but does not
remove the policy layer or reduce all transport code. No history/permalink API or
additional scopes are introduced. `tests/test_slack_sdk_contract.py` exercises the
installed SDK with fake transports and blocked outbound connections; existing
client/notifier/dispatcher semantics must also pass before upgrading.

## GAP compatibility

`lib/gap_notifications/notify_tools.py` remains as the compatibility wrapper for existing GAP callers and tests.
