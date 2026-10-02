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

### Merge Behavior

Fields defined in the template override the existing file. Fields in the existing file but not in the template are preserved (component-specific params like `dockerfile`, `prefetch-input`, `build-platforms`, etc.).

For params (named lists), merge is by name: template params override matching existing params, non-matching existing params are kept in their original position, new template params are appended.

When `inherit_params: true` is set in the config, all parent params not explicitly declared in the template are automatically inherited. Semantically identical values with different formatting are preserved as-is (no cosmetic normalization).

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

Overlays let specific components override individual template fields while inheriting the rest from the base template. Stored under `{config-dir}/{component-prefix}/{template-file}`. Deep-merged onto the base template before rendering.

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
| `to_stable_pr_cel(filename, labels_str)` | Converts push CEL to PR CEL with optional label arm and pathChanged conditions. Uses cel-python AST parsing to decompose the push expression, replace `.tekton/` filenames, and carry over negative guards. |

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

The `lib/cel.py` module uses [cel-python](https://github.com/cloud-custodian/cel-python) (Lark-based parser) to parse CEL expressions into ASTs for structural transformation.

Key functions:

- `decompose_push_cel(expr)` — splits a push CEL into event, branch, negative guards, and path conditions
- `replace_tekton_filename(tree, filename)` — swaps `.tekton/*.yaml` string literals in pathChanged calls
- `build_stable_pr_cel(path_conditions, negative_guards, labels, target_branch)` — assembles a PR CEL expression

This powers the `to_stable_pr_cel` filter, which converts push pipeline CEL triggers into PR pipeline CEL triggers.

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
