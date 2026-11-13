"""Behavior coverage for condition stripping and repository PR overlays."""
from pathlib import Path

import celpy
import pytest
from ruamel.yaml import YAML

from lib.tekton import render_and_merge, resolve_overlay
from lib.tekton import ABSENT
from lib.tekton_filters import ValidationError, ensure_cel_selectors, ensure_cel_tekton_filename

ROOT = Path(__file__).resolve().parents[1]
PARAMS = "manifests/rhoai/base/params-latest.env"


def test_strip_preserves_all_conditions_and_rewrites_filename():
    result = ensure_cel_selectors(
        'event == "push" && target_branch == "rhoai-3.7" && !skip_build'
        ' && files.all.exists(p, p != "ignored")'
        ' && ("src/**".pathChanged() || ".tekton/push.yaml".pathChanged())',
        {"event": ABSENT, "target_branch": ABSENT},
    )
    result = ensure_cel_tekton_filename(result, "pr.yaml")
    assert "event" not in result and "target_branch" not in result
    assert "! skip_build" in result and "files.all.exists" in result
    assert '"src/**".pathChanged()' in result
    assert '".tekton/pr.yaml".pathChanged()' in result
    celpy.Environment().compile(result)


def test_strip_pathless():
    assert ensure_cel_selectors('event == "push"', {"event": ABSENT, "target_branch": ABSENT}) == "true"


@pytest.mark.parametrize("expression", ["", "&&&", 'event == "push" || x'])
def test_strip_rejects_invalid_input(expression):
    with pytest.raises(ValidationError):
        ensure_cel_selectors(expression, {"event": ABSENT, "target_branch": ABSENT})


def test_repository_overlay_precedence_and_scope(tmp_path):
    for selector in ["notebooks", "component", "component-v3-7"]:
        folder = tmp_path / selector
        folder.mkdir()
        (folder / "pr.yaml.j2").write_text("metadata: {}\n")
    options = dict(repository_root=tmp_path, pipeline_path=tmp_path / "sources/project/pr.yaml",
                   mappings=[dict(pipeline_dir="sources/project", template_dir="notebooks")])
    assert resolve_overlay(tmp_path, "pr.yaml.j2", None, None, **options) == str(tmp_path / "notebooks/pr.yaml.j2")
    assert resolve_overlay(tmp_path, "pr.yaml.j2", "component", None, **options) == str(tmp_path / "component/pr.yaml.j2")
    assert resolve_overlay(tmp_path, "pr.yaml.j2", "component", "component-v3-7", **options) == str(tmp_path / "component-v3-7/pr.yaml.j2")
    assert resolve_overlay(tmp_path, "pr.yaml.j2", None, None) is None
    options["pipeline_path"] = tmp_path / "sources/project-other/pr.yaml"
    assert resolve_overlay(tmp_path, "pr.yaml.j2", None, None, **options) is None
    options["pipeline_path"] = tmp_path.parent / "escape.yaml"
    with pytest.raises(ValueError, match="escapes repository"):
        resolve_overlay(tmp_path, "pr.yaml.j2", "component", None, **options)



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
    overlay = resolve_overlay(config, "pull-request.yaml.j2", None, None, repository_root=ROOT, pipeline_path=ROOT / "pipelineruns/notebooks/.tekton/pr.yaml", mappings=[dict(pipeline_dir="pipelineruns/notebooks", template_dir="notebooks")])
    result = render_and_merge(str(config), "pull-request.yaml.j2", child, "pr.yaml", "main",
                              parent_data=parent, mode="fix", errors=[], overlay_path=overlay)
    expression = str(result["metadata"]["annotations"][key])
    assert expression.startswith('event == "pull_request"')
    assert "\n    && (" in expression
    from io import StringIO
    output = StringIO()
    yaml.dump(result, output)
    assert "on-cel-expression: |-" in output.getvalue()
    assert "on-cel-expression: |2-" not in output.getvalue()
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
    assert actual == previous
    env = celpy.Environment()
    evaluate = lambda text: bool(env.program(env.compile(text)).evaluate(
        {"event": celpy.celtypes.StringType(event)}))
    assert evaluate(actual) == evaluate(previous) == (event == "pull_request")

@pytest.mark.parametrize("conditions", [
    '"src/**".pathChanged()',
    '(files.all.exists(p, !p.matches(\'^\\\\\\\\.tekton/\')) || ".tekton/push.yaml".pathChanged())',
    '!skip_build && ("src/**".pathChanged() || ".tekton/push.yaml".pathChanged())',
])
def test_default_template_preserves_legacy_cel_semantics(conditions):
    from lib.tekton_filters import release_push_to_main_pr_cel

    yaml = YAML()
    parent = yaml.load((ROOT / "tests/fixtures/tekton/valid-push.yaml").read_text())
    child = yaml.load((ROOT / "tests/fixtures/tekton/valid-pull-request.yaml").read_text())
    key = "pipelinesascode.tekton.dev/on-cel-expression"
    expression = 'event == "push" && target_branch == "rhoai-3.7" && ' + conditions
    parent["metadata"]["annotations"][key] = expression
    result = render_and_merge(str(ROOT / "tekton-templates/kc-migration"),
                              "pull-request.yaml.j2", child, "pr.yaml", "main",
                              parent_data=parent, mode="fix", errors=[])
    actual = str(result["metadata"]["annotations"][key])
    previous = release_push_to_main_pr_cel(expression, "pr.yaml")
    env = celpy.Environment()
    assert env.compile(actual) == env.compile(previous)
    if "!p.matches" in conditions:
        assert "!p.matches" in actual
