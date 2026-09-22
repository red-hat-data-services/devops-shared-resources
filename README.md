# devops-shared-resources
A central repository for reusable DevOps assets: workflows, templates, scripts, configuration snippets, and shared automation resources that can be referenced across multiple projects.

## Branch sync tooling

Python libraries and a CLI for syncing one git branch into another, with optional PR creation.

- `scripts/sync_branches.py` — CLI entrypoint (`--pr-head sync-branch|source`)
- `lib/` — reusable modules (`config_parser`, `merge_resolver`, `pr_creator`)
- `docs/sync-branches/README.md` — usage, config format, and PR head strategies
- `docs/sync-branches/consumer-setup.md` — infra-repo config format (`git:` source map) and CI integration notes
- `tests/` — unit and local end-to-end tests (CI: `.github/workflows/sync-branches-tests.yml`, pytest only)

## PR status updater (GAP)

Posts GitHub commit statuses for Gated Artifacts Promoter collaborator PRs (RHOAIENG-93330).

- `lib/pr_status_updater.py` — reusable library (`gh api` commit statuses)
- `scripts/post_pr_status.py` — manual CLI
- `docs/pr-status-updater/README.md` — usage
- `tests/` — unit tests (`pytest`; see docs)

## GAP PR monitor (Stage 1)

Reads a leader `state.json`, posts green `gated artifacts promoter` statuses via the
PR status updater, and sets `pr-status` to `success` (RHOAIENG-93565).

- `lib/gap_pr_monitor.py` — state-file orchestration
- `scripts/gap_pr_monitor.py` — manual CLI
- `docs/gap-pr-monitor/README.md` — usage
- Workflow wrapper: `gated-artifacts-promoter` `.github/workflows/gap-pr-monitor.yml`
