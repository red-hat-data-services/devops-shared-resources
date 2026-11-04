# GAP failure reporter

Separate from Stage-1 build gating. Upserts **one** markdown table comment on
each open Leader PR listing children that are `build-failure`, `merge-failure`,
`build-pending`, `new`, or **closed**, including close reasons parsed from PR
comments when present, and clickable GitHub Check build links.

## Marker

```html
<!-- gap-failure-summary -->
```

Each run edits the existing comment with this marker (or creates it once).

## CLI

```bash
python scripts/gap_failure_reporter.py \
  --repo-root /path/to/gated-artifacts-promoter \
  --leader-pr-url https://github.com/org/gated-artifacts-promoter/pull/N \
  --head-ref gap-leader/gap-... \
  [--dry-run]
```

Schedule / all open Leaders:

```bash
python scripts/gap_failure_reporter.py --repo-root . --schedule
```

## Workflow

Thin wrapper: `gated-artifacts-promoter/.github/workflows/gap-failure-reporter.yml`

- Hourly at minute 20
- `workflow_dispatch` with optional Leader URL / dry-run
