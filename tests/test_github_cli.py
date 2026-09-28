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
