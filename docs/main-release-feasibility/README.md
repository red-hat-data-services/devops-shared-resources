# Main-to-release feasibility gate

Implements [RHOAIENG-97053](https://redhat.atlassian.net/browse/RHOAIENG-97053) by
running the existing main-to-release sync action in dry-run mode on stable PRs.

Requires `sync-git-branches@main` with the `dry_run` input from
[PR #3](https://github.com/red-hat-data-services/sync-git-branches/pull/3).

## How it works

1. Read the component mapping and active releases from the private
   `rhods-devops-infra` repository, using the same files as the scheduled sync:
   - [`main-release-source-map.yaml`](https://github.com/red-hat-data-services/rhods-devops-infra/blob/main/src/config/main-release-source-map.yaml)
   - [`releases.yaml`](https://github.com/red-hat-data-services/rhods-devops-infra/blob/main/src/config/releases.yaml)
2. For each active release, run `red-hat-data-services/sync-git-branches@main`
   directly against the component repository, with `dry_run: 'true'` and
   `merge_args: '--no-edit'`.
3. Review its Git merge output and outcome in the release jobs. A failed action
   fails the required workflow. `fail-fast: false` lets every release finish.

The source branch is the mapping's `src-branch`, or the repository default branch
when omitted, matching the existing main-to-release workflow (normally `main`).
This is a **current source-to-release preview**: it does not construct the PR's
proposed stable contents, include stable-only changes, or freeze the source SHA.
Source and release updates can change the result on rerun.

The action owns merging, ignored-file conflict resolution, local commits and
excluded-file restoration. Its existing exit-status/error handling is retained.
Dry-run mode suppresses every push. There are no snapshot or Python merge helpers.

The workflow is thin wiring: checkout, authentication, one preparation call, and
the action matrix. The ruleset controls which repositories and target branches
run it; there is no scope check in the script or workflow steps.

## Input preparation script

`scripts/prepare_main_release_dry_run_inputs.py` has one purpose and three required
arguments: the consumer's `owner/repo`, the source-map file, and the releases file.
The file paths are supplied by the caller, with no hardcoded checkout location.

```bash
uv run --no-project --with-requirements requirements.txt \
  python scripts/prepare_main_release_dry_run_inputs.py \
  red-hat-data-services/kserve \
  /path/to/rhods-devops-infra/src/config/main-release-source-map.yaml \
  /path/to/rhods-devops-infra/src/config/releases.yaml
```

It emits `check` and `matrix` through `GITHUB_OUTPUT`, or stdout outside Actions.
The matrix has one `include` entry per active release, containing every non-secret
action input: upstream/downstream repositories and branches, exclusions,
`merge_args`, `spawn_logs`, and `dry_run`. Runtime credentials stay in the workflow.
Disabled automerge or no active releases produces `check=false` and an empty matrix.

Python and PyYAML (included in `requirements.txt`) are required. The script reads
the supplied files directly, independent of its working directory. It does not
merge, inspect PR branches, or parse/report the sync action's output; that output
remains in the action logs.

## Eligibility and exclusions

- Configure the ruleset to target RHDS stable branches and their merge-queue groups.
- Match the component by `repo-url`, case-insensitively, to accommodate names such
  as `mlserver` mapping to `red-hat-data-services/MLServer`.
- Fail setup for a missing or duplicate mapping.
- Skip repositories explicitly configured with `automerge: 'no'`.
- Check every configured release except `rhoai-2.*`; no active releases means skip.
- Use component-specific `ignore-files` plus `.github/renovate.json` and
  `.tekton/README.md`, matching the scheduled sync. `manual-sync: 'yes'` alone
  does not enable checking.

## Authentication

The organization Actions secrets `RHDS_CI_APP_CLIENT_ID` and
`RHDS_CI_APP_PRIVATE_KEY` must be available to the **component repositories** where
the ruleset workflow runs. Secrets stored only in the workflow source are not
inherited. The CI App needs read access to `rhods-devops-infra`.

Setup creates a short-lived token scoped to that repository with `contents: read`
for the private config checkout. The sync action uses the component repository's
own read-only `GITHUB_TOKEN` to clone/fetch its branches. No write permissions are
needed for a dry run.

Fork/Dependabot PRs on `pull_request` do not receive these Actions secrets, so they
cannot pass the private config checkout. GAP's App-created same-repository PRs
receive organization secrets and trigger workflow runs; PRs created with
`GITHUB_TOKEN` do not trigger new runs.

## Ruleset setup

After the workflow and the action's dry-run support are available:

1. Create or edit an organization branch ruleset for the participating components.
2. Target `refs/heads/stable`.
3. Enable **Require workflows to pass before merging**, selecting:
   - Source: `red-hat-data-services/devops-shared-resources`
   - Workflow: `.github/workflows/main-release-feasibility.yml`
   - Ref: `refs/heads/main`, or a rollout ref containing this workflow.
4. Use **Active** for enforcement, or **Evaluate** for advisory results.
5. Push a commit or close/reopen existing PRs to trigger the new rule.

Component repositories need no caller workflow. Repository and branch targeting
belong to the ruleset; the preparation script only determines action inputs from
the supplied configuration. Missing credentials or configuration errors fail
setup and the workflow.

A failure blocks that component's stable PR until the main-to-release conflict is
resolved. Other component PRs are independent. GAP's Stage-1 dummy status is a
separate check and does not override a required workflow failure.
