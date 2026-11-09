# GAP notifications: Jira registry v2

**Explicit normalized observations; staging by default.** There is no automatic
Leader selection, monitor mutation from the standalone CLI, reopening of closed
issues, or claim of exactly-once delivery. Staging issues retain the
`[GAP STAGING TEST]` marker. Explicit production selection uses `[GAP PRODUCTION]`;
this identifies the Jira destination, not proof of live production build health.
Jira descriptions omit the historical/synthetic provenance boilerplate in both environments;
the evidence fields remain available to callers without being displayed as raw flags.
Slack keeps destination and synthetic-test labels without snapshot/check boilerplate.

Slack build alerts show the selected PipelineRun's completion time in UTC, not the
monitor's check time or the run's creation time. New Konflux receipts retain this
optional value as `completed_at`; old snapshots are not backfilled. Missing, invalid
or timezone-free values display `Unavailable`. Completion means the run finished,
not necessarily when its first task failed. No duration is displayed.

The links row includes a short commit SHA only when the selected run's full SHA
matches the receipt revision and the GitHub repository identity is consistent.
The link uses the full SHA. Formatting and completion metadata do not change event
identities, edit old messages, or cause existing delivery receipts to resend.
The redundant promotion branch line is omitted; failure-reason behavior is unchanged.

`GAP_JIRA_URL` is the single operator-facing destination selector:

| URL | Internal target | Required observation `environment` |
| --- | --- | --- |
| `https://stage-redhat.atlassian.net` (default only when unset) | `stage` | `stage-redhat.atlassian.net/RHAI/<GAP_SLACK_CHANNEL>` |
| `https://redhat.atlassian.net` | `production` | `redhat.atlassian.net/RHAI/<GAP_SLACK_CHANNEL>` |

Only these exact origins, optionally followed by one slash, are accepted.
Explicit empty values, whitespace, case variants, HTTP, credentials, ports,
paths, query/fragment delimiters and lookalike hosts fail closed. Remove legacy
`GAP_JIRA_ENVIRONMENT`: its presence (even empty or matching) is rejected with
sanitized migration guidance, never ignored or used as a fallback. Project `RHAI`, issue type `Bug`,
remain fixed; Slack destination is selected by required `GAP_SLACK_CHANNEL`. Only staging also accepts its legacy
numeric issue-type ID `10016`. The deprecated `--jira-url` option remains a matching assertion
with the same URL rules, not an independent routing override. Internal Python
name-based target/config APIs and environment-bound identities remain unchanged.

Selection alone never enables writes: standalone CLI writes require `--write`,
and monitor notifications remain disabled by default. Supply credentials authorized
for the selected site through the existing explicit variables; there is no credential
discovery, automatic credential switching, or fallback to another Jira site.

Use separate journals for stage and production. New journals record their scope;
unbound legacy schema2 journals remain readable only as staging journals. Pending
work, registry properties, labels and incident identities cannot cross environments.
Existing staging identities and remote receipts are unchanged. Do not rewrite an
old journal or observation file to migrate incidents. Production recovery requires
explicit transition IDs/fields verified for that site; staging IDs are not defaults.
For workflow configuration and the Python API, see [monitor.md](monitor.md).

## Package boundaries

Notification policy lives in `lib/gap_notifications/`, with a minimal initializer
and five explicit tools:

- `analyze_leader_state.py`: offline accepted-snapshot build collection.
- `dispatch_incidents.py`: canonical identity, registry validation, lifecycle,
  journal and delivery coordination; shared receipt/URL validation lives here.
- `notify_tools.py`: GAP Jira/Slack facades, bounded content formatting and pure
  managed ADF sections; no lifecycle or lookup imports.
- `monitor_hook.py`: opt-in post-publication integration and separate tracking
  handoff; disabled/preview/dry-run guarantees are unchanged.
- `lookup_tools.py`: read-only inspection/tracking plus dependency-light table
  rendering. Lookup imports shared validation only when needed; rendering does
  not initiate service queries or load notification transports.

Delivery depends on notify tools, never lookup tools. Lookup uses shared registry
validation without executing delivery. The old unmerged flat GAP module paths
are removed, not retained as forwarding shims. Existing script entry points and
facade/function contracts remain; generic Jira/Slack clients and ADF utilities
stay outside this package. The issue template moves with identical content to
`lib/gap_notifications/templates/issue.md.j2`, declared as package data.

