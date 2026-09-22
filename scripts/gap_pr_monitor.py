#!/usr/bin/env python3
"""CLI for Stage-1 GAP PR monitor (RHOAIENG-93565)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.gap_pr_monitor import (
    DEFAULT_CHECK_NAME,
    GapPrMonitorError,
    run_stage1_monitor,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stage-1 GAP PR monitor: read a leader state.json, post a green "
            "'gated artifacts promoter' commit status on each listed PR, and "
            "update pr-status to success in the state file."
        )
    )
    parser.add_argument(
        "--state-file",
        required=True,
        metavar="PATH",
        help="Path to the leader GAP state.json file.",
    )
    parser.add_argument(
        "--check-name",
        default=DEFAULT_CHECK_NAME,
        help=f"Commit status context (default: {DEFAULT_CHECK_NAME!r}).",
    )
    parser.add_argument(
        "--description",
        default="GAP Stage 1 dummy gate",
        help="Optional description posted with each commit status.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pass through to the status updater (read-only gh for SHAs; "
            "do not POST statuses or write the state file)."
        ),
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep processing remaining PRs if one status update fails.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_stage1_monitor(
            Path(args.state_file),
            check_name=args.check_name,
            dry_run=args.dry_run,
            continue_on_error=args.continue_on_error,
            description=args.description,
        )
    except GapPrMonitorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    prefix = "[dry-run] " if result.dry_run else ""
    print(
        f"{prefix}Processed {len(result.updated_urls) + len(result.skipped_urls)} PR(s) "
        f"from {result.state_path}"
    )
    for url in result.updated_urls:
        print(f"{prefix}Posted completed for {url}")
    for url in result.skipped_urls:
        print(f"{prefix}Skipped (already set) for {url}")
    if not result.dry_run:
        print(f"Updated state file pr-status to success: {result.state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
