# Consumer Repo Setup Guide (Evaluation)

> **Evaluation mode:** The gatekeeper is currently advisory only — it posts a PR comment with pass/fail results but does not block merges.

This guide explains how to adopt the post-codefreeze gatekeeper in your repository.

## Prerequisites

- Your repo is in the `red-hat-data-services` GitHub organization
- Jira API secrets are available (org-level, managed by DevTestOps)

## Step 1: Add the Caller Workflow

Copy [`post-codefreeze-gatekeeper.yaml.example`](post-codefreeze-gatekeeper.yaml.example) to `.github/workflows/post-codefreeze-gatekeeper.yaml` on the `rhoai-3.5` branch in your repo.

No configuration needed — all defaults (Jira field IDs, base URL) are baked into the central reusable workflow. The caller is a thin boilerplate that just invokes the central workflow.

## Step 2: Verify

1. Open a PR targeting the `rhoai-3.5` branch
2. Include a Jira issue key (e.g., `RHOAIENG-12345`) in the PR description
3. Verify the gate check runs and posts a comment
4. The check result is informational — it will not prevent merging

## How It Works

The gatekeeper runs on every PR opened/updated/edited against `rhoai-X.Y` or `rhoai-X.Y-ea.N` branches and checks:

1. A Jira issue key (`RHOAIENG-*`, `RHAIENG-*`, or `AIPCC-*`) is present in the PR description
2. The Jira issue belongs to the `RHOAIENG`, `RHAIENG`, or `AIPCC` project
3. The issue's `fixVersion` matches the expected release version
4. The issue's `Release Blocker` field is set to `Approved`

All checks run regardless of earlier failures — the PR comment reports every issue at once.

## Branch → Jira Version Mapping

| Branch Pattern | Jira Version |
|---------------|-------------|
| `rhoai-2.6` | `2.6 GA RHOAI RELEASE` |
| `rhoai-2.6-ea.1` | `2.6 EA1 RHOAI RELEASE` |

## Troubleshooting

**Check not appearing:**
The check only triggers for PRs targeting branches matching `rhoai-[0-9].[0-9]` or `rhoai-[0-9].[0-9]-ea.[0-9]`. Multi-digit versions (e.g., `rhoai-2.16`) are not supported.

**"No Jira issue found" error:**
The PR description must contain a Jira key in the format `RHOAIENG-1234`, `RHAIENG-1234`, or `AIPCC-1234`. URLs to Jira issues also work since the key is extracted from the URL text.

**"Could not fetch issue" error:**
Check that the `JIRA_USER_EMAIL` and `JIRA_API_TOKEN` secrets are correct. The email must match the Atlassian account that generated the API token, and the token must be active.

**Comment not updating (duplicates appearing):**
Ensure only one caller workflow file exists. The comment is identified by an HTML marker and should be idempotent.
