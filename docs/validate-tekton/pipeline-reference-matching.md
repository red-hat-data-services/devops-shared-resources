# Reference-qualified template entries

A template may add an optional `pipeline_ref` qualifier to its existing event matchers:

```yaml
templates:
  - name: special
    file: special.yaml.j2
    match_fn: [is_push_pipeline_by_cel, is_scheduled_pipeline]
    pipeline_ref:
      resolver: git
      params:
        url: https://example.org/team/repository.git
        pathInRepo: pipelines/special.yaml
```

The qualifier requires an exact resolver and exact values for the listed parameters, AND at least one configured event matcher. Other resolver parameters may be present. Inline `pipelineSpec` documents do not match a reference-qualified entry. Duplicate reference parameter names fail closed.

Exactly one qualified match takes precedence over unqualified fallback entries, regardless of config order. Multiple qualified matches are rejected rather than resolved by order. When no qualified entry matches, unqualified entries keep their existing first-match behavior. Existing configurations without qualifiers are unchanged.

The qualifier is literal structural matching, not remote resolution or an authorization decision. Trusted selection policy must still validate revisions, branch-local context and eligibility. Repository URLs, paths, cluster names and resource policy remain caller-owned; no consumer-specific values are built into the shared matcher.
