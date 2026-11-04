# GAP failure reporter

Upserts **one** markdown table comment on the Leader PR listing children that
are `build-failure`, `merge-failure`, `build-pending`, `new`, or **closed**,
including close reasons from PR comments and clickable build links for the
same Konflux checks Stage-1 expects (``select_build_checks`` / ``.tekton``),
preferring PipelineRun URLs over Konflux UI home pages.

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
