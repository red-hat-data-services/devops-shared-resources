# Consumer Repo Setup Guide

This guide explains how to adopt the post-codefreeze gatekeeper in your repository.

## Prerequisites

- Your repo is in the `red-hat-data-services` GitHub organization
- The org-level ruleset `release-branch-codefreeze` is active (contact your org admin)
- Jira API secrets are available (org-level or repo-level)

## Step 1: Add the Caller Workflow

Copy [`caller-gate.yml.example`](post-codefreeze-gatekeeper.yaml.example) to `.github/workflows/post-codefreeze-gate.yml` in your repo.

No configuration needed — all defaults (Jira field IDs, base URL) are baked into the central reusable workflow. The caller is a thin boilerplate that just invokes the central workflow.

**Important:** Do not rename the job key `post-codefreeze-gate`. The status check name depends on it.

## Step 2: Configure Secrets

Ensure these secrets are available to your repo (org-level secrets are recommended):

| Secret | Description |
|--------|-------------|
| `JIRA_USER_EMAIL` | Email address for Jira REST API authentication |
| `JIRA_API_TOKEN` | API token for Jira REST API authentication |

## Step 3: Verify

1. Create a test branch matching the pattern (e.g., `rhoai-9.9`)
2. Open a PR targeting that branch
3. Verify the gate check runs and posts a comment
4. Clean up the test branch

## How It Works

The gatekeeper runs on every PR opened/updated/edited against `rhoai-X.Y` or `rhoai-X.Y-ea.N` branches and checks:

1. A Jira issue key (`RHOAIENG-*` or `RHAIENG-*`) is present in the PR description
2. The Jira issue belongs to the `RHOAIENG` or `RHAIENG` project
3. The issue's `fixVersion` matches the expected release version
4. The issue's `Target Version` matches the expected release version
5. The issue's `Release Blocker` field is set to `Approved`

All checks run regardless of earlier failures — the PR comment reports every issue at once.

## Branch → Jira Version Mapping

| Branch Pattern | Jira Version |
|---------------|-------------|
| `rhoai-2.6` | `2.6 GA RHOAI RELEASE` |
| `rhoai-2.6-ea.1` | `2.6 EA1 RHOAI RELEASE` |

## Troubleshooting

**Status check not appearing:**
The check only triggers for PRs targeting branches matching `rhoai-[0-9].[0-9]` or `rhoai-[0-9].[0-9]-ea.[0-9]`. Multi-digit versions (e.g., `rhoai-2.16`) are not supported.

**"No Jira issue found" error:**
The PR description must contain a Jira key in the format `RHOAIENG-1234` or `RHAIENG-1234`. URLs to Jira issues also work since the key is extracted from the URL text.

**"Could not fetch issue" error:**
Check that the `JIRA_USER_EMAIL` and `JIRA_API_TOKEN` secrets are correct and the Jira user has read access to the issue.

**Comment not updating (duplicates appearing):**
Ensure only one caller workflow file exists. The comment is identified by an HTML marker and should be idempotent.
