# Reconcile Tekton PipelineRuns

`scripts/reconcile-tekton` owns the end-to-end operation: discover PipelineRuns,
apply trusted templates in a temporary checkout, validate the result, check
idempotence, and optionally publish a PR. `scripts/validate-tekton` remains the
standalone validator/fixer, including its existing namespace, variables, parent,
layout and generation options.

Use clean, reviewed shared and policy checkouts. The examples assume dependencies
are already available; the command does not acquire credentials or install them.

## Preview by default

```sh
uv run --project shared --no-sync --offline python shared/scripts/reconcile-tekton \
  --path target --branch "$RELEASE" \
  --config-dir "policy/tekton-templates/$TEMPLATE_SET" \
  --report-dir "$REPORT" --allowed-operations M,D --json
```

The original target remains unchanged. The command reads the layout from the
existing template configuration (`tekton_layout: flat` or `nested`); there is no
operational layout override. Both `.yaml` and `.yml` are supported.

The report directory must be new and outside the target. It contains diagnostic
`result.json`, `phases/*.json`, `phases/*.log` and `enforcement.patch`. Internal
candidate checkouts and exact-change plans are temporary and are not copied into
reports. Reports cannot be resumed, verified or used as input to a later command.
Do not upload temporary stage trees.

In GitHub Actions, successful previews automatically append a readable section to
`GITHUB_STEP_SUMMARY` without replacing prior content or changing stdout JSON.
It includes operation counts, planned paths, deferred manual/unresolved counts,
and a collapsible escaped diff. Readiness applies to the plan, not complete
branch alignment. Escaped UTF-8 output is bounded (48 KB paths, 160 KB diff,
220 KB per preview, 900 KB total including prior summary text); truncation is
explicit. Download the job's report artifact and open `enforcement.patch` for
the full unchanged patch, or `result.json` for all paths and diagnostics.
Workflows still upload those reports; the shared command does not upload them.
Summary failures only warn on stderr. Local runs without `GITHUB_STEP_SUMMARY`,
inventory calls, and publication calls do not append this preview section.

Only existing regular-file changes are supported. The default is modifications
only (`--allowed-operations M`). Explicit `M,D` also permits deletions, but only
for matched PipelineRuns with an initial template deletion signal. Deletion
permission is never inferred from the template. Additions, renames, unexpected
file/mode/kind changes, unsafe paths and unrelated changes fail closed.
Resource-only YAML streams and absent PipelineRuns can produce a no-op.
External parents and generation are not supported by this operation; ordinary
`validate-tekton` retains its separate existing behavior.

## Plan and publish in one invocation

Add `--publish` and the PR arguments to the same invocation:

```sh
uv run --project shared --no-sync --offline python shared/scripts/reconcile-tekton \
  --path target --branch "$RELEASE" \
  --config-dir "policy/tekton-templates/$TEMPLATE_SET" \
  --report-dir "$REPORT" --allowed-operations M,D --publish \
  --repository "red-hat-data-services/$REPOSITORY" \
  --head-prefix "automation/enforce-pipelineruns-$RELEASE-$TEMPLATE_SET-" \
  --marker "<!-- template-set:$TEMPLATE_SET -->" \
  --title "chore: reconcile component repo PipelineRuns on $RELEASE" \
  --body-file policy/docs/component-repo-pipelinerun-pr-body.md \
  --lifecycle create-or-report
```

`--head-prefix` appends the policy digest's first 12 characters, with no extra
separator, preserving the existing component PR identity. It is mutually exclusive
with the ordinary literal `--head`. There is no branch-format DSL.

The caller supplies authentication and Git author/committer identity. Planning,
rendering, filters and trusted policy code run while any supplied token is present.
This is the accepted **trusted-planner, same-job model**, not isolation against a
compromised planner. There is no separate publication/continuation CLI. Preview
needs no publication token. `--publish` with an empty plan makes no remote calls.

Before publishing, the command rechecks source, policy, base, permitted operations
and exact candidate bytes. The publisher additionally checks origin, whole-checkout
scope, index/staged blobs, remote base freshness and PR lifecycle, and pushes
without force. No automatic merge or workflow dispatch occurs. Publication can
leave staged files, local Git objects, or a remote branch after partial failure;
inspect the result and retry deliberately in a fresh checkout, not from reports.

Static Markdown notes remain caller-owned; shared code appends changes, checks,
provenance and deferred cases. `--tracking-url https://...` adds a Markdown link.
`--source-sha` optionally checks the actual policy commit. Runtime validator,
worker and publisher revisions come from the actual clean shared checkout.

