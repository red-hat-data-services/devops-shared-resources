# Main-to-release feasibility gate

Implements [RHOAIENG-97053](https://redhat.atlassian.net/browse/RHOAIENG-97053).
Before a component is promoted into `stable`, check whether its proposed stable
contents would introduce conflicts during the subsequent stable-to-release sync.

## Ruleset setup

After the workflow and implementation are merged into this repository:

1. Create or edit an **organization branch ruleset** for the component repositories.
2. Target **`refs/heads/stable`**. Required workflows run through PRs and merge queues,
   so apply this rule to branches updated through PRs.
3. Enable **Require workflows to pass before merging** and select:
   - Source repository: `red-hat-data-services/devops-shared-resources`
   - Workflow: `.github/workflows/main-release-feasibility.yml`
   - Source ref: `refs/heads/main`, or a rollout ref containing both workflow and code.
4. Choose **Active** for enforcement. **Evaluate** runs the check without blocking.
5. For existing PRs, push a commit or close/reopen the PR to trigger the new rule.

Component repositories need no caller workflow. The source workflow supports
`pull_request` and `merge_group`, and checks applicability in the job because
GitHub ignores ruleset workflow event filters. PRs targeting other branches
receive a successful not-applicable result. The check covers **all stable PRs**,
not just PRs created by GAP or carrying a particular label.

The workflow source must be visible to the target repositories. For an internal
or private source repository, enable organization access in **Settings → Actions
→ General**. GAP creates child PRs using a GitHub App token, which triggers required
workflows (events created by `GITHUB_TOKEN` do not).

## Authentication

`rhods-devops-infra` is private. The component repository's `GITHUB_TOKEN` cannot
read that repository, so the workflow uses the organization's CI App credentials:

- `RHDS_CI_APP_CLIENT_ID`
- `RHDS_CI_APP_PRIVATE_KEY`

These organization Actions secrets must be available to the **component
repositories where the ruleset workflow runs**. Secrets stored only in the
workflow source repository are not inherited. The CI App installation must include
`rhods-devops-infra` and allow reading repository contents.

The workflow requests a short-lived installation token restricted to
`rhods-devops-infra` with `contents: read`, and supplies it only to the infra
checkout. Checkout does not persist the credential, and the token action revokes
the token at job completion. The component checkout uses its own read-only
`GITHUB_TOKEN`; the public shared-resources checkout also uses the default token.

With the current `pull_request` trigger, fork PRs and Dependabot PRs do not receive
these Actions secrets and cannot pass the private infra checkout. GAP's same-repo
main-to-stable PRs and merge-queue runs have access to the organization secrets.
Supporting fork PRs would require a trusted `pull_request_target` entrypoint with
explicit candidate selection, since `github.sha` on that event is the base commit.
Missing credentials or a failed private checkout fail the gate rather than
bypassing the feasibility check.

## Inputs and behavior

Configuration comes from one checkout of `rhods-devops-infra` at `main`:

- [`src/config/main-release-source-map.yaml`](https://github.com/red-hat-data-services/rhods-devops-infra/blob/main/src/config/main-release-source-map.yaml)
- [`src/config/releases.yaml`](https://github.com/red-hat-data-services/rhods-devops-infra/blob/main/src/config/releases.yaml)

This follows the current
[main-to-release automerge workflow](https://github.com/red-hat-data-services/rhods-devops-infra/blob/main/.github/workflows/main-release-auto-merge.yaml):

| Condition | Result |
| --- | --- |
| Exactly one matching repository with `automerge: 'yes'` | Check all active releases |
| Repository explicitly has `automerge: 'no'` | Not applicable, with explanation |
| Missing/duplicate repository mapping or malformed configuration | Fail |
| Empty release list, or only `rhoai-2.*` releases | Not applicable, with explanation |
| Missing active release branch or Git error | Fail |
| Conflict outside ignored paths in any active release | Fail |
| All active releases feasible | Pass |

Mapping uses `repo-url`, not the component `name`: for example, `mlserver` maps to
`red-hat-data-services/MLServer`. GitHub repository names are matched case-insensitively.
`manual-sync: 'yes'` alone does not enable checking, matching the scheduled sync.
Repositories in the main-to-stable map must have an explicit release mapping:
update the release map for newly onboarded components, even when release sync is disabled.

The candidate is the immutable GitHub test-merge commit (`github.sha`) for a PR,
or the combined candidate for a stable merge-queue group. This includes current
stable contents as well as the incoming changes. GAP uses merge commits when
merging child PRs; preserve this merge method so ancestry matches the simulation.
The checker evaluates the candidate, rather than refetching a moving `main`.

Ignored paths combine each component's `ignore-files` with:

- `.github/renovate.json`
- `.tekton/README.md`

As in `sync-git-branches`, the `ours` merge driver preserves release-side content
for ignored content conflicts. Remaining paths are matched with the action's Bash
`[[ "$file" == $exclusion ]]` expression, including escapes, character classes and
patterns matching nested paths. Structural conflicts on excluded paths are
resolved in the disposable index to the release-side state or deletion.
Nonignored conflicts still fail. Every release is checked even if another fails.
The checker operates in disposable local clones, without committing or pushing;
the original checkout, refs and index are preserved. Component code is not run.
The implementation comes from the same source commit as the required workflow.

`ignore-files` must be a comma/space-separated string (a YAML folded string is
fine). Multiline, tab-delimited and list values fail configuration validation;
the sync action consumes one line with `IFS=', ' read`. `automerge` must be the
string `yes` or `no`, quoted or unquoted; boolean `true` is not treated as `yes`.

### Parity with the existing sync

The conflict policy was audited against the actual `sync-git-branches` entrypoint
at [525c3167fe3a85ae79038d6810cf60a0c5aceb59](https://github.com/red-hat-data-services/sync-git-branches/blob/525c3167fe3a85ae79038d6810cf60a0c5aceb59/entrypoint.sh),
using disposable local bare remotes and the same source/target commits. The audit
covers clean and fast-forward merges, content and binary conflicts, add/add,
modify/delete, rename/delete, nested exclusions and Bash pattern syntax.

The gate has deliberate differences from running the scheduled workflow:

- **Source:** the sync uses `src-branch`, falling back to the repository's default
  branch when omitted. The gate uses the proposed merged `stable` commit, as input
  to the future stable-to-release sync, so it also checks stable-only changes.
- **Side effects:** the simulation uses `--no-commit --no-ff` to keep a merge open
  for inspection. It does not create/amend a commit, restore cleanly merged excluded
  files in a final release tree, push, or send notifications. Those operations are
  outside conflict feasibility.
- **Errors:** malformed configuration and Git failures fail the gate explicitly;
  matching the sync script's output-string/error-handling quirks is not its goal.
- **Scope:** eligibility matches the scheduled sync. A manual dispatch can select
  `manual-sync: 'yes'` repositories that this gate skips when automerge is disabled.

Results apply to the checked revisions, not to future pushes. The sync currently
uses `@main` and `alpine:latest`; the checker runs Git on `ubuntu-latest`. Changes
to the action, configuration, or Git versions can change behavior and should be
checked against these parity cases.

## Results and resolving failures

The job summary includes candidate and configuration revisions, ignored patterns,
and a per-release table of target revisions, results and blocking paths. Job logs
include Git's merge output for each release.

A failed required workflow blocks the child PR from merging into stable, keeping
that component out of promotion until its conflicts are resolved. Other component
PRs are independent. The existing Stage-1 GAP monitor's dummy `gated artifacts
promoter` status is a separate check: it only classifies main-to-stable Git conflicts
and may still report `success` while this required workflow blocks promotion.

Resolve the reported release conflicts with the component team and rerun the
workflow. New PR commits trigger a fresh candidate. If only release branches or
infra configuration changed, rerun the workflow to use those updated inputs.
The result is a snapshot; release updates can invalidate an earlier pass. Checks
run again for merge-queue groups when a queue is used.

## Local reproduction

Check out this repository at the workflow source revision and infra at the
configuration revision from the summary. Fetch full component history and the
PR test-merge ref:

```bash
git clone https://github.com/red-hat-data-services/COMPONENT.git component
git -C component fetch origin refs/pull/PR_NUMBER/merge
git -C component checkout --detach FETCH_HEAD

uv run --project devops-shared-resources \
  devops-shared-resources/scripts/check_main_release_feasibility.py \
  --repo-path component \
  --repository red-hat-data-services/COMPONENT \
  --source-ref HEAD \
  --source-map rhods-devops-infra/src/config/main-release-source-map.yaml \
  --releases rhods-devops-infra/src/config/releases.yaml \
  --summary feasibility-report.md
```

Use the candidate SHA and release revisions in the summary for an exact replay
while those objects remain available. A fresh test-merge ref may differ from a
previous run. Standalone YAML files are also accepted; their config revision is
reported as unknown when they are outside a Git checkout.

Exit status is `0` for pass/not-applicable and `1` for conflicts/configuration/Git
errors. `--summary` appends Markdown; it defaults to `GITHUB_STEP_SUMMARY` in CI.

## Development checks

```bash
uv run --no-managed-python --no-project --with-requirements requirements-dev.txt pytest
actionlint .github/workflows/main-release-feasibility.yml
```
