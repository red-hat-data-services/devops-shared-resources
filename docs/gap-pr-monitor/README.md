# GAP PR monitor (RHOAIENG-93565)

Stage-1 script: read leader `state.json`, classify each child PR, post commit
statuses via `PRStatusUpdater`, update `pr-status`, write the file back.

When every child is Stage-1 terminal, the monitor also sets `overall-status`
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

## Stage-1 outcomes

| Child PR condition | `pr-status` | Commit status |
|--------------------|-------------|---------------|
| Merge conflict (`CONFLICTING` / `DIRTY`) | `merge-failure` | `failure` |
| Otherwise mergeable | `success` | `success` (dummy green gate) |
| Already merged | `success` | skip post |

### Overall status (RHOAIENG-97052)

The monitor **writes** `overall-status` into `state.json` only when **every**
component PR has reached a Stage-1 final status (`success` or `merge-failure`).

| Children | `overall-status` written | Leader auto-merge signal |
|----------|--------------------------|---------------------------|
| Any still `new` / non-final | omitted | no |
| All final, any `merge-failure` | `failure` | no (Stage-1: keep Leader open) |
| All `success` | `success` | yes — green check on Leader |

For Stage 1, the Leader is auto-merged only when every component PR is a
**success**.

When `overall-status` is `success` and the workflow passes the Leader PR URL
(`GAP_LEADER_PR_URL` / `--leader-pr-url`), the monitor posts
`gated artifacts promoter` = **success** on that Leader PR so auto-merge can
land history on `main`.

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
3. Run `python scripts/gap_pr_monitor.py --repo-root . --allow-gap-dir-fallback`

The workflow passes GitHub context via env vars (no resolution logic in YAML):

| Env var | Source |
|---------|--------|
| `GAP_STATE_FILE` | `workflow_dispatch` input `state_file` |
| `GAP_TRIGGER_ID` | `workflow_dispatch` input `trigger_id` |
| `GAP_PR_LABELS` | Leader PR labels (comma-separated) |
| `GAP_LEADER_PR_URL` | Leader PR HTML URL (e.g. `github.event.pull_request.html_url`) |

The script resolves `state.json`, updates it, and writes `state_path=` (and
`overall_status=` when set) to `GITHUB_OUTPUT` for the commit step.

### Manual (local or workflow_dispatch)

```bash
# by trigger id
python scripts/gap_pr_monitor.py --trigger-id gap-4e997b5f8c224668b51d2fc8b4677495

# by label(s)
python scripts/gap_pr_monitor.py --label gap-4e997b5f8c224668b51d2fc8b4677495

# explicit path override + Leader auto-merge signal
python scripts/gap_pr_monitor.py \
  --state-file GAP Leaders/2026-09-25_gap-4e997b5f8c224668b51d2fc8b4677495/state.json \
  --leader-pr-url https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/12
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
