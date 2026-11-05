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
saved separately in diagnostic `publication.json`. Publication to main/master is
rejected. Caller-provided `--allowed-branches` restricts releases further.

## Scan-specific policy

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
