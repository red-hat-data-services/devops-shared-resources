#!/usr/bin/env python3
"""Prepare non-secret sync action inputs from the supplied infra configuration."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

GLOBAL_IGNORE_LIST = ".github/renovate.json, .tekton/README.md"


class ConfigLoader(yaml.SafeLoader):
    """Read yes/no as strings, matching the existing workflow's YAML 1.2 parser."""


def _yaml_bool(loader, node):
    value = loader.construct_scalar(node)
    if value.lower() in ("true", "false"):
        return yaml.SafeLoader.construct_yaml_bool(loader, node)
    return value


ConfigLoader.add_constructor("tag:yaml.org,2002:bool", _yaml_bool)


def _read_config(path: Path) -> dict:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=ConfigLoader)
    if not isinstance(data, dict):
        raise TypeError(f"Configuration must be a mapping: {path}")
    return data


def _repo_key(url: str) -> str:
    return url.casefold().rstrip("/").removesuffix(".git")


def prepare_inputs(repository: str, source_map: Path, releases_file: Path) -> tuple[bool, dict]:
    repo_url = f"https://github.com/{repository}.git"
    entries = _read_config(source_map).get("git")
    if not isinstance(entries, list):
        raise TypeError("Source map must contain a git list")
    matches = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("repo-url"), str):
            raise TypeError("Each source-map entry must have a repo-url string")
        if _repo_key(entry["repo-url"]) == _repo_key(repo_url):
            matches.append(entry)
    if len(matches) != 1:
        raise ValueError(f"Expected one release source-map entry for {repo_url}")
    mapping = matches[0]
    if mapping.get("automerge") == "no":
        print("Not applicable: release automerge is disabled.", file=sys.stderr)
        return False, {"include": []}
    if mapping.get("automerge") != "yes":
        raise ValueError("automerge must be yes or no")
    releases = _read_config(releases_file).get("releases")
    if not isinstance(releases, list) or not all(isinstance(value, str) for value in releases):
        raise ValueError("Release configuration must contain a releases list of strings")
    releases = sorted({value for value in releases if not value.startswith("rhoai-2.")})
    if not releases:
        print("Not applicable: no active releases.", file=sys.stderr)
        return False, {"include": []}
    source_branch = mapping.get("src-branch")
    source_branch = "" if source_branch is None else source_branch
    ignore = mapping.get("ignore-files")
    ignore = "" if ignore is None else ignore
    if any(not isinstance(value, str) or "\n" in value or "\r" in value for value in (source_branch, ignore)):
        raise ValueError("Action inputs must be single-line strings")
    ignore_files = ", ".join(value for value in (ignore, GLOBAL_IGNORE_LIST) if value)
    return True, {"include": [{
        "upstream_repo": repo_url,
        "upstream_branch": source_branch,
        "downstream_repo": repo_url,
        "downstream_branch": release,
        "ignore_files": ignore_files,
        "merge_args": "--no-edit",
        "spawn_logs": "false",
        "dry_run": "true",
    } for release in releases]}


def _write_outputs(check: bool, matrix: dict) -> None:
    content = f"check={'true' if check else 'false'}\nmatrix={json.dumps(matrix, separators=(',', ':'))}\n"
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        print(content, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", metavar="owner/repo")
    parser.add_argument("source_map", type=Path, metavar="source-map.yaml")
    parser.add_argument("releases_file", type=Path, metavar="releases.yaml")
    args = parser.parse_args(argv)
    try:
        check, matrix = prepare_inputs(args.repository, args.source_map, args.releases_file)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        _write_outputs(False, {"include": []})
        return 1
    _write_outputs(check, matrix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
