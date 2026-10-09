# GAP PR monitor (RHOAIENG-93565, RHOAIENG-97054)

Read leader `state.json`, evaluate each child PR, post commit statuses via
`PRStatusUpdater`, update `pr-status`, and write the file back.

Stage 2 requires two gates before a repository PR can pass:

- every component build selected from that repository's `.tekton` PipelineRuns
- every job in the main-to-release feasibility workflow

Both gates use the same check-run retrieval and conclusion rules. A missing
check, a query error, or any completed conclusion other than `success` does
not pass. Unresolved checks fail six hours after the head commit's committer
timestamp. A new commit starts a new window; reruns of the same commit do not.
The build producer defaults to the `konflux-internal-p02` GitHub App
(`GAP_BUILD_CHECK_APP_SLUG` / `GAP_BUILD_CHECK_APP_ID` select another
environment). PipelineRun names come from the repository configuration,
including `{{pull_request_number}}` and component labels that differ from the
check name.

| Repository PR condition | `pr-status` | Commit status |
|-------------------------|-------------|---------------|
| Merge conflict | `merge-failure` | `failure` |
| Feasibility check failed or timed out | `merge-failure` | `failure` |
| Any expected component build failed, skipped, or timed out | `build-failure` | `failure` |
| A required check is queued, running, or missing | `build-pending` | `pending` |
| No component build is triggered, and feasibility succeeded | `success` | `success` |
| All expected builds and feasibility jobs succeeded | `success` | `success` |
| Already merged | `success` | skip post |

A failed repository gate posts a failing promoter status on that PR. Merging
the leader PR to keep run history does not mark the repository PR successful.
`builds[].image` is left unchanged; image URIs are not required for the gate.

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
3. Run `python scripts/gap_pr_monitor.py --repo-root . --allow-gap-dir-fallback`

The workflow passes GitHub context via env vars (no resolution logic in YAML):

| Env var | Source |
|---------|--------|
| `GAP_STATE_FILE` | `workflow_dispatch` input `state_file` |
| `GAP_TRIGGER_ID` | `workflow_dispatch` input `trigger_id` |
| `GAP_PR_LABELS` | Leader PR labels (comma-separated) |
| `GAP_LEADER_REPO` | Optional override for schedule discovery (default: `GITHUB_REPOSITORY`) |
| `GAP_LEADER_LABEL` | Optional Leader label for schedule discovery (default: `gated-artifacts-promoter`) |

The script resolves `state.json`, updates it, and writes `state_path=` to
`GITHUB_OUTPUT` for the commit step.

### Scheduled runs (RHOAIENG-97050)

When Actions sets `GITHUB_EVENT_NAME=schedule` (or you pass `--schedule`), the
**same** script discovers every **open** Leader PR labeled
`gated-artifacts-promoter`, checks out each head branch, runs the same
build and feasibility monitor, and commits/pushes `state.json` updates. Existing `pull_request` and
`workflow_dispatch` behavior is unchanged.

```bash
python scripts/gap_pr_monitor.py --schedule --leader-repo org/gap --dry-run
```

### Manual (local or workflow_dispatch)

```bash
# by trigger id
python scripts/gap_pr_monitor.py --trigger-id gap-4e997b5f8c224668b51d2fc8b4677495

# by label(s)
python scripts/gap_pr_monitor.py --label gap-4e997b5f8c224668b51d2fc8b4677495

# explicit path override
python scripts/gap_pr_monitor.py \
  --state-file GAP Leaders/2026-09-25_gap-4e997b5f8c224668b51d2fc8b4677495/state.json
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
