"""Shared GitHub CLI execution helpers."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

# GitHub occasionally returns transient 5xx/rate-limit responses from the API.
# Read-only calls can be retried safely; mutating calls must remain single-shot.
GH_READ_RETRY_ATTEMPTS = 3
GH_READ_RETRY_DELAYS = (1.0, 3.0)


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
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=False,
        )
    except OSError as exc:
        raise GhCommandError(command, 127, str(exc)) from exc


def run_gh(args: Sequence[str], *, runner: Runner | None = None) -> str:
    """Run GitHub CLI arguments and return stripped stdout.

    Read-only GitHub queries are retried for transient API failures. The
    monitor performs many independent reads, so a short-lived GitHub 5xx,
    rate-limit response, or proxy error should not abort the whole batch.
    Mutating commands are deliberately never retried.
    """
    command = ["gh", *args]
    execute = runner or default_runner
    attempts = GH_READ_RETRY_ATTEMPTS if _is_read_only(args) else 1
    last_output = ""
    last_returncode = 1

    for attempt in range(attempts):
        try:
            result = execute(command)
        except GhCommandError as exc:
            last_output = exc.output
            last_returncode = exc.returncode
            if attempt + 1 < attempts and _looks_transient(exc.output):
                time.sleep(GH_READ_RETRY_DELAYS[min(attempt, len(GH_READ_RETRY_DELAYS) - 1)])
                continue
            raise

        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        output = (stdout + stderr).strip()
        last_output = output
        last_returncode = result.returncode
        # Inspect streams separately: a paginated command may have emitted a
        # valid partial page to stdout before GitHub reports HTTP 503 on stderr.
        transient = _looks_transient(stdout) or _looks_transient(stderr)

        if result.returncode == 0 and not transient:
            return stdout.strip()

        should_retry = (
            attempt + 1 < attempts
            and transient
        )
        if should_retry:
            time.sleep(GH_READ_RETRY_DELAYS[min(attempt, len(GH_READ_RETRY_DELAYS) - 1)])
            continue

        if result.returncode != 0:
            raise GhCommandError(command, result.returncode, output)
        # A successful process with a known transient body (for example
        # ``Server Error``) is still an API failure. Raising here prevents a
        # later JSON parser from turning it into an opaque invalid-JSON error.
        raise GhCommandError(command, 1, output or "transient GitHub API response")

    raise GhCommandError(command, last_returncode, last_output)


def _is_read_only(args: Sequence[str]) -> bool:
    """Return whether a gh invocation can be retried without side effects."""
    if not args:
        return False
    if args[0] == "api":
        mutating_flags = {"--method", "-X", "--input", "-f", "-F", "--raw-field", "--field"}
        mutating_prefixes = (
            "--method=",
            "--input=",
            "--raw-field=",
            "--field=",
            "-X",
            "-f",
            "-F",
        )
        return not any(
            arg in mutating_flags or arg.startswith(mutating_prefixes)
            for arg in args[1:]
        )
    return tuple(args[:2]) in {
        ("pr", "view"),
        ("pr", "list"),
        ("pr", "diff"),
        ("run", "view"),
        ("repo", "view"),
    }


def _looks_transient(output: str) -> bool:
    """Recognize transient errors without scanning arbitrary JSON content."""
    text = output.strip().lower()
    if not text:
        return False

    # API responses can contain user-authored text such as ``timeout`` in a
    # comment body. Only inspect structured responses when the error text is in
    # a conventional error/message field; otherwise scan the plain body.
    if text.startswith(("{", "[")):
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(payload, dict):
                error_text = " ".join(
                    str(payload.get(key) or "")
                    for key in ("message", "error", "errors")
                ).lower()
                return _contains_transient_marker(error_text)
            return False

    return _contains_transient_marker(text)


def _contains_transient_marker(text: str) -> bool:
    """Return whether plain error text contains a retryable failure marker."""
    markers = (
        "server error",
        "internal server error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "rate limit",
        "secondary rate limit",
        "abuse detection",
        "timed out",
        "request timeout",
        "network timeout",
        "operation timeout",
        "connection reset",
        "connection refused",
        "temporarily unavailable",
        "temporary failure",
        "try again later",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "status 429",
        "status 500",
        "status 502",
        "status 503",
        "status 504",
    )
    if any(text.startswith(marker) for marker in markers):
        return True
    if text.startswith(("error", "failed", "failure", "gh:", "api")) and "timeout" in text:
        return True
    # Some runners prefix the failure with a generic label, for example
    # ``ERROR: timeout`` or ``HTTP 503: ...``. Keep this bounded so a normal
    # plain-text API value containing the word ``timeout`` is not retried.
    prefixes = (
        "error",
        "failed",
        "failure",
        "http",
        "status",
        "code",
        "reason",
        "api",
        "github",
        "gh:",
    )
    return any(
        text.startswith(prefix) and marker in text
        for prefix in prefixes
        for marker in markers
    )
