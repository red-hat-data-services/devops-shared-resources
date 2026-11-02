"""Revision-pinned changed-file collections, independent of CEL and GAP.

GitHub comparisons return at most 300 files. Ambiguous/incomplete responses
fall back to a temporary bare partial fetch and merge-base-to-head Git diff.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time

from lib.github_check_runs import CheckQueryError, GhRunner, github_api_json
from lib.github_cli import GhCommandError

_SHA = re.compile(r"[0-9a-fA-F]{40}")
_NAME = re.compile(r"[A-Za-z0-9_.-]+")
MAX_DIFF_BYTES = 32 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 120


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise CheckQueryError("comparison requires full immutable commit SHAs")
    return value


def file_collections(files: list[dict]) -> dict[str, tuple[str, ...]]:
    """Normalize API or Git changes; renamed paths use their new filename."""
    groups: dict[str, list[str]] = {key: [] for key in ("all", "added", "deleted", "modified", "renamed")}
    statuses = {"added": "added", "removed": "deleted", "modified": "modified",
                "renamed": "renamed", "copied": "added", "changed": "modified"}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str) or not item["filename"]:
            raise CheckQueryError("comparison returned an invalid filename")
        group = statuses.get(item.get("status"))
        if group is None:
            raise CheckQueryError("comparison returned an unknown file status")
        groups["all"].append(item["filename"])
        groups[group].append(item["filename"])
    return {key: tuple(names) for key, names in groups.items()}


def fetch_comparison_files(
    owner: str, repo: str, base_sha: str, head_sha: str, runner: GhRunner,
) -> dict[str, tuple[str, ...]]:
    """Compare pinned revisions; never fall back to the live PR-files endpoint."""
    base_sha, head_sha = _sha(base_sha), _sha(head_sha)
    if not _NAME.fullmatch(owner) or not _NAME.fullmatch(repo) or owner in {".", ".."} or repo in {".", ".."}:
        raise CheckQueryError("invalid GitHub repository name")
    merge_base = None
    try:
        payload = github_api_json(runner, f"repos/{owner}/{repo}/compare/{base_sha}...{head_sha}?per_page=1&page=1")
        if not isinstance(payload, dict):
            raise CheckQueryError("comparison response is not an object")
        if payload.get("base_commit", {}).get("sha") != base_sha:
            raise CheckQueryError("comparison response does not match pinned revisions")
        merge_base = _sha(payload.get("merge_base_commit", {}).get("sha"))
        files = payload.get("files")
        if not isinstance(files, list) or len(files) >= 300:
            raise CheckQueryError("comparison file list is missing or potentially truncated at 300 files")
        return file_collections(files)
    except (GhCommandError, CheckQueryError, AttributeError, TypeError):
        logging.getLogger(__name__).warning("Using local Git diff for %s/%s pinned comparison", owner, repo)
    try:
        return _git_file_collections(f"https://github.com/{owner}/{repo}.git", base_sha, head_sha, merge_base)
    except (OSError, subprocess.SubprocessError, ValueError, CheckQueryError) as exc:
        raise CheckQueryError(f"could not obtain complete pinned file comparison for {owner}/{repo}: {type(exc).__name__}") from exc


def _parse_name_status(data: bytes) -> list[dict[str, str]]:
    """Parse Git's NUL-delimited status/path format, including rename pairs."""
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise CheckQueryError("incomplete Git diff output")
    tokens = data[:-1].decode("utf-8").split("\0")
    files = []
    index = 0
    statuses = {"A": "added", "D": "removed", "M": "modified", "T": "modified", "R": "renamed", "C": "copied"}
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status or status[0] not in statuses:
            raise CheckQueryError("unsupported Git diff status")
        if status[0] in {"R", "C"}:
            index += 1  # consume old filename; CEL uses the new filename
        if index >= len(tokens) or not tokens[index]:
            raise CheckQueryError("incomplete Git diff filename")
        files.append({"filename": tokens[index], "status": statuses[status[0]]})
        index += 1
    return files


def _git_file_collections(url: str, base_sha: str, head_sha: str, merge_base: str | None = None) -> dict[str, tuple[str, ...]]:
    """Bounded no-checkout fetch; no credential helpers, hooks, or external diff.

    No shallow boundary when finding a merge base locally. With an API-provided
    merge base only the three commit tips are needed. Rename detection may lazily
    download blobs. Private repositories requiring Git credentials fail closed.
    """
    _sha(base_sha)
    _sha(head_sha)
    if merge_base is not None:
        _sha(merge_base)
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    with tempfile.TemporaryDirectory(prefix="github-pinned-diff-") as directory:
        def git(*args: str) -> bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckQueryError("local diff exceeded time budget")
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                result = subprocess.run(
                    ["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *args],
                    cwd=directory, env=env, stdout=stdout, stderr=stderr,
                    timeout=remaining, check=False,
                )
                if result.returncode:
                    raise CheckQueryError("local Git comparison command failed")
                if stdout.tell() > MAX_DIFF_BYTES:
                    raise CheckQueryError("local Git comparison exceeded output limit")
                stdout.seek(0)
                return stdout.read()
        git("init", "--bare", "--template=", ".")
        git("remote", "add", "origin", url)
        args = ["fetch", "--filter=blob:none", "--no-tags"]
        if merge_base:
            args.append("--depth=1")
        git(*args, "origin", *dict.fromkeys([base_sha, head_sha, *([merge_base] if merge_base else [])]))
        if merge_base is None:
            merge_base = _sha(git("merge-base", base_sha, head_sha).decode().strip())
        changes = git("diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "--find-renames", merge_base, head_sha, "--")
        return file_collections(_parse_name_status(changes))
