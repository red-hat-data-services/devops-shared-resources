"""Target YAML may select trusted overlays, never outside executable templates."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from lib.tekton import resolve_overlay
from lib.tekton_config import load_config


@pytest.mark.parametrize("field", ["label", "prefix"])
@pytest.mark.parametrize("selector", ["/absolute", "../outside", "nested/../../outside", "a/../b", "a\\b", "bad\nname", 42, False, ["name"], {"name": "bad"}])
def test_reject_unsafe_selector_before_fallback(tmp_path, field, selector):
    (tmp_path / "valid").mkdir()
    (tmp_path / "valid/push.yaml.j2").write_text("trusted")
    label, prefix = (selector, "valid") if field == "label" else ("valid", selector)
    with pytest.raises(ValueError, match=f"unsafe overlay component {field}"):
        resolve_overlay(tmp_path, "push.yaml.j2", prefix, label)


@pytest.mark.parametrize("field", ["label", "prefix"])
@pytest.mark.parametrize("link_kind", ["directory", "leaf", "dangling", "ancestor"])
def test_reject_symlink_escape(tmp_path, field, link_kind):
    root = tmp_path / "trusted"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "push.yaml.j2").write_text("outside must not be opened")
    if link_kind == "directory":
        (root / "component").symlink_to(outside, target_is_directory=True)
    elif link_kind == "ancestor":
        (root / "alias").symlink_to(outside, target_is_directory=True)
        (outside / "nested").mkdir()
        selector = "alias/nested"
    else:
        (root / "component").mkdir()
        target = outside / ("missing" if link_kind == "dangling" else "push.yaml.j2")
        (root / "component/push.yaml.j2").symlink_to(target)
    selector = "alias/nested" if link_kind == "ancestor" else "component"
    prefix, label = (selector, None) if field == "prefix" else (None, selector)
    with pytest.raises(ValueError, match="escapes trusted config"):
        resolve_overlay(root, "push.yaml.j2", prefix, label)


@pytest.mark.parametrize("template", ["/absolute.yaml.j2", "../outside.yaml.j2"])
def test_reject_escaping_template_name(tmp_path, template):
    with pytest.raises(ValueError, match="unsafe overlay template name"):
        resolve_overlay(tmp_path, template, "component", None)


def test_valid_nested_overlay_and_version_fallback(tmp_path):
    root = tmp_path / "trusted"
    (root / "group/component").mkdir(parents=True)
    prefix = root / "group/component/push.yaml.j2"
    prefix.write_text("prefix")
    assert resolve_overlay(root, "push.yaml.j2", "group/component", "component-v3-6") == str(prefix)
    (root / "component-v3-6").mkdir()
    version = root / "component-v3-6/push.yaml.j2"
    version.write_text("version")
    assert resolve_overlay(root, "push.yaml.j2", "group/component", "component-v3-6") == str(version)


def test_contained_symlink_and_trusted_root_alias(tmp_path):
    root = tmp_path / "trusted"
    (root / "actual").mkdir(parents=True)
    (root / "actual/push.yaml.j2").write_text("trusted")
    (root / "component").symlink_to(root / "actual", target_is_directory=True)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert resolve_overlay(alias, "push.yaml.j2", "component", None) == str(alias / "component/push.yaml.j2")


def test_relative_config_root_preserved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config/component").mkdir(parents=True)
    (tmp_path / "config/component/push.yaml.j2").write_text("trusted")
    assert resolve_overlay("config", "push.yaml.j2", "component", None) == "config/component/push.yaml.j2"


@pytest.mark.parametrize("mode", ["validate", "fix"])
@pytest.mark.parametrize("field", ["label", "prefix"])
@pytest.mark.parametrize("escape", ["absolute", "traversal", "symlink"])
def test_validator_rejects_before_any_template_read_or_render(tmp_path, monkeypatch, mode, field, escape):
    """Exercise actual CLI functions with data-derived prefix, not just the helper."""
    script = Path(__file__).resolve().parents[1] / "scripts/validate-tekton"
    loader = importlib.machinery.SourceFileLoader("overlay_validator", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    validator = importlib.util.module_from_spec(spec)
    loader.exec_module(validator)
    root = tmp_path / "config"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # Inert text only; no hostile Jinja is executed by these regression tests.
    (outside / "push.yaml.j2").write_text("outside overlay")
    (root / "push.yaml.j2").write_text("kind: PipelineRun\n")
    (root / "config.yaml").write_text("templates:\n  - name: all\n    file: push.yaml.j2\n    match_fn: is_pipeline_run\n")
    config = load_config(str(root))
    if escape == "absolute":
        selector = str(outside)
    elif escape == "traversal":
        selector = "../outside"
    else:
        (root / "escape").symlink_to(outside, target_is_directory=True)
        selector = "escape"
    data = {"apiVersion": "tekton.dev/v1", "kind": "PipelineRun", "metadata": {
        "name": selector + "-on-push" if field == "prefix" else "valid-on-push",
        "labels": {"appstudio.openshift.io/component": selector if field == "label" else "valid"}}}
    target = tmp_path / "target"
    target.mkdir()
    import json
    path = target / "valid-push.yaml"
    original = json.dumps(data)
    path.write_text(original)
    def no_render(*args, **kwargs):
        pytest.fail("render reached before unsafe overlay rejection")
    monkeypatch.setattr(validator, "render_and_merge", no_render)
    original_read = Path.read_text
    def no_template_read(path, *args, **kwargs):
        if path.suffix == ".j2":
            pytest.fail("template opened before unsafe overlay rejection")
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", no_template_read)
    with pytest.raises(ValueError, match="unsafe overlay component"):
        if mode == "validate":
            validator.validate_tekton_dir(target, "rhoai-3.6", None, config=config)
        else:
            validator.fix_tekton_dir(target, "rhoai-3.6", None, dry_run=False, config=config)
    assert original_read(path) == original