`--github-output FILE` emits `status=ready|no-changes`, `base`,
`policy_digest_short`, and a `publication_url` when available. These describe the
completed operation, not a gate for a second command. Publication details are
saved separately in diagnostic `publication.json`.

Trusted config may declare `publication_branches: [main, "rhoai-*"]` to restrict
PR bases. Entries are exact names or literal prefixes with one trailing `*`;
the wildcard must match a **nonempty** suffix (`rhoai-` does not match).
Other wildcard forms, empty/null/non-list policies, invalid entries and duplicates
are rejected, never treated as absence. With a configured policy, every publishing
invocation must also include its exact target in `--allowed-branches`; this CLI
list remains exact comma-separated tokens, not pattern matching. Thus a matched
family without exact caller authorization is still denied.

Publication scope is checked against clean trusted config before planning (even
for no-change publication), then again at the direct publication boundary with
the usual config digest/provenance checks. Preview and inventory do not enforce
publication scope. Configs without the field retain legacy behavior: main/master
PR bases are rejected; other bases retain the existing optional exact CLI scope.
This is configurable adapter policy, not a hard-coded KC branch family.

A PR base is not a push destination: publication still requires a distinct, valid
`automation/` head and pushes only to that head, never directly to main/master
or the target base. Partial/error results, stale provenance, dirty sources and
unsafe heads remain unpublishable.

## Complete PR lifecycle inventory

Before any staging/push/PR write, both lifecycles cursor-paginate all open PRs
for the exact target base using the repository GraphQL connection (not search).
There is no fixed 1,000-PR cap. Matching still considers the generated marker
**or** the head branch name across that whole base inventory, including forks;
multiple matches remain a review-required error.

`create-or-update` never queries historical PRs. `create-or-report` first returns
an existing open match; otherwise it queries history narrowly by exact base and
head name, retaining closed/merged same-head disposition safeguards. Historical
absence requires completed pagination. API errors, malformed nodes/page metadata,
repeated/nonadvancing cursors and incomplete pagination fail closed before writes.
This does not change automation-head safety, marker ownership, append-only update
history, remote-base freshness or non-force push requirements.

## Partial previews for malformed files

Strict reconciliation profiles (template selection or legacy document policy)
isolate individual malformed YAML files instead of losing every valid-file
preview. The ordinary standalone validator and the legacy no-policy subprocess
reconciliation path retain their existing fail-closed behavior and schemas.

Only these exact ruamel exceptions from safe per-file parsing are isolated:
`DuplicateKeyError`, `ParserError`, `ScannerError`, and `ReaderError`. Invalid
UTF-8 is included. An unparseable YAML candidate is reported as an error, never
silently classified as an unrelated resource. Valid resource-only streams and
the existing rejection of bundled PipelineRun streams are unchanged.

Each diagnostic contains `path`, `error_class`, and a fixed-category `reason`
with location where available. Raw parser messages/source snippets are not
copied into diagnostics. `diagnostics.errors`, `counts.error`, and file rows
with `status: error` remain distinct from unresolved/manual eligibility cases.
The root excluded-file classifications and byte/mode identities are immutable;
the initial stage and every phase must match them before and after processing.
No newly malformed file, changed error classification, repaired excluded file,
or excluded-byte/mode change is silently absorbed during a run.

With any file error, the result is **`partial`**, including when there are no
valid-file changes or every candidate is malformed. Exit code **2** accompanies
useful stdout JSON, `result.json`, all phase diagnostics and `enforcement.patch`.
The preview job summary lists bounded/escaped error paths and prominently marks
the result **PARTIAL / INCOMPLETE / NOT PUBLISHABLE**. The patch includes only
valid-file changes and is diagnostic, not resumable publication authority.

`--publish` with any partial result is blocked **before** the publisher or the
no-change publication branch: `publication.status` is `blocked`, diagnostics
and JSON are retained, stderr explains the block, and exit code is 2. Publish
invocations still do not append the preview job summary. There is no flag to
allow partial publication. Complete operations return 0; fatal failures return 1.

Inventory/path/mode/symlink/source/context failures, unknown YAML tags, alias
composition/policy failures, graph/depth/type/security budgets, memory/recursion
errors, guard rejection and unsupported serialization remain fatal, not partial.
This does not relax parsing, repair malformed files or authorize rollout.

## Template selection and optional guards

An optional `selection_template` config key names a contained Jinja sidecar.
It renders JSON with exactly two keys: `status` (`applicable`, `skipped`,
`manual`, or `unresolved`) and string `reason`. Results are limited to 16 KiB;
reasons to 4096 characters. Duplicate JSON keys and extra fields are rejected.
No selection sidecar means the existing template matchers remain authoritative.

