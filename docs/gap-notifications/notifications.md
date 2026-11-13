# GAP Notifications System

This document describes the overall function of the Gated Artifacts Promoter (GAP) notifications subsystem. 

## Background on Gated Artifacts Promoter

GAP is an orchestrated multi-PR promotion flow:

 1. Orchestrator (scripts/run_gated_artifacts_promoter.py,
    plus lib/trigger_id.py, lib/state_file.py,
    lib/leader_pr.py) runs configured branch syncs with a
    shared trigger ID and opens a Leader PR that tracks
    the child PRs (one per repo/branch being promoted).
    All state lives in a state.json on the Leader.
 2. PR status updater (lib/pr_status_updater.py /
    scripts/post_pr_status.py) posts gated artifacts
    promoter commit statuses on child PRs.

### The monitor code itself: `scripts/gap_pr_monitor.py`

This is the heart of "GAP monitoring". On a set schedule, it:

 - Resolves the Leader/state.json snapshot.
 - For each child PR, evaluates two gates:
     - Component builds — Konflux PipelineRuns restricted
       to a per-repository allowlist
       (component_repo_map.json from konflux-central).
     - Main-to-release feasibility — the dry-run sync
       workflow for every active release.
 - Publishes decisions as child commit statuses +
   pr-status in state. Key policy: good children stay
   pending while siblings or the Early Gate are
   outstanding; only the combined Early Gate greens them.
 - Publishes state, then fires off the notification
   subsystem.

## The Notifications System

The PR monitor gathers information on the status of all the component PRs and their build statuses. The GAP notifications subsystem takes in this information from the PR monitor and decides on the following things:

- Does a new JIRA need to be created?
- Does an existing JIRA need to be closed?
- Does a new slack notification need to be sent, and where?

Based on the answers to these questions, it will dispatch/update JIRA and slack notifications as needed.

## Event Identity and Deduplication

TODO explain what specific variables are factored into determining a unique incident ID and how that translates to a jira label "identity"


## Repo Secrets and Environment Variables
TODO list how the secrets and environment variables in the gated-artifacts-promoter repo can be used to modify notification behavior

### Brief Overview of the notification system's pieces

 - monitor_hook.py - Runs after the PR monitor. It can be set to either disabled|preview|write to globally control notification behavior. 
 - analyze_leader_state.py / analyze_merge_state.py —
   offline analyzers that normalize accepted evidence into
   observations: build / conflict / feasibility, each with
   status failure|success|unknown and stable event IDs.
   They never query live services or invent recovery from
   aggregate PR status (that is the job of the PR monitor)
 - merge_receipts.py — structured evidence (conflict
   status from PR mergeability, feasibility from workflow
   runs/checks). TODO explain to me the diff between this and analyze_leader_state
 - dispatch_incidents.py — the dispatcher: single-writer,
   environment-scoped incident lifecycle where Jira is the
   cross-Leader registry and routing authority
   (gap.notification.v1 property +
   gap-registry-<fingerprint> labels + per-issue episode
   UUIDs). Failures create/reuse issues; explicit positive
   recovery closes them via a verified transition; unknown
   holds; manual closure + continued failure → successor
   issue, never reopen. TODO explain this better
 - notify_tools.py — Makes working with Jira/Slack easier. Jira/Slack facades, bounded content,
   templates (templates/issue.md.j2, slack.md.j2), managed
   ADF sections (the cosmetic "Slack discussion" and
   "notification metadata" projections).
 - lookup_tools.py — read-only inspection/tracking (feeds
   the "Jira / Slack" column in the leader PR failure report comment table).
 - ownership.py — live component/team ownership mapping
   (GitLab konflux-to-jira map + rhoai-ops GitHub repo
   map) used to assign Jira components and @-mention
   owning Slack teams.

TODO what is the purpose of the following two tools?
Standalone operator CLI (scripts/gap_notify.py): dry-run
 by default; --write for real sends; --inspect for
 read-only Jira diagnostics; --resolve-receipt for manual
 receipt reconciliation. Environment selection via
 GAP_JIRA_URL (staging vs production, fail-closed
 validation), GAP_SLACK_CHANNEL required.

 Workflow glue (scripts/gap_monitor_workflow.py):
 stdlib-only receipt skip-detection + run policy wrapper
 for the reusable workflow.
