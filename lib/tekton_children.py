"""Opt-in child creation/reconciliation for validate-tekton's template engine.

No CLI, Git, branch policy, or writes: produce expected children from parents.
"""
import copy
import re
from pathlib import Path

from lib.tekton import classify_pipeline, extract_component_prefix, render_and_merge, resolve_overlay
from lib.tekton_matchers import make_parent_matchers


def plan_children(pipelines, config, branch, cli_vars=None, *, repository_root=None):
    """Return expected {path: data}, orphan paths. Plan everything before writes.

    A generated child is rebuilt from its parent, not merged with stale child
    build params. Only explicitly configured annotations survive from the child.
    Parent and unknown files are never generation targets.
    """
    expected, orphans = {}, []
    matchers = make_parent_matchers()
    for tmpl in config.templates:
        if not tmpl.generate:
            continue
        policy = tmpl.generate
        parents = {p: d for p, d in pipelines.items()
                   if classify_pipeline(p.name, d, config) == tmpl.parent.template}
        children = {p: d for p, d in pipelines.items()
                    if classify_pipeline(p.name, d, config) == tmpl.name}
        matches = {p: [] for p in parents}
        # Unlike the historical index, retain duplicates so they fail explicitly.
        identities = set()
        for parent_path, parent in parents.items():
            prefix = extract_component_prefix(parent_path.name, parent)
            if not prefix or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", prefix):
                raise ValueError(f"unsafe or missing component prefix: {parent_path}")
            identity = (parent_path.parent, prefix)
            if identity in identities:
                raise ValueError(f"ambiguous parent component: {parent_path}")
            identities.add(identity)
        for child_path, child in children.items():
            candidates = []
            for fn_name in tmpl.parent.match_fns:
                fn = matchers[fn_name]
                candidates = [p for p, parent in parents.items()
                              if p.parent == child_path.parent and (
                                  fn(parent, child, config.component_aliases) if fn_name == "alias_match"
                                  else fn(parent, child))]
                if candidates:
                    break
            if len(candidates) > 1:
                raise ValueError(f"ambiguous parent match: {child_path}")
            if not candidates:
                orphans.append(child_path)
            else:
                matches[candidates[0]].append(child_path)
        for parent_path, parent in sorted(parents.items()):
            prefix = extract_component_prefix(parent_path.name, parent)
            existing = matches[parent_path]
            if len(existing) > 1:
                raise ValueError(f"ambiguous child match: {parent_path}")
            filename = policy["filename"].replace("{component}", prefix)
            if Path(filename).name != filename or not filename.endswith((".yaml", ".yml")):
                raise ValueError(f"unsafe generated filename: {filename}")
            path = existing[0] if existing else parent_path.with_name(filename)
            if not existing and (path.exists() or path in pipelines):
                raise ValueError(f"generated filename collision: {path}")
            if path in expected:
                raise ValueError(f"duplicate generated target: {path}")
            if parent_path.is_symlink() or path.is_symlink():
                raise ValueError(f"symlink PipelineRun: {path}")
            for field in policy.get("required_parent_fields", []):
                value = parent
                for key in field:
                    value = value.get(key) if isinstance(value, dict) else None
                if value is None or value == "":
                    raise ValueError(f"missing parent field {field}: {parent_path}")
            seed = copy.deepcopy(parent)
            # Metadata retention is policy-controlled; strip runtime/push metadata.
            metadata = {"name": policy["name"].replace("{component}", prefix)}
            for key in policy.get("inherit_metadata", ["labels", "namespace"]):
                if key in parent.get("metadata", {}):
                    metadata[key] = copy.deepcopy(parent["metadata"][key])
            old = pipelines[path] if existing else {}
            annotations = old.get("metadata", {}).get("annotations", {})
            metadata["annotations"] = {key: annotations[key] for key in policy.get("preserve_annotations", []) if key in annotations}
            seed["metadata"] = metadata
            allowed_spec = policy.get("allowed_spec_fields")
            if allowed_spec is not None and set(seed.get("spec", {})) - set(allowed_spec):
                raise ValueError(f"unsupported parent spec fields: {parent_path}")
            if policy.get("git_pipeline_ref"):
                ref = seed.get("spec", {}).get("pipelineRef", {})
                params = ref.get("params", [])
                if ref.get("resolver") != "git" or len(params) != 3 or {p["name"] for p in params} != {"url", "revision", "pathInRepo"}:
                    raise ValueError(f"unsupported parent pipeline reference: {parent_path}")
            params = seed.get("spec", {}).get("params", [])
            if len({p["name"] for p in params}) != len(params):
                raise ValueError(f"duplicate parent params: {parent_path}")
            overlay = resolve_overlay(config.config_dir, tmpl.file, prefix,
                                      (parent.get("metadata", {}).get("labels") or {}).get("appstudio.openshift.io/component"),
                                      pipeline_path=path, repository_root=repository_root, mappings=config.template_overlays)
            errors = []
            result = render_and_merge(config.config_dir, tmpl.file, seed, path.name, branch,
                                      parent_data=parent, cli_vars=cli_vars, mode="fix", errors=errors,
                                      overlay_path=overlay)
            # The merge also reports corrected differences; validate final data.
            validation = []
            render_and_merge(config.config_dir, tmpl.file, result, path.name, branch,
                             parent_data=parent, cli_vars=cli_vars, mode="validate", errors=validation,
                             overlay_path=overlay)
            if validation:
                raise ValueError(f"invalid generated child {path}: {validation}")
            expected[path] = result
    if not expected:
        raise ValueError("no matching parent PipelineRuns for generated children")
    return expected, sorted(orphans)
