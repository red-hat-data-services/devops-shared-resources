"""Audit and enable the GitHub repository auto-merge setting."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from lib.github_cli import GhCommandError, Runner, run_gh

Sleeper = Callable[[float], None]


class RepositoryAutoMergeError(RuntimeError):
    """Raised when repository auto-merge cannot be read, changed, or verified."""


@dataclass(frozen=True)
class RepositoryState:
    """Current repository auto-merge state returned by GitHub."""

    full_name: str
    auto_merge_enabled: bool


@dataclass(frozen=True)
class RepositoryAutoMergeResult:
    """Outcome of auditing or updating one repository."""

    requested_repository: str
    repository: str
    status: str


def check_authentication(*, runner: Runner | None = None) -> None:
    """Verify that gh is authenticated to GitHub.com."""
    run_gh(
        ["auth", "status", "--active", "--hostname", "github.com"], runner=runner
    )


def get_repository_state(
    repository: str,
    *,
    runner: Runner | None = None,
) -> RepositoryState:
    """Read the canonical repository name and auto-merge setting."""
    output = run_gh(
        [
            "api",
            f"repos/{repository}",
            "--jq",
            "[.full_name, .allow_auto_merge] | @tsv",
        ],
        runner=runner,
    )
    try:
        full_name, enabled = output.split("\t", maxsplit=1)
    except ValueError as exc:
        raise RepositoryAutoMergeError(
            f"invalid GitHub response for {repository}: {output!r}"
        ) from exc
    if not full_name or enabled not in {"true", "false"}:
        raise RepositoryAutoMergeError(
            f"invalid GitHub response for {repository}: {output!r}"
        )
    return RepositoryState(full_name, enabled == "true")


def set_repository_automerge(
    repository: str,
    *,
    runner: Runner | None = None,
) -> None:
    """Enable repository auto-merge and validate the PATCH response."""
    output = run_gh(
        [
            "api",
            "--method",
            "PATCH",
            f"repos/{repository}",
            "-F",
            "allow_auto_merge=true",
            "--jq",
            ".allow_auto_merge",
        ],
        runner=runner,
    )
    if output != "true":
        raise RepositoryAutoMergeError(
            f"GitHub update did not enable auto-merge for {repository}: {output!r}"
        )


def verify_repository_automerge(
    repository: str,
    *,
    runner: Runner | None = None,
    attempts: int = 5,
    delay: float = 2.0,
    sleeper: Sleeper = time.sleep,
) -> None:
    """Retry GET requests until GitHub reports auto-merge enabled."""
    _validate_verification_options(attempts, delay)

    last_error: GhCommandError | None = None
    for attempt in range(attempts):
        try:
            if get_repository_state(repository, runner=runner).auto_merge_enabled:
                return
            last_error = None
        except GhCommandError as exc:
            last_error = exc
        if attempt + 1 < attempts:
            sleeper(delay)

    message = f"auto-merge remained disabled after {attempts} checks"
    if last_error:
        message = f"verification failed after {attempts} checks: {last_error}"
    raise RepositoryAutoMergeError(f"{repository}: {message}")


def _validate_verification_options(attempts: int, delay: float) -> None:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if delay < 0:
        raise ValueError("delay must be at least 0")


def manage_repository_automerge(
    repository: str,
    *,
    apply: bool = False,
    runner: Runner | None = None,
    verify_attempts: int = 5,
    verify_delay: float = 2.0,
    sleeper: Sleeper = time.sleep,
) -> RepositoryAutoMergeResult:
    """Audit one repository and optionally enable auto-merge."""
    if apply:
        _validate_verification_options(verify_attempts, verify_delay)

    state = get_repository_state(repository, runner=runner)
    if state.auto_merge_enabled:
        return RepositoryAutoMergeResult(
            requested_repository=repository,
            repository=state.full_name,
            status="already_enabled",
        )
    if not apply:
        return RepositoryAutoMergeResult(
            requested_repository=repository,
            repository=state.full_name,
            status="would_enable",
        )

    set_repository_automerge(state.full_name, runner=runner)
    verify_repository_automerge(
        state.full_name,
        runner=runner,
        attempts=verify_attempts,
        delay=verify_delay,
        sleeper=sleeper,
    )
    return RepositoryAutoMergeResult(
        requested_repository=repository,
        repository=state.full_name,
        status="enabled",
    )
