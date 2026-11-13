import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from lib.tekton_config import (
    PipelineConfig,
    TemplateConfig,
    ParentConfig,
    default_config,
    load_config,
    _normalize_match_fn,
)

TEMPLATES_DIR = str(REPO_ROOT / "tekton-templates" / "embargo")


class TestNormalizeMatchFn:
    def test_string_becomes_list(self):
        assert _normalize_match_fn("is_push_pipeline") == ["is_push_pipeline"]

    def test_list_stays_list(self):
        assert _normalize_match_fn(["a", "b"]) == ["a", "b"]

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="must be a string or list"):
            _normalize_match_fn(42)


class TestDefaultConfig:
    def test_has_three_templates(self):
        config = default_config(TEMPLATES_DIR)
        assert len(config.templates) == 3

    def test_template_names(self):
        config = default_config(TEMPLATES_DIR)
        names = [t.name for t in config.templates]
        assert names == ["push", "pull-request", "scheduled"]

    def test_push_has_no_parent(self):
        config = default_config(TEMPLATES_DIR)
        push = config.get_template("push")
        assert push is not None
        assert push.parent is None

    def test_pr_has_push_parent(self):
        config = default_config(TEMPLATES_DIR)
        pr = config.get_template("pull-request")
        assert pr is not None
        assert pr.parent is not None
        assert pr.parent.template == "push"
        assert "exact_component_match" in pr.parent.match_fns

    def test_scheduled_has_no_parent(self):
        config = default_config(TEMPLATES_DIR)
        sched = config.get_template("scheduled")
        assert sched is not None
        assert sched.parent is None

    def test_config_dir_set(self):
        config = default_config(TEMPLATES_DIR)
        assert config.config_dir == TEMPLATES_DIR

    def test_parent_template_names(self):
        config = default_config(TEMPLATES_DIR)
        assert config.parent_template_names() == {"push"}


class TestGetTemplate:
    def test_found(self):
        config = default_config(TEMPLATES_DIR)
        assert config.get_template("push") is not None

    def test_not_found(self):
        config = default_config(TEMPLATES_DIR)
        assert config.get_template("nonexistent") is None


class TestLoadConfig:
    def test_loads_from_yaml(self, tmp_path):
        (tmp_path / "push.yaml.j2").write_text("dummy")
        (tmp_path / "pr.yaml.j2").write_text("dummy")
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(textwrap.dedent("""\
            templates:
              - name: push
                file: push.yaml.j2
                match_fn: is_push_pipeline
              - name: pr
                file: pr.yaml.j2
                match_fn: is_pr_pipeline
                parent:
                  template: push
                  match_fn:
                    - exact_component_match
                    - odh_prefix_match
        """))

        config = load_config(str(tmp_path))
        assert len(config.templates) == 2
        assert config.get_template("push").match_fns == ["is_push_pipeline"]
        pr = config.get_template("pr")
        assert pr.parent.template == "push"
        assert pr.parent.match_fns == ["exact_component_match", "odh_prefix_match"]

    def test_loads_from_explicit_file(self, tmp_path):
        (tmp_path / "push.yaml.j2").write_text("dummy")
        config_yaml = tmp_path / "my-config.yaml"
        config_yaml.write_text(textwrap.dedent("""\
            templates:
              - name: push
                file: push.yaml.j2
                match_fn: is_push_pipeline
        """))

        config = load_config(str(tmp_path), config_file=str(config_yaml))
        assert len(config.templates) == 1

    def test_falls_back_to_default_when_no_file(self):
        config = load_config(TEMPLATES_DIR)
        assert len(config.templates) == 3
        assert config.get_template("push") is not None

    def test_string_match_fn_normalized(self, tmp_path):
        (tmp_path / "push.yaml.j2").write_text("dummy")
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(textwrap.dedent("""\
            templates:
              - name: push
                file: push.yaml.j2
                match_fn: is_push_pipeline
        """))

        config = load_config(str(tmp_path))
        assert config.get_template("push").match_fns == ["is_push_pipeline"]

    def test_unknown_pipeline_matcher_raises(self, tmp_path):
        (tmp_path / "push.yaml.j2").write_text("dummy")
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(textwrap.dedent("""\
            templates:
              - name: push
                file: push.yaml.j2
                match_fn: nonexistent_matcher
        """))

        with pytest.raises(ValueError, match="unknown pipeline matcher 'nonexistent_matcher'"):
            load_config(str(tmp_path))

    def test_unknown_parent_matcher_raises(self, tmp_path):
        (tmp_path / "push.yaml.j2").write_text("dummy")
        (tmp_path / "pr.yaml.j2").write_text("dummy")
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(textwrap.dedent("""\
            templates:
              - name: push
                file: push.yaml.j2
                match_fn: is_push_pipeline
              - name: pr
                file: pr.yaml.j2
                match_fn: is_pr_pipeline
                parent:
                  template: push
                  match_fn: nonexistent_parent_fn
        """))

        with pytest.raises(ValueError, match="unknown parent matcher 'nonexistent_parent_fn'"):
            load_config(str(tmp_path))

    def test_missing_parent_template_raises(self, tmp_path):
        (tmp_path / "pr.yaml.j2").write_text("dummy")
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(textwrap.dedent("""\
            templates:
              - name: pr
                file: pr.yaml.j2
                match_fn: is_pr_pipeline
                parent:
                  template: nonexistent
                  match_fn: exact_component_match
        """))

        with pytest.raises(ValueError, match="parent references unknown template 'nonexistent'"):
            load_config(str(tmp_path))

    def test_missing_template_file_raises(self, tmp_path):
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(textwrap.dedent("""\
            templates:
              - name: push
                file: missing.yaml.j2
                match_fn: is_push_pipeline
        """))

        with pytest.raises(ValueError, match="file not found"):
            load_config(str(tmp_path))

    def test_missing_templates_key_raises(self, tmp_path):
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text("foo: bar\n")

        with pytest.raises(ValueError, match="must contain a 'templates' key"):
            load_config(str(tmp_path))

    def test_missing_required_fields_raises(self, tmp_path):
        (tmp_path / "push.yaml.j2").write_text("dummy")
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(textwrap.dedent("""\
            templates:
              - name: push
                file: push.yaml.j2
        """))

        with pytest.raises(ValueError, match="must have 'name', 'file', and 'match_fn'"):
            load_config(str(tmp_path))


