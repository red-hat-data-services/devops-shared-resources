# devops-shared-resources
A central repository for reusable DevOps assets: workflows, templates, scripts, configuration snippets, and shared automation resources that can be referenced across multiple projects.

## Branch sync tooling

Python libraries and a CLI for syncing one git branch into another, with optional PR creation.

- `scripts/sync_branches.py` — CLI entrypoint (`--pr-head sync-branch|source`)
- `lib/` — reusable modules (`config_parser`, `merge_resolver`, `pr_creator`)
- `docs/sync-branches/README.md` — usage, config format, and PR head strategies
- `docs/sync-branches/consumer-setup.md` — infra-repo config format (`git:` source map) and CI integration notes
- `tests/` — unit and local end-to-end tests (CI: `.github/workflows/sync-branches-tests.yml`, pytest only)

## Gated Artifacts Promoter orchestrator (RHOAIENG-93524)

Runs configured branch syncs with a shared trigger ID, then opens a Leader PR that tracks child PRs.

- `scripts/run_gated_artifacts_promoter.py` — CLI orchestrator
- `lib/trigger_id.py`, `lib/state_file.py`, `lib/leader_pr.py` — reusable libraries
- `docs/gated-artifacts-promoter/README.md` — usage
- `docs/gated-artifacts-promoter/consumer-setup.md` — infra workflow contract

## PR status updater (GAP)

Posts GitHub commit statuses for Gated Artifacts Promoter collaborator PRs (RHOAIENG-93330).

- `lib/pr_status_updater.py` — reusable library (`gh api` commit statuses)
- `scripts/post_pr_status.py` — manual CLI
- `docs/pr-status-updater/README.md` — usage
- `tests/` — unit tests (`pytest`; see docs)

## GAP PR monitor (Stage 1)

Reads a leader `state.json`, classifies each child PR (success or merge-failure),
posts the matching `gated artifacts promoter` commit status, and updates
`pr-status` (RHOAIENG-93565; state layout RHOAIENG-93564).

- `scripts/gap_pr_monitor.py` — CLI (uses `lib/pr_status_updater.py`)
- `docs/gap-pr-monitor/README.md` — usage
- Workflow wrapper: `gated-artifacts-promoter` (`.github/workflows/gap-pr-monitor.yml`)

## Repository auto-merge manager

Audits or enables the repository-level GitHub setting that permits pull request auto-merge.

- `lib/repository_automerge.py` — idempotent GitHub API operations and verification
- `scripts/manage_repository_automerge.py` — dry-run-first CLI accepting arguments or stdin
- `docs/repository-automerge/README.md` — usage, permissions, and behavior
- `tests/` — unit tests with mocked GitHub CLI calls

## Main-to-release feasibility gate (RHOAIENG-97053)

An organization ruleset workflow runs the existing main-to-release sync in
dry-run mode for every active release, using the existing release sync exclusions.

- `.github/workflows/main-release-feasibility.yml` — ruleset workflow source
- `scripts/prepare_main_release_dry_run_inputs.py` — prepares action inputs from the supplied infra configuration
- `red-hat-data-services/sync-git-branches` — merge engine, invoked with `dry_run: 'true'`
- `docs/main-release-feasibility/README.md` — ruleset setup and local reproduction
