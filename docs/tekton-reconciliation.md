# End-to-end Tekton reconciliation

The existing `scripts/validate-tekton` entrypoint supports three additional
operations. Ordinary validation and `--fix` remain available unchanged; discovery
now accepts both `.yaml` and `.yml`.

Use a clean, reviewed shared checkout and a clean, reviewed policy checkout.
Prepare dependencies before publication credentials are acquired. These examples
assume an existing environment (`uv run --project shared --no-sync --offline python`).
No commands below install dependencies or acquire credentials themselves.

## Inventory and release selection

```sh
uv run --project shared --no-sync --offline python shared/scripts/validate-tekton \
  --inventory-root release/pipelineruns --select-repository "$REPOSITORY" \
  --branch "$RELEASE" \
  --allowed-branches rhoai-2.25,rhoai-3.4,rhoai-3.5,rhoai-3.6,rhoai-3.7-ea.1 \
  --publication-intent "$PUBLISH_REQUESTED" \
  --allow-batch-publication "$ALLOW_BATCH_PUBLICATION" --json
```

This is read-only and emits `{"include":[{"repository":"...",
"inventory_revision":"..."}]}`. Both boolean arguments accept only `true` or
`false`. Publishing all repositories requires explicit batch confirmation before
matrix fanout. The caller owns the allowed release list and inventory root;
shared code owns safe enumeration, membership checks and consent enforcement.
Inventory controls do not acquire tokens or publish anything. Supply the same
`--allowed-branches` to planning/resume to enforce the caller's policy there too.

## Component repositories: no consumer Python adapter

```sh
uv run --project shared --no-sync --offline python shared/scripts/validate-tekton \
  --reconcile --path target --branch "$RELEASE" \
  --config-dir "policy/tekton-templates/$TEMPLATE_SET" \
  --allowed-operations M,D --inventory-revision "$INVENTORY_REVISION" \
  --report-dir "$REPORT" --github-output "$GITHUB_OUTPUT"
```

Planning leaves `target` untouched and never needs publication credentials. The
report must be outside the target and must not already exist. Shared code owns
strict YAML/discovery, template selection/overlay containment, isolated staging,
initial → preview → fix → non-fix validation → idempotence, exact scope/mode/byte
checks, patch, data-only bundle and receipt generation. Only existing regular
files can change. D is supported only for selected PipelineRuns with an explicit
initial template deletion signal. Additions, renames, mode or resource-kind
changes are rejected. Resource-only streams and absent PipelineRun scopes can
produce a no-op. External parents and generation are not supported by this
existing-file operation.

The shared outputs are `status=ready|no-changes`, `base`, and
`policy_digest_short`. Only for a non-preview request with `status=ready`, obtain
a narrowly scoped token and check out a publication target at that exact base.
Then use the same shared entrypoint:

```sh
uv run --project shared --no-sync --offline python shared/scripts/validate-tekton \
  --resume-report "$REPORT" --publish --path publish-target --branch "$RELEASE" \
  --config-dir "policy/tekton-templates/$TEMPLATE_SET" --allowed-operations M,D \
  --inventory-revision "$INVENTORY_REVISION" \
  --repository "red-hat-data-services/$REPOSITORY" \
  --head "automation/enforce-pipelineruns-$RELEASE-$TEMPLATE_SET-$POLICY_DIGEST_SHORT" \
  --marker "<!-- template-set:$TEMPLATE_SET -->" \
  --title "chore: reconcile component repo PipelineRuns on $RELEASE" \
  --body-file policy/docs/component-reconcile-pr.md --lifecycle create-or-report
```

`POLICY_DIGEST_SHORT` comes from the shared plan output, not consumer bundle
parsing. Body text is trusted caller-owned Markdown, optionally including legacy
markers and policy-specific review notes. Shared code appends the changed files,
validation/provenance and deferred-case summary. `--tracking-url https://...` adds
an explicit Markdown tracking link. No publication-metadata module or DSL exists.

Without `--publish`, resume verifies in a disposable local checkout and leaves
the caller's target untouched. It emits `ready_to_publish=true` only for verified
nonempty changes. This supports pre-token verification followed by the identical
resume invocation with `--publish`. Publication cannot target main/master.
When supplied, `--source-sha` cross-checks the policy checkout's actual commit.

## Scan-specific policy extension

For nested KC PipelineRuns, add `--tekton-layout nested --policy-python
source/script/scan_resource_policy.py` to planning and resume; omit D (M is the
default). The static publication arguments are:

```sh
--repository red-hat-data-services/konflux-central \
--head "automation/pipelinerun-conformance/$RELEASE" \
--marker 'Conformance-Generated: true' \
--title "chore: reconcile PipelineRun conventions for $RELEASE" \
--body-file source/docs/scan-resource-pr.md --lifecycle create-or-update
```

A trusted workflow selects the extension path in its clean policy checkout,
never the target or receipt. Its only API is:

- `CONTEXT_PATHS`: fixed tuple of branch-local tracked data filenames.
- `filters`: mapping of filter names to trusted local functions.
- `decision(data, context)`: returns `status` (`applicable`, `skipped`, `manual`,
  `unresolved`), `reason`, and `variables` for one parsed PipelineRun.
- `check(original, candidate, context)`: returns exactly `True` after checking
  policy ownership/resource rules, or rejects the change.

`context` is `{"branch": branch, "documents": {relative_path: parsed_data_or_None}}`.
Inputs are detached dict/list/scalar data, copied per call; context files remain
byte/mode-identical through every phase. No phase, filesystem, stage, report,
command or rendering callbacks are supplied. The extension retains task/ref
applicability, manual task budgets, resource floors/preservation and field
ownership. Shared code owns all loops, serialization and reporting. There is no
network resolver, general graph engine, artifact-selected import or callback.

## Receipts, source and publication safety

These are **trusted same-job planner receipts**, not protection against a
compromised trusted planner. The optional one-shot `--reconcile --publish`
operation is intended for trusted callers with pre-provisioned authentication:
it runs trusted planning, templates, filters and the optional policy module
while any already-present publication credentials remain in the environment.
It does not isolate planning from those credentials. Prefer the consumer flow
shown above: plan, verify, acquire the scoped token, then `--resume-report
--publish`. The resume operation itself never reruns templates or imports the policy
module under publication credentials. It compares CLI-selected source/config and
recorded roles, the exact manifest, base Git bytes, modes and current clean
checkout before replay; the existing publisher independently checks origin,
whole-checkout scope, index/staged bytes, remote base freshness and no-force PR
lifecycle. Hashes alone never authorize an arbitrary planner.

Operational CLI imports use source bytes rather than ignored `lib` bytecode;
wrong/dirty source and stale receipts fail closed. Runtime source revision is
derived from the actual checkout, not taken from a report. Caller-root aliases
(such as macOS `/var`) are supported; symlink descendants in inventory/policy
paths are rejected.

Upload useful `result.json`, `phases/*.json`, `phases/*.log`, diagnostic
`enforcement.patch`, and optional final `publication.json`. Do not upload the
stage checkout. Bundles are internal continuation data; consumers do not parse,
mutate or execute them. Old consumer-specific inventory/request sidecar schemas
are not required.

For precommit integration, `tests/tekton_candidate_source.py` creates a durable
clean disposable source fixture. Its fixture-only commit is **not a production
pin**; production rollout requires a separately reviewed committed source SHA.
