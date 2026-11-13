# validate-tekton

Validates and fixes Tekton PipelineRun files against Jinja2 templates. The templates define the desired state — enforced values, transforms, and validation checks — all in one place.

Supports two directory layouts:
- **Flat** (`.tekton/`): PipelineRuns in a single directory (e.g. private GitLab repos)
- **Nested** (`pipelineruns/*/.tekton/`): PipelineRuns organized by component (e.g. konflux-central)

## Quick Start

```bash
# Validate embargo builds (flat layout, default)
validate-tekton --path /path/to/repo --config-dir tekton-templates/embargo -b rhoai-3.5 -n rhoai-private-tenant

# Fix embargo builds
validate-tekton --path /path/to/repo --config-dir tekton-templates/embargo -b rhoai-3.5 -n rhoai-private-tenant --fix

# Migrate konflux-central PR PipelineRuns (nested layout, cross-branch parenting)
# parent_ref and tekton_layout are configured in tekton-templates/kc-migration/config.yaml
validate-tekton --path /path/to/konflux-central --config-dir tekton-templates/kc-migration -b main --no-checkout

# Fix konflux-central PR PipelineRuns
validate-tekton --path /path/to/konflux-central --config-dir tekton-templates/kc-migration -b main --no-checkout --fix
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

## Generated children

Templates can opt into creating missing child PipelineRuns and reconciling them
from read-only working-tree parents using `generate`. See
[generated-child and release PR maintenance](release-pr-maintenance.md).
Existing template configurations remain unchanged.

## Template Sets

Template sets live under `tekton-templates/`, each in its own subdirectory with a `config.yaml`:

```
tekton-templates/
  embargo/                      # Private GitLab embargo builds
    config.yaml
    push.yaml.j2
    pull-request.yaml.j2
    scheduled.yaml.j2
    odh-operator-bundle/        # Component-specific overlays
      push.yaml.j2
    ...
  kc-migration/                 # Konflux-central PR PipelineRun migration
    config.yaml
    push.yaml.j2               # Minimal classifier for parent push PLRs
    pull-request.yaml.j2       # Consolidated CEL trigger template
```

Use `--config-dir` to select a template set:

```bash
validate-tekton --config-dir tekton-templates/embargo
validate-tekton --config-dir tekton-templates/kc-migration
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
output-image: "{{ _parent | split_tag }}:on-pr-{{ '{{pull_request_number}}' }}"
```

**Literal values** — bare values without `{{ }}` are enforced as-is:

```yaml
# Must be exactly this value (implicit must_equal)
cancel-in-progress: "true"
image-expires-after: 30d
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
# Keep this annotation only if the existing file has it
some-optional-annotation: "{{ _? }}"
```

### Native array expressions

A whole-value expression ending in `| as_array` opts into native array rendering.
Other expressions retain their existing string-rendering behavior, including
numeric-looking strings and literal PaC variables. `_` and `_parent` retain
native array values in this mode; `_?` and `_parent?` still remove missing fields.

```yaml
spec:
  params:
    - name: build-platforms
      value: "{{ _? | exclude_values(['linux/ppc64le', 'linux/s390x'], delete_if_empty=true) | as_array }}"
    - name: additional-labels
      value: "{{ _? | replace_prefix('version=', 'version=on-pr-' ~ vars['revision']) | as_array }}"
    - name: additional-tags
      value: "{{ params.get('additional-tags', []) | append_unique('on-pr-{{pull_request_number}}') | as_array }}"
