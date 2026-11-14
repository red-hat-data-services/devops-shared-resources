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

These matchers classify pipeline families; they do not authorize repository URLs or revisions and do not resolve remote content. Trusted caller compatibility guards (or existing selection policies) must enforce any required repository URL, revision and pipeline-definition contracts. Resource and cluster policy remain template-owned.

## Container-family routing without a selector

`is_container_build_push`, `is_container_build_pull`, and
`is_container_build_scheduled` combine event identity with the standard Git
`pathInRepo` values `pipelines/container-build.yaml` and
`pipelines/multi-arch-container-build.yaml`. Inline Pipelines match only when
`tasks`/`finally` contain a unique `rpms-signature-scan` task. Malformed inline
evidence and duplicate reference/task identities fail closed. Other paths,
including chart/FBC paths, never match these container entries.

```yaml
guard_only: true
tekton_layout: nested
templates:
  # Existing chart/FBC entries precede these.
  - name: pull-request
    file: pull-request.yaml.j2
    match_fn: is_container_build_pull
    exclude_branches: ["rhoai-*"]
  - name: push
    file: push.yaml.j2
    match_fn: is_container_build_push
  - name: scheduled
    file: container-scheduled.yaml.j2
    match_fn: is_container_build_scheduled
  - name: default
    file: default.yaml.j2  # Empty mapping: {}. No resources or workspaces.
    match_fn: is_pipeline_run
```

`guard_only: true` opts strict reconciliation into normal classification plus a
trusted `check(original, candidate, context)` callback, without a selector or
`decision()` callback. It conflicts with `selection_template`. Existing consumers
without this flag retain their selector/legacy-decision contracts. Keep all
reference/task/workspace compatibility assertions necessary to justify actual
writes in the guard; classification alone does not resolve external Pipelines.

`exclude_branches` accepts unique exact names or literal prefixes with one
trailing `*`. The selected template is excluded terminally, not retried against a
later matcher. A branch is required when such an entry is encountered. Exclusion
is supported for existing-file templates that neither inherit/generate nor act
as parents. Strict phase reports retain a `skipped` row; the ordinary validator
and fixer honor the same scope before field rendering. Existing YAML/path,
all-candidates-before-write, receipt, and publication checks remain enforced.

In guard-only strict reconciliation, a file with no matching template is
reported `skipped`. An explicit no-op catch-all instead renders and reports it
unchanged/conformant. A caller guard should reject any changes to no-op inputs.
