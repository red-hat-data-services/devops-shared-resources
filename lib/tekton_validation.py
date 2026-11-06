"""Optional selection and strict writer profile for the ordinary validator engine.

No policy-specific discovery or fix loop lives here. Target documents are data;
only the caller's trusted configuration selects Jinja and optional guard code.
"""
from collections import Counter
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path

from ruamel.yaml import YAML

from lib.tekton import _create_jinja_env, classify_pipeline, extract_template_vars, render_and_merge
from lib.tekton_yaml import localized_yaml

STATUSES = {"applicable", "skipped", "manual", "unresolved"}


def selection(config, data, filename, branch, context):
    """Render a bounded, strictly shaped JSON selection using ordinary Jinja."""
    if not config.selection_template:
        return {"status": "applicable", "reason": ""}
    env = _create_jinja_env(config.config_dir)
    variables = extract_template_vars(data, filename, branch)
    variables["context"] = deepcopy(context)
    rendered = env.get_template(config.selection_template).render(**variables)
    if len(rendered.encode("utf-8")) > 16384:
        raise ValueError("selection result exceeds 16384 bytes")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate selection JSON key")
            result[key] = value
        return result
    value = json.loads(rendered, object_pairs_hook=unique_object)
    if (not isinstance(value, dict) or set(value) != {"status", "reason"}
            or not isinstance(value["status"], str) or value["status"] not in STATUSES
            or not isinstance(value["reason"], str) or len(value["reason"]) > 4096):
        raise ValueError("selection must contain only a valid status and string reason")
    return value


class StrictProfile:
    """A per-phase adapter: the ordinary engine supplies ordering and rendering."""
    def __init__(self, root, phase, config, branch, documents, context, policy=None):
        self.root, self.phase, self.config, self.branch = Path(root), phase, config, branch
        self.documents, self.context, self.policy = documents, deepcopy(context), policy
        self.rows, self.pending = {}, []
        self.yaml = YAML()
        self.yaml.preserve_quotes, self.yaml.width = True, 4096

    def load(self):
        pipelines = {}
        for name, original in self.documents.items():
            decision = selection(self.config, original, Path(name).name, self.branch, self.context)
            if not self.config.selection_template and self.policy:
                decision = self.policy.decision(deepcopy(original), deepcopy(self.context))
            if (not isinstance(decision, dict) or decision.get("status") not in STATUSES
                    or not isinstance(decision.get("reason", ""), str)
                    or not isinstance(decision.get("variables", {}), dict)):
                raise ValueError("invalid policy decision")
            self.rows[name] = {"path": name, "status": decision["status"], "reason": decision.get("reason", "")}
            if decision["status"] != "applicable":
                continue
            path = self.root / name
            data = self.yaml.load(path.read_bytes())
            template = self.config.get_template(classify_pipeline(path.name, data, self.config))
            if template is None or template.parent:
                raise ValueError("policy requires an existing non-parent template")
            pipelines[path] = data
            self.rows[name]["_variables"] = decision.get("variables", {})
        return pipelines, {}, {}, 0

    def render(self, *args, **kwargs):
        # Engine supplies the absolute source path separately: basenames need not
        # be unique in nested layouts.
        name = self.current_path.relative_to(self.root).as_posix()
        original = self.documents[name]
        kwargs["mode"] = "validate" if self.phase == "validation" else "fix"
        errors = []
        kwargs["errors"] = errors if self.phase == "validation" else None
        kwargs["cli_vars"] = self.rows[name].get("_variables", {})
        kwargs["template_context"] = self.context
        kwargs["template_filters"] = getattr(self.policy, "filters", {})
        candidate = render_and_merge(*args, **kwargs)
        if errors:
            raise ValueError(f"policy rejected candidate {name}: {errors}")
        from lib.tekton_reconciliation import detached_data
        candidate_data = detached_data(candidate)
        guard = getattr(self.policy, "check", None)
        if guard and guard(deepcopy(original), candidate_data, deepcopy(self.context)) is not True:
            raise ValueError(f"policy rejected candidate {name}")
        self.rows[name]["status"] = "conformant" if candidate == original else "planned"
        return candidate

    def serialize(self, path, candidate):
        source = path.read_bytes().decode("utf-8")
        text = localized_yaml(source, candidate)
        if YAML(typ="safe", pure=True).load(text) != candidate:
            raise ValueError("serialized candidate differs")
        self.pending.append((path, text.encode("utf-8")))

    def finish(self):
        if self.phase in ("validation", "idempotence") and self.pending:
            raise ValueError("validation/idempotence still plans changes")
        # All candidates and guards have completed before the first strict write.
        if self.phase == "fix":
            for path, data in self.pending:
                path.write_bytes(data)
        rows = [{key: value for key, value in row.items() if not key.startswith("_")} for row in self.rows.values()]
        return {"files": rows, "counts": dict(Counter(row["status"] for row in rows))}


def write_candidate(path, candidate, yaml, dry_run, profile=None):
    if profile:
        profile.serialize(path, candidate)
    elif not dry_run:
        stream = StringIO()
        yaml.dump(candidate, stream)
        path.write_text(stream.getvalue())
