from __future__ import annotations

import pytest
import yaml

from scripts.check_main_release_feasibility import main
from tests.conftest import run


@pytest.fixture
def cli_args(tmp_path, git_repo_factory):
    repo = git_repo_factory("component")
    (repo / "README.md").write_text("candidate\n")
    run(["git", "add", "README.md"], cwd=repo)
    run(["git", "commit", "-m", "Initial candidate"], cwd=repo)
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    source_map = tmp_path / "source-map.yaml"
    source_map.write_text(yaml.safe_dump({"git": [{
        "name": "component",
        "repo-url": "https://github.com/red-hat-data-services/component.git",
        "automerge": "yes",
    }]}))
    releases = tmp_path / "releases.yaml"
    releases.write_text("releases:\n- rhoai-3.6\n")
    summary = tmp_path / "summary.md"
    args = [
        "--repo-path", str(repo),
        "--repository", "red-hat-data-services/component",
        "--source-map", str(source_map),
        "--releases", str(releases),
        "--summary", str(summary),
    ]
    return args, repo, source_map, releases, summary


def test_cli_passes_and_reports_revisions(cli_args, capsys) -> None:
    args, repo, _, _, summary = cli_args
    assert main(args) == 0
    candidate = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    report = summary.read_text()
    assert candidate in report
    assert "unknown (local files)" in report
    assert "rhoai-3.6" in report
    assert "**PASSED**" in report
    assert "Already up to date" in capsys.readouterr().out


def test_cli_returns_failure_and_lists_conflicting_paths(cli_args) -> None:
    args, repo, _, _, summary = cli_args
    (repo / "README.md").write_text("main changes\n")
    run(["git", "commit", "-am", "Main changes"], cwd=repo)
    candidate = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    (repo / "README.md").write_text("release changes\n")
    run(["git", "commit", "-am", "Release changes"], cwd=repo)
    assert main([*args, "--source-ref", candidate]) == 1
    assert "README.md" in summary.read_text()
    assert "**FAILED" in summary.read_text()


@pytest.mark.parametrize("skip", ["disabled", "no-releases", "legacy-only"])
def test_cli_explicit_skip_does_not_require_a_component_checkout(cli_args, skip) -> None:
    args, _, source_map, releases, summary = cli_args
    if skip == "disabled":
        payload = yaml.safe_load(source_map.read_text())
        payload["git"][0]["automerge"] = "no"
        source_map.write_text(yaml.safe_dump(payload))
    else:
        releases.write_text("releases: []\n" if skip == "no-releases" else "releases: [rhoai-2.16]\n")
    assert main([*args, "--repo-path", "/nonexistent/component"]) == 0
    assert "Not applicable" in summary.read_text()


def test_cli_reports_configuration_failure(cli_args, capsys) -> None:
    args, _, source_map, _, summary = cli_args
    source_map.write_text("git: []\n")
    assert main(args) == 1
    assert "found 0" in summary.read_text()
    assert "ERROR" in capsys.readouterr().err


def test_cli_reports_bad_candidate_ref(cli_args) -> None:
    args, _, _, _, summary = cli_args
    assert main([*args, "--source-ref", "nonexistent"]) == 1
    assert "**FAILED" in summary.read_text()


def test_cli_reports_missing_release(cli_args) -> None:
    args, _, _, releases, summary = cli_args
    releases.write_text("releases: [rhoai-3.7]\n")
    assert main(args) == 1
    assert "missing" in summary.read_text()


def test_cli_defaults_to_actions_summary_and_appends(cli_args, monkeypatch) -> None:
    args, _, _, _, summary = cli_args
    summary.write_text("Earlier step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(args[:-2]) == 0
    assert summary.read_text().startswith("Earlier step\n## Main-to-release feasibility")


def test_cli_records_actual_infra_revision(cli_args, git_repo_factory) -> None:
    args, _, source_map, releases, summary = cli_args
    infra = git_repo_factory("infra")
    (infra / "source-map.yaml").write_text(source_map.read_text())
    (infra / "releases.yaml").write_text(releases.read_text())
    run(["git", "add", "."], cwd=infra)
    run(["git", "commit", "-m", "Infra configuration"], cwd=infra)
    config_sha = run(["git", "rev-parse", "HEAD"], cwd=infra).stdout.strip()
    assert main([
        *args, "--source-map", str(infra / "source-map.yaml"),
        "--releases", str(infra / "releases.yaml"),
    ]) == 0
    assert config_sha in summary.read_text()


def test_cli_escapes_conflicting_path_in_report(cli_args) -> None:
    args, repo, _, _, summary = cli_args
    # Reproduces a real conflict with characters significant to Markdown/HTML.
    path = "<build|config>.yaml"
    run(["git", "checkout", "main"], cwd=repo)
    (repo / path).write_text("main\n")
    run(["git", "add", "--", path], cwd=repo)
    run(["git", "commit", "-m", "Candidate file"], cwd=repo)
    candidate = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    (repo / path).write_text("release\n")
    run(["git", "add", "--", path], cwd=repo)
    run(["git", "commit", "-m", "Release file"], cwd=repo)
    assert main([*args, "--source-ref", candidate]) == 1
    assert "&lt;build&#124;config&gt;.yaml" in summary.read_text()
