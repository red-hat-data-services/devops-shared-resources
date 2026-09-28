from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from lib.git_utils import GitCommit
from lib.pr_creator import PRCreator, format_default_pr_body, format_default_pr_title


def test_format_default_pr_title_same_repo() -> None:
    title = format_default_pr_title(
        "main",
        "stable",
        source_repo="https://github.com/org/repo.git",
        target_repo="https://github.com/org/repo.git",
    )
    assert title == "Automated Sync from main to stable"


def test_format_default_pr_body_includes_commit_summary() -> None:
    body = format_default_pr_body(
        "main",
        "stable",
        source_repo="https://github.com/org/repo.git",
        target_repo="https://github.com/org/repo.git",
        commits=[
            GitCommit(
                sha="abc123def456",
                short_sha="abc123d",
                subject="task(PROJ-1): latest change (#42)",
            ),
            GitCommit(
                sha="def456abc789",
                short_sha="def456a",
                subject="chore(deps): bump dependency (#41)",
            ),
        ],
        head_branch="sync-main-to-stable-20250101-120000",
        automerge=True,
    )

    assert "## Automated Sync from main to stable" in body
    assert "**Latest commit:** [`abc123d`](https://github.com/org/repo/commit/abc123def456)" in body
    assert "**Total commits to sync:** 2" in body
    assert "### Commits to be synced" in body
    assert "chore(deps): bump dependency (#41)" in body
    assert "`sync-main-to-stable-20250101-120000`" in body
    assert "GitHub automerge is enabled" in body


def test_format_default_pr_body_includes_conflict_files() -> None:
    body = format_default_pr_body(
        "main",
        "stable",
        source_repo="https://github.com/org/repo.git",
        target_repo="https://github.com/org/repo.git",
        conflict_files=["README.md", "config.yaml"],
    )

    assert "### Merge conflicts" in body
    assert "`README.md`" in body
    assert "Resolve the merge conflicts above" in body


def test_format_default_pr_title_cross_repo() -> None:
    title = format_default_pr_title(
        "main",
        "stable",
        source_repo="https://github.com/org/upstream.git",
        target_repo="https://github.com/org/downstream.git",
    )
    assert "upstream" in title
    assert "downstream" in title


def test_create_pull_request_calls_github_api() -> None:
    creator = PRCreator("token")
    creator.session = MagicMock()
    creator._request = MagicMock(
        return_value={"number": 42, "html_url": "https://github.com/org/repo/pull/42"}
    )
    creator.ensure_delete_branch_on_merge = MagicMock()
    creator.add_labels = MagicMock()
    creator.request_reviewers = MagicMock()
    creator.enable_automerge = MagicMock(return_value=False)

    result = creator.create_pull_request(
        "https://github.com/org/repo.git",
        title="Sync",
        body="body",
        head_branch="sync-branch",
        base_branch="stable",
        labels=["automation"],
        reviewers=["alice"],
        automerge=True,
    )

    assert result.number == 42
    assert result.created is True
    creator.ensure_delete_branch_on_merge.assert_called_once()
    creator.add_labels.assert_called_once()
    creator.request_reviewers.assert_called_once()
    creator.enable_automerge.assert_called_once()


def test_enable_automerge_error_identifies_pull_request() -> None:
    creator = PRCreator("token")
    creator._request = MagicMock(
        return_value={"node_id": "PR_node_id", "head": {"sha": "head_sha"}}
    )
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "errors": [{"type": "UNPROCESSABLE", "message": "Pull request is in unstable status"}]
    }
    creator.session.post = MagicMock(return_value=response)

    with pytest.raises(RuntimeError) as exc_info:
        creator.enable_automerge("https://github.com/org/repo.git", number=42)

    assert "org/repo pull request #42" in str(exc_info.value)
    assert "Pull request is in unstable status" in str(exc_info.value)


def test_enable_automerge_queues_when_github_accepts_request() -> None:
    creator = PRCreator("token")
    creator._request = MagicMock(
        return_value={"node_id": "PR_node_id", "head": {"sha": "head_sha"}}
    )
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": {"enablePullRequestAutoMerge": {}}}
    creator.session.post = MagicMock(return_value=response)

    merged = creator.enable_automerge(
        "https://github.com/org/repo.git",
        number=42,
        merge_when_ready=True,
    )

    assert merged is False
    creator._request.assert_called_once_with("GET", "/repos/org/repo/pulls/42")


def test_enable_automerge_merges_unstable_mergeable_pr_when_ready() -> None:
    creator = PRCreator("token")
    pull = {"node_id": "PR_node_id", "head": {"sha": "head_sha"}}
    current = {
        "mergeable": True,
        "mergeable_state": "unstable",
        "head": {"sha": "head_sha"},
    }
    creator._request = MagicMock(side_effect=[pull, current, {"merged": True}])
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "errors": [{"type": "UNPROCESSABLE", "message": "Pull request is in unstable status"}]
    }
    creator.session.post = MagicMock(return_value=response)

    merged = creator.enable_automerge(
        "https://github.com/org/repo.git",
        number=42,
        merge_when_ready=True,
    )

    assert merged is True
    assert creator._request.call_args_list == [
        call("GET", "/repos/org/repo/pulls/42"),
        call("GET", "/repos/org/repo/pulls/42"),
        call(
            "PUT",
            "/repos/org/repo/pulls/42/merge",
            json={"merge_method": "merge", "sha": "head_sha"},
        ),
    ]


