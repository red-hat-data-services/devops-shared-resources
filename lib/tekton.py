"""Template-based validation and fix logic for Tekton PipelineRun files."""

import re
from collections import defaultdict
from pathlib import Path


def get_param(data: dict, name: str) -> str | None:
    for p in data.get("spec", {}).get("params", []):
        if p.get("name") == name:
            return p.get("value")
    return None


def get_annotation(data: dict, key: str) -> str | None:
    return data.get("metadata", {}).get("annotations", {}).get(key)


def get_label(data: dict, key: str) -> str | None:
    return data.get("metadata", {}).get("labels", {}).get(key)


def get_pipeline_ref_param(data: dict, name: str) -> str | None:
    ref = data.get("spec", {}).get("pipelineRef", {})
    for p in ref.get("params", []):
        if p.get("name") == name:
            return p.get("value")
    return None


def classify_pipeline(filename: str, data: dict, config=None) -> str:
    if config is not None:
        from lib.tekton_matchers import make_pipeline_matchers
        all_matchers = make_pipeline_matchers()
        name = data.get("metadata", {}).get("name", "")
        for tmpl in config.templates:
            for fn_name in tmpl.match_fns:
                fn = all_matchers.get(fn_name)
                if fn and fn(name, data):
                    return tmpl.name
        return "unknown"

    on_event = get_annotation(data, "pipelinesascode.tekton.dev/on-event")
    if on_event and "pull_request" in on_event:
        return "pull_request"

    name = data.get("metadata", {}).get("name", "")

    if "-on-schedule" in name:
        return "scheduled"
    if "pull-request" in name:
        return "pull_request"
    if name.endswith("-on-push"):
        return "push"

    return "unknown"


def _preserve_type(old_val, new_val):
    """Apply the new string content but keep the original's scalar type."""
    from ruamel.yaml.scalarstring import LiteralScalarString
    if isinstance(new_val, str) and '\n' in new_val:
        return LiteralScalarString(new_val)
    if type(old_val) is type(new_val):
        return new_val
    try:
        return type(old_val)(new_val)
    except (TypeError, ValueError):
        return new_val


def extract_component_prefix(filename: str, data: dict | None = None) -> str | None:
    """Extract component prefix from a tekton pipeline.

    Prefers metadata.name (consistent '-on-push'/'-on-pull-request'/'-on-schedule' convention)
    over filename when data is provided.

    'odh-dashboard-v3-5-on-push' → 'odh-dashboard'
    'odh-dashboard-on-pull-request' → 'odh-dashboard'
    """
    if data:
        meta_name = data.get("metadata", {}).get("name", "")
        if meta_name:
            prefix = re.sub(r"-on-(push|pull-request|schedule).*$", "", meta_name)
            prefix = re.sub(r"-v\d+-\d+.*$", "", prefix)
            if prefix != meta_name:
                return prefix

    name = filename.removesuffix(".yaml")
    if "-pull-request" in name:
        prefix = name.split("-pull-request")[0].removesuffix("-on")
        m = re.match(r"(.+)-v\d+-\d+.*$", prefix)
        return m.group(1) if m else prefix
    m = re.match(r"(.+)-v\d+-\d+.*-(push|on-push|scheduled|on-schedule)", name)
    if m:
        return m.group(1)
    if name.endswith("-push") or name.endswith("-on-push"):
        return re.sub(r"-(push|on-push)$", "", name)
    return None


# --- Template rendering and merging ---


def _create_jinja_env(template_dir: str, mode: str = "validate", errors: list | None = None,
                      component: str | None = None):
    import jinja2
    from lib import tekton_filters

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        keep_trailing_newline=True,
    )

    if errors is None:
        errors = []
    env._skipped = False
    env.filters.update(tekton_filters.make_filters(mode, errors, component=component, env=env))

    return env


def _params_to_dict(data: dict) -> dict:
    """Convert spec.params list to a flat {name: value} dict."""
    return {str(p["name"]): p.get("value", "") for p in data.get("spec", {}).get("params", [])}


def _annotations_dict(data: dict) -> dict:
    """Get annotations as a plain dict with string keys/values."""
    return {str(k): str(v) for k, v in data.get("metadata", {}).get("annotations", {}).items()}


