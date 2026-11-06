"""Trusted publication policy is separate from exact caller scope and safe heads."""
import json
import subprocess

import pytest

from lib import tekton_reconciliation as operation
from lib.tekton_config import load_config
from test_tekton_reconciliation import tooling, setup, commit, git, operation_args, publication_args, publication_policy


def config(directory, patterns):
    (directory / "template.yaml.j2").write_text("spec: {}\n")
    (directory / "config.yaml").write_text('templates:\n  - name: all\n    file: template.yaml.j2\n    match_fn: is_pipeline_run\npublication_branches: ' + json.dumps(patterns) + '\n')
    return load_config(str(directory))


@pytest.mark.parametrize("patterns", [None, [], "main", {}, [""], ["*"], ["rhoai-**"], ["ma*in"], ["main", "main"], [None], [1], ["main?"], ["main[ab]"], ["../main"], ["main\n"], ["x//y"], ["main/"], ["main.lock"], ["x/.hidden"]])
def test_invalid_present_policy_never_falls_back(tmp_path, patterns):
    with pytest.raises(ValueError, match="publication_branches"):
        config(tmp_path, patterns)


@pytest.mark.parametrize("branch,allowed,accept", [
    ("main", "main", True), ("rhoai-3.6", "rhoai-3.6", True),
    ("rhoai-3.7-ea.1", "main,rhoai-3.7-ea.1", True),
    ("rhoai-", "rhoai-", False), ("main-other", "main-other", False),
    ("master", "master", False), ("feature/test", "feature/test", False),
    ("release-1", "release-1", False), ("main", None, False),
    ("rhoai-3.6", None, False), ("rhoai-3.6", "rhoai-*", False),
    ("main", "mainline", False), ("main", " main", False),
])
def test_configured_policy_and_exact_invocation_scope(tmp_path, branch, allowed, accept):
    policy = config(tmp_path, ["main", "rhoai-*"])
    if accept:
        operation.validate_publication_scope(policy, branch, allowed)
    else:
        with pytest.raises(ValueError):
            operation.validate_publication_scope(policy, branch, allowed)


@pytest.mark.parametrize("branch", ["rhoai-bad..ref", "rhoai-*", "rhoai-bad@{ref"])
def test_patterns_do_not_bypass_actual_git_ref_validation(tmp_path, branch):
    policy = config(tmp_path, ["main", "rhoai-*"])
    with pytest.raises(subprocess.CalledProcessError):
        operation.validate_publication_scope(policy, branch, branch)


def test_direct_publication_rechecks_exact_scope_after_planning(tooling, setup, tmp_path, monkeypatch):
    publication_policy(setup[0])
    monkeypatch.setattr(operation, "ROOT", tooling)
    original = operation.plan
    def planned(args, report):
        result = original(args, report)
        args.allowed_branches = "mainline"
        return result
    monkeypatch.setattr(operation, "plan", planned)
    monkeypatch.setattr(operation.publisher, "publish", lambda *args: pytest.fail("direct boundary ignored changed caller scope"))
    args = [*operation_args(setup, tmp_path / "report"), *publication_args(tmp_path),
            "--publish", "--branch", "main", "--allowed-branches", "main"]
    assert operation.main(list(map(str, args))) == 1
    assert not git(setup[1], "status", "--porcelain")


def test_absence_preserves_legacy_and_policy_is_digest_input(tmp_path):
    policy = config(tmp_path, ["main", "rhoai-*"])
    before = operation.policy_digest(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(path.read_text().split("publication_branches:")[0])
    legacy = load_config(str(tmp_path))
    assert legacy.publication_branches is None
    for branch in ("release-1", "rhoai-3.6", "feature/test"):
        operation.validate_publication_scope(legacy, branch, None)
    for branch in ("main", "master"):
        with pytest.raises(ValueError):
            operation.validate_publication_scope(legacy, branch, branch)
    with pytest.raises(ValueError, match="release not allowed"):
        operation.validate_publication_scope(legacy, "release-1", "release-2")
    assert operation.policy_digest(tmp_path) != before


@pytest.mark.parametrize("branch,allowed,configured", [
    ("rhoai-", "rhoai-", True), ("master", "master", True),
    ("feature/test", "feature/test", True), ("rhoai-3.6", None, True),
    ("rhoai-3.6", "rhoai-*", True), ("main", "main", False),
])
def test_denied_publication_stops_before_plan_even_if_no_changes(tooling, setup, tmp_path, monkeypatch, branch, allowed, configured):
    if configured:
        publication_policy(setup[0])
    monkeypatch.setattr(operation, "ROOT", tooling)
    monkeypatch.setattr(operation, "plan", lambda *args: pytest.fail("denied publication must not plan"))
    monkeypatch.setattr(operation.publisher, "publish", lambda *args: pytest.fail("denied publication must not publish"))
    args = [*operation_args(setup, tmp_path / "report"), *publication_args(tmp_path), "--publish", "--branch", branch]
    if allowed is not None:
        args += ["--allowed-branches", allowed]
    assert operation.main(list(map(str, args))) == 1
    assert not git(setup[1], "status", "--porcelain")


def test_main_deletion_still_requires_explicit_operation_authorization(tooling, setup, tmp_path, monkeypatch):
    source, target = setup
    publication_policy(source)
    (source / "templates/all.yaml.j2").write_text('kind: "{{ _ if (metadata.get(\'name\') | delete_if(\'example-on-push\')) else _ }}"\n')
    commit(source)
    monkeypatch.setattr(operation, "ROOT", tooling)
    monkeypatch.setattr(operation.publisher, "publish", lambda *args: pytest.fail("M-only main publication must not delete"))
    args = [*operation_args(setup, tmp_path / "report"), *publication_args(tmp_path),
            "--publish", "--branch", "main", "--allowed-branches", "main"]
    assert operation.main(list(map(str, args))) == 1
    assert (target / ".tekton/example.yml").exists()
    assert not git(target, "status", "--porcelain")


def test_allowed_main_nochange_still_passes_scope_without_publisher(tooling, setup, tmp_path, monkeypatch):
    source, target = setup
    publication_policy(source)
    path = target / ".tekton/example.yml"
    path.write_text(path.read_text() + '  labels: {reconciled: "yes"}\n')
    commit(target)
    monkeypatch.setattr(operation, "ROOT", tooling)
    monkeypatch.setattr(operation.publisher, "publish", lambda *args: pytest.fail("no-change result must not publish"))
    args = [*operation_args(setup, tmp_path / "report"), *publication_args(tmp_path), "--publish",
            "--branch", "main", "--allowed-branches", "main"]
    assert operation.main(list(map(str, args))) == 0


@pytest.mark.parametrize("branch", ["feature/test", "rhoai-"])
def test_preview_unaffected_by_publication_family(tooling, setup, tmp_path, branch):
    publication_policy(setup[0])
    from test_tekton_reconciliation import run
    completed = run(tooling, *operation_args(setup, tmp_path / "report"), "--branch", branch)
    assert completed.returncode == 0, completed.stderr
