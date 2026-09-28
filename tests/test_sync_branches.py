from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.config_parser import ConfigError
from scripts.sync_branches import _entry_from_args, _validate_cli_exclusivity, main, run_sync_entry
from tests.conftest import run


def _commit(repo: Path, relative_path: str, content: str, message: str) -> None:
    file_path = repo / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    run(["git", "add", relative_path], cwd=repo)
    run(["git", "commit", "-m", message], cwd=repo)


def test_validate_cli_exclusivity_rejects_mixed_flags() -> None:
    args = MagicMock(
        config="config.yaml",
        sync_type="push",
        source_repo="https://github.com/org/repo.git",
        source_branch="main",
        target_repo=None,
        target_branch="stable",
        ignore_files=["README.md"],
    )
    with pytest.raises(ConfigError, match="do not also pass"):
        _validate_cli_exclusivity(args)


def test_entry_from_args_builds_normalized_entry() -> None:
    args = MagicMock(
        sync_type="pr",
        source_repo="https://github.com/org/repo.git",
        source_branch="main",
        target_repo=None,
        target_branch="stable",
        ignore_files=["README.md"],
        pr_branch="sync-branch",
        tracking_label="lake-gate",
        labels=["automation"],
        automerge=True,
        pr_title=None,
        pr_body=None,
        reviewers=["alice"],
    )
    entry = _entry_from_args(args)
    assert entry["dest"]["url"] == "https://github.com/org/repo.git"
    assert entry["ignore_files"] == ["README.md"]
    assert entry["pr"]["tracking_label"] == "lake-gate"


def test_dry_run_does_not_touch_repos() -> None:
    entry = {
        "sync_type": "pr",
        "src": {"url": "https://github.com/org/repo.git", "branch": "main"},
        "dest": {"url": "https://github.com/org/repo.git", "branch": "stable"},
        "ignore_files": [],
        "pr": {
            "branch": None,
            "head_strategy": "sync-branch",
            "tracking_label": None,
            "labels": [],
            "automerge": False,
            "title": None,
            "body": None,
            "reviewers": [],
        },
        "merge_args": [],
        "fetch_args": [],
        "push_args": [],
    }
    outcome = run_sync_entry(entry, token=None, dry_run=True)
    assert "Dry run" in outcome.message


@patch("scripts.sync_branches.push_branch")
@patch("scripts.sync_branches.PRCreator")
@patch("scripts.sync_branches._prepare_worktree")
def test_run_sync_entry_creates_pr(mock_prepare, mock_pr_creator, mock_push, git_repo_factory) -> None:
    target = git_repo_factory("target")
    _commit(target, "README.md", "stable\n", "stable init")
    run(["git", "branch", "stable"], cwd=target)
    _commit(target, "README.md", "main\n", "main change")
    run(["git", "checkout", "stable"], cwd=target)

    mock_prepare.return_value = (target, "main", "sync-branch")

    pr_instance = MagicMock()
    pr_instance.find_open_pr_by_label.return_value = None
    pr_instance.create_or_update_tracking_pr.return_value = MagicMock(
        number=1,
        url="https://github.com/example/repo/pull/1",
        branch="sync-branch",
        created=True,
        updated=False,
        merged=False,
    )
    mock_pr_creator.return_value = pr_instance

    entry = {
        "sync_type": "pr",
        "src": {"url": str(target), "branch": "main"},
        "dest": {"url": str(target), "branch": "stable"},
        "ignore_files": [],
        "pr": {
            "branch": "sync-branch",
            "head_strategy": "sync-branch",
            "tracking_label": None,
            "labels": [],
            "automerge": False,
            "title": None,
            "body": None,
            "reviewers": [],
        },
        "merge_args": [],
        "fetch_args": [],
        "push_args": [],
    }

    outcome = run_sync_entry(entry, token="token", dry_run=False)
    assert outcome.pr_url == "https://github.com/example/repo/pull/1"
    pr_instance.create_or_update_tracking_pr.assert_called_once()
    assert (
        pr_instance.create_or_update_tracking_pr.call_args.kwargs["merge_when_ready"]
        is False
    )
    mock_push.assert_called_once()


