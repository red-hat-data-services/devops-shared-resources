# Monitor → staging notification hook (opt-in prototype)

The monitor CLI now has a **default-disabled** build notification hook. Normal
monitor commands do not import the hook, access notification credentials, open a
notification journal, or construct notification clients. No workflow enables it.
Multiple active Leaders retain the configured notification mode. Leaders with
matching incident identities may update evidence in the same Jira issue and
Slack thread. Leader identity is not part of the incident key, and newest-Leader
authority is not enforced: distinct observations from overlapping Leaders may
change the shared incident lifecycle, including recovery or recurrence.

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
Accepted child entries may additionally carry versioned `merge-observations`; older state without these receipts remains valid.

The existing `collect_build_observations` resolves the stream from structured
`creator-outcomes`, validates repository/PR/current-head/component evidence, and
normalizes accepted Konflux build receipts. The existing dispatcher and shared
Jira registry remain unchanged. Missing/conflicting stream evidence holds the
hook with a sanitized collection error; incomplete/unknown build results do not
become recovery. Aggregate `pr-status=success`, absent children, closed/merged
PRs and retired state do not establish recovery. A fresh explicit successful
build receipt is required for build recovery. The default composite collector also
normalizes independent conflict and feasibility receipts, without changing build IDs.

Merge receipts preserve exact PR/head/base binding and the observed PR head branch
(which may differ from the logical creator source branch). Exact creator PR mapping
supplies incident stream identity. A notification-only PR snapshot verifies the same
head/base/state/branches at acceptance; missing/moving/contradictory evidence becomes
unknown, never a changed gate verdict, status publication or promotion decision.
Conflict recovery requires an explicitly mergeable open PR; same-pair contradictory
flips hold unknown against available predecessor receipts. No cross-Leader historical
contradiction detection is claimed when predecessor evidence is unavailable.

Feasibility evidence retains workflow execution ID/attempt/reported head and selected
mandatory checks before aggregate verdict priority can mask its outcome. Recovery
requires a complete successful workflow and all mandatory checks explicitly successful
on the current head. A mandatory completed `skipped` (or other non-success) conclusion
is failure, unlike an unqueried conflict-short-circuit, which is unknown. Query errors
are unknown even after gate timeout; authoritative missing/pending checks may fail at
the existing six-hour deadline. Build success never resolves a merge incident.

Fingerprints include revision/execution/check facts, not check time, prose, state URL
or API ordering. Genuine changed head/base or feasibility attempts supply new evidence;
there is no epoch journal. Replayed pre-recovery events retain existing suppression.
Optional malformed notification metadata is dropped/held without aborting ordinary
child processing. Workflow and commit links are constructed from validated repository
and execution/revision identifiers. Monitor detection time is not conflict onset or
workflow completion; no Konflux completion timestamp is attached to merge failures.

## Flags and safety

- `--notification-mode disabled|preview|write` (default `disabled`).
- `--notification-ledger /absolute/path/outside/checkout/journal.json`: schema2
  journal, **outside the monitored checkout**, never `state.json`. This prevents
  subsequent state publications from accidentally committing delivery metadata.
- `--notification-trial-id SAFE_ID`: stable namespace shared with other runners
  in the same trial; changing only the journal does not isolate remote incidents.
- `--notification-transition-id ID` and
  `--notification-transition-fields '{"resolution":{"id":"VERIFIED_ID"}}'`:
  explicitly verified done transition/required fields for the selected Jira site. Without a done
  transition, positive recovery holds visibly instead of guessing how to close.

Preview collects/validates and prints observation counts; it does **not** access
notification credentials, create clients, or read/write the notification ledger.
It is a notification preview only: the **ordinary monitor may still publish
GitHub state/statuses**. Monitor `--dry-run` suppresses this hook altogether;
use the mocked tests below for a fully offline integration preview.

Jira selection is explicit through `GAP_JIRA_URL`: unset defaults to
`https://stage-redhat.atlassian.net`; `https://redhat.atlassian.net` selects
production. Only the two exact HTTPS origins, optionally with one trailing slash,
are accepted. Explicit empty, whitespace/case variants, credentials, ports, paths,
queries/fragments and unknown hosts are invalid. Remove legacy
`GAP_JIRA_ENVIRONMENT`: any explicit presence is rejected with sanitized migration
guidance. Internal name-based target/config APIs remain unchanged. Project `RHAI`, issue type `Bug`, remain fixed; Slack destination is selected by required `GAP_SLACK_CHANNEL`. Production does not inherit staging's numeric issue-type or transition IDs.

Only explicit write mode reads `JIRA_USER_EMAIL`/`JIRA_API_EMAIL`, `JIRA_API_TOKEN`,
and `SLACK_TOKEN`/`SLACK_BOT_TOKEN`. Operators must supply credentials authorized
for the selected site. Selecting production does not enable notification mode,
change the Slack channel, switch credentials, or authorize live execution.
No credentials or live services were used to validate environment selection.

### Workflow environment variable

A workflow running a shared-code revision that supports this selector can pass:

```yaml
env:
  GAP_JIRA_URL: ${{ vars.GAP_JIRA_URL || 'https://stage-redhat.atlassian.net' }}
```

