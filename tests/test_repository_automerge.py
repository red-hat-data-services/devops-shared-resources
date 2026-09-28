from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from lib.github_cli import GhCommandError
from lib.repository_automerge import (
    RepositoryAutoMergeError,
    check_authentication,
    get_repository_state,
    manage_repository_automerge,
)


class QueueRunner:
    def __init__(self, *responses: tuple[int, str, str]) -> None:
        self.responses = list(responses)
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        returncode, stdout, stderr = self.responses.pop(0)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def response(stdout: str = "", *, returncode: int = 0, stderr: str = "") -> tuple[int, str, str]:
    return returncode, stdout, stderr


def test_authentication_checks_only_active_github_com_account() -> None:
    runner = QueueRunner(response())

    check_authentication(runner=runner)

    assert runner.commands == [
        ["gh", "auth", "status", "--active", "--hostname", "github.com"]
    ]


def test_audit_reports_canonical_renamed_repository() -> None:
    runner = QueueRunner(response("org/new-name\tfalse\n"))

    result = manage_repository_automerge("org/old-name", runner=runner)

    assert result.requested_repository == "org/old-name"
    assert result.repository == "org/new-name"
    assert result.status == "would_enable"
    assert len(runner.commands) == 1


def test_apply_skips_repository_that_is_already_enabled() -> None:
    runner = QueueRunner(response("org/repo\ttrue\n"))

    result = manage_repository_automerge("org/repo", apply=True, runner=runner)

    assert result.status == "already_enabled"
    assert len(runner.commands) == 1


def test_apply_updates_and_retries_stale_verification() -> None:
    runner = QueueRunner(
        response("org/repo\tfalse\n"),
        response("true\n"),
        response("org/repo\tfalse\n"),
        response("org/repo\ttrue\n"),
    )
    sleeps: list[float] = []
    result = manage_repository_automerge(
        "org/repo",
        apply=True,
        runner=runner,
        verify_attempts=3,
        verify_delay=0.25,
        sleeper=sleeps.append,
    )

    assert result.status == "enabled"
    assert sleeps == [0.25]
    assert runner.commands[1] == [
        "gh",
        "api",
        "--method",
        "PATCH",
        "repos/org/repo",
        "-F",
        "allow_auto_merge=true",
        "--jq",
        ".allow_auto_merge",
    ]


def test_apply_fails_after_verification_attempts_are_exhausted() -> None:
    runner = QueueRunner(
        response("org/repo\tfalse\n"),
        response("true\n"),
        response("org/repo\tfalse\n"),
        response("org/repo\tfalse\n"),
    )
    sleeps: list[float] = []
    with pytest.raises(
        RepositoryAutoMergeError,
        match="remained disabled after 2 checks",
    ):
        manage_repository_automerge(
            "org/repo",
            apply=True,
            runner=runner,
            verify_attempts=2,
            verify_delay=1,
            sleeper=sleeps.append,
        )

    assert sleeps == [1]


@pytest.mark.parametrize(
    ("verify_attempts", "verify_delay", "message"),
    [(0, 2.0, "attempts"), (5, -1.0, "delay")],
)
def test_invalid_verification_options_fail_before_api_requests(
    verify_attempts: int,
    verify_delay: float,
    message: str,
) -> None:
    runner = QueueRunner()

    with pytest.raises(ValueError, match=message):
        manage_repository_automerge(
            "org/repo",
            apply=True,
            runner=runner,
            verify_attempts=verify_attempts,
            verify_delay=verify_delay,
        )

    assert runner.commands == []


def test_invalid_get_response_is_rejected() -> None:
    runner = QueueRunner(response("org/repo\n"))

    with pytest.raises(RepositoryAutoMergeError, match="invalid GitHub response"):
        get_repository_state("org/repo", runner=runner)


def test_failed_gh_command_includes_error_output() -> None:
    runner = QueueRunner(response(returncode=1, stderr="permission denied"))

    with pytest.raises(GhCommandError, match="permission denied"):
        get_repository_state("org/repo", runner=runner)
