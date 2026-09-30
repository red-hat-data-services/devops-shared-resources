# validate-tekton

Validates and fixes Tekton PipelineRun files (`.tekton/`) in private GitLab repos against Jinja2 templates. The templates define the desired state — enforced values, transforms, and validation checks — all in one place.

## Quick Start

```bash
# Run from inside a cloned repo
cd /path/to/cloned-repo

# Validate (report errors, don't change anything)
validate-tekton -b rhoai-3.5 -n rhoai-private-tenant

# Fix (write corrected files to working tree)
validate-tekton -b rhoai-3.5 -n rhoai-private-tenant --fix

# Fix, commit to feature branch, and create MR
validate-tekton -b rhoai-3.5 -n rhoai-private-tenant --mr

# Validate a repo without cd-ing into it
validate-tekton --path /path/to/cloned-repo -b rhoai-3.5 -n rhoai-private-tenant
```

## How It Works

The tool uses an Ansible-style approach: templates are valid YAML with Jinja2 expressions in quoted strings. The engine:

1. Parses the template as YAML
2. Walks the tree, resolving `_` references from the existing file
3. Renders each value through Jinja (filters, Tekton variable escaping)
4. Merges the result with the existing file (preserving order, formatting, and component-specific fields)

### Modes

- **Validate** (`default`): transforms are skipped, filters check raw values, errors collected
- **Fix** (`--fix`): transforms run, then filters validate the result, corrected files written

## Templates

Built-in templates live in `templates/`. One per pipeline type:

- `push.yaml.j2` — push pipelines
- `pull-request.yaml.j2` — pull request pipelines
- `scheduled.yaml.j2` — scheduled pipelines

