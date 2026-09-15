# PR status updater (RHOAIENG-93330)

Posts GitHub **commit statuses** on pull request head commits for the Gated Artifacts Promoter (GAP).

## Requirements

- `gh` CLI authenticated (`gh auth login` or `GITHUB_TOKEN`)
- Python 3.10+

## Manual usage

```bash
pip install -r requirements-dev.txt

# Post a green dummy gate on one PR
python scripts/post_pr_status.py \
  --pr-url https://github.com/red-hat-data-services/odh-dashboard/pull/123 \
  --status success

# Dry-run (prints gh api commands only)
python scripts/post_pr_status.py \
  --pr-url https://github.com/org/repo/pull/1 \
  --status success \
  --dry-run

# Discover PRs by label
python scripts/post_pr_status.py \
  --repo red-hat-data-services/odh-dashboard \
  --label gated-artifacts-promoter \
  --status success
```

Default check context name: `gated artifacts promoter` (override with `--check-name`).

## Tests

```bash
pytest -v tests/test_pr_status_updater.py tests/test_post_pr_status.py
```
