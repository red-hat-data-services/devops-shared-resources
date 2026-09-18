# Repository Auto-Merge Manager

Audit or enable GitHub's repository-level **Allow auto-merge** setting. Enabling
this setting permits users with write access to configure individual pull
requests to merge after all required checks and reviews pass.

This tool does not enable auto-merge on individual pull requests and does not
create branch protections, rulesets, required checks, or review requirements.

## Requirements

- Python 3.10 or newer
- GitHub CLI (`gh`), authenticated to `github.com`
- Repository administration permission for `--apply`

Authenticate before running the tool:

```bash
gh auth login
gh auth status
```

## Audit

The default mode is read-only. Pass bare repository names to use the default
`red-hat-data-services` organization, or pass full `owner/name` slugs:

```bash
python scripts/manage_repository_automerge.py \
  kserve \
  kubeflow \
  another-owner/another-repository
```

Use `--org` to change the organization applied to bare names:

```bash
python scripts/manage_repository_automerge.py --org example repo-one repo-two
```

When there are no positional repositories, the tool reads one repository per
line from standard input. Blank lines are ignored and duplicates are removed
while preserving input order:

```bash
printf '%s\n' kserve kubeflow odh-dashboard |
  python scripts/manage_repository_automerge.py
```

If positional repositories are provided, standard input is not read.

## Enable Auto-Merge

Pass `--apply` to enable auto-merge on repositories where it is disabled:

```bash
printf '%s\n' kserve kubeflow odh-dashboard |
  python scripts/manage_repository_automerge.py --apply
```

Updates are idempotent. Repositories that already allow auto-merge are skipped,
so it is safe to rerun the same command after a partial failure.

GitHub can briefly return stale repository settings immediately after a PATCH.
The tool validates the PATCH response, then retries the repository GET up to
five times with a two-second delay before reporting verification failure.

GitHub redirects renamed repositories to their current canonical name. Output
uses that canonical name, so an input such as an old repository slug can produce
a different repository name in the result.

## Output And Exit Status

Each repository produces one of these statuses:

- `ALREADY_ENABLED`: no change was needed
- `WOULD_ENABLE`: auto-merge is disabled and audit mode made no change
- `ENABLED`: `--apply` enabled and verified auto-merge
- `FAILED`: lookup, update, or verification failed

The final summary reports totals for each outcome. The command exits with status
`1` when any repository fails or the input is invalid. Finding disabled
repositories during an audit is not considered an execution failure.

Because updates are not transactional, a failed batch can contain repositories
that were successfully changed before the failure. Review the per-repository
output or rerun the command to reconcile the full input.
