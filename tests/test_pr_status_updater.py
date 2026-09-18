from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lib.pr_status_updater import (
    PRStatusUpdater,
    StatusUpdateBatchError,
    normalize_repo_slug,
    normalize_status,
    parse_github_repo,
    parse_pr_url,
    validate_check_name,
    validate_description,
    validate_target_url,
)


def test_parse_pr_url() -> None:
    pr = parse_pr_url("https://github.com/org/repo/pull/42")
    assert pr.owner == "org"
    assert pr.repo == "repo"
    assert pr.number == 42


def test_parse_pr_url_invalid() -> None:
    with pytest.raises(ValueError):
        parse_pr_url("https://example.com/not-a-pr")


def test_normalize_status_check_names_map_to_commit_state() -> None:
    assert normalize_status("completed") == "success"
    assert normalize_status("failure") == "failure"
    assert normalize_status("in_progress") == "pending"
    assert normalize_status("queued") == "pending"


def test_normalize_status_rejects_gap_aliases() -> None:
    with pytest.raises(ValueError, match="Unsupported status"):
        normalize_status("merge-failure")


def test_build_status_command() -> None:
    updater = PRStatusUpdater(check_name="gated artifacts promoter", dry_run=True)
    command = updater.build_status_command(
        "org",
        "repo",
        "abc123",
        "success",
        description="ok",
        target_url="https://example.com",
    )
    assert command[:4] == ["api", "--method", "POST", "repos/org/repo/statuses/abc123"]
    assert "state=success" in command
    assert "context=gated artifacts promoter" in command
    assert "description=ok" in command
    assert "target_url=https://example.com" in command


def test_post_status_for_pr_uses_gh_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str]) -> MagicMock:
        calls.append(command)
        result = MagicMock()
        result.returncode = 0
        if command[1] == "api" and command[2].endswith("/pulls/7"):
            result.stdout = (
                '{"sha":"sha123","owner":"org","repo":"repo","state":"open"}\n'
            )
        elif command[1] == "api" and "/commits/" in command[2] and command[2].endswith("/statuses"):
            result.stdout = ""
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    updater = PRStatusUpdater(check_name="gated artifacts promoter", runner=fake_runner)
    result = updater.post_status_for_pr(
        "https://github.com/org/repo/pull/7",
        "success",
        description="GAP",
    )

    assert result.head_sha == "sha123"
    assert len(calls) == 3
    assert calls[0][0:3] == ["gh", "api", "repos/org/repo/pulls/7"]
    assert calls[1][2] == "repos/org/repo/commits/sha123/statuses"
    assert calls[2][0:4] == ["gh", "api", "--method", "POST"]
    assert "repos/org/repo/statuses/sha123" in calls[2][4]


def test_dry_run_prints_commands(capsys: pytest.CaptureFixture[str]) -> None:
    def fake_runner(command: list[str]) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        if command[1] == "api" and command[2].endswith("/pulls/3"):
            result.stdout = (
                '{"sha":"realsha3","owner":"org","repo":"repo","state":"open"}\n'
            )
        elif command[1] == "api" and "/commits/" in command[2]:
            result.stdout = ""
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    updater = PRStatusUpdater(
        check_name="gated artifacts promoter",
        dry_run=True,
        runner=fake_runner,
    )
    updater.post_status_for_pr("https://github.com/org/repo/pull/3", "success")
    captured = capsys.readouterr().out
    assert "[dry-run] gh api repos/org/repo/pulls/3" in captured
    assert "repos/org/repo/statuses/realsha3" in captured
    assert "dry-run-sha" not in captured


def test_post_status_uses_head_repo_for_fork_pr() -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str]) -> MagicMock:
        calls.append(command)
        result = MagicMock()
        result.returncode = 0
        if command[1] == "api" and "/pulls/9" in command[2]:
            result.stdout = (
                '{"sha":"forksha","owner":"contributor","repo":"repo","state":"open"}\n'
            )
        elif command[1] == "api" and "/commits/" in command[2] and command[2].endswith("/statuses"):
            result.stdout = ""
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    updater = PRStatusUpdater(check_name="ctx", runner=fake_runner)
    updater.post_status_for_pr("https://github.com/org/repo/pull/9", "success")
    assert "repos/contributor/repo/statuses/forksha" in calls[2][4]


def test_resolve_pr_urls_requires_label_for_repo() -> None:
    updater = PRStatusUpdater(check_name="ctx")
    with pytest.raises(ValueError, match="--label"):
        updater.resolve_pr_urls([], ["org/repo"], None)


def test_resolve_pr_urls_requires_target() -> None:
    updater = PRStatusUpdater(check_name="ctx")
    with pytest.raises(ValueError, match="--pr-url"):
        updater.resolve_pr_urls([], [], None)


def test_resolve_pr_urls_rejects_label_without_repo() -> None:
    updater = PRStatusUpdater(check_name="ctx")
    with pytest.raises(ValueError, match="--label is only used with --repo"):
        updater.resolve_pr_urls([], [], "some-label")


def test_parse_pr_url_strips_whitespace() -> None:
    pr = parse_pr_url("  https://github.com/org/repo/pull/7/  ")
    assert pr.number == 7


def test_parse_pr_url_rejects_issue_url() -> None:
    with pytest.raises(ValueError):
        parse_pr_url("https://github.com/org/repo/issues/1")


def test_normalize_status_case_insensitive() -> None:
    assert normalize_status("SUCCESS") == "success"


def test_normalize_status_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unsupported status"):
        normalize_status("purple")


def test_normalize_repo_slug_https() -> None:
    assert normalize_repo_slug("https://github.com/org/repo.git") == ("org", "repo")


