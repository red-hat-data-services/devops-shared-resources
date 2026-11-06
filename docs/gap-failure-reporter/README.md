# GAP failure reporter

Upserts **one** markdown table comment on the Leader PR listing children that
are `build-failure`, `merge-failure`, `build-pending`, `new`, or **closed**,
including close reasons from PR comments. For `build-failure` rows, Build links
are completed Konflux GitHub Checks whose name includes `on-pull-request`
(never `on-push`), preferring PipelineRun URLs over Konflux UI home pages.
`merge-failure` / `build-pending` rows leave Build links blank.

## How it runs

Invoked automatically at the end of **`gap_pr_monitor`**
(`run_monitor_publish_and_signal`) after `state.json` is published, so the
summary matches the same monitor pass. The existing `gap-pr-monitor` workflow
is enough — no separate hourly reporter workflow is required.

## Marker

```html
<!-- gap-failure-summary -->
```

Each run edits the existing comment with this marker (or creates it once).

## CLI (optional / debug)

```bash
python scripts/gap_failure_reporter.py \
  --repo-root /path/to/gated-artifacts-promoter \
  --leader-pr-url https://github.com/org/gated-artifacts-promoter/pull/N \
  --state-file "GAP Leaders/<date>_<trigger>/state.json" \
  [--dry-run]
```

## Repository creation and auto-merge observations

RHAI-19863 and RHAI-19864 add an optional `creator-outcomes` list to Leader
state. It records each attempted repository and source/target pair, outcome,
sanitized reason, child PR URL when known, creator Actions run URL and UTC
observation time. Success observations are retained to clear older warnings on
a retry; they are not shown as failures. Legacy state files remain valid.

| Outcome | Meaning |
| --- | --- |
| `creation-failure` | Sync/create/update failed without a known child PR URL |
| `pr-operation-failure` | A PR was found/created, but a later API operation failed |
| `manual-merge` | Source map explicitly sets `automerge: no`; manual action is needed |
| `automerge-unavailable` | GitHub explicitly rejected auto-merge as disallowed |
| `success` | The attempt completed, or there was nothing to sync |

The creator continues after per-repository errors, saves the Leader and report,
then exits nonzero if errors remain. Manual-policy/disallowed-auto-merge
warnings do not become component build failures. An unrelated permission or
network error is never inferred to mean auto-merge is disabled.

The creator updates only the `gap-creator-outcomes` section of the same
`gap-failure-summary` comment, preserving an existing build table. The monitor
then rebuilds the consolidated comment from persisted state on its normal
passes. There is no additional reporter workflow or comment stream.

The table has stable repository, source/target, category, child PR, reason,
workflow and observed-time columns for dashboard extraction. A failed repository
with no PR is shown with a repository link and an empty PR cell (`—`).
This works even when all child creations fail.

A successful same-trigger retry replaces that repository's older warning.
`--only` retries preserve unattempted repository observations and child progress.
The reporter hides manual/operation warnings once the PR is merged and hides
a previously disallowed-auto-merge warning after auto-merge is enabled. If the
current PR lookup fails, the recorded evidence remains visible with an
uncertainty note; unknown does not mean resolved or newly disabled.

Tokens, URL userinfo and Authorization headers are redacted before reasons
are stored or rendered. Publishing the comment requires the creator App to
write Leader PR issue comments; failure is surfaced after the state has been
saved so a subsequent reporter pass can recover it. Dry run sends no comment
writes. This work neither changes component gate decisions nor enables repository
auto-merge settings, Slack notifications, or run-history retention.

A failed retry that cannot determine the child PR URL retains the previous
child PR and its recorded build/status data. A successful no-change retry
still clears that repository's old child entry. Malformed optional creator
observations are warned about and omitted from rendering; the build table and
reports for other Leaders continue, and the stored state is not rewritten.

## Konflux-backed monitor entries

For entries with current-head Konflux build observations, the report uses the
monitor's persisted PipelineRun evidence and links rather than independently
classifying GitHub build checks. See [the source-of-truth migration](../gap-pr-monitor/konflux-build-status.md).
