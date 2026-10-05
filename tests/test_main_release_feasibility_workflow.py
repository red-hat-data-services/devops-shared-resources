from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.prepare_main_release_dry_run_inputs import ConfigLoader
from tests.conftest import run

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/main-release-feasibility.yml"
SCRIPT = WORKFLOW.parents[2] / "scripts/prepare_main_release_dry_run_inputs.py"


@pytest.fixture
def workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def _step(workflow, job, step_id):
    return next(step for step in workflow["jobs"][job]["steps"] if step.get("id") == step_id)


def _select(tmp_path, mappings, releases, repository="red-hat-data-services/component"):
    config = tmp_path / "supplied config files"
    config.mkdir()
    source_map = config / "component-map.yml"
    releases_file = config / "active-releases.yml"
    source_map.write_text(yaml.safe_dump({"git": mappings}))
    releases_file.write_text(yaml.safe_dump({"releases": releases}))
    output = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), repository, str(source_map), str(releases_file)],
        cwd=tmp_path, env={**os.environ, "GITHUB_OUTPUT": str(output)},
        capture_output=True, text=True, check=False,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return result, values


def _mapping(**fields):
    return {
        "repo-url": "https://github.com/red-hat-data-services/component.git",
        "automerge": "yes", **fields,
    }


def test_prepares_all_non_secret_action_inputs_from_supplied_configs(tmp_path):
    result, values = _select(tmp_path, [
        _mapping(**{"src-branch": "main", "ignore-files": ".tekton/*, build/**"}),
    ], ["rhoai-3.7-ea.1", "rhoai-2.16", "rhoai-3.6", "rhoai-3.6"])
    assert result.returncode == 0, result.stderr
    assert values["check"] == "true"
    rows = json.loads(values["matrix"])["include"]
    assert [row["downstream_branch"] for row in rows] == ["rhoai-3.6", "rhoai-3.7-ea.1"]
    for row in rows:
        assert row == {
            "upstream_repo": "https://github.com/red-hat-data-services/component.git",
            "upstream_branch": "main",
            "downstream_repo": "https://github.com/red-hat-data-services/component.git",
            "downstream_branch": row["downstream_branch"],
            "ignore_files": ".tekton/*, build/**, .github/renovate.json, .tekton/README.md",
            "merge_args": "--no-edit", "spawn_logs": "false", "dry_run": "true",
        }


def test_repo_matching_is_case_insensitive_and_uses_url(tmp_path):
    result, values = _select(tmp_path, [
        _mapping(name="mlserver", **{"repo-url": "https://github.com/red-hat-data-services/MLServer.git"}),
    ], ["rhoai-3.6"], repository="red-hat-data-services/mlserver")
    assert result.returncode == 0, result.stderr
    assert values["check"] == "true"
    assert json.loads(values["matrix"])["include"][0]["upstream_branch"] == ""


def test_preparation_works_from_a_git_worktree_directory(git_repo_factory, tmp_path):
    repo = git_repo_factory("source")
    (repo / "README.md").write_text("fixture\n")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", "Fixture"], cwd=repo)
    worktree = tmp_path / "worktree"
    run(["git", "worktree", "add", "-b", "example", str(worktree)], cwd=repo)
    assert (worktree / ".git").is_file()
    result, values = _select(worktree, [_mapping()], ["rhoai-3.6"])
    assert result.returncode == 0, result.stderr
    assert values["check"] == "true"
    assert json.loads(values["matrix"])["include"][0]["downstream_branch"] == "rhoai-3.6"


def test_config_loader_preserves_yaml12_flags_without_changing_global_loader():
    assert yaml.load("automerge: yes\n", Loader=ConfigLoader) == {"automerge": "yes"}
    assert yaml.load("automerge: no\n", Loader=ConfigLoader) == {"automerge": "no"}
    assert yaml.load("automerge: true\n", Loader=ConfigLoader) == {"automerge": True}
    assert yaml.safe_load("automerge: yes\n") == {"automerge": True}