def _labels_dict(data: dict) -> dict:
    """Get labels as a plain dict with string keys/values."""
    return {str(k): str(v) for k, v in data.get("metadata", {}).get("labels", {}).items()}


def extract_template_vars(
    data: dict,
    filename: str,
    branch: str | None = None,
    parent_data: dict | None = None,
    cli_vars: dict | None = None,
) -> dict:
    """Extract template variables from a PipelineRun file.

    Provides params, annotations, labels as flat dicts for direct access
    in templates.
    """
    params = _params_to_dict(data)
    annotations = _annotations_dict(data)
    labels = _labels_dict(data)

    repo_annotation = annotations.get("build.appstudio.openshift.io/repo", "")
    repo_url = repo_annotation.split("?")[0] if "?" in repo_annotation else repo_annotation

    pipelineref_params = {
        str(p["name"]): str(p.get("value", ""))
        for p in data.get("spec", {}).get("pipelineRef", {}).get("params", [])
    }

    branch_str = branch or ""
    m = re.match(r"^rhoai-(.+)$", branch_str)
    version_suffix = "v" + m.group(1).replace(".", "-") if m else ""

    variables = {
        "ABSENT": ABSENT,
        "filename": filename,
        "branch": branch_str,
        "version_suffix": version_suffix,
        "params": params,
        "annotations": annotations,
        "labels": labels,
        "pipelineref": pipelineref_params,
        "repo_url": repo_url,
        "metadata": data.get("metadata", {}),
        "spec": data.get("spec", {}),
        "_existing_data": dict(data) if hasattr(data, 'keys') else data,
        "parent_params": {},
        "parent_annotations": {},
        "parent_labels": {},
        "parent_pipelineref": {},
        "vars": defaultdict(lambda: None, cli_vars or {}),
    }

    if parent_data:
        variables["parent_params"] = _params_to_dict(parent_data)
        variables["parent_annotations"] = _annotations_dict(parent_data)
        variables["parent_labels"] = _labels_dict(parent_data)
        variables["parent_pipelineref"] = {
            str(p["name"]): str(p.get("value", ""))
            for p in parent_data.get("spec", {}).get("pipelineRef", {}).get("params", [])
        }
        parent_repo_ann = variables["parent_annotations"].get("build.appstudio.openshift.io/repo", "")
        variables["repo_url"] = parent_repo_ann.split("?")[0] if "?" in parent_repo_ann else parent_repo_ann
        variables["_parent_data"] = dict(parent_data) if hasattr(parent_data, 'keys') else parent_data

    return variables


def _lookup_path(data, path: tuple):
    """Look up a value in nested data by path, handling params lists."""
    node = data
    i = 0
    while i < len(path):
        key = path[i]
        if isinstance(node, dict):
            if key not in node:
                return None
            node = node[key]
        elif isinstance(node, list):
            found = None
            for item in node:
                if isinstance(item, dict) and str(item.get("name")) == str(key):
                    found = item
                    break
            if found is None:
                return None
            if i + 1 < len(path) and path[i + 1] == "value":
                node = found.get("value")
                i += 1
            else:
                node = found
        else:
            return None
        i += 1
    return node


_SELF_REF_RE = re.compile(r'(?<![a-zA-Z0-9_])(_parent|_)(\??)(?![a-zA-Z0-9_])')


def _to_block_scalar(val):
    """Wrap multi-line strings as LiteralScalarString for block scalar YAML output."""
    from ruamel.yaml.scalarstring import LiteralScalarString
    if isinstance(val, str) and '\n' in val:
        return LiteralScalarString(val)
    return val


