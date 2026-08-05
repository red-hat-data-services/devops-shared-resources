# Post-Codefreeze Gatekeeper

A reusable GitHub Actions workflow that enforces post-code-freeze policies on RHOAI release branches by validating that all PRs are backed by approved Jira release blockers.

## Overview

After code freeze, only changes tied to approved release blockers should land on release branches. This workflow runs as a CI status check on PRs and validates:

- A Jira issue (`RHOAIENG-*` / `RHAIENG-*`) is referenced in the PR description
- The issue belongs to the correct Jira project
- The issue has the correct `fixVersion` and `Target Version` for the release
- The issue's `Release Blocker` field is set to `Approved`

The workflow posts a detailed comment on the PR with pass/fail results for each check.

## Architecture

```
Consumer Repo                          devops-shared-resources
┌──────────────────────────┐          ┌───────────────────────────────────┐
│ .github/workflows/       │          │ .github/workflows/                │
│   post-codefreeze-       │          │   post-codefreeze-gate-logic.yml  │
│   gate.yml               │─call───▶ │       │                           │
│   (thin caller,          │          │       ▼                           │
│    no logic)             │          │ .github/actions/                  │
└──────────────────────────┘          │   validate-jira-issues/           │
                                      │     action.yml                    │
                                      │       │                           │
                                      │       ▼ curl                      │
                                      │ Jira REST API                     │
                                      └───────────────────────────────────┘
```

All logic lives in the central repo. The per-repo caller is a thin boilerplate (~12 lines) that never needs updating.

Combined with an org-level GitHub **ruleset** requiring this status check on `rhoai-*` branches, the system enforces that:
- Direct pushes to release branches are blocked
- All changes go through PRs
- PRs cannot merge unless the gatekeeper passes

## Quick Start

See [consumer-setup.md](consumer-setup.md) for step-by-step adoption instructions.

## Components

| File | Purpose |
|------|---------|
| `.github/workflows/post-codefreeze-gate-logic.yml` | Reusable gate-check workflow (all logic) |
| `.github/actions/validate-jira-issues/action.yml` | Composite action with Jira validation logic |
| `scripts/discover-jira-fields.sh` | Helper to find Jira custom field IDs |
| `docs/post-codefreeze-gatekeeper/caller-gate.yml.example` | Template caller workflow for consumer repos |
| `docs/post-codefreeze-gatekeeper/ruleset-config.json.example` | Reference GitHub ruleset configuration |

## Supported Branch Patterns

| Pattern | Example | Jira Version |
|---------|---------|-------------|
| `rhoai-X.Y` | `rhoai-2.6` | `2.6 GA RHOAI RELEASE` |
| `rhoai-X.Y-ea.N` | `rhoai-2.6-ea.1` | `2.6 EA1 RHOAI RELEASE` |

X, Y, and N are single digits (0-9).

## GitHub Ruleset

An org-level ruleset targeting `rhoai-*` branches should be configured with:
- **Restrict updates** — blocks direct pushes
- **Require pull request** — enforces PR workflow
- **Require status checks** — requires `post-codefreeze-gate / post-codefreeze-gate` to pass
- **Block force pushes**

See [ruleset-config.json.example](ruleset-config.json.example) for the full configuration.

## Secrets Required

| Secret | Description |
|--------|-------------|
| `JIRA_USER_EMAIL` | Email for Jira REST API Basic Auth |
| `JIRA_API_TOKEN` | API token for Jira REST API Basic Auth |

These should be configured as org-level GitHub Actions secrets.
