#!/usr/bin/env python3
"""CLI to post GAP commit statuses on GitHub pull requests."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.pr_status_updater import (
    GhCommandError,
    PRStatusUpdater,
    StatusUpdateBatchError,
    StatusUpdateResult,
    normalize_status,
    validate_check_name,
)


DEFAULT_CHECK_NAME = "gated artifacts promoter"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Post or update GitHub commit statuses on PR head commits.",
    )
    parser.add_argument(
        "--check-name",
        default=DEFAULT_CHECK_NAME,
        help=f"Commit status context name (default: {DEFAULT_CHECK_NAME!r}).",
    )
    parser.add_argument(
        "--status",
        required=True,
        help="Status to post: success, failure, pending, error, or GAP aliases like merge-failure.",
    )
    parser.add_argument(
        "--pr-url",
        action="append",
        default=[],
        metavar="URL",
        help=(
            "Pull request URL (repeatable). Required unless using --repo with --label."
        ),
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help=(
            "Repository slug or GitHub URL (repeatable). Requires --label; "
            "finds open PRs with that label."
        ),
    )
    parser.add_argument(
        "--label",
        metavar="NAME",
        help="Label on open PRs; required when any --repo is given.",
    )
    parser.add_argument(
        "--description",
        help="Optional short description shown in the GitHub UI.",
    )
    parser.add_argument(
        "--target-url",
        help="Optional URL linked from the status check.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print gh commands without calling the GitHub API.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="When updating multiple PRs, keep going if one PR fails.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        validate_check_name(args.check_name)
        normalize_status(args.status)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    updater = PRStatusUpdater(
        check_name=args.check_name,
        dry_run=args.dry_run,
    )

    try:
        pr_urls = updater.resolve_pr_urls(args.pr_url, args.repo, args.label)
        results = updater.post_status_for_many(
            pr_urls,
            args.status,
            description=args.description,
            target_url=args.target_url,
            continue_on_error=args.continue_on_error,
        )
    except StatusUpdateBatchError as exc:
        for result in exc.successes:
            _print_result(result)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, GhCommandError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for result in results:
        _print_result(result)
    return 0


def _print_result(result: StatusUpdateResult) -> None:
    prefix = "[dry-run] " if result.dry_run else ""
    print(
        f"{prefix}Posted {result.state} for {result.pr.url} "
        f"(sha={result.head_sha}, context={result.context})"
    )


if __name__ == "__main__":
    raise SystemExit(main())