## Readable presentation

Slack messages use the packaged `lib/gap_notifications/templates/slack.md.j2`
Jinja2 template for Slack mrkdwn (not Jira Markdown). Like the Jira template, it
uses StrictUndefined and loads independently of the current directory. Edit this
file to change message layout and wording for roots, threaded updates and recovery
notifications. The template receives only selected, escaped text, validated links
and explicit outcome/routing flags; raw observations are not exposed and evidence
is never evaluated as template source. Python retains evidence selection, URL
validation, truncation and escaping. Template changes do not change delivery,
threading or replay identity. Both templates are included by the existing
`templates/*.md.j2` package-data rule.

Feasibility failure roots reuse the Jira feasibility paragraph, explaining that
the job dry-runs the normal sync from the current configured source branch to
configured active release branches, without testing the proposed stable PR contents
or pushing changes. They point readers to the feasibility job for merge conflicts
or configuration/workflow errors; a failed result alone does not prove a merge
conflict. Success roots retain only the neutral explanation, not failure claims or
failure-specific guidance. Threaded updates omit this explanation to avoid repeating
root context.

Jira descriptions use the packaged `lib/gap_notifications/templates/issue.md.j2`
with Jinja2 StrictUndefined, independent of the current directory. Feasibility
issues include a main-to-release dry-run explanation, distinguish it from testing
the proposed stable PR contents, and direct failures to the feasibility job rather
than assuming a merge conflict. They refer to the configured source branch without
guessing its name, which is not yet carried in the notification evidence. Their
workflow evidence link is labeled **Feasibility run**, not **Creator workflow**. The restricted
Markdown converter supports headings, one-line paragraphs, flat bullets, explicit
HTTPS links and inline code; no HTML, emphasis, tables, nested lists or code fences.
Unsupported markup fails clearly. Evidence is escaped literal data, never a template.
Whitespace is normalized; fields over 2000 characters, links over 4096 characters,
and rendered bodies over 24000 characters fail rather than silently truncate.
URLs must be HTTPS without credentials, whitespace or Markdown delimiter characters.
Supply percent-encoded URL delimiters when necessary. The renderer does not load
raw-log fields or credentials; callers must supply sanitized, non-secret evidence.
Allowlisted text fields such as reason and note are rendered literally, without
automatic secret detection or redaction. Only allowlisted diagnostic fields and
evidence links appear, not full JSON.

Jira descriptions omit the affected-targets section: automatic observations do not
supply authoritative release-impact data. Legacy `evidence.affected_targets` values
are ignored by presentation; release impact is not inferred from promotion branches.
The historical fixture fields `reason`, `head_sha`, `leader_url`, `pr_url`,
`state_url`, `build_url`, `note`, `historical_snapshot`, `synthetic` are supported;
collector `selected-run`, `creator-outcome`, and `head-sha` fields are also used.
Optional `workflow_url` or `creator-outcome.workflow-url` supplies the creator link.
Absent fields are shown as unavailable or omitted, without implying recovery.
Mandatory staging/test context is template-owned, outside supplied values.

Human labels are additive: build adds `gap-build-failure`; conflict adds
`gap-sync-failure` and `gap-merge-conflict`; feasibility adds `gap-sync-failure`
and `gap-feasibility-failure`. Changed open failures use Jira atomic `update.labels` add operations rather than
replacing a registry-search snapshot, preserving even concurrently added machine
and user labels. The adapter's optional `add_labels` is mutually exclusive with its
existing replacement `labels` API and verifies required additions on readback.
Description rendering and adapter ADF/label validation finish before create/update
intent is persisted, so deterministic presentation errors do not strand write intent. Identical events remain no-ops; closed tickets are not
backfilled. Summary/registry identity and Slack lifecycle are unchanged.

## Discussion projection and read-only inspection

The metadata link uses the suffix `(Jira permissions required)`. Managed-section
validation recognizes both this wording and the original `Jira permissions required.`
suffix, while still requiring exact link labels, URLs and section structure. Other
edits and duplicate sections continue to fail closed. Cosmetic projection emits the
new wording without changing notification delivery or incident identity.

