# Monitor → staging notification hook (opt-in prototype)

The monitor CLI now has a **default-disabled** build notification hook. Normal
monitor commands do not import the hook, access notification credentials, open a
notification journal, or construct notification clients. No workflow enables it.
This prototype assumes **one active Leader and one notification writer**. Leader
selection/authority and supersession are deliberately unchanged and deferred.

## Execution boundary

`run_monitor_publish_and_signal(..., notification_config=NotificationConfig(...))`
invokes the hook **only after successful state publication**, before the existing
Early Gate/Leader signals. `publish=False`, retired Leaders and monitor `--dry-run`
do not send notifications. The hook's sanitized result is available as
`MonitorResult.notification_result`; enabled preview/write attempts log bounded
counts/statuses to stderr. Errors and unresolved delivery tasks are reported as
`error`/`held`, never delivery success, and never change promotion gates or the
monitor exit code. `settled` means no outstanding local delivery tasks, not a
claim that an unknown observation succeeded or that delivery is exactly-once.
Write results include `delivery_task_count`, fixed-key `delivery_state_counts`
(`confirmed`, `superseded`, `pending`, `inflight`, `reconciliation_required`,
`rejected`, `other`), and numeric `presentation_warning_count`. These summarize
all retained outbox entries, including historical ones; no per-task state list,
warning text, or evidence is logged. Unrecognized states are counted as `other`
rather than becoming new log keys. Cosmetic projection warnings remain visible
separately from core `settled`/`held` status: a confirmed Jira/Slack delivery can
be `settled` with a nonzero warning count, without retrying the core send.

Only this pass's accepted decisions populate ephemeral `notification_urls`.
Head-move deferrals/status-post failures cannot reuse historical persisted build
receipts. The collector receives a copy containing only these accepted children.
The monitor's persisted state format is unchanged.

The existing `collect_build_observations` resolves the stream from structured
`creator-outcomes`, validates repository/PR/current-head/component evidence, and
normalizes accepted Konflux build receipts. The existing dispatcher and shared
Jira registry remain unchanged. Missing/conflicting stream evidence holds the
hook with a sanitized collection error; incomplete/unknown build results do not
become recovery. Aggregate `pr-status=success`, absent children, closed/merged
PRs and retired state do not establish recovery. A fresh explicit successful
build receipt is required. Missing evidence for conflicts/feasibility remains a
producer gap: **this hook currently emits build observations only**, without
inventing observations for those categories.

## Flags and safety

- `--notification-mode disabled|preview|write` (default `disabled`).
- `--notification-ledger /absolute/path/outside/checkout/journal.json`: schema2
  journal, **outside the monitored checkout**, never `state.json`. This prevents
  subsequent state publications from accidentally committing delivery metadata.
- `--notification-trial-id SAFE_ID`: stable namespace shared with other runners
  in the same trial; changing only the journal does not isolate remote incidents.
- `--notification-transition-id ID` and
  `--notification-transition-fields '{"resolution":{"id":"VERIFIED_ID"}}'`:
  explicitly verified staging done transition/required fields. Without a done
  transition, positive recovery holds visibly instead of guessing how to close.

Preview collects/validates and prints observation counts; it does **not** access
notification credentials, create clients, or read/write the notification ledger.
It is a notification preview only: the **ordinary monitor may still publish
GitHub state/statuses**. Monitor `--dry-run` suppresses this hook altogether;
use the mocked tests below for a fully offline integration preview.

Write mode is explicitly staging-only: fixed Jira
`https://stage-redhat.atlassian.net`, project `RHAI`, issue type `Bug`, and Slack
channel `C0883L8FARE`. There is no destination override. Only this explicit mode
reads `JIRA_USER_EMAIL`/`JIRA_API_EMAIL`, `JIRA_API_TOKEN`, and
`SLACK_TOKEN`/`SLACK_BOT_TOKEN`, using the existing service adapters. Operators
must provide staging-authorized credentials; configuring an opt-in hook is not
authorization to run a live trial or mutate production. No credentials or live
services were used to validate this change.

Example flags to append to an **already authorized** normal publishing monitor
command with an explicit Leader URL:

```text
--publish-state --leader-pr-url https://github.com/OWNER/REPO/pull/NUMBER
--notification-mode preview --notification-trial-id YOUR_STABLE_TRIAL
```

For an independently authorized staging write, replace `preview` with `write`,
provide the outside-checkout journal path and verified transition configuration.
No production writes are enabled or authorized by this prototype.

## Lifecycle / limitations

Jira `gap.notification.v1` remains the shared incident and delivery authority;
status determines open/closed. Description links are cosmetic. The local ledger
is cache/audit/uncertain-create evidence, not cross-Leader authority. Duplicate
receipts reuse the issue/thread; unknown results never recover; explicit success
closes/replies; manual closure followed by continued current failure creates a
successor and new root, never reopens. Ambiguous mutations retain durable holds
and are not blindly retried. JQL/create is not atomic: no production exactly-once
or concurrent-writer safety is claimed. GitHub publication and notification
writes are not transactional: a crash before invoking this hook can miss a pass;
a later normal monitor pass can re-observe current evidence. No queue/scheduler,
workflow wiring, credential setup or live-service trial is included.

## Offline validation

`tests/test_gap_notification_monitor.py` runs the **real collector and existing
Dispatcher** with fake Jira/Slack transports. It covers failure, repeat dedup,
unknown non-recovery, explicit recovery/reply, recovered failure replay,
manual-close successor, ambiguity/no-blind-retry, preview/no-op guards, sanitized
failures, and coordinator publication ordering/gate isolation. Existing monitor
and notification suites also pass. Use the existing repository interpreter; no
installation or live service access is required:

```sh
uv run --no-project --python /path/to/existing/.venv/bin/python -m pytest -q \
  tests/test_gap_pr_monitor.py tests/test_gap_notification_*.py tests/test_gap_notify.py
```
