"""Behavior coverage for condition stripping and repository PR overlays."""
from pathlib import Path

import celpy
import pytest
from ruamel.yaml import YAML

from lib.tekton import pipeline_repository, render_and_merge, resolve_overlay
from lib.tekton_filters import ValidationError, strip_release_push_selectors

ROOT = Path(__file__).resolve().parents[1]
PARAMS = "manifests/rhoai/base/params-latest.env"


def test_strip_preserves_all_conditions_and_rewrites_filename():
    result = strip_release_push_selectors(
        'event == "push" && target_branch == "rhoai-3.7" && !skip_build'
        ' && files.all.exists(p, p != "ignored")'
        ' && ("src/**".pathChanged() || ".tekton/push.yaml".pathChanged())',
        "pr.yaml",
    )
    assert "event" not in result and "target_branch" not in result
    assert "! skip_build" in result and "files.all.exists" in result
    assert '"src/**".pathChanged()' in result
    assert '".tekton/pr.yaml".pathChanged()' in result
    celpy.Environment().compile(result)


def test_strip_pathless():
    assert strip_release_push_selectors('event == "push"') == "true"


@pytest.mark.parametrize("expression", ["", "&&&", 'target_branch == "release"', 'event == "push" || x'])
def test_strip_rejects_invalid_input(expression):
    with pytest.raises(ValidationError):
        strip_release_push_selectors(expression)


def test_repository_overlay_precedence_and_scope(tmp_path):
    for selector in ["notebooks", "component", "component-v3-7"]:
        folder = tmp_path / selector
        folder.mkdir()
        (folder / "pr.yaml.j2").write_text("metadata: {}\n")
    assert resolve_overlay(tmp_path, "pr.yaml.j2", None, None, "notebooks") == str(tmp_path / "notebooks/pr.yaml.j2")
    assert resolve_overlay(tmp_path, "pr.yaml.j2", "component", None, "notebooks") == str(tmp_path / "component/pr.yaml.j2")
    assert resolve_overlay(tmp_path, "pr.yaml.j2", "component", "component-v3-7", "notebooks") == str(tmp_path / "component-v3-7/pr.yaml.j2")
    assert pipeline_repository("pipelineruns/notebooks/.tekton/pr.yaml", "nested") == "notebooks"
    assert pipeline_repository("pipelineruns/notebooks/.tekton/pr.yaml", "flat") is None
    assert pipeline_repository("unrelated/notebooks/.tekton/pr.yaml", "nested") is None
    with pytest.raises(ValueError, match="unsafe overlay repository"):
        resolve_overlay(tmp_path, "pr.yaml.j2", "component", None, "../escape")
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes trusted"):
        resolve_overlay(tmp_path, "pr.yaml.j2", "component", None, "escape")


@pytest.mark.parametrize("source,target,event,changed,expected", [
    ("main", "stable", "pull_request", [PARAMS], True),
    ("main", "stable", "pull_request", ["unrelated"], False),
    ("feature", "stable", "pull_request", [PARAMS], False),
    ("main", "other", "pull_request", [PARAMS], False),
    ("feature", "stable", "pull_request", ["src/**", PARAMS], True),
    ("feature", "other", "pull_request", ["src/**"], True),
    ("main", "stable", "push", ["src/**", PARAMS], False),
])
def test_notebooks_rendered_trigger(source, target, event, changed, expected):
    yaml = YAML()
    parent = yaml.load((ROOT / "tests/fixtures/tekton/valid-push.yaml").read_text())
    child = yaml.load((ROOT / "tests/fixtures/tekton/valid-pull-request.yaml").read_text())
    key = "pipelinesascode.tekton.dev/on-cel-expression"
    parent["metadata"]["annotations"][key] = (
        'event == "push" && target_branch == "rhoai-3.7"'
        f' && !("{PARAMS}".pathChanged())'
        ' && ("src/**".pathChanged() || ".tekton/push.yaml".pathChanged())'
    )
    config = ROOT / "tekton-templates/kc-migration"
    overlay = resolve_overlay(config, "pull-request.yaml.j2", None, None, "notebooks")
    result = render_and_merge(str(config), "pull-request.yaml.j2", child, "pr.yaml", "main",
                              parent_data=parent, mode="fix", errors=[], overlay_path=overlay)
    expression = str(result["metadata"]["annotations"][key])
    env = celpy.Environment()
    program = env.program(env.compile(expression), functions={
        "pathChanged": lambda path: celpy.celtypes.BoolType(str(path) in changed),
    })
    assert bool(program.evaluate(celpy.json_to_cel({
        "event": event, "source_branch": source, "target_branch": target,
        "files": {"all": changed},
    }))) == expected

@pytest.mark.parametrize("event", ["push", "pull_request", "other"])
def test_pathless_template_matches_legacy_semantics(event):
    from lib.tekton_filters import release_push_to_main_pr_cel

    yaml = YAML()
    parent = yaml.load((ROOT / "tests/fixtures/tekton/valid-push.yaml").read_text())
    child = yaml.load((ROOT / "tests/fixtures/tekton/valid-pull-request.yaml").read_text())
    key = "pipelinesascode.tekton.dev/on-cel-expression"
    expression = 'event == "push" && target_branch == "rhoai-3.7"'
    parent["metadata"]["annotations"][key] = expression
    result = render_and_merge(str(ROOT / "tekton-templates/kc-migration"),
                              "pull-request.yaml.j2", child, "pr.yaml", "main",
                              parent_data=parent, mode="fix", errors=[])
    actual = str(result["metadata"]["annotations"][key])
    previous = release_push_to_main_pr_cel(expression, "pr.yaml")
    assert previous == 'event == "pull_request"'
    env = celpy.Environment()
    evaluate = lambda text: bool(env.program(env.compile(text)).evaluate(
        {"event": celpy.celtypes.StringType(event)}))
    assert evaluate(actual) == evaluate(previous) == (event == "pull_request")
