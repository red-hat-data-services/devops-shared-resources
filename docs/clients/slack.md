# Slack client

Reusable Slack delivery operations live in `lib/slack_client.py`.

## API

- `SlackClient(token, session=None, timeout=20)`
- `post_message(channel, text, thread_ts=None, client_msg_id=None) -> {"channel": ..., "ts": ...}`

## Behavior

- Uses `chat.postMessage` with `unfurl_links=False` and `unfurl_media=False`.
- Requires an explicit token and an injected session for tests.
- Returns a confirmed receipt only when Slack responds with a verified matching channel and timestamp.
- Preserves sanitized error handling for ambiguous, rate-limited, and permanent failures.
- Does not add history, edit, upload, discovery, or retry orchestration.

## GAP compatibility

`lib/gap_notification_slack.py` remains as the compatibility wrapper for existing GAP callers and tests.
