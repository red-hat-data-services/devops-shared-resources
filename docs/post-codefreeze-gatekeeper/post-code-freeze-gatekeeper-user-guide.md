# Post-Codefreeze Gatekeeper — User Guide

This check runs on every PR targeting the `rhoai-3.5` branch and validates that the PR is backed by an approved Jira release blocker.

## Setup (one-time, per repo)

1. Copy [`post-codefreeze-gatekeeper.yaml.example`](post-codefreeze-gatekeeper.yaml.example) into your repo as `.github/workflows/post-codefreeze-gatekeeper.yaml` on the `rhoai-3.5` branch.

2. Ensure the org-level secrets `JIRA_USER_EMAIL` and `JIRA_API_TOKEN` are available to your repo (taken care by DevTestOps for red-hat-data-services repos).

## What it checks

For each Jira issue referenced in the PR description, the gatekeeper verifies:

- The issue belongs to the `RHOAIENG` or `RHAIENG` project
- `fixVersion` includes `3.5 GA RHOAI RELEASE`
- `Target Version` includes `3.5 GA RHOAI RELEASE`
- `Release Blocker` is set to `Approved`

All referenced issues must pass every check. The results are posted as a comment on the PR.

## How to use

1. Include a Jira issue key in your PR description (e.g., `RHOAIENG-12345`). Multiple keys are supported — all must pass. You can also provide the full jira url like https://redhat.atlassian.net/browse/RHOAIENG-1234

2. The check runs automatically when the PR is opened, updated, the description is edited or `run-gatekeeper` label is added.

3. If the check fails, fix the Jira issue(s) and re-trigger using one of:
   - Push a new commit
   - Edit the PR description
   - Add the `run-gatekeeper` label to the PR (it is auto-removed after each run)
