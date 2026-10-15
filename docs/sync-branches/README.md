# Branch Sync Tooling

Python tooling for syncing one git branch into another, with optional PR creation.

## Layout

- `scripts/sync_branches.py` — CLI entrypoint
- `lib/config_parser.py` — config file parsing and validation
- `lib/merge_resolver.py` — merge logic with ignore-file handling
- `lib/pr_creator.py` — GitHub pull request creation and tracking-label updates

## Quick start

```bash
pip install -r requirements-dev.txt
export GITHUB_TOKEN="your-token"

# Default: merge into a temporary sync branch, then open a PR into stable
python scripts/sync_branches.py \
  --source-repo https://github.com/org/repo.git \
  --source-branch main \
  --target-branch stable \
  --tracking-label lake-gate

# Or open the PR directly from main -> stable (no sync branch)
python scripts/sync_branches.py \
  --source-repo https://github.com/org/repo.git \
  --source-branch main \
  --target-branch stable \
  --pr-head source \
  --tracking-label lake-gate
```

## Config file

When `--config` is provided, per-entry sync flags must not also be passed on the CLI.

Run a subset of configured entries:

```bash
export GITHUB_TOKEN="your-token"

python scripts/sync_branches.py \
  --config config/sync-branches.yaml \
  --only kserve,kubeflow
```

### Native `syncs:` format

```yaml
syncs:
  - name: main-to-stable
    sync-type: pr
    src:
      url: https://github.com/org/repo.git
      branch: main
    dest:
      branch: stable
    ignore-files:
      - config/overrides.yaml
    pr:
      head-strategy: sync-branch   # or: source (PR directly from src.branch)
      branch: sync-main-to-stable  # only used with head-strategy: sync-branch
      tracking-label: lake-gate
      labels:
        - automation
      automerge: true
      reviewers:
        - alice
```

`dest.url` defaults to `src.url` when omitted.

### Infra repo `git:` format

For orchestrator repos such as `rhods-devops-infra`, the existing component source map is supported:

```yaml
defaults:
  source-branch: main
  target-branch: stable
  tracking-label: lake-gate
  pr-head: sync-branch   # or: source

git:
  - name: kserve
    automerge: "yes"
    repo-url: https://github.com/red-hat-data-services/kserve.git
    ignore-files: .tekton/*   # requires pr-head: sync-branch
  - name: odh-dashboard
    automerge: "yes"
    repo-url: https://github.com/red-hat-data-services/odh-dashboard.git
    pr-head: source           # PR opens as main -> stable
```

See `docs/sync-branches/consumer-setup.md` for infra-repo config format and how the script is invoked from CI.

## PR head strategy

When using `sync-type: pr`, choose how the pull request is opened:

- `sync-branch` (default): merge the source branch into a temporary branch, push it, and open a PR into the target branch. Supports `ignore-files` and pre-resolved merge conflicts.
- `source`: open the PR directly from the source branch into the target branch (e.g. `main` → `stable`). Requires the same repository and cannot be combined with `ignore-files`.

CLI:

```bash
python scripts/sync_branches.py \
  --source-repo https://github.com/org/repo.git \
  --source-branch main \
  --target-branch stable \
  --pr-head source
```

Config:

```yaml
pr:
  head-strategy: source
```

## Sync types

- `pr` (default): open or update a PR using the selected PR head strategy
- `push`: push a bootstrap branch without opening a PR
- `commit-merge`: merge directly into the target branch and push

When `sync-type` is `pr` and the target branch does not exist yet, the run
uses `push` instead and creates that branch from the source tip. An existing
target branch stays on the PR path and is not force-pushed.

## Testing

```bash
pytest -v
```
