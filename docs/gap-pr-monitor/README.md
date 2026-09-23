# GAP PR monitor (RHOAIENG-93565)

Stage-1 script: read leader `state.json`, classify each child PR, post commit
statuses via `PRStatusUpdater`, update `pr-status`, write the file back.

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

## State file path (RHOAIENG-93564)

Layout:

```text
gap-leaders/
  gap-<uuid>/
    state.json
```

The Leader PR’s **`gap-<uuid>` label** is the trigger id. The matching state
file is **`gap-leaders/<trigger_id>/state.json`**.

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

The script resolves `state.json`, updates it, and writes `state_path=` to
`GITHUB_OUTPUT` for the commit step.

### Manual (local or workflow_dispatch)

```bash
# by trigger id
python scripts/gap_pr_monitor.py --trigger-id gap-4e997b5f8c224668b51d2fc8b4677495

# by label(s)
python scripts/gap_pr_monitor.py --label gap-4e997b5f8c224668b51d2fc8b4677495

# explicit path override
python scripts/gap_pr_monitor.py \
  --state-file gap-leaders/gap-4e997b5f8c224668b51d2fc8b4677495/state.json
```

Resolution order (`resolve_state_file`, after merging CLI + `GAP_*` env):

1. `--state-file` / `GAP_STATE_FILE` (manual override)
2. `--trigger-id` / `GAP_TRIGGER_ID` → `gap-leaders/<id>/state.json`
3. `--label gap-*` / `GAP_PR_LABELS` → same
4. Optional `--allow-gap-dir-fallback`: exactly one `gap-leaders/gap-*/state.json`

## Local run

```bash
python scripts/gap_pr_monitor.py --trigger-id gap-e2e20260923133000
python scripts/gap_pr_monitor.py --state-file gap-leaders/gap-e2e…/state.json --dry-run
```
