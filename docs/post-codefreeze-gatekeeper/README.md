# Post-Codefreeze Gatekeeper (Evaluation)

A reusable GitHub Actions workflow that validates post-code-freeze policies on RHOAI release branches by checking that PRs are backed by approved Jira release blockers.

> **Evaluation mode:** This is currently deployed as an advisory check only. It reports pass/fail via a PR comment but does **not** block merges. No org-level ruleset is enforced. This allows teams to evaluate the check and resolve any issues before it becomes mandatory in a future release.

## Overview

After code freeze, only changes tied to approved release blockers should land on release branches. This workflow runs as a CI check on PRs and validates:

- A Jira issue (`RHOAIENG-*` / `RHAIENG-*` / `AIPCC-*`) is referenced in the PR description
- The issue belongs to the correct Jira project
- The issue has the correct `fixVersion` for the release
- The issue's `Release Blocker` field is set to `Approved`

The workflow posts a detailed comment on the PR with pass/fail results for each check.

## Architecture

```
Consumer Repo                          devops-shared-resources
┌──────────────────────────┐          ┌───────────────────────────────────┐
│ .github/workflows/       │          │ .github/workflows/                │
│   post-codefreeze-       │          │   post-codefreeze-gate-logic.yml  │
│   gatekeeper.yaml        │─call───▶ │       │                           │
│   (thin caller,          │          │       ▼                           │
│    no logic)             │          │ .github/actions/                  │
└──────────────────────────┘          │   validate-jira-issues/           │
                                      │     action.yml                    │
                                      │       │                           │
                                      │       ▼ curl                      │
                                      │ Jira REST API                     │
                                      └───────────────────────────────────┘
```

All logic lives in the central repo. The per-repo caller is a thin boilerplate that never needs updating.

## Quick Start

See [post-code-freeze-gatekeeper-user-guide.md](post-code-freeze-gatekeeper-user-guide.md) for step-by-step adoption instructions.

## Components

| File | Purpose |
|------|---------|
| `.github/workflows/post-codefreeze-gate-logic.yml` | Reusable gate-check workflow (all logic) |
| `.github/actions/validate-jira-issues/action.yml` | Composite action with Jira validation logic |
| `docs/post-codefreeze-gatekeeper/post-codefreeze-gatekeeper.yaml.example` | Template caller workflow for consumer repos |

## Supported Branch Patterns

| Pattern | Example | Jira Version |
|---------|---------|-------------|
| `rhoai-X.Y` | `rhoai-2.6` | `2.6 GA RHOAI RELEASE` |
| `rhoai-X.Y-ea.N` | `rhoai-2.6-ea.1` | `2.6 EA1 RHOAI RELEASE` |

X, Y, and N are single digits (0-9).

## Secrets Required

| Secret | Description |
|--------|-------------|
| `JIRA_USER_EMAIL` | Email of the Atlassian account that owns the API token |
| `JIRA_API_TOKEN` | Atlassian API token (Basic Auth) |

These are configured as org-level GitHub Actions secrets for red-hat-data-services repos.