@pytest.fixture
def directory_overlay_config(tmp_path):
    (tmp_path / "overlay").mkdir()
    (tmp_path / "base.yaml.j2").write_text("kind: PipelineRun\n")
    base = (
        "templates:\n  - name: all\n    file: base.yaml.j2\n"
        "    match_fn: is_pipeline_run\n"
    )
    def configure(mapping):
        (tmp_path / "config.yaml").write_text(base + "template_overlays: " + mapping + "\n")
        return load_config(str(tmp_path))
    return configure


def test_directory_overlay_mapping_loads(directory_overlay_config):
    config = directory_overlay_config("[{pipeline_dir: sources/project, template_dir: overlay}]")
    assert config.template_overlays == ({"pipeline_dir": "sources/project", "template_dir": "overlay"},)


@pytest.mark.parametrize("mapping", [
    "null", "{}", "[{}]", "[{pipeline_dir: src, template_dir: overlay, extra: x}]",
    "[{pipeline_dir: /src, template_dir: overlay}]",
    "[{pipeline_dir: src/../other, template_dir: overlay}]",
    "[{pipeline_dir: src//other, template_dir: overlay}]",
    "[{pipeline_dir: src, template_dir: missing}]",
    "[{pipeline_dir: src, template_dir: overlay}, {pipeline_dir: src/child, template_dir: overlay}]",
    "[{pipeline_dir: src, template_dir: overlay}, {pipeline_dir: src, template_dir: overlay}]",
    "[{pipeline_dir: 42, template_dir: overlay}]",
    "[{pipeline_dir: src, template_dir: /overlay}]",
    "[{pipeline_dir: src, template_dir: ../overlay}]",
    "[{pipeline_dir: src, template_dir: 'nested\\overlay'}]",
    "[{pipeline_dir: '.', template_dir: overlay}]",
])
def test_invalid_directory_overlay_mapping(directory_overlay_config, mapping):
    with pytest.raises(ValueError, match="template_overlays"):
        directory_overlay_config(mapping)


def test_directory_overlay_mapping_symlink_rejected(tmp_path, directory_overlay_config):
    (tmp_path / "linked").symlink_to(tmp_path / "overlay", target_is_directory=True)
    with pytest.raises(ValueError, match="template_overlays.*non-symlink"):
        directory_overlay_config("[{pipeline_dir: sources/project, template_dir: linked}]")
