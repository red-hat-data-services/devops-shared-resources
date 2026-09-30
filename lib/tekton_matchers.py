"""Pipeline and parent matcher functions for configurable tekton validation.

Pipeline matchers classify a PipelineRun file as belonging to a template type.
Parent matchers determine whether a candidate parent pipeline matches a child.

Both use a decorator-based registry pattern identical to @jinja_filter in
tekton_filters.py.

Usage in config.yaml:

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
"""

from lib.tekton import extract_component_prefix, get_annotation


def pipeline_matcher(fn):
    """Mark a function as a pipeline matcher. Receives (name, data) -> bool."""
    fn._is_pipeline_matcher = True
    return fn


def parent_matcher(fn):
    """Mark a function as a parent matcher. Receives (parent_data, child_data) -> bool."""
    fn._is_parent_matcher = True
    return fn


# --- Pipeline matchers ---


@pipeline_matcher
def is_push_pipeline(name, data):
    """Match push pipelines by metadata.name ending with '-on-push'."""
    return name.endswith("-on-push")


@pipeline_matcher
def is_push_pipeline_by_cel(name, data):
    """Match push pipelines by CEL expression containing event == 'push'.

    Used for konflux-central release branches where push pipelines use
    on-cel-expression instead of on-event annotations, and metadata.name
    does not end with '-on-push'.
    """
    cel = get_annotation(data, "pipelinesascode.tekton.dev/on-cel-expression")
    return bool(cel and 'event == "push"' in cel)


@pipeline_matcher
def is_pr_pipeline(name, data):
    """Match pull-request pipelines by annotation or name."""
    on_event = get_annotation(data, "pipelinesascode.tekton.dev/on-event")
    if on_event and "pull_request" in on_event:
        return True
    return "pull-request" in name


@pipeline_matcher
def is_scheduled_pipeline(name, data):
    """Match scheduled pipelines by name containing '-on-schedule'."""
    return "-on-schedule" in name


# --- Parent matchers ---


@parent_matcher
def exact_component_match(parent_data, child_data):
    """Match when parent and child have the same component prefix."""
    parent_prefix = extract_component_prefix("", parent_data)
    child_prefix = extract_component_prefix("", child_data)
    if not parent_prefix or not child_prefix:
        return False
    return parent_prefix == child_prefix


@parent_matcher
def odh_prefix_match(parent_data, child_data):
    """Match when child prefix with 'odh-' prepended equals parent prefix."""
    parent_prefix = extract_component_prefix("", parent_data)
    child_prefix = extract_component_prefix("", child_data)
    if not parent_prefix or not child_prefix:
        return False
    if child_prefix.startswith("odh-"):
        return False
    return parent_prefix == f"odh-{child_prefix}"


@parent_matcher
def component_starts_with(parent_data, child_data):
    """Match when parent prefix starts with child prefix."""
    parent_prefix = extract_component_prefix("", parent_data)
    child_prefix = extract_component_prefix("", child_data)
    if not parent_prefix or not child_prefix:
        return False
    return parent_prefix.startswith(child_prefix)


@parent_matcher
def alias_match(parent_data, child_data, aliases=None):
    """Match when child prefix is a known alias for the parent prefix.

    Aliases are configured in templates/config.yaml under
    component_aliases as a mapping of short → full name.
    """
    if not aliases:
        return False
    parent_prefix = extract_component_prefix("", parent_data)
    child_prefix = extract_component_prefix("", child_data)
    if not parent_prefix or not child_prefix:
        return False
    return aliases.get(child_prefix) == parent_prefix or \
           aliases.get(parent_prefix) == child_prefix


# --- Registry discovery ---


def make_pipeline_matchers():
    """Discover all @pipeline_matcher functions and return {name: fn}."""
    import inspect

    current_module = inspect.getmodule(make_pipeline_matchers)
    matchers = {}
    for name, fn in inspect.getmembers(current_module, inspect.isfunction):
        if getattr(fn, "_is_pipeline_matcher", False):
            matchers[name] = fn
    return matchers


def make_parent_matchers():
    """Discover all @parent_matcher functions and return {name: fn}."""
    import inspect

    current_module = inspect.getmodule(make_parent_matchers)
    matchers = {}
    for name, fn in inspect.getmembers(current_module, inspect.isfunction):
        if getattr(fn, "_is_parent_matcher", False):
            matchers[name] = fn
    return matchers