The managed **Slack discussion** section is a cosmetic projection of the confirmed
root stored in the validated Jira `gap.notification.v1` property, not a routing
source. Caller evidence and visible descriptions cannot supply a root. Links use
the verified fixed workspace `https://redhat-internal.slack.com`, validated channel
and receipt timestamp; no Slack lookup/API scope is needed.

The separate managed **Notification metadata** section links directly to the
issue's `gap.notification.v1` JSON API using the explicitly selected origin, validated
actual issue key, and fixed property name. Its label is “View notification metadata
(JSON — source of truth)” with a Jira-permissions-required note. Opening the link
requires the viewer's Jira authentication/permissions; no credentials are embedded.
The property is routing/delivery authority, while Jira issue status remains status
authority. This metadata link is independent of a confirmed Slack root: validated
pending/failed delivery can show metadata without manufacturing a discussion link.
Evidence and existing visible body URLs cannot redirect either trusted link.

Projection runs in a `finally` around Slack delivery, so delivery exceptions can
still expose metadata; cosmetic errors are swallowed separately and preserve the
original core exception/hold/intents/retry deadline. Both metadata and discussion
links survive later normal failure-description updates. Inspection additionally
reports `metadata_url`, `metadata_note`, and `visible_metadata_link`, without
requiring credentials beyond the existing explicit Jira authentication.

For the Slack link, the root receipt must first be durably saved. Projection re-reads the issue/property,
validates scope/trial/incident/episode identity, and skips closed issues. A failed
fresh property read prevents projection. It appends
an absent managed section or replaces a uniquely recognized unchanged managed
section while preserving other current ADF nodes. Edited/duplicate sections warn
rather than deleting arbitrary content. Cosmetic failure sets a separate
`presentation_warning` in local task/CLI output, never changes delivery uncertainty,
loses the root or replays Slack. Re-submit the same observation to retry projection
idempotently without replaying notification; draining `[]` does not select confirmed
tasks. Later normal failure descriptions retain the stored root link. There is no
atomic protection against simultaneous human description edits: Jira uses a full
field PUT. Closed-state races are guarded by the adapter's fresh status check.

```sh
uv run --no-project --python /Users/ckodama/code/devops-shared-resources/.venv/bin/python scripts/gap_notify.py --inspect RHAI-166
# Optional --trial-id trial-name compares the expected trial; omission reports stored trial.
```

Inspection requires only explicitly supplied Jira environment authentication
(`JIRA_USER_EMAIL` or `JIRA_API_EMAIL`, plus `JIRA_API_TOKEN`). It does not discover
credentials, read observations/ledger, acquire lockfiles, construct Slack clients,
acknowledge receipts, repair, or mutate anything. It honors `GAP_JIRA_URL`
with the same default and validation as write/preview mode. Inspect
is exclusive with write/observation/ledger/receipt/transition options. JSON reports
actual Jira status category, validated property scope/trial/incident identity,
recorded outcome, confirmed root/link, actual managed visible-link match/missing,
and Jira/Slack pending intents including a retry deadline when recorded. Missing or
malformed properties are reported safely without dumping raw properties or errors.
A supplied mismatched trial is reported, not silently replaced with `default`.
Jira status is status authority; property is delivery/routing authority; the local
ledger is cache/audit only and the visible link is only a projection.

### Verified staging projection