def test_normalize_repo_slug_invalid() -> None:
    with pytest.raises(ValueError):
        normalize_repo_slug("not-a-slug")


def test_parse_github_repo_ssh() -> None:
    assert parse_github_repo("git@github.com:org/repo.git") == ("org", "repo")


def test_validate_check_name_empty() -> None:
    with pytest.raises(ValueError, match="check-name"):
        validate_check_name("   ")


def test_validate_description_max_length() -> None:
    with pytest.raises(ValueError, match="140"):
        validate_description("x" * 141)


def test_validate_target_url_requires_http() -> None:
    with pytest.raises(ValueError, match="target-url"):
        validate_target_url("ftp://example.com")


def test_resolve_pr_urls_dedupes() -> None:
    updater = PRStatusUpdater(check_name="ctx", dry_run=True)
    urls = updater.resolve_pr_urls(
        [
            "https://github.com/org/repo/pull/1",
            "https://github.com/org/repo/pull/1",
        ],
        [],
        None,
    )
    assert urls == ["https://github.com/org/repo/pull/1"]


def test_resolve_pr_urls_rejects_invalid_explicit_url() -> None:
    updater = PRStatusUpdater(check_name="ctx")
    with pytest.raises(ValueError, match="Invalid PR URL"):
        updater.resolve_pr_urls(["https://github.com/org/repo/issues/1"], [], None)


def test_resolve_pr_urls_ignores_blank_entries() -> None:
    updater = PRStatusUpdater(check_name="ctx", dry_run=True)
    urls = updater.resolve_pr_urls(
        ["", "  ", "https://github.com/org/repo/pull/2"],
        [],
        None,
    )
    assert urls == ["https://github.com/org/repo/pull/2"]


def test_resolve_pr_urls_dry_run_repo_label() -> None:
    updater = PRStatusUpdater(check_name="ctx", dry_run=True)
    urls = updater.resolve_pr_urls([], ["org/repo"], "gap-test")
    assert urls == ["https://github.com/org/repo/pull/1"]


def test_get_pull_head_invalid_json() -> None:
    def fake_runner(command: list[str]) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = "not-json"
        result.stderr = ""
        return result

    updater = PRStatusUpdater(check_name="ctx", runner=fake_runner)
    with pytest.raises(RuntimeError, match="Invalid JSON"):
        updater.post_status_for_pr("https://github.com/org/repo/pull/1", "success")


def test_post_status_for_many_continue_on_error() -> None:
    calls = {"n": 0}

    def fake_runner(command: list[str]) -> MagicMock:
        calls["n"] += 1
        result = MagicMock()
        if command[1] == "api" and "/pulls/1" in command[2]:
            result.returncode = 0
            result.stdout = '{"sha":"s1","owner":"org","repo":"repo","state":"open"}\n'
        elif command[1] == "api" and "/pulls/2" in command[2]:
            result.returncode = 1
            result.stdout = ""
            result.stderr = "not found"
        elif command[1] == "api" and "statuses" in command[2]:
            result.returncode = 0
            result.stdout = ""
        else:
            result.returncode = 0
            result.stdout = ""
        result.stderr = result.stderr or ""
        return result

    updater = PRStatusUpdater(check_name="ctx", runner=fake_runner)
    with pytest.raises(StatusUpdateBatchError) as exc_info:
        updater.post_status_for_many(
            [
                "https://github.com/org/repo/pull/1",
                "https://github.com/org/repo/pull/2",
            ],
            "success",
            continue_on_error=True,
        )
    assert len(exc_info.value.successes) == 1
    assert len(exc_info.value.failures) == 1


def test_post_status_skips_when_context_already_has_same_state() -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str]) -> MagicMock:
        calls.append(command)
        result = MagicMock()
        result.returncode = 0
        path = command[2] if len(command) > 2 else ""
        if path.endswith("/pulls/11"):
            result.stdout = (
                '{"sha":"sha11","owner":"org","repo":"repo","state":"open"}\n'
            )
        elif path.endswith("/statuses") and "/commits/" in path:
            result.stdout = (
                '{"state":"success","description":"old","target_url":null}\n'
            )
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    updater = PRStatusUpdater(check_name="gated artifacts promoter", runner=fake_runner)
    result = updater.post_status_for_pr(
        "https://github.com/org/repo/pull/11",
        "success",
        description="old",
    )
    assert result.skipped is True
    assert not any("POST" in str(c) for c in calls)
    assert len(calls) == 2


def test_post_status_warns_when_pr_is_closed() -> None:
    def fake_runner(command: list[str]) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        if command[2].endswith("/pulls/20"):
            result.stdout = (
                '{"sha":"sha20","owner":"org","repo":"repo","state":"closed"}\n'
            )
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    updater = PRStatusUpdater(check_name="ctx", runner=fake_runner)
    with pytest.warns(UserWarning, match="closed"):
        updater.post_status_for_pr("https://github.com/org/repo/pull/20", "success")


def test_post_status_updates_description_when_state_unchanged() -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str]) -> MagicMock:
        calls.append(command)
        result = MagicMock()
        result.returncode = 0
        path = command[2] if len(command) > 2 else ""
        if path.endswith("/pulls/12"):
            result.stdout = (
                '{"sha":"sha12","owner":"org","repo":"repo","state":"open"}\n'
            )
        elif path.endswith("/statuses") and "/commits/" in path:
            result.stdout = (
                '{"state":"success","description":"old text","target_url":null}\n'
            )
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    updater = PRStatusUpdater(check_name="gated artifacts promoter", runner=fake_runner)
    result = updater.post_status_for_pr(
        "https://github.com/org/repo/pull/12",
        "success",
        description="new text",
    )
    assert result.skipped is False
    assert any("POST" in c for c in calls)
    assert "description=new text" in calls[-1]
