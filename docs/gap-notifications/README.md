# GAP staging notifications: Jira registry v2

**Local CLI, explicit normalized observations, staging tests only.** No production
writes, automatic Leader selection, monitor mutation, reopen transitions or claim of
exactly-once delivery. Every issue/message is marked `[GAP STAGING TEST]` and visibly
identifies test-only, historical/synthetic evidence and the supplied note.

Destinations are fixed: Jira `https://stage-redhat.atlassian.net`, project `RHAI`,
Bug (`10016`), Slack `C0883L8FARE`. Observation `environment` must be exactly
`stage-redhat.atlassian.net/RHAI/C0883L8FARE`.

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
no silent hash eviction or receipt truncation. Full observations belong in descriptions
and supplied local inputs, not this compact property.

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
previously queued work and never infers recovery. Normalized snapshot/live extraction is
separate; structured conflict and per-target feasibility collection remains future work.

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
