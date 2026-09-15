# devops-shared-resources
A central repository for reusable DevOps assets: workflows, templates, scripts, configuration snippets, and shared automation resources that can be referenced across multiple projects.

## PR status updater (GAP)

Posts GitHub commit statuses for Gated Artifacts Promoter collaborator PRs (RHOAIENG-93330).

- `lib/pr_status_updater.py` — reusable library (`gh api` commit statuses)
- `scripts/post_pr_status.py` — manual CLI
- `docs/pr-status-updater/README.md` — usage
- `tests/` — unit tests (`pytest`; see docs)
