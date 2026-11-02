"""Opt-in generated children through the public validate-tekton APIs and CLI."""
import copy
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from lib.tekton_config import load_config
from lib.tekton_filters import to_release_pr_cel, ValidationError

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("generated_children_validator", str(ROOT / "scripts/validate-tekton"))
spec = importlib.util.spec_from_loader(loader.name, loader)
vt = importlib.util.module_from_spec(spec)
loader.exec_module(vt)
PAC = "pipelinesascode.tekton.dev/"


def write(path, data):
    with path.open("w") as stream:
        YAML().dump(data, stream)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "repo"
    directory = root / "pipelineruns/widget/.tekton"
    directory.mkdir(parents=True)
    config = tmp_path / "policy"
    config.mkdir()
    write(config / "config.yaml", {"tekton_layout": "nested", "component_aliases": {"short": "widget"}, "templates": [
        {"name": "push", "file": "push.yaml.j2", "match_fn": "is_push_pipeline_by_cel"},
        {"name": "pull-request", "file": "pull-request.yaml.j2", "match_fn": "is_pr_pipeline",
         "parent": {"template": "push", "match_fn": ["alias_match", "exact_component_match"]},
         "generate": {"filename": "{component}-pull-request.yaml", "name": "{component}-on-pull-request",
                      "git_pipeline_ref": True, "allowed_spec_fields": ["params", "pipelineRef"],
                      "preserve_annotations": [PAC + "on-comment"]}}]})
    (config / "push.yaml.j2").write_text("{}\n")
    (config / "pull-request.yaml.j2").write_text('''metadata:
  annotations:
    pipelinesascode.tekton.dev/on-comment: "{{ _ | default_comment }}"
    pipelinesascode.tekton.dev/on-cel-expression: "{{ _parent | to_release_pr_cel(branch, filename) }}"
spec:
  params:
  - name: output-image
    value: "{{ _parent | split_tag }}:on-pr-{{ '{{pull_request_number}}' }}"
  - name: additional-tags
    value: "{{ ABSENT }}"
  pipelineRef:
    params:
    - name: revision
      value: "{{ branch }}"
''')
    push = {"apiVersion": "tekton.dev/v1", "kind": "PipelineRun", "metadata": {
        "name": "widget-v3-6-on-push", "labels": {"appstudio.openshift.io/component": "widget-v3-6", "appstudio.openshift.io/application": "rhoai-v3-6"},
        "annotations": {PAC + "on-cel-expression": 'event == "push" && target_branch == "rhoai-3.6" && "src/**".pathChanged() && ".tekton/widget-v3-6-push.yaml".pathChanged()', "push-nudge": "production"}},
        "spec": {"params": [{"name": "output-image", "value": "quay.io/test/widget:release"}, {"name": "rhoai-version", "value": "3.6.0"}, {"name": "additional-tags", "value": ["release"]}], "pipelineRef": {"resolver": "git", "params": [{"name": "url", "value": "https://example.com/pipelines"}, {"name": "revision", "value": "{{target_branch}}"}, {"name": "pathInRepo", "value": "pipelines/build.yaml"}]}}}
    path = directory / "widget-v3-6-push.yaml"
    write(path, push)
    return root, directory, config, push, path


def validate(root, config, branch="rhoai-3.6"):
    return vt.validate_tekton_dir(root, branch, None, config=load_config(str(config)), layout="nested")


def fix(root, config, branch="rhoai-3.6", dry=False):
    return vt.fix_tekton_dir(root, branch, None, dry, config=load_config(str(config)), layout="nested")


def test_create_check_idempotence_push_unchanged(setup):
    root, directory, config, push, path = setup
    before = path.read_bytes()
    assert any(r["errors"] for r in validate(root, config).values())
    fixed, _ = fix(root, config, dry=True)
    assert fixed and not (directory / "widget-pull-request.yaml").exists()
    assert fix(root, config)[0]
    assert not any(r["errors"] for r in validate(root, config).values())
    assert not fix(root, config)[0]
    child = YAML(typ="safe").load((directory / "widget-pull-request.yaml").read_text())
    params = {p["name"]: p["value"] for p in child["spec"]["params"]}
    assert params["rhoai-version"] == "3.6.0"
    assert params["output-image"] == "quay.io/test/widget:on-pr-{{pull_request_number}}"
    assert "additional-tags" not in params
    assert "push-nudge" not in child["metadata"]["annotations"]
    assert path.read_bytes() == before


def test_existing_alias_filename_comment_stale_param_and_orphan(setup):
    root, directory, config, push, _ = setup
    old = copy.deepcopy(push)
    old["metadata"]["name"] = "short-on-pull-request-123"
    old["metadata"]["annotations"][PAC + "on-comment"] = "^/build-widget"
    old["spec"]["params"].append({"name": "obsolete", "value": "wrong"})
    path = directory / "legacy-pull-request.yaml"
    write(path, old)
    orphan = copy.deepcopy(old)
    orphan["metadata"]["name"] = "orphan-on-pull-request"
    orphan_path = directory / "orphan-pull-request.yaml"
    write(orphan_path, orphan)
    before = orphan_path.read_bytes()
    assert fix(root, config)[0] == [str(path)]
    assert "^/build-widget" in path.read_text() and "obsolete" not in path.read_text()
    assert validate(root, config)[str(orphan_path)]["warnings"]
    assert orphan_path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["duplicate", "bad-ref", "bad-spec", "collision", "bad-cel", "symlink"])
