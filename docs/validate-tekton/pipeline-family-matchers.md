# Pipeline-family matchers

`is_chart` and `is_fbc` are ordinary registered pipeline match functions:

```yaml
templates:
  - name: charts
    file: charts.yaml.j2
    match_fn: is_chart
  - name: fbc-fragment
    file: fbc-fragment.yaml.j2
    match_fn: is_fbc
  # Put generic event-based entries after the family-specific entries.
```

Both match push and scheduled PipelineRuns, excluding explicitly identified PRs. They require a Git pipeline reference with the exact `pathInRepo` value `pipelines/helm-chart-build.yaml` or `pipelines/fbc-fragment-build.yaml`, respectively. Inline PipelineSpecs do not match, and duplicate reference parameters fail closed.

Classification keeps its existing first-match behavior: put family entries before generic event entries. There is no separate qualifier field, priority tier, matcher-argument schema, or ambiguity-resolution mechanism.

These matchers classify pipeline families; they do not authorize repository URLs or revisions and do not resolve remote content. Trusted caller selection must still enforce the repository URL, branch-local revisions and pipeline-definition context. Resource and cluster policy remain template-owned.
