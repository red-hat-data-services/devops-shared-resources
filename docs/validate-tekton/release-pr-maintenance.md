# Generated child PipelineRuns in validate-tekton

Child creation/reconciliation is opt-in through a template's `generate` block.
Existing configurations are unchanged. There is no separate maintenance CLI:
use the normal validator in fix mode, then validate independently from disk.

```sh
uv run scripts/validate-tekton --path ../release-checkout \
  --config-dir ../kc/tekton-templates/release-pr \
  --branch rhoai-3.6 --no-checkout --fix
uv run scripts/validate-tekton --path ../release-checkout \
  --config-dir ../kc/tekton-templates/release-pr \
  --branch rhoai-3.6 --no-checkout
```

Use --pipeline-dir pipelineruns/kserve to restrict nested scope, --json for
machine-readable results, or --fix --dry-run to preview without writing files.
Validation exits nonzero if a generated child is missing or differs from the
parent/template. JSON uses the normal files, summary, fixed, and changes fields.

## Configuration

The templates/configuration remain in the consumer repository (KC).
A child template can specify:

```yaml
parent:
  template: push
  match_fn: [alias_match, exact_component_match]
generate:
  filename: "{component}-pull-request.yaml"
  name: "{component}-on-pull-request"
  inherit_metadata: [labels, namespace]
  preserve_annotations: [pipelinesascode.tekton.dev/on-comment]
  allowed_spec_fields: [params, pipelineRef, taskRunTemplate, workspaces, timeouts, taskRunSpecs]
  git_pipeline_ref: true
  required_parent_fields:
    - [metadata, annotations, pipelinesascode.tekton.dev/on-cel-expression]
```

generate requires a mapping, a parent, and nonempty string filename/name.
Unknown keys and malformed optional values are rejected, including non-boolean
git_pipeline_ref and invalid metadata/annotation/spec-field lists or key paths.
An explicitly empty or null generate value is invalid; omit it to disable generation.
It opts the run into
generated-child reconciliation: parents and unknown files are read-only,
only generated child templates are maintained. Ordinary templates are still
validated, and their errors fail the run even after generated children are fixed.
Do not combine it with unrelated templates that you expect this run to fix.
parent_ref is unsupported;
parents come from the working tree. Existing matching child filenames survive.
Missing files use the filename pattern with {component} substituted.

The internal lib/tekton_children.py helper plans expected children using the
same render_and_merge engine used by validate-tekton. It has no CLI, Git access,
or file writes. It rebuilds from parent spec plus configured metadata and
preserved child annotations. Templates control PR overrides and ABSENT removes
push-only params. Other stale child-only settings disappear. Configured overlay
templates apply. Review consumer policy whenever new publishing params arise.

Matching respects configured parent matcher priority and aliases within each
directory; duplicate parents/children, ambiguous matches, unsafe paths, invalid
YAML, and unsupported configured parent fields fail before PipelineRun writes.
Orphan children are warnings, never deleted. Missing parents fail the run.
Normal template-only validation/fix behavior is unchanged without generate.

For consumers with bounded metadata names, `shorten_name(max_length)` leaves
short strings unchanged and shortens longer ones to a readable prefix plus
`-` and the first eight lowercase hex characters of SHA-256 of the full input.
It requires an integer limit of at least 10. It does not sanitize names or set
consumer naming policy. Reapplying it to an already bounded name is a no-op.

The optional `component_label_match` parent matcher compares exact, nonempty
`appstudio.openshift.io/component` labels, independently of metadata names.
Consumers shortening child names can put it before prefix/alias matchers to
retain parent matching without changing component labels. Missing or different
labels do not match; existing ambiguous-parent/child checks still apply.

Legacy PR names may include the exact terminal `-{{target_branch}}` release
placeholder before their event suffix. Component-prefix extraction recognizes
that placeholder (including whitespace inside the braces), but does not strip
arbitrary placeholders or bypass ambiguity/filename-collision checks.

## Release CEL filter

to_release_pr_cel(branch, filename) is a registered filter in tekton_filters.py.
It parses push CEL, requires exact push-event and release-branch clauses,
retains every other top-level condition/guard, rewrites PipelineRun path
references, and emits PR-event/exact-release clauses. Top-level OR and complex
event/branch selectors fail. Both GA and EA branches are supported.

This is deterministic template/drift validation, not Kubernetes admission
validation or a real build. Tests exercise the validator APIs and real CLI.
