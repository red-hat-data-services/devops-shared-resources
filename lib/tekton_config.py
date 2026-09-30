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


@dataclass
class PipelineConfig:
    templates: list[TemplateConfig]
    config_dir: str
    component_aliases: dict[str, str] = None
    parent_ref: str | None = None
    tekton_layout: str = "flat"

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


def _validate_config(config: PipelineConfig):
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

        templates.append(TemplateConfig(
            name=str(entry["name"]),
            file=str(entry["file"]),
            match_fns=_normalize_match_fn(entry["match_fn"]),
            parent=parent,
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
