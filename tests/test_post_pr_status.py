from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lib.github_cli import GhCommandError
from lib.pr_status_updater import PRStatusUpdater
from scripts import post_pr_status


def _install_dry_run_gh_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run now executes read-only gh calls; avoid real network in CLI tests."""

    def fake_runner(self, command: list[str]) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        path = command[2] if len(command) > 2 else ""
        if path.endswith("/pulls/1"):
            result.stdout = (
                '{"sha":"sha1","owner":"org","repo":"repo","state":"open"}\n'
            )
        elif path.endswith("/pulls/2"):
            result.stdout = (
                '{"sha":"sha2","owner":"org","repo":"repo","state":"open"}\n'
            )
        else:
            result.stdout = ""
        return result

    monkeypatch.setattr(PRStatusUpdater, "_default_runner", fake_runner)


def test_main_requires_status() -> None:
    with pytest.raises(SystemExit) as exc_info:
        post_pr_status.main([])
    assert exc_info.value.code != 0


def test_main_requires_pr_targets() -> None:
    assert post_pr_status.main(["--status", "success"]) == 1


def test_main_rejects_invalid_status() -> None:
    assert post_pr_status.main(["--status", "not-a-state", "--pr-url", "x"]) == 1


def test_main_rejects_invalid_pr_url_before_gh() -> None:
    assert (
        post_pr_status.main(
            ["--status", "success", "--pr-url", "https://example.com/nope"]
        )
        == 1
    )


def test_main_handles_gh_command_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = GhCommandError(["gh", "api"], 1, "request failed")
    monkeypatch.setattr(PRStatusUpdater, "resolve_pr_urls", MagicMock(side_effect=error))

    assert (
        post_pr_status.main(
            [
                "--status",
                "success",
                "--pr-url",
                "https://github.com/org/repo/pull/1",
            ]
        )
        == 1
    )
    assert "request failed" in capsys.readouterr().err


def test_main_dry_run_multiple_pr_urls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_dry_run_gh_fake(monkeypatch)
    code = post_pr_status.main(
        [
            "--dry-run",
            "--status",
            "success",
            "--pr-url",
            "https://github.com/org/a/pull/1",
            "--pr-url",
            "https://github.com/org/b/pull/2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "org/a/pulls/1" in out
    assert "org/b/pulls/2" in out
    assert "sha=sha1" in out
    assert "sha=sha2" in out


def test_main_dry_run_repo_and_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_dry_run_gh_fake(monkeypatch)
    code = post_pr_status.main(
        [
            "--dry-run",
            "--status",
            "success",
            "--repo",
            "org/repo",
            "--label",
            "gap-test",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "gh pr list --repo org/repo" in out
    assert "gap-test" in out
