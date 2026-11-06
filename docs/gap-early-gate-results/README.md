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
   trusted comment.
5. Publish `state.json` and green the Leader (`gated artifacts promoter`)
   (RHAI-8079 — green on both success and failure).
6. On build-passing children only (`pr-status` `success` / `build-success`):
   - EG **passed** → post `success` (auto-merge signal).
   - EG **failed** → post `failure` with a short held note.
   Children that already failed Stage-1 builds keep their failing check.

Early Gate is a **combined** Leader pipeline over the child set, not a per-child
EG build. Stage-1 still records per-child build/gate `pr-status` in `state.json`
and holds child GitHub greens as `pending` until this monitor runs.

Only comments from trusted reporters (default `rhods-ci-bot`) with the
`early-gate-build-complete` marker are used. Progress comments and casual
discussion are ignored. The workflow wake-up is only a signal — the script
always reconciles full comment history and takes the latest trusted terminal
report. Final overall `success` / `failure` are never overwritten.

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
