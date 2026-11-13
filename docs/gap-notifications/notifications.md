# GAP Notifications System

This document describes the overall function of the Gated Artifacts Promoter (GAP) notifications subsystem.

## Background on Gated Artifacts Promoter

GAP is an orchestrated multi-PR promotion flow:

1. Orchestrator (`scripts/run_gated_artifacts_promoter.py`, plus `lib/trigger_id.py`, `lib/state_file.py`, `lib/leader_pr.py`) runs configured branch syncs with a shared trigger ID and opens a Leader PR that tracks the child PRs (one per repo/branch being promoted). All state lives in a `state.json` on the Leader.
2. PR status updater (`lib/pr_status_updater.py` / `scripts/post_pr_status.py`) posts `gated artifacts promoter` commit statuses on child PRs.

### The monitor code itself: `scripts/gap_pr_monitor.py`

This is the heart of "GAP monitoring". On a set schedule, it:

- Resolves the Leader/`state.json` snapshot.
- For each child PR, evaluates two gates:
  - Component builds — Konflux PipelineRuns restricted to a per-repository allowlist (`component_repo_map.json` from konflux-central).
  - Main-to-release feasibility — the dry-run sync workflow for every active release.
- Publishes decisions as child commit statuses + `pr-status` in state. Key policy: good children stay `pending` while siblings or the Early Gate are outstanding; only the combined Early Gate greens them.
- Publishes state, then fires off the notification subsystem.

## The Notifications System

The PR monitor gathers information on the status of all the component PRs and their build statuses. The GAP notifications subsystem takes in this information from the PR monitor and decides on the following things and determines if any "observations" have occurred.

### Observations 

An "observation" refers to something that was observed on a PR monitor run, and the context surrounding it. Observations can be for failures, successes, or unknowns. For example:

- Build failure on konflux `component A`, in `repo X`, `child PR B`, at `commit sha Y`, on `leader PR C` 
- Main-to-release feasibility check success on `Repo X`, `child PR B`, at `commit sha Y`, seen on on `leader PR C` 
- Unknown error - caused by query errors, pending checks, etc

### Incidents == Jira Tickets

"Incidents" are a concept used to deduplicate observations. The following fields of an observation uniquely identify what incident it belongs to:

- component repository
- category (`build`, `conflict`, or `feasibility`)
- (if build) konflux component

Note - the following additional fields also make up an incident, but these are not often expected to differentiate incidents: 
- Jira environment (stage vs production)
- slack channel
- source and destination branch of child PR

These fields are hashed to produce an "incident ID" for every observation. Once an incident ID is determined, the notification system goes through the following process.

1. Search for an existing JIRA with a matching incident ID. 
2. If an open match exists, update it and its Slack thread with information about the new observation — closing the Jira if the observation is a success and a verified done transition is configured.
3. If no open match exists and the observation is a failure, create a new JIRA and start a new Slack notification thread. Unknown observations do none of these.

## Repo Secrets and Variables

These are the gap notification-related variables set in the gated-artifacts-promoter repo.

### Variables

| Variable | Description |
|---|---|
| `GAP_NOTIFICATION_MODE` | Selects `disabled` (default), `preview`, or `write` mode for monitor-run notifications. |
| `GAP_JIRA_URL` | Selects the Jira destination — unset defaults to staging; `https://stage-redhat.atlassian.net` and `https://redhat.atlassian.net` select staging and production respectively; anything else fails closed. |
| `GAP_SLACK_CHANNEL` | The Slack channel ID that notifications are sent to |
| `GAP_OWNERSHIP_CA_BUNDLE` | Optional path to a CA bundle for the internal GitLab ownership-mapping fetch. |
| `JIRA_USER_EMAIL` | Jira account email (fallback name: `JIRA_API_EMAIL`) paired with the token below. |

### Secrets

| Secret | Description |
|---|---|
| `JIRA_API_TOKEN` | Jira API token; must be valid for whichever site `GAP_JIRA_URL` selected. |
| `SLACK_TOKEN` | Slack bot token (fallback name: `SLACK_BOT_TOKEN`) for sending notifications. |

### Brief Overview of the Notification System's Pieces

- `merge_receipts.py` — runs inside the PR monitor during gate evaluation, snapshotting conflict and feasibility evidence (PR mergeability, workflow runs/checks) into `state.json`. 
- `monitor_hook.py` — runs inside the PR monitor, after state publication, and controls monitor notifications (the standalone CLI writes independently). It can be set to either `disabled|preview|write` to control notification behavior.
- `analyze_leader_state.py` / `analyze_merge_state.py` — offline analyzers that read the published `state.json` (including `merge_receipts` output) and normalize it into observations. They never query live services (the monitor has already captured the evidence) or invent recovery from aggregate PR status.
- `dispatch_incidents.py` — the dispatcher: takes observations and drives the incident lifecycle against the Jira registry (search/update/create/close via `notify_tools`). Failures create or reuse issues, explicit successes close them when a verified done transition is configured, and anything ambiguous holds rather than guessing.
- `notify_tools.py` — makes working with Jira/Slack easier. Jira/Slack facades, bounded content, templates (`templates/issue.md.j2`, `slack.md.j2`), managed ADF sections (the cosmetic "Slack discussion" and "notification metadata" projections).
- `ownership.py` — live component/team ownership mapping (GitLab konflux-to-jira map + rhoai-ops GitHub repo map) used to assign Jira components and @-mention owning Slack teams.
- `lookup_tools.py` — read-only inspection/tracking (feeds the "Jira / Slack" column in the Leader PR failure report comment table).

In addition to the library modules, two scripts round out the system:

- `scripts/gap_notify.py` — standalone operator CLI that runs the same dispatcher on a supplied observations file: dry-run preview by default, `--write` for real sends, `--inspect` for read-only Jira diagnostics, `--resolve-receipt` for manually reconciling ambiguous deliveries. Used for ad-hoc runs, trials, and cleaning up held incidents without waiting for a monitor pass.
- `scripts/gap_monitor_workflow.py` — glue for the reusable GitHub workflow: detects receipt-only state commits that can skip a scan, and translates the notification environment variables into the corresponding `gap_pr_monitor.py` flags (failing closed on invalid configuration).
