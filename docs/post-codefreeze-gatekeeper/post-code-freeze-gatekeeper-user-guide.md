# Post-Codefreeze Gatekeeper — User Guide (Evaluation)

> **Note:** This is the evaluation version of the gatekeeper. The check is **advisory only** — it does not block PR merges. It reports pass/fail as a PR comment to help teams validate their Jira issues before merging, but PRs can still be merged regardless of the result.

This check runs on every PR targeting the `rhoai-3.5` branch and validates that the PR is backed by an approved Jira release blocker.

## Setup (one-time, per repo)

1. Copy [`post-codefreeze-gatekeeper.yaml.example`](post-codefreeze-gatekeeper.yaml.example) into your repo as `.github/workflows/post-codefreeze-gatekeeper.yaml` on the `rhoai-3.5` branch. (will be taken care by the DevTestOps team for this evaluation phase)

2. Ensure the org-level secret `JIRA_API_TOKEN` is available to your repo (taken care by DevTestOps for red-hat-data-services repos).

## What it checks

For each Jira issue referenced in the PR description, the gatekeeper verifies:

- The issue belongs to the `RHOAIENG`, `RHAIENG`, or `AIPCC` project
- `fixVersion` includes `3.5 GA RHOAI RELEASE`
- `Release Blocker` is set to `Approved`

All referenced issues must pass every check. The results are posted as a comment on the PR.

## How to use

1. Include the Jira issue key for the issue that went through the post-code-freeze exception process and has its `Release Blocker` field set to `Approved` (e.g., `RHOAIENG-12345`). Multiple keys are supported — all must pass. You can also provide the full Jira URL like https://redhat.atlassian.net/browse/RHOAIENG-1234

2. The check runs automatically when the PR is opened, updated, the description is edited or `run-gatekeeper` label is added.

3. If the check fails, fix the Jira issue(s) and re-trigger using one of:
   - Push a new commit
   - Edit the PR description
   - Add the `run-gatekeeper` label to the PR (it is auto-removed after each run)

4. Even if the check fails, you can still merge the PR. Use the comment to verify your Jira issue is correctly configured before merging.
