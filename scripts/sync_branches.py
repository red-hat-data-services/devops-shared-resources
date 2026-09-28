#!/usr/bin/env python3
"""Sync one git branch into another via PR, direct push, or merge commit."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.config_parser import (
    ConfigError,
    filter_sync_entries,
    load_sync_config,
    normalize_sync_entry,
)
from lib.git_utils import (
    add_remote,
    checkout_remote_branch,
    clone_repo,
    fetch_remote,
    is_local_path,
    list_commits_between,
    normalize_repo_url,
    resolve_branch_ref,
    push_branch,
    run_git,
)
from lib.merge_resolver import MergeError, merge_branches
from lib.pr_creator import (
    PRCreator,
    format_default_pr_body,
    format_default_pr_title,
)


@dataclass
class SyncOutcome:
    sync_type: str
    message: str
    pr_url: str | None = None
    branch: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync a source branch into a target branch.",
    )
    parser.add_argument(
        "--config",
        help="YAML config file with one or more sync entries. "
        "When set, per-entry CLI flags below must not be used.",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated sync entry names to run. Requires --config.",
    )
    parser.add_argument(
        "--sync-type",
        choices=["pr", "push", "commit-merge"],
        default=None,
        help="Sync mode: open a PR (default), push a bootstrap branch, or merge directly.",
    )
    parser.add_argument("--source-repo", default=None, help="Source repository URL or local path.")
    parser.add_argument("--source-branch", default=None, help="Source branch to merge from.")
    parser.add_argument(
        "--target-repo",
        default=None,
        help="Target repository URL or local path. Defaults to the source repo.",
    )
    parser.add_argument("--target-branch", default=None, help="Target branch to merge into.")
    parser.add_argument(
        "--ignore-files",
        nargs="*",
        default=None,
        help="Files or glob patterns to keep at the target version on conflict.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan actions without changing remotes.")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print extra details such as merge conflict file lists.",
    )
    parser.add_argument("--pr-branch", help="Branch name to use when opening a PR.")
    parser.add_argument(
        "--pr-head",
        choices=["sync-branch", "source"],
        default=None,
        help=(
            "PR head strategy: merge into a temporary sync branch (default) or open the PR "
            "directly from the source branch."
        ),
    )
    parser.add_argument("--tracking-label", help="Reuse an open PR with this label instead of creating a new one.")
    parser.add_argument("--label", dest="labels", action="append", default=[], help="Additional PR label.")
    parser.add_argument("--automerge", action="store_true", help="Enable GitHub automerge on created PRs.")
    parser.add_argument("--pr-title", help="Custom PR title.")
    parser.add_argument("--pr-body", help="Custom PR body.")
    parser.add_argument("--reviewer", dest="reviewers", action="append", default=[], help="PR reviewer to request.")
    return parser


def _validate_cli_exclusivity(args: argparse.Namespace) -> None:
    if args.only and not args.config:
        raise ConfigError("--only requires --config")
    if not args.config:
        return
    conflicting = []
    flag_names = {
        "sync_type": "--sync-type",
        "source_repo": "--source-repo",
        "source_branch": "--source-branch",
        "target_repo": "--target-repo",
        "target_branch": "--target-branch",
        "ignore_files": "--ignore-files",
    }
    for name, flag in flag_names.items():
        if getattr(args, name) is not None:
            conflicting.append(flag)
    if conflicting:
        raise ConfigError(
            "When --config is provided, do not also pass: " + ", ".join(conflicting)
        )


def _entry_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if not args.source_repo or not args.source_branch or not args.target_branch:
        raise ConfigError(
            "Without --config, --source-repo, --source-branch, and --target-branch are required."
        )
    return normalize_sync_entry(
        {
            "sync-type": args.sync_type or "pr",
            "src": {"url": args.source_repo, "branch": args.source_branch},
            "dest": {
                "url": args.target_repo or args.source_repo,
                "branch": args.target_branch,
            },
            "ignore-files": args.ignore_files or [],
            "pr": {
                "branch": args.pr_branch,
                "head-strategy": args.pr_head,
                "tracking-label": args.tracking_label,
                "labels": args.labels,
                "automerge": args.automerge,
                "title": args.pr_title,
                "body": args.pr_body,
                "reviewers": args.reviewers,
            },
        }
    )


def resolve_github_token() -> str | None:
    """Read a GitHub token from the environment."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("SYNC_TOKEN")


def _entry_needs_github_token(entry: dict[str, Any]) -> bool:
    if entry["sync_type"] == "pr":
        return True
    return not is_local_path(entry["src"]["url"]) or not is_local_path(entry["dest"]["url"])


