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
    """Write one YAML fixture while preserving ordinary mapping/list structure."""
    with path.open("w") as stream:
        YAML().dump(data, stream)


@pytest.fixture
def setup(tmp_path):
    """Build an isolated release checkout and generated-child template policy."""
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
    """Validate the fixture through the public validator without writing files."""
    return vt.validate_tekton_dir(root, branch, None, config=load_config(str(config)), layout="nested")


def fix(root, config, branch="rhoai-3.6", dry=False):
    """Run the public validator's generated-child fix or dry-run path."""
    return vt.fix_tekton_dir(root, branch, None, dry, config=load_config(str(config)), layout="nested")


def test_create_check_idempotence_push_unchanged(setup):
    """Create a missing child, validate it, and prove idempotence and read-only parents."""
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
    """Reconcile an alias match while retaining its filename/comment and orphan files."""
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
    """Reject unsafe or ambiguous input before any PipelineRun is changed."""
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
    """Convert GA/EA triggers without losing path filters or negative guards."""
    expression = f'event == "push" && target_branch == "{branch}" && "a".pathChanged() && "b".pathChanged() && !("skip".pathChanged()) && ".tekton/old.yaml".pathChanged()'
    result = to_release_pr_cel(expression, branch, "new.yaml")
    assert f'target_branch == "{branch}"' in result
    assert all(p in result for p in ['"a"', '"b"', '"skip"', '.tekton/new.yaml'])
    assert 'event == "pull_request"' in result


@pytest.mark.parametrize("expression", ['event == "push" || target_branch == "rhoai-3.6"', 'event == "push" && target_branch.matches(".*")'])
def test_unsupported_cel(expression):
    """Reject CEL structures that cannot be converted safely."""
    with pytest.raises(ValidationError):
        to_release_pr_cel(expression, "rhoai-3.6", "x.yaml")


def test_public_cli_fix_then_validate_and_drift(setup):
    """Exercise real CLI creation, independent validation, and stale-param detection."""
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
    """Rewrite both YAML extensions and quote styles in PipelineRun path literals."""
    result = to_release_pr_cel(f'event == "push" && target_branch == "rhoai-3.6" && {old_path}.pathChanged()', "rhoai-3.6", "new.yml")
    assert ".tekton/new.yml" in result and "old." not in result


def test_pipeline_directory_scope(setup):
    """Keep invalid files outside the selected repository from affecting its run."""
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
    """Require a valid parent relationship and a generated filename pattern."""
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


@pytest.mark.parametrize("policy", [True, False, None, "enabled", [], ["filename", "name"]])
def test_generate_rejects_nonmapping_values(setup, policy):
    """Reject non-mappings, including falsey values, before policy conversion."""
    _, _, config, _, _ = setup
    data = YAML(typ="safe").load((config / "config.yaml").read_text())
    data["templates"][1]["generate"] = policy
    write(config / "config.yaml", data)
    with pytest.raises(ValueError, match="generate must be a mapping"):
        load_config(str(config))


@pytest.mark.parametrize("overrides, message", [
    ({"allowed_spec_field": ["params"]}, "unknown keys"),
    ({"filename": ""}, "requires filename"),
    ({"name": "  "}, "requires name"),
    ({"inherit_metadata": "labels"}, "list of nonempty strings"),
    ({"inherit_metadata": ["name"]}, "inherit_metadata must not include 'name'"),
    ({"inherit_metadata": ["labels", "name", "namespace"]}, "inherit_metadata must not include 'name'"),
    ({"preserve_annotations": [123]}, "list of nonempty strings"),
    ({"allowed_spec_fields": None}, "list of nonempty strings"),
    ({"git_pipeline_ref": "false"}, "must be a boolean"),
    ({"required_parent_fields": "metadata.name"}, "string key paths"),
    ({"required_parent_fields": [[]]}, "string key paths"),
    ({"required_parent_fields": [["metadata", 123]]}, "string key paths"),
])
def test_generate_rejects_unknown_keys_and_wrong_types(setup, overrides, message):
    """Misspelled or malformed guard settings must not silently weaken policy."""
    _, directory, config, _, _ = setup
    data = YAML(typ="safe").load((config / "config.yaml").read_text())
    data["templates"][1]["generate"].update(overrides)
    write(config / "config.yaml", data)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(ValueError, match=message):
        fix(setup[0], config)
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}


