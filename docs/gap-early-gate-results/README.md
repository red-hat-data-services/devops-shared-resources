# GAP Early Gate results monitor

After Stage-1 sets `overall-status=early-gate-triggered` and comments
`/early-gate` on the Leader, Early Gate posts a **terminal** comment on that
same Leader (for example):

```text
<!-- early-gate-build-complete -->
✅ **Early Gate passed** — `3.5.2`
| **Status** | ✅ SUCCESS |
```

or the failed variant (`Early Gate failed` / `FAILED`).

## What this monitor does

1. Trigger on Leader `issue_comment` (or `workflow_dispatch`).
2. Exit immediately unless the comment looks like an EG terminal report.
3. Require current `overall-status=early-gate-triggered`.
4. Set `overall-status` to `success` or `failure` from the **latest** terminal
   comment (or the triggering comment body).
5. Publish `state.json` and green the Leader (`gated artifacts promoter`) so
   auto-merge can proceed (same as RHAI-8079).

Only comments from trusted reporters (default `rhods-ci-bot`) with the
`early-gate-build-complete` marker are used. Progress comments and casual
discussion are ignored. The workflow wake-up is only a signal — the script
always reconciles full comment history and takes the latest trusted terminal
report. Final `success` / `failure` are never overwritten.

## Script

```bash
python scripts/gap_early_gate_results.py \
  --repo-root /path/to/gated-artifacts-promoter \
  --leader-pr-url https://github.com/org/gated-artifacts-promoter/pull/N \
  --trigger-id gap-<uuid> \
  --publish-state
```

Use `--dry-run` to parse without writing. Use `--comment-body` /
`GAP_EG_COMMENT_BODY` when Actions already has the triggering comment.