@patch("scripts.sync_branches._collect_sync_commits")
@patch("scripts.sync_branches.PRCreator")
def test_run_sync_entry_creates_source_pr(mock_pr_creator, mock_collect, git_repo_factory) -> None:
    from lib.git_utils import GitCommit

    target = git_repo_factory("target")
    mock_collect.return_value = (
        target,
        [
            GitCommit(
                sha="abc123",
                short_sha="abc123",
                subject="main change",
            )
        ],
    )

    pr_instance = MagicMock()
    pr_instance.create_or_update_tracking_pr.return_value = MagicMock(
        number=2,
        url="https://github.com/example/repo/pull/2",
        branch="main",
        created=True,
        updated=False,
        merged=False,
    )
    mock_pr_creator.return_value = pr_instance

    entry = {
        "sync_type": "pr",
        "src": {"url": str(target), "branch": "main"},
        "dest": {"url": str(target), "branch": "stable"},
        "ignore_files": [],
        "pr": {
            "branch": None,
            "head_strategy": "source",
            "tracking_label": "lake-gate",
            "labels": [],
            "automerge": False,
            "title": None,
            "body": None,
            "reviewers": [],
        },
        "merge_args": [],
        "fetch_args": [],
        "push_args": [],
    }

    outcome = run_sync_entry(entry, token="token", dry_run=False)
    assert outcome.pr_url == "https://github.com/example/repo/pull/2"
    mock_collect.assert_called_once()
    pr_instance.create_or_update_tracking_pr.assert_called_once()
    assert pr_instance.create_or_update_tracking_pr.call_args.kwargs["head_branch"] == "main"
    assert pr_instance.create_or_update_tracking_pr.call_args.kwargs["delete_branch_on_merge"] is False


def test_source_pr_rejects_ignore_files(git_repo_factory) -> None:
    target = git_repo_factory("target")
    entry = {
        "sync_type": "pr",
        "src": {"url": str(target), "branch": "main"},
        "dest": {"url": str(target), "branch": "stable"},
        "ignore_files": [".tekton/*"],
        "pr": {
            "branch": None,
            "head_strategy": "source",
            "tracking_label": None,
            "labels": [],
            "automerge": False,
            "title": None,
            "body": None,
            "reviewers": [],
        },
        "merge_args": [],
        "fetch_args": [],
        "push_args": [],
    }

    with pytest.raises(ConfigError, match="cannot be used with ignore-files"):
        run_sync_entry(entry, token="token", dry_run=False)


def test_main_with_config_file_and_only_filter(tmp_path: Path) -> None:
    config = tmp_path / "sync.yaml"
    config.write_text(
        """
git:
  - name: kserve
    repo-url: https://github.com/org/kserve.git
  - name: kubeflow
    repo-url: https://github.com/org/kubeflow.git
""".strip(),
        encoding="utf-8",
    )

    with patch("scripts.sync_branches.run_sync_entry") as mock_run:
        mock_run.return_value = MagicMock(message="ok", pr_url=None)
        assert main(["--config", str(config), "--only", "kserve", "--dry-run"]) == 0
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0]["name"] == "kserve"


def test_main_with_config_file(tmp_path: Path) -> None:
    config = tmp_path / "sync.yaml"
    config.write_text(
        """
syncs:
  - src:
      url: https://github.com/org/repo.git
      branch: main
    dest:
      branch: stable
""",
        encoding="utf-8",
    )
    with patch("scripts.sync_branches.run_sync_entry") as mock_run:
        mock_run.return_value = MagicMock(message="ok", pr_url=None)
        assert main(["--config", str(config), "--dry-run"]) == 0
        mock_run.assert_called_once()