The repository variable is not a secret. The workflow expression defaults a missing
or empty repository variable to staging; the Python entry points reject an explicitly
empty process variable. The monitor entry point resolves the selector only when
notifications are enabled, then passes immutable configuration to the hook. Invalid
enabled configuration suppresses notifications with a sanitized warning without
changing the ordinary monitor's gate behavior. The standalone notification CLI
returns a failing exit status for invalid selection. The deprecated standalone
`--jira-url` flag only asserts a matching allowlisted URL; it cannot select or
override a destination. Disabled monitor notifications do not inspect either
selector variable; preview never reads credentials or opens a journal.

For direct Python calls, use `NotificationConfig(..., jira_environment="production")`.
The hook does not implicitly reread process configuration or credential variables in
preview/disabled mode. Production preview collects production-scoped observations
without credentials, clients or journal access. Existing staging callers retain
their defaults and injected-factory call shape.

Use an isolated production journal and a stable namespace for that environment;
never carry the staging workflow's namespace or transition41/resolution10000
into production. No production recovery IDs are established by this refactor.
Unbound older journals are staging-only, and retained outbox/create identities are
validated before draining work. Cross-environment observations, properties, adapter
origins and journals fail closed. Production recovery still requires an explicitly
verified transition and its required fields; absence remains a visible hold.

This snippet is documentation, not rollout: the promoter workflow files, shared-code
pin, repository variables/secrets and notification activation are unchanged. An older
pinned shared-code revision will not honor this selector; update the pin as a separate
reviewed rollout step before relying on production selection.

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

## Leader table tracking projection

The existing failure/pending/closed table gains a **Jira / Slack** column.
After the already-enabled write-mode dispatcher finishes, a separate optional
structured handoff resolves current accepted component/pipeline observations
with search/GET only, using the **configured stable trial ID**. For each identity
it validates the entire staging registry property and issue identity with the
existing diagnostics helpers. It selects the unique open incident, or the
latest validated closed episode when none is open, explicitly marked **Closed**.
A closed ticket without a confirmed root says **No confirmed Slack thread**,
not that delivery is queued. Multiple open incidents,
duplicate episodes, wrong scope/trial/identity/channel, malformed properties or
failed lookups do not produce links. Search visibility can lag; no match is
displayed as **No confirmed registry match**, never as proof that no ticket
exists. Current-task known keys can fill a search lag only after identical fresh
issue/property validation; historical outbox receipts are not display authority.

Rows pair escaped component/pipeline labels with Jira and confirmed Slack root
links, separating multiple entries with `<br>`. Successful-empty (`not_found`)
entries are condensed to one counted row-level note so they do not bury links;
unavailable components remain individually labelled. A confirmed Jira issue is shown
even if Slack is pending/unconfirmed; no thread is invented. Existing confirmed
roots remain usable when later delivery is held, with an explicit held label.
Visible descriptions, previous comment text and arbitrary test namespaces are
never scanned for routing. There are no Slack history requests or extra scopes.

The projection reuses the existing explicitly configured Jira adapter, never
constructs clients or reads credentials by itself. The rendering helper has no
eager service-client/transport imports (the preexisting `lib` package initializer
still imports `requests` for PRCreator; this feature does not alter that). It
never creates, updates, closes or resends anything for display. Lookup failures remain **Unavailable**
and cannot change dispatcher held/error status or promotion gates. No per-task
links or historical evidence are added to bounded console summaries or state.json.

Standalone reporter refreshes, disabled/preview/dry-run notification modes, and
children deferred by the current monitor pass show **Not checked** when no
explicit current projection exists. Standalone reporting does not implicitly
load notification credentials or query services. It may replace prior displayed
links with this explicit uncertainty, not a claim of no incident; prior display
text is not authoritative. Creator-only refresh still replaces only its managed
creator section and preserves the build table, including tracking cells.

This is shared-repository code only. Consumer workflow pins/rollout configuration
require a separately reviewed follow-up; this change enables no workflow or live
trial. Original PR84 captured preview namespace is
`leader84-full-cce96577-stage-001`; the retired isolated refactor namespace must
not be merged into those results.

## Offline validation

`tests/gap_notifications/test_monitor_hook.py` runs the **real collector and existing
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


## Configured destination and workflow boundary

`GAP_SLACK_CHANNEL` is authoritative and must be a nonblank Slack channel ID.
There is no fixed-channel allowlist or routing fallback. CLI `--channel` is
an optional matching assertion, never an override. Requested channel, adapter,
acknowledgement, stored root and manually attested receipt must agree.
Slack links retain the exact verified workspace origin.

Channel remains part of incident identity. Selecting the historical channel
preserves existing identities and dedup. Changing channels selects a separate
namespace, may create new Jira incidents, and does not migrate or automatically
close old-channel incidents. Old-channel journals/properties are not reused.
Example channel IDs in this document are examples only, not defaults.

`scripts/gap_monitor_workflow.py` owns workflow notification configuration,
environment-specific recovery defaults and receipt-only skip policy. Its
stdlib-only `receipt` command runs after the trusted shared checkout and before
dependency setup. Its `run` command resolves policy and invokes the unchanged
monitor entry point. YAML retains infrastructure and write/non-dry-run credential
boundaries only. Invalid/disabled/skip outcomes emit bounded diagnostics.
Production recovery remains held without a verified transition.

Roll out shared support before the new promoter caller. Both moving writer refs
remain unchanged. Publishing shared changes may affect live runs; publishing the
new promoter caller removes the previous channel block and can enable delivery.
Publication and activation require separate authorization.
