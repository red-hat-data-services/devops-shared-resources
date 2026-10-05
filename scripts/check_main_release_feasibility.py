#!/usr/bin/env python3
"""Read-only CLI for the ruleset-required main-to-release feasibility gate."""

from __future__ import annotations

import argparse
import html
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.git_utils import GitCommandError, run_git
from lib.main_release_feasibility import (
    FeasibilityError,
    ReleasePolicy,
    ReleaseResult,
    check_releases,
    load_release_policy,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-path", required=True, help="Component checkout with full history.")
    parser.add_argument("--repository", required=True, help="GitHub OWNER/NAME slug.")
    parser.add_argument("--source-ref", default="HEAD", help="Proposed post-merge stable commit.")
    parser.add_argument("--source-map", required=True, help="Infra main-release-source-map.yaml.")
    parser.add_argument("--releases", required=True, help="Infra releases.yaml.")
    parser.add_argument(
        "--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="Append a Markdown report to this file.",
    )
    return parser


def _code(value: str) -> str:
    """Keep paths and diagnostic text from breaking the Markdown report."""
    escaped = html.escape(value, quote=False).replace("|", "&#124;")
    return "<code>" + escaped.replace("\n", " ").replace("\r", " ") + "</code>"


def format_report(
    policy: ReleasePolicy,
    source_sha: str,
    results: tuple[ReleaseResult, ...],
    *,
    config_sha: str,
) -> str:
    lines = [
        "## Main-to-release feasibility",
        "",
        f"**Repository:** {_code(policy.repository)}",
        f"**Candidate stable commit:** {_code(source_sha)}",
        f"**Infra configuration commit:** {_code(config_sha)}",
        f"**Ignored paths:** {', '.join(_code(pattern) for pattern in policy.ignore_files)}",
        "",
    ]
    if policy.skip_reason:
        lines.append(f"**Not applicable:** {policy.skip_reason}")
    else:
        lines.extend([
            "| Release | Target commit | Result | Blocking conflicts |",
            "| --- | --- | --- | --- |",
        ])
        for result in results:
            conflicts = ", ".join(_code(path) for path in result.conflict_files) or "—"
            lines.append(
                f"| {_code(result.branch)} | {_code(result.target_sha or 'missing')} | {result.status} | {conflicts} |"
            )
        passed = all(result.status == "passed" for result in results)
        lines.extend([
            "",
            "**PASSED**" if passed else (
                "**FAILED — this component cannot be promoted until the release "
                "conflicts or errors are resolved.**"
            ),
        ])
        for result in results:
            if result.ignored_conflicts:
                paths = ", ".join(_code(path) for path in result.ignored_conflicts)
                lines.extend(["", f"{_code(result.branch)}: ignored conflicts on {paths}"])
            if result.status == "error":
                lines.extend(["", f"{_code(result.branch)} error: {_code(result.message)}"])
    return "\n".join(lines) + "\n"


def _publish(report: str, summary: str | None) -> None:
    print(report)
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = load_release_policy(args.source_map, args.releases, repository=args.repository)
        # Config files are checked out together in Actions. Local standalone
        # YAML files are supported too, with an explicit unknown revision.
        revision = run_git(
            ["rev-parse", "HEAD"], cwd=Path(args.source_map).resolve().parent,
            check=False,
        )
        config_sha = revision.stdout.strip() if revision.returncode == 0 else "unknown (local files)"
        if policy.skip_reason:
            source_sha, results = args.source_ref, ()
        else:
            source_sha, results = check_releases(
                args.repo_path, source_ref=args.source_ref, policy=policy
            )
        for result in results:
            print(f"=== {result.branch}: {result.status} ===")
            print(result.message)
        _publish(format_report(policy, source_sha, results, config_sha=config_sha), args.summary)
        return 1 if any(result.status != "passed" for result in results) else 0
    except (FeasibilityError, GitCommandError, OSError) as exc:
        report = f"## Main-to-release feasibility\n\n**FAILED:** {_code(str(exc))}\n"
        print(f"ERROR: {exc}", file=sys.stderr)
        _publish(report, args.summary)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