def _require_github_token(entry: dict[str, Any], token: str | None) -> None:
    if not _entry_needs_github_token(entry):
        return
    if not token:
        raise ConfigError(
            "A GitHub token is required. Set the GITHUB_TOKEN or SYNC_TOKEN environment variable."
        )


def _uses_source_pr_head(entry: dict[str, Any]) -> bool:
    return entry["sync_type"] == "pr" and entry["pr"]["head_strategy"] == "source"


def _validate_source_pr_head(entry: dict[str, Any]) -> None:
    source_url = normalize_repo_url(entry["src"]["url"])
    target_url = normalize_repo_url(entry["dest"]["url"])
    if source_url != target_url:
        raise ConfigError(
            "pr head strategy 'source' requires the source and target repositories to be the same."
        )
    if entry["ignore_files"]:
        raise ConfigError(
            "pr head strategy 'source' cannot be used with ignore-files; "
            "use 'sync-branch' when you need ignore-files."
        )


def _collect_sync_commits(
    entry: dict[str, Any],
    *,
    token: str | None,
) -> tuple[Path, list]:
    """Clone the target repo and return (workdir, commits from target..source)."""
    target_url = normalize_repo_url(entry["dest"]["url"])
    source_url = normalize_repo_url(entry["src"]["url"])
    workdir = Path(tempfile.mkdtemp(prefix="sync-branches-commits-"))
    clone_repo(target_url, workdir, token=token)
    if source_url != target_url or not is_local_path(source_url):
        add_remote(workdir, "source", source_url, token=token)
        fetch_remote(workdir, "source", tags=True)
        source_ref = f"source/{entry['src']['branch']}"
    else:
        fetch_remote(workdir, "origin", refspec=entry["src"]["branch"])
        source_ref = f"origin/{entry['src']['branch']}"
    target_ref = resolve_branch_ref(workdir, entry["dest"]["branch"])
    commits = list_commits_between(workdir, target_ref, source_ref)
    return workdir, commits


def _create_or_update_pr(
    entry: dict[str, Any],
    *,
    token: str,
    head_branch: str,
    commits_to_sync: list,
    conflict_files: tuple[str, ...] = (),
    delete_branch_on_merge: bool = True,
) -> SyncOutcome:
    source = entry["src"]
    target = entry["dest"]
    creator = PRCreator(token)
    title = entry["pr"]["title"] or format_default_pr_title(
        source["branch"],
        target["branch"],
        source_repo=source["url"],
        target_repo=target["url"],
    )
    body = entry["pr"]["body"] or format_default_pr_body(
        source["branch"],
        target["branch"],
        source_repo=source["url"],
        target_repo=target["url"],
        commits=commits_to_sync,
        head_branch=head_branch,
        automerge=entry["pr"]["automerge"],
        conflict_files=conflict_files,
    )
    pr_result = creator.create_or_update_tracking_pr(
        target["url"],
        title=title,
        body=body,
        head_branch=head_branch,
        base_branch=target["branch"],
        tracking_label=entry["pr"]["tracking_label"],
        labels=entry["pr"]["labels"],
        reviewers=entry["pr"]["reviewers"],
        automerge=entry["pr"]["automerge"],
        merge_when_ready=entry["pr"].get("merge_when_ready", False),
        delete_branch_on_merge=delete_branch_on_merge,
    )
    action = (
        "Merged"
        if pr_result.merged
        else ("Updated" if pr_result.updated else "Created")
    )
    message = f"{action} pull request #{pr_result.number}"
    if conflict_files:
        message += " (contains merge conflicts)"
    return SyncOutcome(
        sync_type=entry["sync_type"],
        message=message,
        pr_url=pr_result.url,
        branch=pr_result.branch,
    )


def _default_pr_branch(entry: dict[str, Any]) -> str:
    configured = entry["pr"]["branch"]
    if configured:
        return configured
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"sync-{entry['src']['branch']}-to-{entry['dest']['branch']}-{timestamp}"


def _prepare_worktree(
    entry: dict[str, Any],
    *,
    token: str | None,
    work_branch: str,
) -> tuple[Path, str]:
    """Clone the target repo and return (repo_path, source_ref)."""
    target_url = normalize_repo_url(entry["dest"]["url"])
    source_url = normalize_repo_url(entry["src"]["url"])
    workdir = Path(tempfile.mkdtemp(prefix="sync-branches-"))
    clone_repo(target_url, workdir, token=token)

    tracking_branch = None
    if entry["sync_type"] == "pr" and entry["pr"]["tracking_label"] and token:
        creator = PRCreator(token)
        existing = creator.find_open_pr_by_label(
            target_url,
            base_branch=entry["dest"]["branch"],
            label=entry["pr"]["tracking_label"],
        )
        if existing:
            tracking_branch = existing["head"]["ref"]

    checkout_remote_branch(workdir, entry["dest"]["branch"])
    branch_to_use = tracking_branch or work_branch
    run_git(["checkout", "-B", branch_to_use], cwd=workdir)

    if source_url != target_url or not is_local_path(source_url):
        add_remote(workdir, "source", source_url, token=token)
        fetch_remote(workdir, "source", tags=True)
        source_ref = f"source/{entry['src']['branch']}"
    else:
        fetch_remote(workdir, "origin", refspec=entry["src"]["branch"])
        source_ref = f"origin/{entry['src']['branch']}"

    return workdir, source_ref, branch_to_use


