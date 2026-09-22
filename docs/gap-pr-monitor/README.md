# GAP PR monitor (RHOAIENG-93565)

Stage-1 shell: read a leader `state.json`, post a **green** `gated artifacts promoter`
commit status on every listed PR (via the 93330 status updater), set each entry's
`pr-status` to `success`, write the file, and exit.

## Manual usage

```bash
cd devops-shared-resources
pip install -r requirements-dev.txt

python scripts/gap_pr_monitor.py \
  --state-file /path/to/gap-<trigger-id>/state.json

# Dry-run (does not POST statuses or write the state file)
python scripts/gap_pr_monitor.py \
  --state-file /path/to/state.json \
  --dry-run
```

Requires authenticated `gh` (`gh auth login` or `GH_TOKEN` / `GITHUB_TOKEN`).

## State file shape

Pretty-printed JSON (see RHOAIENG-93564):

```json
{
  "pull-requests": [
    {
      "repo": "odh-dashboard",
      "pr-url": "https://github.com/org/repo/pull/1",
      "pr-status": "new",
      "builds": []
    }
  ]
}
```

## Tests

```bash
pytest -v tests/test_gap_pr_monitor.py
```

## Workflow

The thin GitHub Actions wrapper lives in `gated-artifacts-promoter`
(`.github/workflows/gap-pr-monitor.yml`) and calls this script.
