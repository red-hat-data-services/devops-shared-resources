# Gated Artifacts Promoter

Orchestrates main→stable (or configured) branch syncs via `scripts/sync_branches.py`,
then opens a **Leader PR** in `red-hat-data-services/gated-artifacts-promoter` that
tracks every child sync PR for a given trigger ID.

## Layout

- `scripts/run_gated_artifacts_promoter.py` — CLI orchestrator
- `lib/trigger_id.py` — unique trigger ID generator
- `lib/state_file.py` — `state.json` build/validate helpers
- `lib/leader_pr.py` — Leader PR create/update via `gh`

## Quick start

```bash
pip install -r requirements-dev.txt
export GITHUB_TOKEN="your-token"   # or SYNC_TOKEN
export GH_TOKEN="$GITHUB_TOKEN"    # required for Leader PR (gh CLI)

python scripts/run_gated_artifacts_promoter.py \
  --config /path/to/main-stable-source-map.yaml \
  --only kserve \
  --dry-run
```

Omit `--trigger-id` to generate one (used in the state path and as a PR label).
Each new trigger ID **merges** any previously open Leader PR (with
`--delete-branch`), then creates `gap-leader/<trigger-id>` from main and opens
a **new Leader PR** with a dated state file at
`GAP Leaders/<UTC-date>_<trigger-id>/state.json`. A rerun with the same trigger
ID updates that trigger's existing PR and branch. Child sync PRs are reused via
the shared `gated-artifacts-promoter` tracking label.

The orchestrator defaults to `pr-head: source` (main/master → stable) with **no**
`ignore-files`. Set `pr-head: sync-branch` on an entry when you need
`ignore-files` (e.g. `.tekton/*`).

## Config

Infra config is `src/config/main-stable-source-map.yaml` (mirrors
`main-release-source-map.yaml`, target `stable`). Example:

```yaml
defaults:
  source-branch: main
  target-branch: stable
  pr-head: source

git:
  - name: kserve
    automerge: "no"
    repo-url: https://github.com/red-hat-data-services/kserve.git
  - name: odh-dashboard
    automerge: "no"
    repo-url: https://github.com/red-hat-data-services/odh-dashboard.git
```

## Outputs

Stdout includes:

- `trigger_id=...`
- Per-entry sync messages and child PR URLs
- Leader PR URL (or dry-run summary)

Leader PR path: `GAP Leaders/<UTC-date>_<trigger-id>/state.json`
(head branch: `gap-leader/<trigger-id>`)

Stage 1 `state.json` shape ([RHOAIENG-93564](https://redhat.atlassian.net/browse/RHOAIENG-93564)):

```json
{
  "pull-requests": [
    {
      "repo": "kserve",
      "pr-url": "https://github.com/org/kserve/pull/1",
      "pr-status": "new",
      "builds": []
    }
  ]
}
```

## Consumer workflow

The GitHub Actions workflow lives in `rhods-devops-infra`:

1. Checkout infra (for config)
2. Checkout this repo
3. Install requirements
4. Run `scripts/run_gated_artifacts_promoter.py`

See `docs/gated-artifacts-promoter/consumer-setup.md`.