Manager verification on staging [RHAI-166](https://stage-redhat.atlassian.net/browse/RHAI-166)
used inspection with Slack environment credentials removed: the property was valid,
the issue was open, and the visible discussion link was initially missing. A guarded
same-observation run permitted only the issue's description update and blocked
creation, closure, property writes, and Slack sends. It performed exactly one
description update and zero Slack attempts; the property remained byte-equivalent.
Subsequent inspection reported `visible_link=match` and `open=true`, pointing to
[the stored discussion](https://redhat-internal.slack.com/archives/C0883L8FARE/p1793984743525239).
This verifies staging projection/inspection, not production behavior or recovery.
Manager evidence is retained locally at
`/Users/ckodama/projects/gap-jiras/artifacts/discussion-review/`:
`unit-tests.log`, `inspect-before.json`, `inspect-after.json`, and
`projection-live-verification.json`. The manager also independently verified
175 passing tests and 11 passing subtests.

The manager subsequently verified the metadata-link extension on the same open
staging issue: the guarded same-event path made one description update and zero
Slack attempts, retained the existing Slack link, and added the matching
[notification metadata API link](https://stage-redhat.atlassian.net/rest/api/3/issue/RHAI-166/properties/gap.notification.v1).
The property remained unchanged; a second run was a no-op (still one total update).
Inspection reported both visible links matching and the issue open. Independent
local validation passed 183 tests and 11 subtests, plus syntax and diff checks.
Evidence is retained at
`/Users/ckodama/projects/gap-jiras/artifacts/metadata-link-review/`:
`unit-tests.log`, `inspect-before.json`, `inspect-after.json`, and
`live-verification.json`. This remains staging-only verification, not a production
or recovery claim.

## Registry and policy

Jira, **not the local ledger or Leader state**, is the cross-Leader registry. A stable
searchable `gap-registry-<fingerprint>` label identifies staging scope + trial + canonical
repository + actual source/destination promotion stream + category. Build additionally
includes component/pipeline. PR, Leader, revision, run UID and tested downstream
feasibility branches are evidence, not incident identity. All affected release branches
share one feasibility incident per actual promotion stream.

Every supplied definitive failure queries the complete registry **even if its event ID
was seen locally**. Exactly one valid open issue is reused. Only closed matches and a
current definitive failure create a successor issue and new Slack root, never reopen.
This includes a manually closed unresolved failure with the **same** event ID. Further
polls discover the successor open issue rather than repeatedly creating successors.

Positive recovery closes the matching open issue and replies in its original Slack
thread. Explicit positive recovery after manual closure records recovered metadata on
the already-closed issue (and replies if a root exists), without status/field mutation. A recovered ticket retains compact seen-event hashes: replay of a known historical
pre-recovery failure is not a new recurrence. A genuinely new failure creates a new issue.
Seen hashes are inherited by successors. With several closed predecessors, newest matching
validated RHAI numeric key determines the latest outcome: an older unresolved manual closure
cannot override a later recorded positive recovery. This ordering assumes only our
single writer creates these matching RHAI incidents; predecessor key/episode is recorded
for audit. This is ticket history ordering, **not newest-Leader authority**.

Multiple matching open issues, malformed/missing metadata, incomplete/erroring search,
identity mismatch or uncertainty HOLD instead of guessing or creating another ticket.
Unknown/pending observations neither create, close, recreate nor post notifications.

Each issue has a unique episode UUID (`gap-episode-<UUID hex>` label) and fixed Jira
property `gap.notification.v1` containing bounded non-secret metadata:

- schema `1`, staging scope, immutable trial ID, full incident key and episode UUID;
- compact `seen_event_ids` hash/status index, last accepted event/outcome;
- `jira_intent` for uncertain issue-field update or closure;
- `delivery.root` Slack channel/timestamp, delivered event and durable send intent;
- predecessor episode/key where applicable.

The adapter writes initial property metadata atomically in the Jira create request and
verifies readback. All registry records are validated against project/type/status,
incident label, episode label, expected summary and exact property scope/identity.
Metadata is capped **below 24 KiB** (Jira limit 32 KiB). Capacity exhaustion holds;
no silent hash eviction or receipt truncation. Full observations remain in supplied local inputs; descriptions contain bounded
human-readable evidence, not full JSON or this compact property.

### Partial writes and concurrency

Before Slack POST, send intent is verified in the Jira property. Confirmed root/thread
receipts are persisted afterward. A fresh runner discovers the same issue and thread
from Jira. A crash/timeout/ambiguous POST or receipt persistence failure leaves durable
intent and **blocks later sends across fresh runners**. No automatic Slack read scopes
or blind POST retry. Known response receipts are also cached locally for operator audit.
Confirmed rate-limit rejection records a remote retry deadline (bounded 1–3600 seconds)
and a definitely-not-sent marker; fresh runners cannot resend before that deadline and
may safely retry afterward. No background scheduler is included. Permanent rejections
remain held for explicit operator disposition; uncertain timeouts/5xx never take this path.

Before Jira update/close, a remote `jira_intent` is persisted. Ambiguous completion holds
until explicit verified receipt reconciliation. Closure does not reopen or regress issue
status. Property-only writes on closed issues finalize recovery/delivery metadata.
Creation intent is also saved in the local schema2 journal before POST. A known created
key survives readback failure and is read directly even if JQL is not yet consistent.
An ambiguous create with zero search results never blindly retries from that journal.

**Single writer required for an incident/trial across ALL runners/ledgers.** Jira property
PUT is not compare-and-swap and JQL indexing is eventually consistent. Even serialized
writers do not make label search + create unique or atomic: an empty fresh runner cache
cannot prove a just-created, not-yet-indexed ticket does not exist. Local flock only
coordinates one ledger file on one host, not separate runners. A fresh runner with no
journal cannot prove an invisible just-created issue does not exist. Serialize invocation
externally, retain the original uncertainty journal after errors, and do not use fresh
ledgers to evade a hold. Neither Jira search nor this prototype guarantees distributed
exactly-once creation or sending.

## Input contract and caller responsibility

`--observations` supplies a JSON array, not a raw Leader state:

```json
[{
  "environment": "stage-redhat.atlassian.net/RHAI/C0883L8FARE",
  "repository": "owner/repo",
  "source_branch": "main",
  "destination_branch": "stable",
  "category": "build",
  "component": "example-component",
  "pipeline": "example-pr-build",
  "status": "failure",
  "observation_id": "stable-evidence-UID-and-status-hash",
  "evidence": {
    "test_only": true,
    "historical_snapshot": true,
    "synthetic": false,
    "note": "Historical snapshot for staging trial, not current evaluation.",
    "pr_url": "https://github.com/owner/repo/pull/1"
  }
}]
```

Categories: build/conflict/feasibility. Status: failure/success/unknown. Build requires
component; pipeline optional. Destination means actual child base branch, **not a tested
rhoai-X feasibility target**. Put affected-target details in evidence; a changed target
set is new evidence for the same incident.

A supplied `success` is an explicit category-wide positive recovery assertion. Dispatcher
does not verify live checks or independently aggregate targets. Feasibility success must
mean affected targets affirmatively recovered and no applicable failures remain. Missing
or removed targets, skipped/query-error checks, pending checks, empty sets, retired Leader,
and closed/merged PR are NOT recovery. Use unknown when unresolved. Empty `[]` only retries
previously queued work and never infers recovery. The opt-in monitor hook now combines
build collection with structured current-revision conflict and workflow-wide feasibility
receipts; see [monitor integration](monitor.md). Required completed skipped checks are
nonpassing failures, whereas unqueried/query-error evidence is unknown. Per-target
prose is not routing or recovery authority.

Stable event IDs must describe facts, not observation timestamps/Leader URLs. Reuse an ID
only for identical facts. Jira persists seen hashes where a ticket exists. A success before
any issue exists has no remote registry to remember it (local journal remembers it only).
Filtering an unseen stale observation or selecting authoritative current evidence remains
caller responsibility; newest-Leader authority is deferred.

Evidence must be valid finite JSON, bounded 64 KiB per observation, 1000 observations and
4 MiB per file. Never include credentials, Authorization headers or unredacted raw logs.
Jira renders explicit HTTPS evidence links plus structured JSON; Slack includes bounded
evidence text. Dry-run output includes supplied evidence and may be sensitive.

## CLI and trial isolation

```bash
uv run --no-project --no-sync --python /path/to/existing/venv/bin/python \
  scripts/gap_notify.py --observations trial-failure.json \
  --ledger /tmp/registry-v2-trial.json --trial-id leader84-registry-001
```

Default dry-run makes no clients, reads no credentials, creates no lock/ledger, performs
no registry query or mutation. It previews candidate observations/registry work only;
it cannot predict whether remote issues already exist or will be held.

Only the authorized operator adds `--write`. Environment credentials: JIRA_USER_EMAIL
(fallback JIRA_API_EMAIL), JIRA_API_TOKEN, SLACK_TOKEN (fallback SLACK_BOT_TOKEN).
No dotenv loading or credential logging. Workers must not read/source credentials or
execute live sends. Recovery additionally requires explicit verified `--transition-id`
and optional JSON `--transition-fields`; no workflow metadata IDs are guessed.

Trial ID is a stable safe 1–64-character ASCII alphanumeric/underscore/hyphen slug
(default `default`). Same trial ID discovers the same remote registry across fresh local
ledgers and replacement Leaders. **Different tests require different trial IDs**, not
merely different ledger files. It namespaces external incident labels/task IDs, not the
logical incident key. Never randomize it per invocation when testing dedup.

Local journal schema is **2**. Existing v1/schema1 journals fail closed: use an explicit
new v2 file; no silent migration. Journals use flock, fsync/atomic replace and a 16 MiB
cap. Keep original journals when writes are uncertain. Never use a Leader state.json or
the observation input as the journal. There is no state-transfer or dedicated branch.

Exit codes: 0 settled/dry-run, 1 config/input failure, 2 pending/reconciliation-required.

### Explicit manual receipt reconciliation

An operator may use `--resolve-receipt resolution.json --write` with an existing local
task (normally `--observations` pointing to `[]`). Example for a verified Slack send:

```json
{"task_id":"task-ID-from-journal", "receipt":{
  "system":"slack", "key":"RHAI-123", "channel":"C0883L8FARE",
  "ts":"1234567890.123456", "reason":"Operator independently verified this send"
}}
```

Jira receipts use system=jira and key; recovery also requires done=true and an actual
terminal issue readback. Only existing durable intents can be resolved, with a required
audit reason; registry scope/issue identity is revalidated. Slack root is preserved on
thread receipts. Successful property update clears intent and audit is saved locally.
No command blindly clears an ambiguous intent in order to resend. This is explicit
operator attestation, not permission to guess receipts. Permanent rejected intents require an operator decision; confirmed rate rejection alone
may retry after its remote deadline. No automatic replacement channel or permission changes.

## Validation and limitations

### Actual staging v2 verification

The manager completed trial `leader84-jira-registry-001` using **nine initially empty
local runner journals** with the same trial ID:

- Runner A created [RHAI-163](https://stage-redhat.atlassian.net/browse/RHAI-163) and
  a Slack root; B reused it with unchanged Jira registry property/root.
- After the manager manually closed 163 outside the dispatcher (leaving recorded outcome
  `failure`), C submitted the same failure ID and created successor
  [RHAI-164](https://stage-redhat.atlassian.net/browse/RHAI-164) with a new root.
  D reused 164; E supplied synthetic replacement evidence and updated its existing thread.
- F supplied synthetic recovery, closing 164 and replying in its thread. G replayed the
  old failure: no new issue, despite older 163 still recording unresolved failure.
- H supplied synthetic recurrence and created
  [RHAI-165](https://stage-redhat.atlassian.net/browse/RHAI-165) with a third root;
  I supplied final synthetic recovery and closed it with a thread reply.

Final manager readback verified all three issues Closed, three distinct roots and no
remaining remote mutation intents. Final proof is in the manager project artifact
`artifacts/trial-leader84-jira-registry-001/final-verification.json`. Manager independently
verified **118 tests plus 11 subtests passed**. This validates the bounded staging trial,
not production delivery guarantees or actual recovery of the historical production build.
All replacement, recovery and recurrence assertions in this trial were explicitly synthetic.
No production mutation or commit was performed.

### Mocked coverage and remaining scope

Mocked suites cover fresh-ledger reuse/thread discovery, manually closed same-ID successor,
recovery/recurrence and old-event replay, multi-closed history, grouped feasibility evidence,
unknown, multiple-open/malformed registry holds, ambiguous creation, known-key search lag,
remote Slack intent and receipt-persistence failures. Adapters use mocked HTTP contracts;
manager alone performs staging tests and records their results separately.

The earlier **v1** manager trial created
[RHAI-161](https://stage-redhat.atlassian.net/browse/RHAI-161) and
[RHAI-162](https://stage-redhat.atlassian.net/browse/RHAI-162), both closed after synthetic
recovery; ten delivery tasks were confirmed. It tested local-ledger policy, not the new
Jira-registry/manual-closure-successor policy. Those results are not v2 verification or
proof production failures recovered. Historical failure, synthetic recovery/recurrence,
and real staging writes are explicitly separate concepts. No production workflow wiring,
newest-Leader reconciliation, bulk creates or distributed coordination is included.


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
