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

An active failed Leader remains open so its child builds and Early Gate can be
retried. Only a verified newer Leader on the same target branch may retire an
unsuccessful batch. Retirement records the replacement URL and preserves actual
build/test results; it does not approve child PRs.

Successful Leaders may finalize once component builds and EG build/tests pass.
Both successful finalization and verified retirement use normal GitHub auto-merge
with `--match-head-commit`. Repository checks, approvals and merge rules apply.

| Run result | Leader label | Title prefix | State retained |
| --- | --- | --- | --- |
| Success | `result-gap-success` | `[GAP passed]` | `GAP History/successful/<run>/state.json` |
| Replaced failure | `result-gap-failure` | `[GAP failed]` | `GAP History/unsuccessful/<run>/state.json` |
| Replaced incomplete run | `result-gap-superseded` | `[GAP superseded]` | `GAP History/unsuccessful/<run>/state.json` |

With coordinated `GAP_MOVE_FINAL_STATE=true` deployment, each finalized run has
one history record and its active `GAP Leaders/<run>/state.json` is removed in
the same commit. Failed active runs remain under `GAP Leaders` until replacement.
A Leader's green archival check permits merging its record; the failure title,
label and saved gate outcomes retain its actual result.

Legacy failed history copies without retirement metadata resume active monitoring
on the next publishing pass. Successful and retired archives remain immutable.
Already merged Leaders are not reopened automatically. The GitHub App requires
label/title write and pull-request auto-merge permissions; blocked operations
surface an error and leave the record available for retry.

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

### Leader finalization and child merge approval

A Leader is successful only after its component builds and Early Gate build/tests
pass. Failed component or EG checks keep the Leader open for retry and do not
approve passing siblings for merge. A failure alone never requests Leader
history auto-merge.

When a genuine newer Leader in the same repository and target branch opens, the
monitor validates that replacement, records its URL in `leader-lifecycle`, and
retires the previous batch into `GAP History/unsuccessful`. That merge preserves
the run record; retirement never approves child PRs. Successful batches move to
`GAP History/successful` without requiring a replacement.

Legacy failed history copies without a retirement receipt resume active
monitoring on a publishing monitor pass. A history copy by itself is not evidence
that a replacement exists. Already merged records remain historical records.
