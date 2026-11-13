from __future__ import annotations

import subprocess

import pytest

from lib.github_cli import GhCommandError, run_gh


def test_run_gh_returns_stripped_stdout() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "value\n", "")

    assert run_gh(["api", "repos/org/repo"], runner=runner) == "value"
    assert commands == [["gh", "api", "repos/org/repo"]]


def test_run_gh_raises_structured_error() -> None:
    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "stdout ", "stderr")

    with pytest.raises(GhCommandError) as exc_info:
        run_gh(["api", "repos/org/repo"], runner=runner)

    error = exc_info.value
    assert error.command == ["gh", "api", "repos/org/repo"]
    assert error.returncode == 1
    assert error.output == "stdout stderr"
    assert "stdout stderr" in str(error)


def test_run_gh_wraps_executable_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_os_error(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("gh executable not found")

    monkeypatch.setattr(subprocess, "run", raise_os_error)

    with pytest.raises(GhCommandError) as exc_info:
        run_gh(["api", "repos/org/repo"])

    assert exc_info.value.returncode == 127
    assert "gh executable not found" in str(exc_info.value)


def test_run_gh_retries_transient_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        subprocess.CompletedProcess([], 1, "", "Server Error"),
        subprocess.CompletedProcess([], 0, '{"ok": true}\n', ""),
    ]
    commands: list[list[str]] = []
    sleeps: list[float] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return responses.pop(0)

    monkeypatch.setattr("lib.github_cli.time.sleep", sleeps.append)

    assert run_gh(["api", "repos/org/repo"], runner=runner) == '{"ok": true}'
    assert len(commands) == 2
    assert sleeps == [1.0]


def test_run_gh_retries_successful_transient_body(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        subprocess.CompletedProcess([], 0, "Server Error", ""),
        subprocess.CompletedProcess([], 0, "value\n", ""),
    ]
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: None)

    assert run_gh(["api", "repos/org/repo"], runner=lambda command: responses.pop(0)) == "value"


def test_run_gh_does_not_retry_non_transient_or_mutating_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "HTTP 404: Not Found")

    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: pytest.fail("unexpected retry"))

    with pytest.raises(GhCommandError):
        run_gh(["api", "repos/org/repo"], runner=runner)
    with pytest.raises(GhCommandError):
        run_gh(["api", "repos/org/repo", "--method", "POST"], runner=runner)

    assert len(calls) == 2


def test_run_gh_does_not_scan_user_json_for_transient_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, '{"body": "the timeout was documented"}\n', ""
        )

    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: pytest.fail("unexpected retry"))

    assert run_gh(["api", "repos/org/repo/issues/comments"], runner=runner) == (
        '{"body": "the timeout was documented"}'
    )
    assert len(calls) == 1


def test_run_gh_retries_transient_runner_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: None)

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GhCommandError(command, 1, "Temporary failure in name resolution")
        return subprocess.CompletedProcess(command, 0, "value\n", "")

    assert run_gh(["api", "repos/org/repo"], runner=runner) == "value"
    assert calls == 2


def test_run_gh_does_not_retry_plain_text_value_containing_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: pytest.fail("unexpected retry"))

    assert run_gh(
        ["api", "repos/org/repo", "--jq", ".body"],
        runner=lambda command: subprocess.CompletedProcess(command, 0, "the timeout was documented\n", ""),
    ) == "the timeout was documented"


@pytest.mark.parametrize(
    "mutation_args",
    [
        ["--method=POST"],
        ["-XPOST"],
        ["-Fkey=value"],
        ["--field=key=value"],
        ["--raw-field=key=value"],
        ["--input=payload.json"],
    ],
)
def test_run_gh_does_not_retry_attached_mutation_flags(
    mutation_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "Server Error")

    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: pytest.fail("unexpected retry"))

    with pytest.raises(GhCommandError):
        run_gh(["api", "repos/org/repo", *mutation_args], runner=runner)

    assert len(calls) == 1


@pytest.mark.parametrize("error_text", ["HTTP 429", "HTTP 500"])
def test_run_gh_retries_bare_http_transient_errors(
    error_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        subprocess.CompletedProcess([], 1, "", error_text),
        subprocess.CompletedProcess([], 0, "value\n", ""),
    ]
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: None)

    assert run_gh(
        ["api", "repos/org/repo"],
        runner=lambda command: responses.pop(0),
    ) == "value"


def test_run_gh_does_not_scan_successful_pr_diff_for_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff = "diff --git a/Dockerfile b/Dockerfile\n+timeout\n"
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: pytest.fail("unexpected retry"))

    assert run_gh(
        ["pr", "diff", "1", "-R", "org/repo"],
        runner=lambda command: subprocess.CompletedProcess(command, 0, diff, ""),
    ) == diff.strip()


def test_run_gh_does_not_treat_bare_timeout_as_an_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: pytest.fail("unexpected retry"))

    assert run_gh(
        ["api", "repos/org/repo", "--jq", ".body"],
        runner=lambda command: subprocess.CompletedProcess(command, 0, "timeout\n", ""),
    ) == "timeout"


@pytest.mark.parametrize("error_text", ["gh: HTTP 503", "gh: API rate limit exceeded"])
def test_run_gh_retries_cli_prefixed_transient_errors(
    error_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        subprocess.CompletedProcess([], 1, "", error_text),
        subprocess.CompletedProcess([], 0, "value\n", ""),
    ]
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: None)

    assert run_gh(
        ["api", "repos/org/repo"],
        runner=lambda command: responses.pop(0),
    ) == "value"


def test_run_gh_retries_when_stderr_fails_after_partial_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        subprocess.CompletedProcess([], 1, '[{"id": 1}]', "HTTP 503"),
        subprocess.CompletedProcess([], 0, '[{"id": 1}, {"id": 2}]\n', ""),
    ]
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: None)

    assert run_gh(
        ["api", "repos/org/repo/issues"],
        runner=lambda command: responses.pop(0),
    ) == '[{"id": 1}, {"id": 2}]'


def test_run_gh_retries_error_prefixed_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        subprocess.CompletedProcess([], 1, "", "ERROR: timeout"),
        subprocess.CompletedProcess([], 0, "value\n", ""),
    ]
    monkeypatch.setattr("lib.github_cli.time.sleep", lambda _delay: None)

    assert run_gh(
        ["api", "repos/org/repo"],
        runner=lambda command: responses.pop(0),
    ) == "value"