```

These reusable filters accept arrays of strings without modifying the input:

- `exclude_values(values, delete_if_empty=false)` removes exact matches, preserving
  other entries and order. If requested, an empty result signals whole-file
  deletion through the existing `--fix` deletion mechanism.
- `replace_prefix(prefix, replacement)` replaces only matching entries with the
  supplied full replacement string; it neither adds missing entries nor changes
  other entries.
- `append_unique(item)` appends only if absent, retaining existing entries and order.
- `as_array` requires a list and must be the terminal filter of a whole expression
  to opt into typed rendering. It does not parse strings as YAML or coerce scalar types.

Array differences are reported during validation and applied by `--fix`, just
like literal template values. No platform, tag, or label policy is built into
these filters: the template supplies all values and matching rules.

### Merge Behavior

Fields defined in the template override the existing file. Fields in the existing file but not in the template are preserved (component-specific params like `dockerfile`, `prefetch-input`, `build-platforms`, etc.).

For params and steps (lists keyed by `name`), merge is by name. For `taskRunSpecs`, merge is by `pipelineTaskName`. Template entries update matching existing entries, unrelated entries remain in their original positions, and new entries are appended. Nested self-references (`_`) resolve within the matching task and step, not by list position.

`ABSENT` fields are omitted even when their containing task or step override is newly added. An omitted field does not necessarily remove a default from a resolved Task: resource-override precedence and the admitted pod must still be checked before rollout.

When `inherit_params: true` is set in the config, all parent params not explicitly declared in the template are automatically inherited. Semantically identical values with different formatting are preserved as-is (no cosmetic normalization).

## Optional per-file selection

Configs may declare `selection_template: selection.json.j2` and a list of fixed
repository-relative `context_paths`. The ordinary validator/fixer uses this
Jinja sidecar before rendering a file. It must emit exactly
`{"status":"applicable|skipped|manual|unresolved","reason":"..."}`; only
`applicable` files are rendered/fixed. Other statuses are reported with their
reason. Existing configs without selection retain their current behavior.

The sidecar receives ordinary template variables and detached
`context = {branch, documents}`. Each declared context path maps to parsed YAML
or `None`. It can use the generic `unique_item` filter for duplicate-safe keyed
lookup and `fail(message)` for explicit errors. Context cannot be overwritten
by `--var`. See [strict reconciliation](../tekton-reconciliation.md) for JSON
limits, read-only context/provenance, optional post-merge guards and strict
localized output. The standalone CLI keeps its existing whole-document writer;
strict reconciliation invokes the same fix engine with a different writer profile.

## Config File

The config file (`config.yaml`) declares templates, pipeline matchers, parent-child relationships, and optional settings for cross-branch parenting and directory layout.

```yaml
# tekton-templates/kc-migration/config.yaml
parent_ref: origin/rhoai-3.6      # Load parents from this git ref
tekton_layout: nested              # pipelineruns/*/.tekton/ directory structure

templates:
  - name: push
    file: push.yaml.j2
    match_fn: is_push_pipeline_by_cel

  - name: pull-request
    file: pull-request.yaml.j2
    match_fn: is_pr_pipeline
    parent:
      template: push
      inherit_params: true
      match_fn:
        - alias_match
        - exact_component_match
        - odh_prefix_match
        - component_starts_with

component_aliases:
  odh-guardrails-detector-hf-runtime: odh-guardrails-detector-huggingface-runtime
