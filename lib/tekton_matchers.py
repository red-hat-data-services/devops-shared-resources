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

import re

from lib.tekton import extract_component_prefix, get_annotation


def pipeline_matcher(fn):
    """Mark a function as a pipeline matcher. Receives (name, data) -> bool."""
    fn._is_pipeline_matcher = True
    return fn


def parent_matcher(fn):
    """Mark a function as a parent matcher. Receives (parent_data, child_data) -> bool."""
    fn._is_parent_matcher = True
    return fn


def _is_build_family(name, data, pipeline_path):
    """Classify a referenced build family; trusted selection owns eligibility."""
    if not isinstance(data, dict) or data.get("kind") != "PipelineRun":
        return False
    spec = data.get("spec", {})
    if "pipelineSpec" in spec:
        return False
    ref = spec.get("pipelineRef", {})
    if ref.get("resolver") != "git":
        return False
    params = {}
    for item in ref.get("params", []):
        key = item.get("name")
        if key in params:
            raise ValueError(f"duplicate pipeline reference parameter: {key}")
        params[key] = item.get("value")
    return (params.get("pathInRepo") == pipeline_path
            and not is_pr_pipeline(name, data)
            and (is_push_pipeline_by_cel(name, data) or is_scheduled_pipeline(name, data)))


# --- Pipeline matchers ---


@pipeline_matcher
def is_pipeline_run(name, data):
    """Match every PipelineRun, independently of event, namespace, or naming."""
    return isinstance(data, dict) and data.get("kind") == "PipelineRun"


@pipeline_matcher
def is_push_pipeline(name, data):
    """Match push pipelines by metadata.name ending with '-on-push'."""
    return name.endswith("-on-push")


@pipeline_matcher
def is_push_pipeline_by_cel(name, data):
    """Match non-scheduled push pipelines by CEL containing event == 'push'.

    Used for konflux-central release branches where push pipelines use
    on-cel-expression instead of on-event annotations, and metadata.name
    does not end with '-on-push'. Scheduled builds also use push events,
    but must not be selected as normal push parents.
    """
    if is_scheduled_pipeline(name, data):
        return False
    # Incorrect CEL must not hide an explicitly identified PR pipeline.
    on_event = get_annotation(data, "pipelinesascode.tekton.dev/on-event")
    if on_event and "pull_request" in on_event:
        return False
    # Only an explicit terminal PR event marker overrides the CEL event.
    if re.search(
        r"-(?:on-)?pull-request(?:-(?:\d+|\{\{\s*pull_request_number\s*\}\}))?$",
        name,
    ):
        return False
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


def _is_container_build(data):
    """Recognize standard container references or an inline scan-bearing Pipeline.

    Reference paths identify a family, as with chart/FBC matchers. The caller's
    safety guard remains responsible for trusted repository/compatibility policy.
    """
    if not isinstance(data, dict) or data.get("kind") != "PipelineRun":
        return False
    spec = data.get("spec", {})
    if not isinstance(spec, dict):
        raise ValueError("PipelineRun spec must be a mapping")
    if "pipelineSpec" in spec:
        pipeline = spec["pipelineSpec"]
        if not isinstance(pipeline, dict):
            raise ValueError("inline Pipeline source must be a mapping")
        tasks = []
        for location in ("tasks", "finally"):
            collection = pipeline.get(location, [])
            if not isinstance(collection, list) or any(not isinstance(task, dict) for task in collection):
                raise ValueError("Pipeline tasks must be a list of mappings")
            tasks.extend(collection)
        matches = [task for task in tasks if task.get("name") == "rpms-signature-scan"]
        if len(matches) > 1:
            raise ValueError("duplicate Pipeline scan task")
        return bool(matches)
    ref = spec.get("pipelineRef", {})
    if not isinstance(ref, dict) or ref.get("resolver") != "git":
        return False
    params = {}
    items = ref.get("params", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("pipeline reference params must be a list of mappings")
    for item in items:
        key = item.get("name")
        if not isinstance(key, str) or not key:
            raise ValueError("pipeline reference parameter requires a name")
        if key in params:
            raise ValueError(f"duplicate pipeline reference parameter: {key}")
        params[key] = item.get("value")
    return params.get("pathInRepo") in (
        "pipelines/container-build.yaml", "pipelines/multi-arch-container-build.yaml")


@pipeline_matcher
def is_container_build_push(name, data):
    """Match container builds, not arbitrary push-event PipelineRuns."""
    return (not is_pr_pipeline(name, data) and not is_scheduled_pipeline(name, data)
            and (is_push_pipeline_by_cel(name, data) or is_push_pipeline(name, data))
            and _is_container_build(data))


@pipeline_matcher
def is_container_build_pull(name, data):
    """Match container PRs, including legacy annotation-based PR identity."""
    return is_pr_pipeline(name, data) and _is_container_build(data)


@pipeline_matcher
def is_container_build_scheduled(name, data):
    """Match scheduled container builds without changing their trigger policy."""
    return (not is_pr_pipeline(name, data) and is_scheduled_pipeline(name, data)
            and _is_container_build(data))


@pipeline_matcher
def is_chart(name, data):
    """Match Helm-chart push/scheduled runs; URL/revision checks stay in policy."""
    return _is_build_family(name, data, "pipelines/helm-chart-build.yaml")


@pipeline_matcher
def is_fbc(name, data):
    """Match FBC-fragment push/scheduled runs; URL/revision checks stay in policy."""
    return _is_build_family(name, data, "pipelines/fbc-fragment-build.yaml")


# --- Parent matchers ---


@parent_matcher
def component_label_match(parent_data, child_data):
    """Match exact nonempty component labels independently of metadata name length."""
    key = "appstudio.openshift.io/component"
    parent = (parent_data.get("metadata", {}).get("labels") or {}).get(key)
    child = (child_data.get("metadata", {}).get("labels") or {}).get(key)
    return isinstance(parent, str) and bool(parent) and parent == child


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