def test_failures_write_nothing(setup, mutation):
    root, directory, config, push, path = setup
    if mutation == "duplicate":
        write(directory / "duplicate.yaml", push)
    elif mutation == "bad-ref":
        push["spec"]["pipelineRef"]["resolver"] = "bundles"
    elif mutation == "bad-spec":
        push["spec"]["pipelineSpec"] = {}
    elif mutation == "collision":
        (directory / "widget-pull-request.yaml").write_text("kind: ConfigMap\n")
    elif mutation == "symlink":
        (directory / "linked.yaml").symlink_to(path)
    else:
        push["metadata"]["annotations"][PAC + "on-cel-expression"] = 'event == "push" && target_branch == "rhoai-3.5"'
    write(path, push)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert any(r["errors"] for r in validate(root, config).values())
    assert not fix(root, config)[0]
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize("branch", ["rhoai-3.6", "rhoai-3.6-ea.2"])
def test_release_cel_preserves_all_paths_guards(branch):
    expression = f'event == "push" && target_branch == "{branch}" && "a".pathChanged() && "b".pathChanged() && !("skip".pathChanged()) && ".tekton/old.yaml".pathChanged()'
    result = to_release_pr_cel(expression, branch, "new.yaml")
    assert f'target_branch == "{branch}"' in result
    assert all(p in result for p in ['"a"', '"b"', '"skip"', '.tekton/new.yaml'])
    assert 'event == "pull_request"' in result


@pytest.mark.parametrize("expression", ['event == "push" || target_branch == "rhoai-3.6"', 'event == "push" && target_branch.matches(".*")'])
def test_unsupported_cel(expression):
    with pytest.raises(ValidationError):
        to_release_pr_cel(expression, "rhoai-3.6", "x.yaml")


def test_public_cli_fix_then_validate_and_drift(setup):
    root, directory, config, _, _ = setup
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    command = [sys.executable, str(ROOT / "scripts/validate-tekton"), "--path", str(root),
               "--config-dir", str(config), "--branch", "rhoai-3.6", "--no-checkout", "--json"]
    before = subprocess.run(command, capture_output=True, text=True)
    assert before.returncode == 1
    fixed = subprocess.run(command + ["--fix"], capture_output=True, text=True)
    assert fixed.returncode == 0, fixed.stderr
    assert json.loads(fixed.stdout)["fixed"]
    checked = subprocess.run(command, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    child = directory / "widget-pull-request.yaml"
    data = YAML(typ="safe").load(child.read_text())
    data["spec"]["params"].append({"name": "stale", "value": "old"})
    write(child, data)
    assert subprocess.run(command, capture_output=True).returncode == 1


@pytest.mark.parametrize("old_path", ['".tekton/old.yml"', "'.tekton/old.yaml'"])
def test_release_cel_yaml_extensions_and_quotes(old_path):
    result = to_release_pr_cel(f'event == "push" && target_branch == "rhoai-3.6" && {old_path}.pathChanged()', "rhoai-3.6", "new.yml")
    assert ".tekton/new.yml" in result and "old." not in result


def test_pipeline_directory_scope(setup):
    root, directory, config, push, _ = setup
    other = root / "pipelineruns/other/.tekton"
    other.mkdir(parents=True)
    (other / "broken.yaml").write_text("[invalid yaml")
    command = [sys.executable, str(ROOT / "scripts/validate-tekton"), "--path", str(root),
               "--config-dir", str(config), "--branch", "rhoai-3.6", "--no-checkout", "--fix"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert subprocess.run(command, capture_output=True).returncode == 1
    scoped = subprocess.run(command + ["--pipeline-dir", "pipelineruns/widget"], capture_output=True, text=True)
    assert scoped.returncode == 0, scoped.stderr
    assert (directory / "widget-pull-request.yaml").exists()
    assert not (other / "widget-pull-request.yaml").exists()


def test_generate_requires_parent_and_filename(setup):
    _, _, config, _, _ = setup
    data = YAML(typ="safe").load((config / "config.yaml").read_text())
    del data["templates"][1]["parent"]
    write(config / "config.yaml", data)
    with pytest.raises(ValueError, match="generate requires a parent"):
        load_config(str(config))
    data["templates"][1]["parent"] = {"template": "push", "match_fn": "exact_component_match"}
    del data["templates"][1]["generate"]["filename"]
    write(config / "config.yaml", data)
    with pytest.raises(ValueError, match="generate requires filename"):
        load_config(str(config))