Templates and their matching rules are configured in `config.yaml` (see [Config File](#config-file) below). Use `--config-dir` to point to a custom directory, or `--config-file` for an explicit config path:

```bash
validate-tekton --config-dir /path/to/custom-templates/
validate-tekton --config-file /path/to/my-config.yaml
```

### Template Syntax

Templates are valid YAML. Jinja expressions live inside quoted strings.

**Self-references** — `_` resolves to the existing file's value at the same YAML path:

```yaml
# Pass through existing value unchanged
appstudio.openshift.io/application: "{{ _ }}"

# Transform existing value
build.appstudio.openshift.io/repo: "{{ _ | to_private_gitlab }}"

# Validate existing value
rhoai-version: "{{ _ | must_match('^\\d+\\.\\d+\\.\\d+$') }}"
```

**Parent references** — `_parent` resolves to the matching parent pipeline's value (child templates only, e.g. PR templates referencing push pipelines):

```yaml
# Use the parent pipeline's application label
appstudio.openshift.io/application: "{{ _parent }}"

# Transform the parent pipeline's output-image
output-image: "{{ _parent | split_tag | to_private_quay }}:on-pr-{{ '{{pull_request_number}}' }}"
```

**Literal values** — bare values without `{{ }}` are enforced as-is:

```yaml
# Must be exactly this value (implicit must_equal)
namespace: rhoai-private-tenant
disable-slack-notifications: "true"
```

**Tekton variables** — use `{{ '...' }}` to output literal `{{ }}`:

```yaml
value: "{{ '{{revision}}' }}"        # outputs: {{revision}}
value: "{{ '{{ target_branch }}' }}"  # outputs: {{ target_branch }}
```

**ABSENT sentinel** — mark fields that should not exist:

```yaml
- name: enable-slack-failure-notification
  value: "{{ ABSENT }}"
```

In validate mode: error if the field exists. In fix mode: removes it.

**Optional references** — `_?` and `_parent?` act like `_` and `_parent` when the value exists, but produce `ABSENT` (removing the key) when the value is missing:

```yaml
# Use parent's rhoai-version if it exists; remove from child if parent doesn't have it
- name: rhoai-version
  value: "{{ _parent? | must_be_version(vars['rhoai-version']) }}"

# Keep this annotation only if the existing file has it
some-optional-annotation: "{{ _? }}"
```

When the `?` variant encounters a missing value, it short-circuits before any filters run. Filters in the chain only execute when the value is present. Compare with non-optional behavior: `{{ _parent }}` resolves to an empty string when the parent lacks the key, and filters run against that empty string.

### Merge Behavior

Fields defined in the template override the existing file. Fields in the existing file but not in the template are preserved (component-specific params like `dockerfile`, `prefetch-input`, `build-platforms`, etc.).

For params (named lists), merge is by name: template params override matching existing params, non-matching existing params are kept in their original position, new template params are appended.

## Template Overlays

Overlays let specific components override individual template fields while inheriting the rest from the base template. This is useful for repos like `rhoai-build-config` that have tekton files with non-standard CEL expressions or unique parameters.

### Overlay Resolution

Overlays are partial template files stored under `{config-dir}/`. For each pipeline, the tool looks for an overlay using the component prefix (version-stripped, e.g. `odh-operator-bundle-v2-25` → `odh-operator-bundle`), then falls back to the `appstudio.openshift.io/component` label value:

1. `{component-label}/{template-file}` — version-specific, e.g. `odh-operator-bundle-v2-25/push.yaml.j2`
2. `{component-prefix}/{template-file}` — general fallback, e.g. `odh-operator-bundle/push.yaml.j2`

### Writing Overlays

An overlay file contains only the fields that differ from the base template. It gets deep-merged onto the base template before rendering. All Jinja features work in overlays: `{{ _ }}`, `{{ _parent }}`, `{{ ABSENT }}`, filters, etc.

**Override a CEL expression** — accept the existing value as-is instead of running `validate_cel`:

```yaml
# templates/odh-operator-bundle/push.yaml.j2
metadata:
  annotations:
    pipelinesascode.tekton.dev/on-cel-expression: "{{ _ }}"
```

**Override and add params** — params are merged by `name` key, not position:

```yaml
# templates/rhoai-fbc-fragment/push.yaml.j2
spec:
  params:
  - name: output-image
    value: "{{ _ | split_tag | to_private_quay }}:{{ '{{target_branch}}-{{revision}}' }}"
  - name: build-type
    value: "ci"
```

This replaces `output-image` (matched by name), adds `build-type`, and preserves all other base template params unchanged.

**Remove a param** — use `{{ ABSENT }}`:

```yaml
spec:
  params:
  - name: disable-slack-notifications
    value: "{{ ABSENT }}"
```

### Overlay Logging

When an overlay is applied, it is logged in the output:

```
  odh-operator-bundle-v2-25-push.yaml: using overlay odh-operator-bundle/push.yaml.j2
```

## Config File

The config file (`config.yaml`) declares templates, pipeline matchers, and parent-child relationships. It lives in the `--config-dir` directory (default: `templates/`).

```yaml
templates:
  - name: push
    file: push.yaml.j2
    match_fn: is_push_pipeline

  - name: pull-request
    file: pull-request.yaml.j2
    match_fn: is_pr_pipeline
    parent:
      template: push
      match_fn:
        - exact_component_match
        - odh_prefix_match
        - component_starts_with

  - name: scheduled
    file: scheduled.yaml.j2
    match_fn: is_scheduled_pipeline
```

Each template entry has:

- **`name`** — identifier for the template type
- **`file`** — template filename, resolved relative to `--config-dir`
- **`match_fn`** — pipeline matcher function name(s) that classify a PipelineRun as this type. Accepts a single string or list; first match wins.
- **`parent`** (optional) — declares a parent-child relationship:
  - **`template`** — name of the parent template type
  - **`match_fn`** — parent matcher function name(s) for finding the matching parent pipeline instance. Accepts a single string or list; first match wins.
  - **`inherit_params`** (optional, default `false`) — when `true`, all parent `spec.params` not explicitly declared in the child template are automatically inherited. In validate mode, mismatches or missing params are reported as errors. In fix mode, child params are overwritten or added to match the parent. Child-only params (not in the parent or template) are preserved. Works with scalar and list values.

### Pipeline Matchers

Pipeline matchers classify a PipelineRun file as belonging to a template type. Defined in `lib/tekton_matchers.py` with `@pipeline_matcher`. Each receives `(name, data)` and returns `True/False`.

| Matcher | Description |
|---------|-------------|
| `is_push_pipeline` | Name ends with `-on-push` |
| `is_pr_pipeline` | Annotation contains `pull_request` or name contains `pull-request` |
| `is_scheduled_pipeline` | Name contains `-on-schedule` |

### Parent Matchers

Parent matchers determine which parent pipeline instance matches a child. Defined in `lib/tekton_matchers.py` with `@parent_matcher`. Each receives `(parent_data, child_data)` and returns `True/False`.

| Matcher | Description |
|---------|-------------|
| `alias_match` | Child prefix is a known alias for the parent prefix (see `component_aliases`) |
| `exact_component_match` | Component prefixes match exactly |
| `odh_prefix_match` | Child prefix with `odh-` prepended matches parent |
| `component_starts_with` | Parent prefix starts with child prefix (single match only) |

### Component Aliases

The `component_aliases` config key maps short component names to full names for the `alias_match` parent matcher. This allows PR pipelines with abbreviated names to find their push pipeline parents:

```yaml
component_aliases:
  odh-guardrails-detector-hf-runtime: odh-guardrails-detector-huggingface-runtime
```

If no `config.yaml` exists in `--config-dir`, the default config matching the built-in behavior is used.

## Filters

Filters are defined in `lib/tekton_filters.py`. Mark a function with `@jinja_filter` to register it as a template filter.

### Transform Filters

| Filter | Description | Example |
|--------|-------------|---------|
| `split_tag` | Strip tag from image | `quay.io/org/repo:v1` → `quay.io/org/repo` |
| `strip_prefix` | Strip prefix (default: `odh-`) | `odh-dashboard` → `dashboard` |
| `strip_version` | Strip version suffix | `comp-v3-5` → `comp` |
| `repo_name` | Extract repo name from URL | `https://.../repo.git` → `repo` |

### Combined Transform + Validation Filters

These transform the value and validate the result. They raise `ValidationError` with a `fixed_value` if the input doesn't match.

| Filter | Description |
|--------|-------------|
| `to_private_quay` | Ensure `quay.io/rhoai-private/` prefix (also handles `quay.io/repository/rhoai/`) |
| `to_private_gitlab` | Ensure `gitlab.cee.redhat.com/rhoai/private/` URL |
| `delete_if(match)` | Signal file deletion if value matches (e.g. `{{ _ \| delete_if('open-data-hub-tenant') }}`) |

### Validation-Only Filters

| Filter | Description | Example |
|--------|-------------|---------|
| `must_equal(expected)` | Value must equal expected | `{{ _ \| must_equal('true') }}` |
| `must_match(pattern)` | Value must match regex | `{{ _ \| must_match('^\\d+\\.\\d+$') }}` |
| `must_be_version` | Value must be X.Y.Z or X.Y.Z-ea.N; optional expected value | `{{ _ \| must_be_version(vars['rhoai-version']) }}` |
| `must_be_one_of(list)` | Value must be in list | `{{ _ \| must_be_one_of(allowed) }}` |
| `must_be_approved_pipeline` | Pipeline repo URL must be in approved list | `{{ _ \| must_be_approved_pipeline }}` |
| `validate_cel(branch, filename)` | CEL must reference branch and filename; auto-fixes wrong branch | `{{ _ \| validate_cel(branch, filename) }}` |

### Default Filters

| Filter | Description |
|--------|-------------|
| `default_comment` | Returns `^/build-konflux` if value is empty |
| `default_label(component)` | Returns `[kfbuild-all, kfbuild-<stripped>]` if empty |

### Flow Control Filters

| Filter | Description | Example |
|--------|-------------|---------|
| `skip_for(components)` | Skip downstream validation for matching components | `{{ _ \| skip_for(['odh-operator']) \| validate_cel(branch, filename) }}` |

`skip_for` checks the pipeline's `appstudio.openshift.io/component` label (injected via closure, no template argument needed). When the component matches any pattern in the list (substring match), it returns a `SkipValidation` sentinel that causes all downstream filters in the chain to pass through without validating.

### Template Variables (`--var`)

The `--var KEY=VALUE` flag passes CLI parameters into templates, accessible as `vars['key']`. Returns `None` for unset keys (no `KeyError`).

```bash
validate-tekton -b rhoai-2.25 --var rhoai-version=2.25.10
```

```yaml
# Template usage — enforce specific version when --var is set, format-only otherwise
rhoai-version: "{{ _ | must_be_version(vars['rhoai-version']) }}"
```

## Contributing Filters

Add a function to `lib/tekton_filters.py` and mark it with `@jinja_filter`:

```python
@jinja_filter
def my_filter(value):
    """Description of what this filter does."""
    if not some_condition(value):
        raise ValidationError(
            f"error message describing what's wrong",
            fixed_value,  # optional: the corrected value for fix mode
        )
    return value
```

The `@jinja_filter` decorator registers the function as a Jinja filter. The wrapper handles:
- **Validate mode**: catches `ValidationError`, records the error, returns the original value
- **Fix mode**: catches `ValidationError`, records the error, returns `fixed_value`
- **No error**: returns the value as-is

### Testing Filters

Add tests to `tests/test_tekton_filters.py`:

```python
def test_my_filter_pass():
    assert my_filter("valid_input") == "valid_input"

def test_my_filter_fail():
    with pytest.raises(ValidationError) as exc:
        my_filter("invalid_input")
    assert "expected message" in str(exc.value)
    assert exc.value.fixed_value == "corrected_input"  # if applicable
```

## CLI Options

```
validate-tekton [OPTIONS]

  --path PATH                  Path to the target repo (default: current directory)
  -b, --branch BRANCH          Branch for validation (checks out by default, see --no-checkout)
  --no-checkout                Use --branch for validation only, don't check out
  -n, --namespace NS           Only validate PipelineRuns in this namespace
  --config-dir DIR             Directory containing templates and config.yaml
  --config-file PATH           Explicit path to config YAML (overrides config.yaml in --config-dir)
  --var KEY=VALUE              Set a template variable (repeatable)
  --fix                        Write corrected files to working tree
  --commit                     Commit fixes to a feature branch (implies --fix)
  --mr                         Create MR for the fix branch (implies --commit)
  --merge now|auto             Merge strategy: 'now' merges immediately, 'auto' merges when pipeline succeeds (implies --mr)
  --gitlab-host HOST           GitLab hostname for MR creation
  --json                       JSON output
  --dry-run                    Show what would be done
  --verbose                    Detailed progress
```

Fix phases build on each other: `--fix` writes files, `--commit` implies `--fix` and commits, `--mr` implies `--commit` and pushes + creates MR, `--merge now` merges immediately, `--merge auto` merges when pipeline succeeds.

### MR Behavior

MRs created by `--mr` are labeled `validate-tekton`. On re-runs, if an open MR already exists for the fix branch, its title, description, and labels are updated instead of creating a duplicate. `--merge now` merges immediately (retries up to 5 times). `--merge auto` enables merge-when-pipeline-succeeds (retries up to 60s waiting for a pipeline to start).

The tool exits 1 whenever validation errors are found, even if some were auto-fixed. This ensures callers (like `batch-validate-tekton`) can detect repos that still need attention.

## Batch Mode

`batch-validate-tekton` runs `validate-tekton` across multiple repos and branches. It sparse-clones each repo (just `.tekton/`) into a temp directory for minimal I/O.

```bash
# Single repo, one branch
./scripts/batch-validate-tekton odh-dashboard -b rhoai-3.5 -n rhoai-tenant

# Single repo, multiple branches
./scripts/batch-validate-tekton odh-dashboard -b rhoai-3.5 rhoai-3.6 -n rhoai-tenant

# All repos and branches from config
./scripts/batch-validate-tekton --config release-branch-config.yaml -n rhoai-tenant --mr

# Filter to specific branches
./scripts/batch-validate-tekton --config release-branch-config.yaml -b rhoai-3.5 -n rhoai-tenant --mr

# Existing local clone
./scripts/batch-validate-tekton --local-path /path/to/clone -b rhoai-3.5
```

### Batch CLI Options

```
batch-validate-tekton [OPTIONS] [REPO]

  REPO                         Single repo name (e.g., odh-dashboard)
  --config FILE                Path to release-branch-config.yaml
  -b, --branch BRANCH ...      Branch(es) to validate (default: all from --config)
  -t, --target NS              GitLab namespace (default: rhoai/private)
  --local-path PATH            Use existing local clone
  --config-dir DIR             Directory containing templates and config.yaml (passed through)
  --config-file PATH           Explicit path to config YAML (passed through)
  --var KEY=VALUE              Set a template variable (repeatable, passed through)
  --gitlab-host HOST           GitLab hostname (default: gitlab.cee.redhat.com)
  -n, --namespace NS           Only validate PipelineRuns in this namespace
  --fix / --commit / --mr / --merge now|auto
                               Action flags (passed through to validate-tekton)
  --json / --dry-run / --verbose
```

A failure in one repo does not stop the batch — results are collected and a summary is printed at the end. Repos where the branch doesn't exist are reported as SKIPPED (not counted as failures). Unfixed errors are replayed with filenames at the end of the output.

## File Layout

```
embargo-tools/
  scripts/
    validate-tekton            # Single-repo CLI tool
    batch-validate-tekton      # Multi-repo wrapper
  templates/
    config.yaml                # Template config (matchers, parent-child relationships)
    push.yaml.j2               # Push pipeline template
    pull-request.yaml.j2       # PR pipeline template
    scheduled.yaml.j2          # Scheduled pipeline template
                     # Per-component template overlays
      {component-prefix}/      #   e.g. odh-operator-bundle/
        push.yaml.j2           #   Overrides specific fields in push.yaml.j2
  lib/
    tekton.py                  # Template engine (render, merge, resolve)
    tekton_filters.py          # Jinja filters (transform, validate)
    tekton_matchers.py         # Pipeline and parent matchers
    tekton_config.py           # Config file loading
  tests/
    test_validate_tekton.py    # Template engine tests
    test_tekton_filters.py     # Filter tests
    test_tekton_matchers.py    # Matcher tests
    test_tekton_config.py      # Config loading tests
    fixtures/tekton/           # Test YAML fixtures
  docs/
    validate-tekton.md         # This file
```
