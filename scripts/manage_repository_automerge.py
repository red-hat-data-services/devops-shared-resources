#!/usr/bin/env python3
"""Audit or enable GitHub repository auto-merge."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.github_cli import GhCommandError
from lib.repository_automerge import (
    RepositoryAutoMergeError,
    RepositoryAutoMergeResult,
    check_authentication,
    manage_repository_automerge,
)


DEFAULT_ORG = "red-hat-data-services"


def normalize_repository(value: str, *, default_org: str) -> str:
    """Normalize a bare repository name or owner/name slug."""
    repository = value.strip()
    if not repository:
        raise ValueError("repository names cannot be empty")
    if any(character.isspace() for character in repository):
        raise ValueError(f"invalid repository name {value!r}: whitespace is not allowed")

    parts = repository.split("/")
    if len(parts) == 1:
        if not default_org:
            raise ValueError("--org cannot be empty when using bare repository names")
        return f"{default_org}/{repository}"
    if len(parts) == 2 and all(parts):
        return repository
    raise ValueError(
        f"invalid repository name {value!r}: expected NAME or OWNER/NAME"
    )


def collect_repositories(
    positional: Iterable[str],
    *,
    stdin: TextIO,
    default_org: str,
) -> list[str]:
    """Collect ordered, de-duplicated repositories from arguments or stdin."""
    values = list(positional)
    if not values:
        if stdin.isatty():
            raise ValueError("provide repository arguments or newline-separated stdin")
        values = [line.strip() for line in stdin if line.strip()]
    if not values:
        raise ValueError("provide at least one repository")

    repositories: list[str] = []
    seen: set[str] = set()
    for value in values:
        repository = normalize_repository(value, default_org=default_org)
        if repository not in seen:
            seen.add(repository)
            repositories.append(repository)
    return repositories


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit or enable GitHub repository auto-merge. Repositories are read "
            "from positional arguments, or newline-separated stdin when no "
            "arguments are provided. The default mode is read-only."
        )
    )
    parser.add_argument(
        "repositories",
        nargs="*",
        metavar="REPOSITORY",
        help="Bare repository name or OWNER/NAME slug.",
    )
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG,
        help=f"Organization for bare names (default: {DEFAULT_ORG}).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Enable auto-merge; without this flag the command only audits.",
    )
    return parser


def _print_result(result: RepositoryAutoMergeResult) -> None:
    labels = {
        "already_enabled": "ALREADY_ENABLED",
        "would_enable": "WOULD_ENABLE",
        "enabled": "ENABLED",
    }
    print(f"{labels[result.status]:<16} {result.repository}")


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    input_stream = stdin or sys.stdin

    try:
        repositories = collect_repositories(
            args.repositories,
            stdin=input_stream,
            default_org=args.org.strip(),
        )
        check_authentication()
    except (ValueError, GhCommandError, RepositoryAutoMergeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    counts = {"already_enabled": 0, "would_enable": 0, "enabled": 0}
    failures = 0
    for repository in repositories:
        try:
            result = manage_repository_automerge(repository, apply=args.apply)
            _print_result(result)
            counts[result.status] += 1
        except (GhCommandError, RepositoryAutoMergeError) as exc:
            print(f"FAILED           {repository}: {exc}", file=sys.stderr)
            failures += 1

    print(
        "\nSummary: "
        f"total={len(repositories)} "
        f"already_enabled={counts['already_enabled']} "
        f"would_enable={counts['would_enable']} "
        f"updated={counts['enabled']} "
        f"failures={failures}"
    )
    if not args.apply and counts["would_enable"]:
        print("Audit only. Re-run with --apply to enable auto-merge.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