def _resolve_and_render_tree(node, existing_data: dict, parent_data: dict | None, env, variables: dict, path: tuple = (), skipped_paths: set | None = None):
    """Walk a ruamel YAML tree, resolve _ references and render Jinja expressions."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from lib.tekton_filters import SkipValidation

    if isinstance(node, CommentedMap):
        for key in list(node.keys()):
            val = node[key]
            if isinstance(val, str) and "{{" in val:
                raw = _resolve_value(val, path + (key,), existing_data, parent_data, env, variables)
                if isinstance(raw, SkipValidation) and skipped_paths is not None:
                    skipped_paths.add(".".join(str(p) for p in path + (key,)))
                node[key] = _to_block_scalar(raw)
            elif isinstance(val, (CommentedMap, CommentedSeq)):
                _resolve_and_render_tree(val, existing_data, parent_data, env, variables, path + (key,), skipped_paths)
    elif isinstance(node, CommentedSeq):
        for i, item in enumerate(node):
            if isinstance(item, str) and "{{" in item:
                result = _resolve_value(item, path, existing_data, parent_data, env, variables)
                if isinstance(result, SkipValidation) and skipped_paths is not None:
                    skipped_paths.add(".".join(str(p) for p in path))
                node[i] = result
            elif isinstance(item, CommentedMap):
                if "name" in item and "value" in item:
                    item_path = path + (str(item["name"]), "value")
                    val = item["value"]
                    if isinstance(val, str) and "{{" in val:
                        result = _resolve_value(val, item_path, existing_data, parent_data, env, variables)
                        if isinstance(result, SkipValidation) and skipped_paths is not None:
                            skipped_paths.add(".".join(str(p) for p in item_path))
                        item["value"] = result
                    elif isinstance(val, (CommentedMap, CommentedSeq)):
                        _resolve_and_render_tree(val, existing_data, parent_data, env, variables, item_path, skipped_paths)
                else:
                    _resolve_and_render_tree(item, existing_data, parent_data, env, variables, path, skipped_paths)
            elif isinstance(item, CommentedSeq):
                _resolve_and_render_tree(item, existing_data, parent_data, env, variables, path, skipped_paths)


def _resolve_value(val: str, path: tuple, existing_data: dict, parent_data: dict | None, env, variables: dict) -> str:
    """Resolve _ / _parent references in a Jinja expression, then render it."""
    no_parent_data = False
    optional_absent = False

    def replacer(m):
        nonlocal no_parent_data, optional_absent
        ref = m.group(1)
        optional = m.group(2) == "?"
        if ref == "_parent":
            if parent_data is None:
                if optional:
                    optional_absent = True
                    return '""'
                no_parent_data = True
                return '""'
            source = parent_data
        else:
            source = existing_data
        resolved = _lookup_path(source, path)
        if resolved is None:
            if optional:
                optional_absent = True
            return '""'
        return '"' + str(resolved).replace('\\', '\\\\').replace('"', '\\"') + '"'

    resolved_expr = _SELF_REF_RE.sub(replacer, val)

    if optional_absent:
        return ABSENT
    if no_parent_data:
        return ""

    # Render through Jinja — filters like default_label handle empty values
    from lib.tekton_filters import SkipValidation
    jinja_template = env.from_string(resolved_expr)
    result = jinja_template.render(**variables)
    if hasattr(env, '_skipped') and env._skipped:
        env._skipped = False
        return SkipValidation(result)
    return result


def resolve_overlay(config_dir, template_name, component_prefix, component_label):
    """Find a component-specific template overlay.

    Checks: {config_dir}/{component_label}/{template_name} (version-specific), then
            {config_dir}/{component_prefix}/{template_name} (general fallback).
    """
    base = Path(config_dir)
    if component_label:
        path = base / component_label / template_name
        if path.exists():
            return str(path)
    if component_prefix and component_prefix != component_label:
        path = base / component_prefix / template_name
        if path.exists():
            return str(path)
    return None


def render_template(template_dir: str, template_name: str, variables: dict, mode: str = "fix",
                    errors: list | None = None, overlay_path: str | None = None) -> dict:
    """Render a Jinja template Ansible-style.

    1. Parse the template as valid YAML (Jinja expressions are in quoted strings)
    2. If overlay_path is set, deep-merge overlay onto the base template
    3. Walk the tree — for each value containing {{ }}:
       a. Resolve _ / _parent to actual values from existing data
       b. Render through Jinja (filters, escaped Tekton vars)
       c. Validation filters collect errors into the errors list
    4. Merge with existing data
    """
    from io import StringIO
    from ruamel.yaml import YAML

    if errors is None:
        errors = []

    ruamel_parser = YAML()
    ruamel_parser.preserve_quotes = True
    ruamel_parser.width = 4096

    raw_text = (Path(template_dir) / template_name).read_text()
    tree = ruamel_parser.load(StringIO(raw_text))

    if overlay_path:
        overlay_text = Path(overlay_path).read_text()
        overlay_tree = ruamel_parser.load(StringIO(overlay_text))
        if overlay_tree:
            tree = _deep_merge(overlay_tree, tree)

    existing_data = variables.get("_existing_data", {})
    parent_data = variables.get("_parent_data")
    component = variables.get("labels", {}).get("appstudio.openshift.io/component", "")
    env = _create_jinja_env(template_dir, mode=mode, errors=errors, component=component)

    skipped_paths = set()
    _resolve_and_render_tree(tree, existing_data, parent_data, env, variables, skipped_paths=skipped_paths)

    tree._skipped_paths = skipped_paths
    return tree


ABSENT = "__ABSENT__"


def _deep_merge(template, existing, errors: list | None = None, path: str = "", skipped_paths: set | None = None):
    """Recursively merge template into existing data.

    - Dicts: existing key order preserved, template values override, new keys appended
    - Named lists (items with 'name' key): merge by name, existing order preserved
    - Plain lists: template values used, with element-wise type preservation
    - Scalars: template content with existing scalar type preserved

    Records errors for scalar mismatches (implicit must_equal for literals).
    Skips comparison for paths in skipped_paths (from skip_for filter).
    """
    import copy

    if isinstance(template, dict) and isinstance(existing, dict):
        result = type(existing)() if hasattr(existing, 'ca') else {}
        for key in existing:
            if key in template:
                child_path = f"{path}.{key}" if path else key
                if template[key] == ABSENT:
                    if errors is not None:
                        errors.append(f"{child_path}: should not exist")
                else:
                    result[key] = _deep_merge(template[key], existing[key], errors, child_path, skipped_paths)
            else:
                result[key] = copy.deepcopy(existing[key])
        for key in template:
            if key not in result and template[key] != ABSENT:
                if errors is not None:
                    child_path = f"{path}.{key}" if path else key
                    errors.append(f"{child_path}: missing")
                result[key] = copy.deepcopy(template[key])
        return result

    if isinstance(template, list) and isinstance(existing, list):
        _new_list = type(existing) if hasattr(existing, 'ca') else list

        if template and isinstance(template[0], dict) and "name" in template[0]:
            template_by_name = {str(p["name"]): p for p in template}
            result = _new_list()
            for ei in existing:
                name = str(ei.get("name", ""))
                if name in template_by_name:
                    tp = template_by_name.pop(name)
                    if tp.get("value") == ABSENT:
                        if errors is not None:
                            errors.append(f"{path}[{name}]: should not exist")
                    else:
                        child_path = f"{path}[{name}]"
                        result.append(_deep_merge(tp, ei, errors, child_path, skipped_paths))
                else:
                    result.append(copy.deepcopy(ei))
            for name, p in template_by_name.items():
                if p.get("value") != ABSENT:
                    if errors is not None:
                        errors.append(f"{path}[{name}]: missing")
                    result.append(copy.deepcopy(p))
            return result

        result = _new_list()
        for i, tv in enumerate(template):
            if i < len(existing):
                result.append(_deep_merge(tv, existing[i], errors, f"{path}[{i}]", skipped_paths))
            else:
                result.append(copy.deepcopy(tv))
        if errors is not None and len(existing) > len(template):
            dropped = len(existing) - len(template)
            errors.append(f"{path}: {dropped} extra item(s) dropped")
        return result

    if skipped_paths and path in skipped_paths:
        return existing
    if str(existing) == str(template):
        return existing
    if errors is not None:
        errors.append(f"{path}: expected '{template}', got '{existing}'")
    return _preserve_type(existing, template)


def merge_with_existing(rendered: dict, existing: dict, errors: list | None = None) -> dict:
    """Merge rendered template with existing file data.

    Recursively merges, preserving existing ordering and scalar types.
    Records errors for mismatched literal values.
    """
    skipped_paths = getattr(rendered, '_skipped_paths', None)
    return _deep_merge(rendered, existing, errors, skipped_paths=skipped_paths)


def _template_param_names(rendered: dict) -> set[str]:
    """Extract the set of param names declared in a rendered template."""
    return {str(p["name"]) for p in rendered.get("spec", {}).get("params", []) if "name" in p}


def _inherit_parent_params(result: dict, parent_data: dict, template_param_names: set[str],
                           errors: list | None, warnings: list | None, mode: str) -> dict:
    """Ensure child params match parent params not explicitly handled by the template.

    For each parent param not in template_param_names:
    - If child has it with a different value: report error, fix in fix mode
    - If values are semantically equal but formatted differently: warn, fix in fix mode
    - If child is missing it: report error, add in fix mode
    Child-only params (not in parent or template) are left alone.
    """
    import copy

    parent_params = {str(p["name"]): p for p in parent_data.get("spec", {}).get("params", [])}
    result_params = result.get("spec", {}).get("params", [])
    result_by_name = {str(p["name"]): p for p in result_params}

    for name, parent_p in parent_params.items():
        if name in template_param_names:
            continue

        parent_val = parent_p.get("value")

        if name in result_by_name:
            child_val = result_by_name[name].get("value")
            if _values_equal(parent_val, child_val):
                continue
            if _values_semantically_equal(parent_val, child_val):
                if warnings is not None:
                    warnings.append(f"spec.params[{name}]: semantically identical to parent but differently formatted")
                # Don't replace — preserve child's formatting when content is the same
            else:
                if errors is not None:
                    errors.append(f"spec.params[{name}]: expected '{parent_val}' (from parent), got '{child_val}'")
                if mode == "fix":
                    result_by_name[name]["value"] = copy.deepcopy(parent_val)
        else:
            if errors is not None:
                errors.append(f"spec.params[{name}]: missing (present in parent)")
            if mode == "fix":
                result_params.append(copy.deepcopy(parent_p))

    return result


def _values_equal(a, b) -> bool:
    """Compare param values, handling lists and scalars."""
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(str(x) == str(y) for x, y in zip(a, b))
    return str(a) == str(b)


def _to_native(val):
    """Convert a value to a plain Python structure for semantic comparison.

    Handles: JSON strings → parsed, ruamel CommentedMap/CommentedSeq → plain
    dict/list, and leaves other types as-is.
    """
    import json

    # If it's a string, try parsing as JSON
    if isinstance(val, str):
        try:
            return json.loads(val.strip())
        except (json.JSONDecodeError, TypeError):
            return val

    # If it's a ruamel or plain list/dict, recursively convert to plain types
    if isinstance(val, dict):
        return {str(k): _to_native(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_to_native(item) for item in val]

    return val


def _values_semantically_equal(a, b) -> bool:
    """Check if values are semantically equal despite different formatting.

    Handles: list ordering differences, JSON string formatting differences,
    and mixed representations (e.g. YAML list vs JSON string encoding the
    same data).
    """
    if isinstance(a, list) and isinstance(b, list):
        return sorted(str(x) for x in a) == sorted(str(x) for x in b)

    a_native = _to_native(a)
    b_native = _to_native(b)

    if a_native == b_native:
        return True

    # Also check with sorted lists (order-insensitive)
    if isinstance(a_native, list) and isinstance(b_native, list):
        import json
        return sorted(json.dumps(x, sort_keys=True) for x in a_native) == \
               sorted(json.dumps(x, sort_keys=True) for x in b_native)

    return False


def render_and_merge(
    template_dir: str,
    template_name: str,
    existing_data: dict,
    filename: str,
    branch: str | None = None,
    parent_data: dict | None = None,
    mode: str = "validate",
    errors: list | None = None,
    warnings: list | None = None,
    cli_vars: dict | None = None,
    inherit_params: bool = False,
    overlay_path: str | None = None,
) -> dict:
    """Render a template with variables extracted from existing data, then merge.

    If errors list is provided, validation filters will append errors to it.
    Mode: 'fix' applies transforms then validates; 'validate' skips transforms.
    When inherit_params is True and parent_data is provided, all parent params
    not explicitly in the template are inherited (validated or fixed).
    When overlay_path is set, deep-merges the overlay onto the base template
    before rendering.
    """
    variables = extract_template_vars(existing_data, filename, branch, parent_data=parent_data, cli_vars=cli_vars)
    rendered = render_template(template_dir, template_name, variables, mode=mode, errors=errors, overlay_path=overlay_path)

    tmpl_param_names = _template_param_names(rendered) if inherit_params else set()

    result = merge_with_existing(rendered, existing_data, errors=errors)

    if inherit_params and parent_data is not None:
        result = _inherit_parent_params(result, parent_data, tmpl_param_names, errors, warnings, mode)

    return result
