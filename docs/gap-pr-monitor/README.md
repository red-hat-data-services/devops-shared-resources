# GAP PR monitor (RHOAIENG-93565, RHOAIENG-97054)

Read leader `state.json`, evaluate each child PR, post commit statuses via
`PRStatusUpdater`, update `pr-status`, and write the file back.

Stage 2 requires two gates before a repository PR can pass:

- every component build selected from that repository's `.tekton` PipelineRuns
- every job in the main-to-release feasibility workflow

Component builds are restricted to the repository-scoped allowlist in
[component_repo_map.json](https://github.com/red-hat-data-services/konflux-central/blob/main/config/component_repo_map.json).
The monitor fetches one validated snapshot from `main` per Leader evaluation,
including any head-change recheck. A fetch or validation error stops evaluation
before state or commit statuses are updated.

Only components listed under the child PR's exact repository name are gated;
a missing repository entry means no allowed components (repository aliases are
not inferred). PipelineRun component labels match exact mapping keys or numeric
release variants such as `odh-eval-hub-v3-6` for `odh-eval-hub`. Every selected
PipelineRun must have a nonempty `appstudio.openshift.io/component` label;
a missing or invalid label is a configuration error, not an ignored build.
Component names are never inferred from PipelineRun names. Unmapped builds are logged and ignored,
including failed, pending, or missing checks. Mapped builds still have to pass;
feasibility and merge-conflict checks are unaffected.

Both gates use the same check-run retrieval and conclusion rules. A query
error, or any completed conclusion other than `success`, does not pass.
Unresolved feasibility checks fail six hours after the head commit's committer
timestamp. A component build that is still queued, running, or missing stays
`build-pending` until that check completes; it is not a build failure. A new
commit starts a new feasibility window; reruns of the same commit do not.
The build producer defaults to the `konflux-internal-p02` GitHub App
(`GAP_BUILD_CHECK_APP_SLUG` / `GAP_BUILD_CHECK_APP_ID` select another
environment). PipelineRun names come from the repository configuration,
including `{{pull_request_number}}` and component labels that differ from the
check name.

| Repository PR condition | `pr-status` | Commit status |
|-------------------------|-------------|---------------|
| Merge conflict | `merge-failure` | `failure` |
| Feasibility check failed or timed out | `merge-failure` | `failure` |
| Any expected component build completed without success | `build-failure` | `failure` |
| A required build or feasibility check is queued, running, or missing | `build-pending` | `pending` |
| Good child, but a sibling is still pending | `success` | `pending` (deferred; RHAI-8261) |
| All children build/gate success (EG not done) | `success` | `pending` (waiting for Early Gate) |
| All children build/gate success + EG passed | `success` | `success` (posted by EG-results) |
| Build-OK child + EG failed | `success` | `failure` (posted by EG-results; note held) |
| Mixed batch: good child + sibling build/merge failure | `success` | `success` after batch concludes (no EG) |
| Already merged | `success` | skip post |

A failed repository gate posts a failing promoter status on that PR immediately.
A **successful** child does **not** get a green `gated artifacts promoter` check
while siblings are still open (RHAI-8261), and — when the whole batch is
all-success — stays `pending` until the **combined Leader Early Gate** finishes
(`gap_early_gate_results` posts child `success` or `failure`). `pr-status` in
`state.json` still records the Stage-1 build/gate outcome. An all-success batch
sets ``overall-status=early-gate-triggered`` and comments ``/early-gate`` on the
Leader (RHAI-10942). Merging the leader PR to keep run history does not mark a
failed repository PR successful. `builds[].image` is left unchanged; image URIs are
not required for the gate.

When every child has a final gate status, the monitor sets `overall-status`.
All-success batches become `early-gate-triggered` and the monitor comments
`/early-gate` on the **Leader** once (RHAI-10942). Final overall `success` /
`failure`, Leader green, and child check conclusion for build-passing PRs are
written by the Early Gate results monitor; overall `failure` is set immediately
when any child failed without EG (RHOAIENG-97052 / RHAI-8079). Pending children
never conclude the Leader.

## CEL trigger evaluator setup

Build the Go helper before running the monitor (Go 1.27 or newer):

```bash
mkdir -p bin
(cd tools/pac-cel && go build -o ../../bin/pac-cel-evaluate .)
```

The monitor looks for `bin/pac-cel-evaluate` relative to this checkout.
Set `PAC_CEL_EVALUATOR=/absolute/path/to/pac-cel-evaluate` to use a separately
built binary, including when installing the Python package elsewhere. The binary
must be built for the runtime's OS/architecture. No build or download occurs at
monitor runtime; Go is needed only to build the helper.

The helper batches each PR's distinct `on-cel-expression` annotations in one
JSON request. It uses pinned CEL-Go and `gobwas/glob` versions, not a Python
approximation. See [the helper documentation](../../tools/pac-cel/README.md)
for the protocol, upstream baseline, and compatibility limits.

Evaluation uses current PR facts: source/target branches and repository URLs,
title, and revision-pinned file collections (`all`, `added`, `deleted`,
`modified`, `renamed`). Renames use the new path, as PaC does.
SHAs are retained as context, not invented top-level CEL variables.

This is **PR-state selection, not webhook replay**. The monitor does not invent
`body`, `headers`, `event_type`, or Repository custom parameters. Expressions
requiring unavailable facts, invalid/non-boolean expressions, and helper failures
retain the pipeline's expected check and produce a warning; they never establish
that no build is required. A possibly truncated GitHub file listing is a query
error rather than a no-build signal.

### Revision-pinned changed files

The reusable `lib/github_diff.py` collector is independent of CEL and GAP state.
The monitor supplies the base/head commit SHAs captured with the PR snapshot;
it never substitutes the live PR-files endpoint for that pinned comparison.
GitHub's compare API is the fast path. Its first response includes the changed
files, independently of commit pagination. A file list at the 300-file cap,
missing/invalid file data, or an API error triggers a local Git fallback.

The fallback creates a temporary bare repository (no working-tree checkout),
fetches the pinned revisions with `--filter=blob:none`, and diffs the merge base
to the head. With a merge base from the API it fetches only the required commit
tips; otherwise it fetches history to compute the merge base locally. NUL-delimited
output preserves tabs/newlines in paths. Rename detection can download blobs,
and its classifications may differ from GitHub's rename heuristics.

Git must be installed. Git operations share a 120-second budget and their captured
output is limited to 32 MiB. No credential helpers, interactive authentication,
external diff/textconv programs, or checkout hooks are used. A private repository
requiring Git credentials cannot use this fallback and remains unresolved; the
collector does not discover or access credentials. Temporary repositories are
removed after evaluation. Fetch, timeout, invalid-path-encoding, or diff errors
are unresolved query errors, never empty changed-file lists. These limits bound
time and captured output, not total downloaded repository size.

The standalone legacy PR-file helpers remain available for callers without a
pinned revision pair; the monitor always requires both SHAs.

## How merge conflicts are detected

The monitor does **not** run `git merge` locally. It asks GitHub via the `gh` CLI:

```bash
gh pr view <number> -R <owner>/<repo> --json state,mergeable,mergeStateStatus,url
```

| Field | Conflict signal |
|-------|-----------------|
| `mergeable` | `CONFLICTING` |
| `mergeStateStatus` | `DIRTY` (also treated as conflict) |

If `mergeable` is `UNKNOWN` / null (GitHub still computing), the script waits briefly and re-queries once.

## Overall status (RHOAIENG-97052 / RHAI-10942)

The monitor **writes** `overall-status` into `state.json` only when **every**
component PR has a final gate status (`success`, `merge-failure`, or
`build-failure`). `build-pending` and `new` stay in progress.

| Children | `overall-status` written | Leader action | Good-child green |
|----------|--------------------------|---------------|------------------|
| Any still `new` or `build-pending` | omitted | none | no (stay pending) |
| All final, any `merge-failure` or `build-failure` | `failure` | green Leader (history, RHAI-8079) | yes — only `success` children |
| All `success` (or existing `build-success`) | `early-gate-triggered` | comment `/early-gate` once (RHAI-10942); children stay pending until EG-results | no — EG-results posts child success/failure |
| Existing final `success` / `failure` (EG monitor) | preserved | green Leader | yes |

All-success does **not** set `overall-status=success` in this monitor. That final
value (and Leader green for a successful run) belongs to the Early Gate results
monitor (`scripts/gap_early_gate_results.py`, workflow
`gap-early-gate-results.yml`). It watches Leader comments for the terminal
`early-gate-build-complete` / Early Gate passed|failed report, then writes
`success` or `failure` and greens the Leader. This Stage-1 monitor only hands
off via `early-gate-triggered` and the `/early-gate` comment. Existing
`success` / `failure` in state are never overwritten.

With `--publish-state`, ordering is: evaluate children → write `state.json` →
commit/push → then, when a Leader PR URL is provided
(`GAP_LEADER_PR_URL` / `--leader-pr-url`):

- `early-gate-triggered` → comment `/early-gate` on that Leader (idempotent)
- `success` or `failure` → post `gated artifacts promoter` = **success** on the
  **published** Leader head SHA (RHAI-8079)

If another workflow run updated the same Leader branch, publish fetches the
remote tip, retries `git push` with `pull --rebase` between attempts (up to
three), instead of failing the job on a non-fast-forward push.

Example after all children succeed (Early Gate not finished yet):

```json
{
  "pull-requests": [
    {
      "repo": "kserve-branch",
      "pr-url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
      "pr-status": "success",
      "builds": []
    }
  ],
  "overall-status": "early-gate-triggered"
}
```

## State file path (RHOAIENG-93564)

Layout:

```text
GAP Leaders/
  <YYYY-MM-DD>_gap-<uuid>/
    state.json
```

The Leader PR’s **`gap-<uuid>` label** is the trigger id. The matching state
file is **`GAP Leaders/<YYYY-MM-DD>_<trigger_id>/state.json`**.

### Automatic (Leader PR / Actions)

Thin workflow in `gated-artifacts-promoter` only:

1. Checkout leader repo + `devops-shared-resources`
2. Install deps
3. Run `python scripts/gap_pr_monitor.py --repo-root . --allow-gap-dir-fallback --publish-state`

The workflow passes GitHub context via env vars (no resolution logic in YAML):

| Env var | Source |
|---------|--------|
| `GAP_STATE_FILE` | `workflow_dispatch` input `state_file` |
| `GAP_TRIGGER_ID` | `workflow_dispatch` input `trigger_id` |
| `GAP_PR_LABELS` | Leader PR labels (comma-separated) |
| `GAP_LEADER_REPO` | Optional override for schedule discovery (default: `GITHUB_REPOSITORY`) |
| `GAP_LEADER_LABEL` | Optional Leader label for schedule discovery (default: `gated-artifacts-promoter`) |
| `GAP_LEADER_PR_URL` | Leader PR HTML URL, or `LATEST_LEADER_PR` on `workflow_dispatch` |

The script resolves `state.json`, updates it, publishes the commit (when
`--publish-state`), and writes `state_path=`, `overall_status=`, and
`published_sha=` to `GITHUB_OUTPUT`.

### Scheduled runs (RHOAIENG-97050)

When Actions sets `GITHUB_EVENT_NAME=schedule` (or you pass `--schedule`), the
**same** script discovers every **open** Leader PR labeled
`gated-artifacts-promoter`, checks out each head branch, runs the build and
feasibility monitor, then publish-then-signal, and continues on per-Leader
errors. The thin workflow schedules this hourly (`0 * * * *`). Existing
`pull_request` and `workflow_dispatch` behavior is unchanged.

```bash
python scripts/gap_pr_monitor.py --schedule --leader-repo org/gap --dry-run
```

### Manual (local or workflow_dispatch)

```bash
# by trigger id
python scripts/gap_pr_monitor.py --trigger-id gap-4e997b5f8c224668b51d2fc8b4677495

# by label(s)
python scripts/gap_pr_monitor.py --label gap-4e997b5f8c224668b51d2fc8b4677495

# publish state then Leader auto-merge signal (Actions path)
python scripts/gap_pr_monitor.py \
  --state-file GAP Leaders/2026-09-25_gap-4e997b5f8c224668b51d2fc8b4677495/state.json \
  --leader-pr-url https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/12 \
  --publish-state
```

Resolution order (`resolve_state_file`, after merging CLI + `GAP_*` env):

1. `--state-file` / `GAP_STATE_FILE` (manual override)
2. `--trigger-id` / `GAP_TRIGGER_ID` → `GAP Leaders/<YYYY-MM-DD>_<id>/state.json`
3. `--label gap-*` / `GAP_PR_LABELS` → same
4. Optional `--allow-gap-dir-fallback`: exactly one `GAP Leaders/<date>_gap-*/state.json`

## Local run

```bash
python scripts/gap_pr_monitor.py --trigger-id gap-e2e20260923133000
python scripts/gap_pr_monitor.py --state-file GAP Leaders/2026-09-25_gap-e2e…/state.json --dry-run
```