```

### Top-Level Settings

| Setting | Description | Default |
|---------|-------------|---------|
| `parent_ref` | Git ref to load parent pipelines from (e.g. `origin/rhoai-3.6`). Uses `git show` to read without checkout. | `null` (parents from working tree) |
| `tekton_layout` | `flat` (`.tekton/`) or `nested` (`pipelineruns/*/.tekton/`) | `flat` |

Both can be overridden via CLI flags `--parent-ref` and `--tekton-layout`.

### Template Entry Fields

- **`name`** — identifier for the template type
- **`file`** — template filename, resolved relative to `--config-dir`
- **`match_fn`** — pipeline matcher function name(s). Accepts a single string or list; first match wins.
- **`parent`** (optional) — declares a parent-child relationship:
  - **`template`** — name of the parent template type
  - **`match_fn`** — parent matcher function name(s) for finding the matching parent pipeline
  - **`inherit_params`** (optional, default `false`) — inherit all parent params not in the template

### Pipeline Matchers

Defined in `lib/tekton_matchers.py` with `@pipeline_matcher`. Each receives `(name, data)` and returns `True/False`.

| Matcher | Description |
|---------|-------------|
| `is_pipeline_run` | Every `kind: PipelineRun`, independent of event/name/namespace (use in dedicated template sets) |
| `is_push_pipeline` | Name ends with `-on-push` |
| `is_push_pipeline_by_cel` | CEL expression contains `event == "push"` (for repos where push PLRs don't use `-on-push` naming) |
| `is_pr_pipeline` | Annotation contains `pull_request` or name contains `pull-request` |
| `is_scheduled_pipeline` | Name contains `-on-schedule` |

### Parent Matchers

Defined in `lib/tekton_matchers.py` with `@parent_matcher`. Each receives `(parent_data, child_data)` and returns `True/False`.

| Matcher | Description |
|---------|-------------|
| `alias_match` | Child prefix is a known alias for the parent prefix (see `component_aliases`) |
| `exact_component_match` | Component prefixes match exactly |
| `odh_prefix_match` | Child prefix with `odh-` prepended matches parent |
| `component_starts_with` | Parent prefix starts with child prefix (single match only) |

## Template Overlays

Overlays override individual template fields while inheriting the base template. Selection precedence is component label, component prefix, then explicitly configured directory mapping. Generation, validation and fix share this selection for flat and nested layouts. The selected overlay is deep-merged onto the base template before rendering, and all selectors are checked for traversal and symlink escapes.

## Filters

Filters are defined in `lib/tekton_filters.py`. Mark a function with `@jinja_filter` to register it.

### Transform Filters

| Filter | Description |
|--------|-------------|
| `split_tag` | Strip tag from image (`quay.io/org/repo:v1` → `quay.io/org/repo`) |
| `strip_prefix` | Strip prefix (default: `odh-`) |
| `strip_version` | Strip version suffix (`comp-v3-5` → `comp`) |
| `repo_name` | Extract repo name from URL |
| `default_comment` | Returns `^/build-konflux` if value is empty |
| `default_label(component)` | Returns `[kfbuild-all, kfbuild-<stripped>]` if empty |

### CEL Filters

| Filter | Description |
|--------|-------------|
| `validate_cel(branch, filename)` | CEL must reference branch and filename; auto-fixes wrong branch |
| `validate_cel_tekton_ignore` | Replaces bad `!".tekton/**".pathChanged()` with `files.all.exists(...)` |
| `to_pr_cel` | Replaces `event == "push"` with `event == "pull_request"` |
| `ensure_cel_selectors(desired)` | Reconciles explicitly named string-equality selectors to template-supplied values; `ABSENT` removes a selector. Preserves compliant text, unrelated guards, and paths. Template comparison reports drift in validation and writes the desired expression in fix mode. |
| `ensure_cel_tekton_filename(filename)` | Declares the desired filename for literal `.tekton/*.yaml` or `.yml` receivers of zero-argument `pathChanged()` calls. Template comparison validates or autofixes drift. Compliant references and unrelated text stay byte-identical. |
| `release_push_to_main_pr_cel(filename, labels_str)` | Legacy compatibility filter (also available as `to_stable_pr_cel`). Converts push CEL to PR CEL with optional label arm and pathChanged conditions. Uses cel-python AST parsing to decompose the push expression, replace `.tekton/` filenames, and carry over negative guards. For the exact notebooks `!("manifests/rhoai/base/params-latest.env".pathChanged())` exclusion, adds a `source_branch == "main" && target_branch == "stable"` OR arm outside the entire guarded path conjunction, allowing bump-only sync PRs to build. Other triggers are unchanged. |

### Validation Filters

| Filter | Description |
|--------|-------------|
| `must_equal(expected)` | Value must equal expected |
| `must_match(pattern)` | Value must match regex |
| `must_be_version` | Must be X.Y.Z or X.Y.Z-ea.N |
| `must_be_one_of(list)` | Value must be in list |
| `must_be_approved_pipeline` | Pipeline repo URL must be in approved list |
| `to_private_quay` | Ensure `quay.io/rhoai-private/` prefix |
| `to_private_gitlab` | Ensure `gitlab.cee.redhat.com/rhoai/private/` URL |
| `delete_if(match)` | Signal file deletion if value matches |
| `delete_if_contains(substring)` | Signal file deletion if value contains substring |

### Flow Control

| Filter | Description |
|--------|-------------|
| `skip_for(components)` | Skip downstream validation for matching components |

### Template Variables (`--var`)

The `--var KEY=VALUE` flag passes CLI parameters into templates, accessible as `vars['key']`.

```bash
validate-tekton --config-dir tekton-templates/embargo -b rhoai-2.25 --var rhoai-version=2.25.10
```

## CLI Options

```
validate-tekton [OPTIONS]

  --path PATH                  Path to the target repo (default: current directory)
  -b, --branch BRANCH          Branch for validation (checks out by default, see --no-checkout)
  --no-checkout                Use --branch for validation only, don't check out
  -n, --namespace NS           Only validate PipelineRuns in this namespace
  --config-dir DIR             Directory containing templates and config.yaml (required)
  --config-file PATH           Explicit path to config YAML (overrides config.yaml in --config-dir)
  --parent-ref REF             Git ref to load parent pipelines from (overrides config parent_ref)
  --tekton-layout flat|nested  Pipeline file layout (overrides config tekton_layout)
  --var KEY=VALUE              Set a template variable (repeatable)
  --fix                        Write corrected files to working tree
  --json                       JSON output
  --dry-run                    Show what would be done
  --verbose                    Detailed progress
```

## CEL Expression Parsing

`ensure_cel_selectors(desired)` declares selector state, for example
`_parent | ensure_cel_selectors({'event': 'pull_request', 'target_branch': ABSENT})`.
String values add or replace selectors; `ABSENT` requests removal, not CEL null.
Unrelated guards and paths are retained. A compliant expression is returned
unchanged. Equality operands may be reversed or parenthesized; identical
duplicates are normalized. Conflicting values, non-equality predicates, and
selected identifiers embedded in ORs are rejected rather than weakened.
Added selectors guard the whole expression, including any unrelated OR.

The filter constructs expected CEL without diagnosing a valid parent as wrong.
Normal template comparison validates the child annotation or autofixes it.
The helper contains no cluster names or repository-specific policy.


Templates explicitly supply desired event, branch, or routing values, then use
`ensure_cel_tekton_filename(filename)` separately. Both filters construct expected
child values; normal template comparison supplies validation and autofix.
Filename reconciliation never changes selector policy. Repository-specific bypass arms must be composed
before applying their mandatory outer routing selector.

The notebooks overlay in `tekton-templates/kc-migration/notebooks/pull-request.yaml.j2` owns the main-to-stable sync policy: it bypasses normal image paths only when `manifests/rhoai/base/params-latest.env` changes. It explicitly replaces the inherited, canonically rendered `!pathChanged(params)` veto with `files.all.exists(...)`, so mixed source-and-params changes build normally while ordinary params-only PRs do not. Release push files are not changed.

`release_push_to_main_pr_cel` and its alias `to_stable_pr_cel` remain legacy compatibility entry points with their existing label and automatic notebooks bypass behavior. New templates do not call them.

`lib/cel.py` owns structural parsing with [cel-python](https://github.com/cloud-custodian/cel-python): `extract_push_conditions` returns negative guards and the path subtree without choosing PR trigger arms. It also provides `flatten_and`, `replace_tekton_filename`, and `path_conditions_to_text`. Trigger policy tests live in `tests/test_tekton_filters.py`; parsing and AST utility tests live in `tests/test_cel.py`.

## File Layout

```
scripts/
  validate-tekton              # Single-repo CLI tool
tekton-templates/
  embargo/                     # Embargo build templates
    config.yaml
    push.yaml.j2
    pull-request.yaml.j2
    scheduled.yaml.j2
    {component}/push.yaml.j2   # Per-component overlays
  kc-migration/                # Konflux-central migration templates
    config.yaml
    push.yaml.j2
    pull-request.yaml.j2
lib/
  cel.py                       # CEL expression parsing (cel-python/Lark)
  tekton.py                    # Template engine (render, merge, resolve)
  tekton_filters.py            # Jinja filters (transform, validate)
  tekton_matchers.py           # Pipeline and parent matchers
  tekton_config.py             # Config file loading
tests/
  test_validate_tekton.py      # Template engine tests
  test_tekton_filters.py       # Filter tests
  test_tekton_matchers.py      # Matcher tests
  test_tekton_config.py        # Config loading tests
  test_cel.py                  # CEL parsing tests
  fixtures/tekton/             # Test YAML fixtures
docs/
  validate-tekton/
    validate-tekton.md         # This file
```

### Explicit directory overlays

Configure source scopes explicitly; repository names do not select overlays:

```yaml
template_overlays:
  - pipeline_dir: pipelineruns/notebooks
    template_dir: notebooks
```

`pipeline_dir` is a normalized repository-root-relative directory scope (matched
on directory boundaries); `template_dir` is relative to the configuration root
and may have an unrelated name. Source scopes must not overlap. Template roots
must exist and must not traverse symlinks. Absolute paths and traversal are
rejected. Selection precedence is component label, component prefix, directory
mapping, then base template. Without mappings only component overlays apply.
The same mapping is used for generation, validation and fix.
