"""Audit and enable the GitHub repository auto-merge setting."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]


class GhCommandError(RuntimeError):
    """Raised when a GitHub CLI command fails or returns unexpected output."""


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


class RepositoryAutoMergeManager:
    """Manage repository-level auto-merge through the GitHub CLI."""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        verify_attempts: int = 5,
        verify_delay: float = 2.0,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if verify_attempts < 1:
            raise ValueError("verify_attempts must be at least 1")
        if verify_delay < 0:
            raise ValueError("verify_delay must be at least 0")
        self._runner = runner or self._default_runner
        self._verify_attempts = verify_attempts
        self._verify_delay = verify_delay
        self._sleeper = sleeper

    @staticmethod
    def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=False,
        )

    def _run_gh(self, args: Sequence[str]) -> str:
        command = ["gh", *args]
        result = self._runner(command)
        if result.returncode != 0:
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            raise GhCommandError(
                f"gh command failed ({result.returncode}): {' '.join(command)}\n{output}"
            )
        return (result.stdout or "").strip()

    def check_authentication(self) -> None:
        """Verify that gh is authenticated to GitHub.com."""
        self._run_gh(["auth", "status", "--hostname", "github.com"])

    def get_state(self, repository: str) -> RepositoryState:
        """Read the canonical repository name and auto-merge setting."""
        output = self._run_gh(
            [
                "api",
                f"repos/{repository}",
                "--jq",
                "[.full_name, .allow_auto_merge] | @tsv",
            ]
        )
        try:
            full_name, enabled = output.split("\t", maxsplit=1)
        except ValueError as exc:
            raise GhCommandError(
                f"invalid GitHub response for {repository}: {output!r}"
            ) from exc
        if not full_name or enabled not in {"true", "false"}:
            raise GhCommandError(
                f"invalid GitHub response for {repository}: {output!r}"
            )
        return RepositoryState(full_name, enabled == "true")

    def _enable(self, repository: str) -> None:
        output = self._run_gh(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repository}",
                "-F",
                "allow_auto_merge=true",
                "--jq",
                ".allow_auto_merge",
            ]
        )
        if output != "true":
            raise GhCommandError(
                f"GitHub update did not enable auto-merge for {repository}: {output!r}"
            )

    def _verify_enabled(self, repository: str) -> None:
        last_error: GhCommandError | None = None
        for attempt in range(self._verify_attempts):
            try:
                if self.get_state(repository).auto_merge_enabled:
                    return
                last_error = None
            except GhCommandError as exc:
                last_error = exc
            if attempt + 1 < self._verify_attempts:
                self._sleeper(self._verify_delay)

        message = (
            f"auto-merge remained disabled after {self._verify_attempts} checks"
        )
        if last_error:
            message = f"verification failed after {self._verify_attempts} checks: {last_error}"
        raise GhCommandError(f"{repository}: {message}")

    def manage(
        self,
        repository: str,
        *,
        apply: bool = False,
    ) -> RepositoryAutoMergeResult:
        """Audit one repository and optionally enable auto-merge."""
        state = self.get_state(repository)
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

        self._enable(state.full_name)
        self._verify_enabled(state.full_name)
        return RepositoryAutoMergeResult(
            requested_repository=repository,
            repository=state.full_name,
            status="enabled",
        )