def test_generate_allows_other_metadata_without_changing_child_name(setup):
    """Inherit release labels/namespace without replacing the configured child name."""
    root, directory, config, push, path = setup
    push["metadata"]["namespace"] = "release-tenant"
    write(path, push)
    data = YAML(typ="safe").load((config / "config.yaml").read_text())
    policy = data["templates"][1]["generate"]
    policy["inherit_metadata"] = ["labels", "namespace"]
    # 'name' is reserved only for inherited metadata, not these string lists.
    policy["preserve_annotations"] = ["name"]
    policy["allowed_spec_fields"].append("name")
    write(config / "config.yaml", data)
    assert fix(root, config)[0]
    child = YAML(typ="safe").load((directory / "widget-pull-request.yaml").read_text())
    assert child["metadata"]["name"] == "widget-on-pull-request"
    assert child["metadata"]["labels"] == push["metadata"]["labels"]
    assert child["metadata"]["namespace"] == "release-tenant"


@pytest.mark.parametrize("clause, expected", [
    ('".tekton/target_branch.yaml".pathChanged()', '".tekton/new.yaml".pathChanged()'),
    ('".tekton/event.yml".pathChanged()', '".tekton/new.yaml".pathChanged()'),
    ('has_path == ".tekton/old.yaml"', 'has_path == ".tekton/old.yaml"'),
    ('".tekton/old.yaml".matches(".*")', '".tekton/old.yaml".matches(".*")'),
    ('kind == "event"', 'kind == "event"'),
    ('kind == "target_branch"', 'kind == "target_branch"'),
    ('!(".tekton/old.yaml".pathChanged())', '.tekton/new.yaml'),
])
def test_release_cel_only_rewrites_pathchanged_receivers(clause, expected):
    """Leave non-path literals intact and never mistake string contents for selectors."""
    result = to_release_pr_cel(
        f'event == "push" && target_branch == "rhoai-3.6" && {clause}',
        "rhoai-3.6", "new.yaml")
    assert expected in " ".join(result.split())


@pytest.mark.parametrize("clause", [
    'event != "push"', 'event == "pull_request"',
    'target_branch == "rhoai-3.5"',
    '(target_branch == "rhoai-3.6" || allowed)',
    'event.startsWith("push")', '"push" == event',
])
def test_release_cel_rejects_complex_or_conflicting_selectors(clause):
    """Continue to reject selector references outside the supported exact equalities."""
    with pytest.raises(ValidationError):
        to_release_pr_cel(
            f'event == "push" && target_branch == "rhoai-3.6" && {clause}',
            "rhoai-3.6", "new.yaml")


@pytest.mark.parametrize("state", ["matching", "missing", "orphan", "planning-error"])
def test_mixed_generation_always_validates_ordinary_templates(setup, state):
    """Combine ordinary errors with child reconciliation, including failed planning."""
    root, directory, config, push, parent_path = setup
    (config / "push.yaml.j2").write_text("metadata:\n  namespace: required-namespace\n")
    before = parent_path.read_bytes()
    if state != "missing":
        fixed, post_fix = fix(root, config)
        assert fixed and parent_path.read_bytes() == before
        in_memory = vt.validate_tekton_dir(
            root, "rhoai-3.6", None, config=load_config(str(config)), pipelines_data=post_fix)
        assert in_memory[str(parent_path)]["errors"] == ["metadata.namespace: missing"]
    if state == "orphan":
        orphan = copy.deepcopy(push)
        orphan["metadata"]["name"] = "orphan-on-pull-request"
        write(directory / "orphan-pull-request.yaml", orphan)
    elif state == "planning-error":
        push["metadata"]["annotations"][PAC + "on-cel-expression"] = 'event == "push" && target_branch == "rhoai-3.5"'
        write(parent_path, push)
    results = validate(root, config)
    assert results[str(parent_path)]["errors"] == ["metadata.namespace: missing"]
    if state == "missing":
        assert results[str(directory / "widget-pull-request.yaml")]["errors"]
    elif state == "orphan":
        assert results[str(directory / "orphan-pull-request.yaml")]["warnings"]
    elif state == "planning-error":
        assert results["generation"]["errors"]
    else:
        assert not results[str(directory / "widget-pull-request.yaml")]["errors"]


