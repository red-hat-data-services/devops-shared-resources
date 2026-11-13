"""Config loading for validate-tekton template system.

Loads a config.yaml that declares templates, their pipeline matchers,
and parent-child relationships with parent matchers.

Config format:

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
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lib.tekton_matchers import make_parent_matchers, make_pipeline_matchers


@dataclass
class ParentConfig:
    template: str
    match_fns: list[str]
    inherit_params: bool = False


@dataclass
class TemplateConfig:
    name: str
    file: str
    match_fns: list[str]
    parent: ParentConfig | None = None
    generate: dict | None = None


@dataclass
class PipelineConfig:
    templates: list[TemplateConfig]
    config_dir: str
    component_aliases: dict[str, str] = None
    parent_ref: str | None = None
    tekton_layout: str = "flat"
    selection_template: str | None = None
    context_paths: tuple[str, ...] = ()
    publication_branches: tuple[str, ...] | None = None
    template_overlays: tuple[dict, ...] = ()

    def get_template(self, name: str) -> TemplateConfig | None:
        for t in self.templates:
            if t.name == name:
                return t
        return None

    def parent_template_names(self) -> set[str]:
        return {t.parent.template for t in self.templates if t.parent}


def _normalize_match_fn(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ValueError(f"match_fn must be a string or list, got {type(value).__name__}")


def _validate_generate(policy, template_name):
    """Reject malformed generation policy instead of silently disabling guards.

    Optional sequence settings require lists of strings; required parent fields
    are nonempty key paths. Reject unknown keys to catch misspelled safety rules,
    and reserve metadata.name for the configured child identity.
    """
    prefix = f"template '{template_name}': generate"
    if not isinstance(policy, Mapping):
        raise ValueError(f"{prefix} must be a mapping")
    supported = {
        "filename", "name", "inherit_metadata", "preserve_annotations",
        "allowed_spec_fields", "git_pipeline_ref", "required_parent_fields",
    }
    unknown = set(policy) - supported
    if unknown:
        raise ValueError(f"{prefix}: unknown keys {sorted(map(str, unknown))}")
    for key in ("filename", "name"):
        if not isinstance(policy.get(key), str) or not policy[key].strip():
            raise ValueError(f"{prefix} requires {key} as a nonempty string")
    for key in ("inherit_metadata", "preserve_annotations", "allowed_spec_fields"):
        if key in policy:
            value = policy[key]
            if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
                raise ValueError(f"{prefix}.{key} must be a list of nonempty strings")
    if "name" in policy.get("inherit_metadata", []):
        raise ValueError(f"{prefix}.inherit_metadata must not include 'name'; use generate.name for child identity")
    if "git_pipeline_ref" in policy and not isinstance(policy["git_pipeline_ref"], bool):
        raise ValueError(f"{prefix}.git_pipeline_ref must be a boolean")
    if "required_parent_fields" in policy:
        paths = policy["required_parent_fields"]
        if not isinstance(paths, list) or any(
            not isinstance(path, list) or not path
            or any(not isinstance(key, str) or not key for key in path)
            for path in paths
        ):
            raise ValueError(f"{prefix}.required_parent_fields must be a list of nonempty string key paths")


def _validate_config(config: PipelineConfig):
    """Check template files, matcher references, and opt-in generation policy."""
    pipeline_matchers = make_pipeline_matchers()
    parent_matchers = make_parent_matchers()
    template_names = {t.name for t in config.templates}

    for tmpl in config.templates:
        for fn_name in tmpl.match_fns:
            if fn_name not in pipeline_matchers:
                raise ValueError(
                    f"template '{tmpl.name}': unknown pipeline matcher '{fn_name}'. "
                    f"Available: {sorted(pipeline_matchers)}"
                )

        template_path = Path(config.config_dir) / tmpl.file
        if not template_path.exists():
            raise ValueError(f"template '{tmpl.name}': file not found: {template_path}")

        if tmpl.generate is not None:
            if not tmpl.parent:
                raise ValueError(f"template '{tmpl.name}': generate requires a parent")
            _validate_generate(tmpl.generate, tmpl.name)
        if tmpl.parent:
            if tmpl.parent.template not in template_names:
                raise ValueError(
                    f"template '{tmpl.name}': parent references unknown template '{tmpl.parent.template}'"
                )
            for fn_name in tmpl.parent.match_fns:
                if fn_name not in parent_matchers:
                    raise ValueError(
                        f"template '{tmpl.name}': unknown parent matcher '{fn_name}'. "
                        f"Available: {sorted(parent_matchers)}"
                    )


def load_config(config_dir: str, config_file: str | None = None) -> PipelineConfig:
    """Load pipeline config from a YAML file.

    If config_file is given, load it directly. Otherwise look for config.yaml
    in config_dir. If no config file exists, return the default config.
    """
    if config_file:
        config_path = Path(config_file)
    else:
        config_path = Path(config_dir) / "config.yaml"

    if not config_path.exists():
        return default_config(config_dir)

    from ruamel.yaml import YAML

    yaml = YAML()
    with open(config_path) as f:
        raw = yaml.load(f)

    if not raw or "templates" not in raw:
        raise ValueError(f"config file {config_path} must contain a 'templates' key")

    templates = []
    for entry in raw["templates"]:
        if "name" not in entry or "file" not in entry or "match_fn" not in entry:
            raise ValueError(f"each template entry must have 'name', 'file', and 'match_fn'")

        parent = None
        if "parent" in entry:
            p = entry["parent"]
            if "template" not in p or "match_fn" not in p:
                raise ValueError(
                    f"template '{entry['name']}': parent must have 'template' and 'match_fn'"
                )
            parent = ParentConfig(
                template=str(p["template"]),
                match_fns=_normalize_match_fn(p["match_fn"]),
                inherit_params=bool(p.get("inherit_params", False)),
            )

        generate = None
        if "generate" in entry:
            _validate_generate(entry["generate"], str(entry["name"]))
            generate = dict(entry["generate"])

        templates.append(TemplateConfig(
            name=str(entry["name"]),
            file=str(entry["file"]),
            match_fns=_normalize_match_fn(entry["match_fn"]),
            parent=parent,
            generate=generate,
        ))

    aliases = dict(raw.get("component_aliases", {})) if raw.get("component_aliases") else None
    parent_ref = str(raw["parent_ref"]) if raw.get("parent_ref") else None
    tekton_layout = str(raw.get("tekton_layout", "flat"))
    if tekton_layout not in ("flat", "nested"):
        raise ValueError(f"tekton_layout must be 'flat' or 'nested', got '{tekton_layout}'")
    config = PipelineConfig(
        templates=templates,
        config_dir=str(config_dir),
        component_aliases=aliases,
        parent_ref=parent_ref,
        tekton_layout=tekton_layout,
    )
    overlays = raw.get("template_overlays", [])
    if not isinstance(overlays, list):
        raise ValueError("template_overlays must be a list")
    scopes = []
    for entry in overlays:
        if not isinstance(entry, Mapping) or set(entry) != {"pipeline_dir", "template_dir"}:
            raise ValueError("template_overlays entries require pipeline_dir and template_dir")
        for value in entry.values():
            if (not isinstance(value, str) or not value or value == "."
                    or Path(value).is_absolute() or ".." in Path(value).parts
                    or Path(value).as_posix() != value or "\\" in value
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError("template_overlays paths must be normalized relative paths")
        scope = Path(entry["pipeline_dir"])
        if any(scope.is_relative_to(other) or other.is_relative_to(scope) for other in scopes):
            raise ValueError("template_overlays source scopes overlap")
        scopes.append(scope)
        target = Path(config_dir).resolve() / entry["template_dir"]
        if not target.is_dir() or target.resolve() != target:
            raise ValueError("template_overlays template_dir must be a non-symlink directory")
    config.template_overlays = tuple(dict(entry) for entry in overlays)
    selection = raw.get("selection_template")
    if selection is not None:
        if (not isinstance(selection, str) or not selection or Path(selection).is_absolute()
                or ".." in Path(selection).parts or Path(selection).as_posix() != selection):
            raise ValueError("selection_template must be a contained relative file")
        path = Path(config_dir).resolve() / selection
        if not path.is_file() or path.resolve() != path.absolute():
            raise ValueError("selection_template must not traverse symlinks")
    paths = raw.get("context_paths", [])
    if (not isinstance(paths, list) or any(not isinstance(p, str) or not p or Path(p).is_absolute()
            or ".." in Path(p).parts or Path(p).as_posix() != p for p in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError("context_paths must be unique contained relative paths")
    if "publication_branches" in raw:
        import re
        patterns = raw["publication_branches"]
        if (not isinstance(patterns, list) or not patterns
                or any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*\*?", p)
                       or ".." in p or "//" in p or p.rstrip("*").endswith(".") for p in patterns)
                or len(set(patterns)) != len(patterns)):
            raise ValueError("publication_branches must be a nonempty unique list of exact names or literal prefixes with one trailing '*'")
        import subprocess
        for pattern in patterns:
            example = pattern[:-1] + "x" if pattern.endswith("*") else pattern
            if subprocess.run(["git", "check-ref-format", f"refs/heads/{example}"], capture_output=True).returncode:
                raise ValueError("publication_branches contains an invalid branch name or prefix")
        config.publication_branches = tuple(patterns)
    config.selection_template = selection
    config.context_paths = tuple(paths)
    _validate_config(config)
    return config


def default_config(config_dir: str) -> PipelineConfig:
    """Return the built-in default config matching the original hardcoded behavior."""
    return PipelineConfig(
        config_dir=str(config_dir),
        templates=[
            TemplateConfig(
                name="push",
                file="push.yaml.j2",
                match_fns=["is_push_pipeline"],
            ),
            TemplateConfig(
                name="pull-request",
                file="pull-request.yaml.j2",
                match_fns=["is_pr_pipeline"],
                parent=ParentConfig(
                    template="push",
                    match_fns=[
                        "exact_component_match",
                        "odh_prefix_match",
                        "component_starts_with",
                    ],
                    inherit_params=True,
                ),
            ),
            TemplateConfig(
                name="scheduled",
                file="scheduled.yaml.j2",
                match_fns=["is_scheduled_pipeline"],
            ),
        ],
    )
