# GAP PR monitor (RHOAIENG-93565, RHOAIENG-97054)

Read leader `state.json`, evaluate each child PR, post commit statuses via
`PRStatusUpdater`, update `pr-status`, and write the file back.

Stage 2 requires two gates before a repository PR can pass:

- every component build selected from that repository's `.tekton` PipelineRuns
- every job in the main-to-release feasibility workflow

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
| No component build is triggered, and feasibility succeeded | `success` | `success` |
| All expected builds and feasibility jobs succeeded | `success` | `success` |
| Already merged | `success` | skip post |

A failed repository gate posts a failing promoter status on that PR. Merging
the leader PR to keep run history does not mark the repository PR successful.
`builds[].image` is left unchanged; image URIs are not required for the gate.

When every child has a final gate status, the monitor also sets `overall-status`
and (optionally) posts a green `gated artifacts promoter` status on the
**Leader PR itself** so auto-merge can preserve run history on `main`
(RHOAIENG-97052).

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

## Overall status (RHOAIENG-97052)

The monitor **writes** `overall-status` into `state.json` only when **every**
component PR has a final gate status (`success`, `merge-failure`, or
`build-failure`). `build-pending` and `new` stay in progress.

| Children | `overall-status` written | Leader auto-merge signal |
|----------|--------------------------|---------------------------|
| Any still `new` or `build-pending` | omitted | no |
| All final, any `merge-failure` or `build-failure` | `failure` | no (keep Leader open) |
| All `success` | `success` | yes — green check on Leader |

The Leader is auto-merged only when every component PR is a **success**.

With `--publish-state`, ordering is: evaluate children → write `state.json` →
commit/push → then (only if `overall-status` is `success` and a Leader PR URL
is set via `GAP_LEADER_PR_URL` / `--leader-pr-url`) post
`gated artifacts promoter` = **success** on the **published** Leader head SHA
so auto-merge cannot race ahead of the state commit.

If another workflow run updated the same Leader branch, publish fetches the
remote tip, retries `git push` with `pull --rebase` between attempts (up to
three), instead of failing the job on a non-fast-forward push.

Example after all children succeed:

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
  "overall-status": "success"
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