Selection uses the ordinary renderer variables plus detached `context`:
`{"branch": branch, "documents": {path: data_or_None}}`. Declare fixed
branch-local inputs in config `context_paths`. These paths must be unique,
contained and disjoint from writable scope. Existing inputs must be tracked;
they are snapshotted, mode/hash recorded in provenance, and copied into the
stage. Context is parsed once from that initial stage and copied into each
selection/render/guard invocation; CLI variables cannot overwrite it. Context
bytes and the stage index are checked after every phase.

The generic `unique_item(items, key, target)` filter returns a matching mapping
or an empty mapping, rejecting duplicate target identities. The Jinja global
`fail(message)` raises an explicit validation error. Business decisions belong
in the sidecar, not in a framework policy callback.

With selection configured, `--policy-python` may supply only
`check(original, candidate, context)` (and optional isolated `filters`). The
post-merge guard must return `True`; all strict candidates are guarded and
serialized before any strict file write. Without selection configured, the
legacy `decision`/`check` contract below remains supported. No consumer migration
is required to adopt this shared update.

Both profiles use the ordinary `validate-tekton` fix engine. Strict reconciliation
uses a localized, fail-closed writer; the ordinary CLI default retains its legacy
whole-document round-trip writer and anchor support. Strict reports retain the
existing `files` list and status `counts`, including manual/unresolved counts.
Nested strict identities are repository-relative paths, not basenames. The
ordinary CLI output schema and flat basename identities remain unchanged.

## Legacy scan-specific policy

Use `--policy-python source/script/scan_resource_policy.py` with the existing
nested-layout config. Keep M-only unless the operation's profile explicitly
supports more. The trusted policy interface is unchanged:

- `CONTEXT_PATHS`: fixed branch-local tracked data filenames.
- `filters`: trusted template filters.
- `decision(data, context)`: per-document status, reason and variables.
- `check(original, candidate, context)`: explicitly returns `True` or rejects.

Context is detached data: `{"branch": branch, "documents": {path: data_or_None}}`.
There are no filesystem, phase, report or rendering callbacks. No target or saved
report selects executable code. Context files remain read-only across all phases.

For scan PRs, use literal `--head "automation/pipelinerun-conformance/$RELEASE"`,
marker `Conformance-Generated: true`, the existing scan-review body file, and
`--lifecycle create-or-update`.

## Existing-file comments and formatting

The ordinary template merge retains existing round-trip container comments and
styles. Strict reconciliation (selection or legacy policy) emits localized source edits
instead of dumping the entire document. Unchanged source spans are retained,
including unrelated multiline flow values, null spelling and line endings.
Changed fields and list items may be inserted, removed or updated.

This is not a general lossless YAML editor. A changed flow collection or a
structurally changed collection may be emitted as one bounded subtree; formatting
inside that subtree can change. There is no whole-document formatting fallback.
Edits are checked for valid, nonoverlapping source spans, and the complete emitted
document must parse to the candidate before any file is written.

Changed applicable files containing anchors or aliases currently fail closed,
even when the anchor is outside the selected field: aliases do not have distinct
source locations. Collection type changes and flow replacements without retained
round-trip style also fail closed. Existing duplicate-key and policy checks still
apply. Appending after an end-of-file comment retains its text but can change its
association with list items. Inspect the preview diff; byte-identical formatting
or comment associations inside edited collections are not guaranteed.

## Safe inventory before fleet fanout

```sh
uv run --project shared --no-sync --offline python shared/scripts/reconcile-tekton \
  --inventory-root release/pipelineruns --select-repository "$REPOSITORY" \
  --branch "$RELEASE" \
  --allowed-branches rhoai-2.25,rhoai-3.4,rhoai-3.5,rhoai-3.6,rhoai-3.7-ea.1 \
  --publication-intent "$PUBLISH_REQUESTED" \
  --allow-batch-publication "$ALLOW_BATCH_PUBLICATION" --json
```

Inventory is read-only and emits one-line matrix JSON with repository names and
inventory revision. Strict booleans accept only `true`/`false`. Publishing `all`
requires explicit batch consent before fanout. Those intent flags authorize
nothing by themselves and do not publish or acquire tokens. Inventory membership,
release restrictions and source/path checks are retained. `--publish` cannot be
combined with inventory mode.

For local precommit tests, `tests/tekton_candidate_source.py` creates a clean,
disposable candidate source fixture. Fixture commits are test data, never a
production pin; rollout needs a separately reviewed real source commit.