@pytest.mark.parametrize("mappings", [[], [_mapping(), _mapping()]])
def test_missing_or_duplicate_mapping_fails(tmp_path, mappings):
    result, values = _select(tmp_path, mappings, ["rhoai-3.6"])
    assert result.returncode != 0
    assert "Expected one release source-map entry" in result.stderr
    assert values["check"] == "false"


def test_disabled_automerge_skips_even_with_manual_sync(tmp_path):
    result, values = _select(tmp_path, [
        _mapping(automerge="no", **{"manual-sync": "yes"}),
    ], ["rhoai-3.6"])
    assert result.returncode == 0
    assert values["check"] == "false"
    assert json.loads(values["matrix"])["include"] == []
    assert "automerge is disabled" in result.stderr


@pytest.mark.parametrize("releases", [[], ["rhoai-2.16"]])
def test_no_active_releases_skips(tmp_path, releases):
    result, values = _select(tmp_path, [_mapping()], releases)
    assert result.returncode == 0
    assert values["check"] == "false"
    assert json.loads(values["matrix"])["include"] == []
    assert "no active releases" in result.stderr


def test_invalid_release_configuration_fails(tmp_path):
    result, values = _select(tmp_path, [_mapping()], None)
    assert result.returncode != 0
    assert values["check"] == "false"


@pytest.mark.parametrize("automerge", [True, "invalid"])
def test_invalid_automerge_configuration_fails(tmp_path, automerge):
    result, values = _select(tmp_path, [_mapping(automerge=automerge)], ["rhoai-3.6"])
    assert result.returncode != 0
    assert values["check"] == "false"


def test_multiline_action_input_fails_before_enabling_matrix(tmp_path):
    result, values = _select(tmp_path, [
        _mapping(**{"ignore-files": ".tekton/*\ncheck=true"}),
    ], ["rhoai-3.6"])
    assert result.returncode != 0
    assert "single-line strings" in result.stderr
    assert values["check"] == "false"


@pytest.mark.parametrize("args", [[], ["scope"], ["owner/repo", "source-map.yml"]])
def test_repository_and_both_config_paths_are_required(args):
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "owner/repo source-map.yaml releases.yaml" in " ".join(result.stderr.split())


def test_workflow_only_wires_prepared_inputs_and_runtime_credentials(workflow):
    assert workflow["on"] == {
        "pull_request": {"branches": ["stable"]},
        "merge_group": {"branches": ["stable"]},
    }
    assert workflow["permissions"] == {"contents": "read"}
    setup = workflow["jobs"]["setup"]
    script = _step(workflow, "setup", "inputs")
    assert shlex.split(script["run"].replace("\\\n", "")) == [
        "uv", "run", "--python", "3.12", "--no-project", "--with-requirements",
        "shared-resources/requirements.txt", "python",
        "shared-resources/scripts/prepare_main_release_dry_run_inputs.py",
        "$COMPONENT_REPOSITORY", "infra/src/config/main-release-source-map.yaml", "infra/src/config/releases.yaml",
    ]
    assert script["env"]["COMPONENT_REPOSITORY"] == "${{ github.repository }}"
    assert all("if" not in step for step in setup["steps"])
    job = workflow["jobs"]["main-release-feasibility"]
    assert job["if"] == "needs.setup.outputs.check == 'true'"
    assert job["strategy"]["fail-fast"] == "false"
    assert job["strategy"]["matrix"] == "${{ fromJSON(needs.setup.outputs.matrix) }}"
    sync = _step(workflow, "main-release-feasibility", "sync")
    assert sync["uses"] == "red-hat-data-services/sync-git-branches@main"
    for name, value in sync["with"].items():
        assert value == ("${{ github.token }}" if name == "token" else "${{ matrix." + name + " }}")
    assert len(job["steps"]) == 1
    assert "continue-on-error" not in sync and "continue-on-error" not in job