def test_mixed_generation_ordinary_results_keep_directory_identity(setup):
    """Ordinary files with the same basename must not overwrite each other's errors."""
    root, directory, config, push, parent_path = setup
    (config / "push.yaml.j2").write_text("metadata:\n  namespace: required-namespace\n")
    other = root / "pipelineruns/other/.tekton" / parent_path.name
    other.parent.mkdir(parents=True)
    push["metadata"]["namespace"] = "required-namespace"
    write(other, push)
    fix(root, config)
    results = validate(root, config)
    assert results[str(parent_path)]["errors"] == ["metadata.namespace: missing"]
    assert not results[str(other)]["errors"]


def test_mixed_generation_cli_reports_ordinary_errors_without_fixing_parents(setup):
    """Real --fix and subsequent validation both fail on read-only ordinary errors."""
    root, directory, config, _, parent_path = setup
    (config / "push.yaml.j2").write_text("metadata:\n  namespace: required-namespace\n")
    before = parent_path.read_bytes()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    command = [sys.executable, str(ROOT / "scripts/validate-tekton"), "--path", str(root),
               "--config-dir", str(config), "--branch", "rhoai-3.6", "--no-checkout", "--json"]
    for flags in (["--fix"], []):
        result = subprocess.run(command + flags, capture_output=True, text=True)
        assert result.returncode == 1, result.stderr
        assert json.loads(result.stdout)["files"][str(parent_path)]["errors"] == ["metadata.namespace: missing"]
        assert parent_path.read_bytes() == before
    assert (directory / "widget-pull-request.yaml").exists()


def test_shortened_names_keep_parent_matching_and_idempotence(setup):
    """Match shortened children through unchanged labels, not their hashed name prefix."""
    root, directory, config, push, parent_path = setup
    prefix = "odh-data-science-pipelines-argo-workflowcontroller"
    push["metadata"]["name"] = prefix + "-v3-6-on-push"
    push["metadata"]["labels"]["appstudio.openshift.io/component"] = prefix + "-v3-6"
    write(parent_path, push)
    before = parent_path.read_bytes()
    data = YAML(typ="safe").load((config / "config.yaml").read_text())
    data["templates"][1]["parent"]["match_fn"].insert(0, "component_label_match")
    write(config / "config.yaml", data)
    template = config / "pull-request.yaml.j2"
    short_name_template = """metadata:
  name: "{{ metadata.name.split('-on-pull-request')[0] | shorten_name(36) }}-on-pull-request"
"""
    template.write_text(template.read_text().replace("metadata:\n", short_name_template, 1))
    assert fix(root, config)[0]
    child_path = directory / (prefix + "-pull-request.yaml")
    child = YAML(typ="safe").load(child_path.read_text())
    assert len(child["metadata"]["name"].removesuffix("-on-pull-request")) <= 36
    assert child["metadata"]["labels"] == push["metadata"]["labels"]
    assert not any(item["errors"] or item["warnings"] for item in validate(root, config).values())
    assert not fix(root, config)[0]
    assert parent_path.read_bytes() == before


@pytest.mark.parametrize("duplicate", [False, True])
def test_legacy_branch_placeholder_reuses_child_without_weakening_ambiguity(setup, duplicate):
    """Reconcile generic-label legacy PRs, but still reject duplicate children."""
    root, directory, config, push, parent_path = setup
    old = copy.deepcopy(push)
    old["metadata"]["name"] = "widget-{{target_branch}}-on-pull-request-43048"
    old["metadata"]["labels"]["appstudio.openshift.io/component"] = "pull-request-pipelines"
    child = directory / "widget-pull-request.yaml"
    write(child, old)
    if duplicate:
        write(directory / "duplicate-pull-request.yaml", old)
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        assert not fix(root, config)[0]
        assert any("ambiguous child" in error for item in validate(root, config).values() for error in item["errors"])
        assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    else:
        before = parent_path.read_bytes()
        assert fix(root, config)[0] == [str(child)]
        assert not any(item["errors"] or item["warnings"] for item in validate(root, config).values())
        assert not fix(root, config)[0]
        assert parent_path.read_bytes() == before


def test_generate_rejects_empty_mapping(setup):
    """An explicitly empty generation policy is invalid rather than disabled."""
    _, _, config, _, _ = setup
    data = YAML(typ="safe").load((config / "config.yaml").read_text())
    data["templates"][1]["generate"] = {}
    write(config / "config.yaml", data)
    with pytest.raises(ValueError, match="requires filename"):
        load_config(str(config))
