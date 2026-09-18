"""Shared GitHub CLI execution helpers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class GhCommandError(RuntimeError):
    """Raised when a GitHub CLI command fails."""

    def __init__(self, command: Sequence[str], returncode: int, output: str) -> None:
        self.command = list(command)
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"gh command failed ({returncode}): {' '.join(self.command)}\n{output}"
        )


def default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a command without raising for a nonzero exit status."""
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )


def run_gh(args: Sequence[str], *, runner: Runner | None = None) -> str:
    """Run GitHub CLI arguments and return stripped stdout."""
    command = ["gh", *args]
    result = (runner or default_runner)(command)
    if result.returncode != 0:
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        raise GhCommandError(command, result.returncode, output)
    return (result.stdout or "").strip()
