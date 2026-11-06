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
Each new trigger ID preserves previously open Leaders and their branches, then creates `gap-leader/<trigger-id>` from main and opens a **new
Leader PR** with a dated state file at
`GAP Leaders/<UTC-date>_<trigger-id>/state.json`. A rerun with the same trigger
ID updates that trigger's existing PR and branch. Child sync PRs are reused via
the shared `gated-artifacts-promoter` tracking label.

The orchestrator defaults to `pr-head: source` (main/master → stable) with **no**
`ignore-files`. Set `pr-head: sync-branch` on an entry when you need
`ignore-files` (e.g. `.tekton/*`).

A newly onboarded component has no `stable` branch. That sync pushes the
source tip as `stable` and does not open a child PR. Once `stable` exists,
later runs stay on the main→stable PR path.

## Run history and Leader outcomes

A newer daily run no longer closes an older Leader or deletes its branch.
The scheduled monitors continue to process open Leaders until each run has a
terminal outcome. A pending run is not recorded as failed merely because a
newer run started.

Both terminal outcomes use normal GitHub auto-merge to preserve the run record
in the Leader repository. The exact published head is supplied with
`--match-head-commit`; repository checks, approvals and merge rules still apply.
No admin override is used. A blocked merge leaves the PR and branch available.

| Run result | Leader label | Title prefix | State retained |
| --- | --- | --- | --- |
| Success | `result-gap-success` | `[GAP passed]` | Original path plus successful-history snapshot |
| Failure | `result-gap-failure` | `[GAP failed]` | Original path plus unsuccessful-history snapshot |

Both outcomes get a history snapshot committed **with** the canonical state update:

```text
GAP History/
  successful/<UTC-date>_<trigger-id>/state.json
  unsuccessful/<UTC-date>_<trigger-id>/state.json
```

The canonical `GAP Leaders/<UTC-date>_<trigger-id>/state.json` remains where
the monitors expect it. Both copies retain the actual `overall-status` and
individual child statuses/builds. History is outside monitor discovery, so
snapshots cannot trigger duplicate runs. A future dashboard can read both
history folders for completed runs and the open Leaders for in-progress runs.
Count each trigger once; its canonical file and history snapshot are the same run.

The green Leader conclusion check permits merging the **record**, not passing
the product gate. Existing component checks and the distinction between Early
Gate success and failure are unchanged. An incomplete run has no history
snapshot and is not queued for merge by this finalization path.

This change applies to future finalizations. It does not automatically reopen
or recover previously closed Leader PRs. The Leader repository's GitHub App
needs label/title write and pull-request auto-merge permissions. If those
operations fail, the error is surfaced; the run is not silently deleted.

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

## Per-repository creator reporting

The creator records `creator-outcomes` in Leader state and updates the existing
consolidated Leader summary comment, including failures with no child PR URL.
It processes the remaining repositories after an error, publishes what it
collected, then exits nonzero if creation/operation errors remain. An
`automerge: no` policy or GitHub's explicit auto-merge-disabled warning is
reported separately and does not itself fail a successful PR creation.

Same-trigger partial retries preserve unattempted child state and observations.
See [the reporter documentation](../gap-failure-reporter/README.md) for the
data contract, dashboard columns and recovery behavior.