def run_sync_entry(
    entry: dict[str, Any],
    *,
    token: str | None,
    dry_run: bool = False,
    verbose: bool = False,
) -> SyncOutcome:
    sync_type = entry["sync_type"]
    source = entry["src"]
    target = entry["dest"]
    pr_branch = _default_pr_branch(entry)

    if not dry_run:
        _require_github_token(entry, token)

    if dry_run:
        pr_head = source["branch"] if _uses_source_pr_head(entry) else pr_branch
        return SyncOutcome(
            sync_type=sync_type,
            message=(
                f"Dry run: would sync {source['url']}:{source['branch']} "
                f"-> {target['url']}:{target['branch']} using {sync_type}"
                + (
                    f" (PR head: {entry['pr']['head_strategy']})"
                    if sync_type == "pr"
                    else ""
                )
            ),
            branch=pr_head if sync_type == "pr" else target["branch"],
        )

    workdir: Path | None = None
    try:
        if _uses_source_pr_head(entry):
            _validate_source_pr_head(entry)
            assert token is not None
            workdir, commits_to_sync = _collect_sync_commits(entry, token=token)
            if not commits_to_sync:
                return SyncOutcome(
                    sync_type=sync_type,
                    message="Target branch is already up to date.",
                    branch=source["branch"],
                )
            return _create_or_update_pr(
                entry,
                token=token,
                head_branch=source["branch"],
                commits_to_sync=commits_to_sync,
                delete_branch_on_merge=False,
            )

        if sync_type == "push":
            workdir = Path(tempfile.mkdtemp(prefix="sync-branches-push-"))
            clone_repo(source["url"], workdir, token=token, branch=source["branch"])
            run_git(["checkout", "-B", target["branch"]], cwd=workdir)
            push_branch(workdir, target["branch"], force=True)
            return SyncOutcome(
                sync_type=sync_type,
                message=f"Pushed bootstrap branch {target['branch']}",
                branch=target["branch"],
            )

        work_branch = target["branch"] if sync_type == "commit-merge" else pr_branch
        workdir, source_ref, work_branch = _prepare_worktree(
            entry, token=token, work_branch=work_branch
        )
        target_ref = resolve_branch_ref(workdir, target["branch"])
        commits_to_sync = list_commits_between(workdir, target_ref, source_ref)

        merge_result = merge_branches(
            workdir,
            source_ref=source_ref,
            ignore_files=entry["ignore_files"],
            merge_args=entry["merge_args"],
            commit_message=f"Sync {source['branch']} into {target['branch']}",
            allow_conflicts=sync_type == "pr",
        )

        if merge_result.conflict_files:
            conflict_summary = ", ".join(merge_result.conflict_files)
            print(
                f"warning: merge conflicts detected in: {conflict_summary}",
                file=sys.stderr,
            )
            if verbose:
                for file_path in merge_result.conflict_files:
                    print(f"  conflict: {file_path}", file=sys.stderr)

        if merge_result.already_up_to_date:
            return SyncOutcome(
                sync_type=sync_type,
                message="Target branch is already up to date.",
                branch=work_branch,
            )

        push_force = sync_type == "pr" and entry["pr"]["tracking_label"] is not None
        push_branch(workdir, work_branch, force=push_force)

        if sync_type == "commit-merge":
            return SyncOutcome(
                sync_type=sync_type,
                message="Merged and pushed directly to target branch.",
                branch=target["branch"],
            )

        assert token is not None
        return _create_or_update_pr(
            entry,
            token=token,
            head_branch=work_branch,
            commits_to_sync=commits_to_sync,
            conflict_files=merge_result.conflict_files,
            delete_branch_on_merge=True,
        )
    finally:
        if workdir and workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        _validate_cli_exclusivity(args)
        if args.config:
            entries = filter_sync_entries(load_sync_config(args.config), args.only)
        else:
            entries = [_entry_from_args(args)]

        token = resolve_github_token()
        outcomes: list[SyncOutcome] = []
        for entry in entries:
            outcome = run_sync_entry(
                entry,
                token=token,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
            outcomes.append(outcome)
            print(outcome.message)
            if outcome.pr_url:
                print(outcome.pr_url)

        return 0
    except (ConfigError, MergeError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