def test_enable_automerge_does_not_merge_blocked_pr() -> None:
    creator = PRCreator("token")
    pull = {"node_id": "PR_node_id", "head": {"sha": "head_sha"}}
    current = {
        "mergeable": True,
        "mergeable_state": "blocked",
        "head": {"sha": "head_sha"},
    }
    creator._request = MagicMock(side_effect=[pull, current])
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "errors": [{"type": "UNPROCESSABLE", "message": "Pull request is blocked"}]
    }
    creator.session.post = MagicMock(return_value=response)

    with pytest.raises(RuntimeError, match="Failed to enable automerge"):
        creator.enable_automerge(
            "https://github.com/org/repo.git",
            number=42,
            merge_when_ready=True,
        )

    assert creator._request.call_count == 2


def test_enable_automerge_refuses_to_merge_changed_head() -> None:
    creator = PRCreator("token")
    pull = {"node_id": "PR_node_id", "head": {"sha": "original_sha"}}
    current = {
        "mergeable": True,
        "mergeable_state": "unstable",
        "head": {"sha": "new_sha"},
    }
    creator._request = MagicMock(side_effect=[pull, current])
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "errors": [{"type": "UNPROCESSABLE", "message": "Pull request is in unstable status"}]
    }
    creator.session.post = MagicMock(return_value=response)

    with pytest.raises(RuntimeError, match="head commit changed"):
        creator.enable_automerge(
            "https://github.com/org/repo.git",
            number=42,
            merge_when_ready=True,
        )

    assert creator._request.call_count == 2


def test_create_or_update_tracking_pr_updates_existing() -> None:
    creator = PRCreator("token")
    with (
        patch.object(creator, "find_open_pr_by_head", return_value=None),
        patch.object(
            creator,
            "find_open_pr_by_label",
            return_value={
                "number": 7,
                "html_url": "https://github.com/org/repo/pull/7",
                "head": {"ref": "sync-existing"},
            },
        ),
        patch.object(creator, "update_pull_request") as mock_update,
        patch.object(creator, "add_labels"),
        patch.object(creator, "enable_automerge", return_value=True) as mock_automerge,
    ):
        result = creator.create_or_update_tracking_pr(
            "https://github.com/org/repo.git",
            title="Sync",
            body="body",
            head_branch="sync-new",
            base_branch="stable",
            tracking_label="lake-gate",
            labels=["lake-gate"],
            automerge=True,
            merge_when_ready=True,
        )

    assert result.updated is True
    assert result.merged is True
    assert result.branch == "sync-existing"
    mock_update.assert_called_once()
    mock_automerge.assert_called_once_with(
        "https://github.com/org/repo.git",
        number=7,
        merge_when_ready=True,
    )


def test_create_or_update_prefers_existing_head_base_pr() -> None:
    """Source-headed syncs must reuse master→stable even without tracking label."""
    creator = PRCreator("token")
    creator.find_open_pr_by_head = MagicMock(
        return_value={
            "number": 81,
            "html_url": "https://github.com/org/repo/pull/81",
            "head": {"ref": "master"},
        }
    )
    creator.find_open_pr_by_label = MagicMock(
        return_value={
            "number": 84,
            "html_url": "https://github.com/org/repo/pull/84",
            "head": {"ref": "sync-old"},
        }
    )
    creator.update_pull_request = MagicMock()
    creator.add_labels = MagicMock()
    creator.create_pull_request = MagicMock()

    result = creator.create_or_update_tracking_pr(
        "https://github.com/org/repo.git",
        title="Sync",
        body="body",
        head_branch="master",
        base_branch="stable",
        tracking_label="gated-artifacts-promoter",
        labels=["gated-artifacts-promoter"],
    )

    assert result.updated is True
    assert result.number == 81
    assert result.branch == "master"
    creator.find_open_pr_by_label.assert_not_called()
    creator.create_pull_request.assert_not_called()
    creator.add_labels.assert_called_once()


def test_create_or_update_recovers_from_422_already_exists() -> None:
    creator = PRCreator("token")
    creator.find_open_pr_by_head = MagicMock(
        side_effect=[
            None,
            {
                "number": 81,
                "html_url": "https://github.com/org/repo/pull/81",
                "head": {"ref": "master"},
            },
        ]
    )
    creator.find_open_pr_by_label = MagicMock(return_value=None)
    creator.create_pull_request = MagicMock(
        side_effect=RuntimeError(
            'GitHub API POST /repos/org/repo/pulls failed (422): '
            '{"message":"Validation Failed","errors":[{"message":"A pull request already exists for org:master."}]}'
        )
    )
    creator.update_pull_request = MagicMock()
    creator.add_labels = MagicMock()

    result = creator.create_or_update_tracking_pr(
        "https://github.com/org/repo.git",
        title="Sync",
        body="body",
        head_branch="master",
        base_branch="stable",
        tracking_label="gated-artifacts-promoter",
        labels=["gated-artifacts-promoter"],
    )

    assert result.updated is True
    assert result.number == 81
    creator.update_pull_request.assert_called_once()
