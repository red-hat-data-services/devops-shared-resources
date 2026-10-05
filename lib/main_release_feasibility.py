"""Check whether a proposed stable commit can sync into active release branches."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import yaml

from lib.git_utils import GitCommandError, run_git

GLOBAL_IGNORE_FILES = (".github/renovate.json", ".tekton/README.md")
_REPOSITORY_URL = re.compile(
    r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?", re.IGNORECASE
)
# Disposable clones have their own local configuration. Keep global merge
# drivers and identity out of the simulation; configure only the 'ours' driver.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_LFS_SKIP_SMUDGE": "1",
}


class _ConfigLoader(yaml.SafeLoader):
    """Keep yes/no as strings, as in the sync workflow's YAML 1.2 yq parser."""

    yaml_implicit_resolvers: ClassVar[dict] = {
        first: [
            resolver for resolver in resolvers
            if resolver[0] != "tag:yaml.org,2002:bool"
        ]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_ConfigLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class FeasibilityError(ValueError):
    """Raised for invalid configuration or an unusable candidate checkout."""


@dataclass(frozen=True)
class ReleasePolicy:
    repository: str
    component: str
    enabled: bool
    releases: tuple[str, ...]
    ignore_files: tuple[str, ...]

    @property
    def skip_reason(self) -> str | None:
        if not self.enabled:
            return "Release automerge is disabled for this repository."
        if not self.releases:
            return "No active release branches (rhoai-2.* is excluded)."
        return None


@dataclass(frozen=True)
class ReleaseResult:
    branch: str
    target_sha: str | None
    status: str  # passed | conflicts | error
    conflict_files: tuple[str, ...] = ()
    ignored_conflicts: tuple[str, ...] = ()
    message: str = ""


def _load_yaml(path: str | Path) -> dict:
    try:
        payload = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_ConfigLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise FeasibilityError(f"Cannot read configuration {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FeasibilityError(f"Configuration {path} must be a YAML mapping")
    return payload


def _ignore_patterns(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise FeasibilityError("ignore-files must be a comma-separated string")
    # The action's IFS=', ' read consumes one line; multiline/tab-delimited
    # values would silently lose exclusions or produce invalid attributes.
    if any(character.isspace() and character != " " for character in value):
        raise FeasibilityError("ignore-files must be a single line separated by commas/spaces")
    patterns = re.split(r"[, ]+", value.strip())
    return tuple(dict.fromkeys(pattern for pattern in patterns if pattern))


def load_release_policy(
    source_map: str | Path,
    releases_file: str | Path,
    *,
    repository: str,
) -> ReleasePolicy:
    """Use the same repo eligibility, releases and exclusions as scheduled sync."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise FeasibilityError("repository must be an OWNER/NAME slug")
    mappings = _load_yaml(source_map).get("git")
    if not isinstance(mappings, list):
        raise FeasibilityError("Source map must contain a 'git' list")

    matches = []
    for entry in mappings:
        if not isinstance(entry, dict):
            raise FeasibilityError("Every source-map entry must be a mapping")
        url = entry.get("repo-url")
        match = _REPOSITORY_URL.fullmatch(url) if isinstance(url, str) else None
        if match is None:
            raise FeasibilityError(f"Invalid source-map repo-url: {url!r}")
        slug = f"{match[1]}/{match[2]}"
        if slug.casefold() == repository.casefold():
            matches.append(entry)
    if len(matches) != 1:
        raise FeasibilityError(
            f"Expected one release source-map entry for {repository}, found {len(matches)}"
        )
    entry = matches[0]
    automerge = entry.get("automerge")
    if not isinstance(automerge, str) or automerge not in ("yes", "no"):
        raise FeasibilityError("Source-map automerge must be 'yes' or 'no'")

    raw_releases = _load_yaml(releases_file).get("releases")
    if not isinstance(raw_releases, list):
        raise FeasibilityError("Release configuration must contain a 'releases' list")
    releases: list[str] = []
    for branch in raw_releases:
        if not isinstance(branch, str) or not branch or branch != branch.strip():
            raise FeasibilityError(f"Invalid release branch: {branch!r}")
        if run_git(["check-ref-format", f"refs/heads/{branch}"], check=False).returncode:
            raise FeasibilityError(f"Invalid release branch: {branch!r}")
        if not branch.startswith("rhoai-2.") and branch not in releases:
            releases.append(branch)

    return ReleasePolicy(
        repository=repository,
        component=str(entry.get("name") or repository),
        enabled=automerge == "yes",
        releases=tuple(releases),
        ignore_files=tuple(dict.fromkeys((
            *_ignore_patterns(entry.get("ignore-files")), *GLOBAL_IGNORE_FILES
        ))),
    )


def _resolve_commit(repo: Path, ref: str) -> str:
    return run_git(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=repo,
        env=_GIT_ENV,
    ).stdout.strip()


def _resolve_release(repo: Path, branch: str) -> str:
    # actions/checkout fetch-depth: 0 populates remote branches. Local branches
    # are also supported for reproductions and local integration tests.
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        try:
            return _resolve_commit(repo, ref)
        except GitCommandError:
            pass
    raise FeasibilityError(f"Release branch {branch!r} is missing from the checkout")


def _path_is_ignored(path: str, patterns: Sequence[str]) -> bool:
    """Use the same Bash matcher as sync-git-branches, not Python fnmatch."""
    result = subprocess.run(
        [
            "bash", "--noprofile", "--norc", "-c",
            (
                'file=$1; shift; for exclusion in "$@"; do '
                'if [[ "$file" == $exclusion ]]; then exit 0; fi; done; exit 1'
            ),
            "--", path, *patterns,
        ],
        env={**os.environ, "BASH_ENV": "/dev/null"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise FeasibilityError(f"Cannot evaluate ignore patterns: {result.stderr.strip()}")
    return result.returncode == 0


def _resolve_ignored_conflicts(work: Path, paths: Sequence[str]) -> None:
    """Actually restore the downstream side or deletion in the disposable index."""
    for path in paths:
        ours_exists = run_git(
            ["cat-file", "-e", f":2:{path}"], cwd=work, check=False, env=_GIT_ENV
        ).returncode == 0
        if ours_exists:
            run_git(["checkout", "--ours", "--", path], cwd=work, env=_GIT_ENV)
            run_git(["add", "--", path], cwd=work, env=_GIT_ENV)
        else:
            run_git(["rm", "-f", "--", path], cwd=work, env=_GIT_ENV)


def _check_release(
    repo: Path,
    *,
    branch: str,
    source_sha: str,
    ignore_files: Sequence[str],
) -> ReleaseResult:
    target_sha = None
    try:
        target_sha = _resolve_release(repo, branch)
        with tempfile.TemporaryDirectory(prefix="release-feasibility-") as directory:
            work = Path(directory) / "repository"
            run_git(
                ["clone", "--shared", "--no-checkout", "--", str(repo), str(work)],
                env=_GIT_ENV,
            )
            run_git(["checkout", "--detach", target_sha], cwd=work, env=_GIT_ENV)
            attributes = work / ".git" / "info" / "attributes"
            attributes.parent.mkdir(parents=True, exist_ok=True)
            attributes.write_text(
                "".join(f"{pattern} merge=ours\n" for pattern in ignore_files),
                encoding="utf-8",
            )
            merge = run_git(
                [
                    # Model the Linux sync runner even in local reproductions
                    # on a case-insensitive filesystem (Git attributes honor it).
                    "-c", "core.ignoreCase=false",
                    "-c", "merge.ours.driver=true",
                    "-c", "user.name=release-feasibility",
                    "-c", "user.email=release-feasibility@users.noreply.github.com",
                    "merge", "--no-commit", "--no-ff", "--no-edit", source_sha,
                ],
                cwd=work,
                check=False,
                env=_GIT_ENV,
            )
            message = ((merge.stdout or "") + (merge.stderr or "")).strip()
            unmerged = run_git(
                ["diff", "--name-only", "--diff-filter=U", "-z"], cwd=work, env=_GIT_ENV
            ).stdout.split("\0")
            # Bash's [[ "$file" == $pattern ]] in sync-git-branches is
            # case-sensitive and allows * to match directory separators.
            ignored = tuple(
                path for path in unmerged
                if path and _path_is_ignored(path, ignore_files)
            )
            conflicts = tuple(path for path in unmerged if path and path not in ignored)
            _resolve_ignored_conflicts(work, ignored)
            remaining = run_git(
                ["diff", "--name-only", "--diff-filter=U", "-z"], cwd=work, env=_GIT_ENV
            ).stdout.split("\0")
            if any(path in remaining for path in ignored):
                raise FeasibilityError("Excluded conflicts could not be resolved")
            if conflicts:
                status = "conflicts"
            elif merge.returncode and (merge.returncode != 1 or not ignored):
                status = "error"
            else:
                # Excluded conflicts are resolved in the index. No commit or
                # final-tree restoration is needed to test merge feasibility.
                status = "passed"
            return ReleaseResult(branch, target_sha, status, conflicts, ignored, message)
    except (GitCommandError, FeasibilityError, OSError) as exc:
        return ReleaseResult(branch, target_sha, "error", message=str(exc))


def check_releases(
    repo_path: str | Path,
    *,
    source_ref: str,
    policy: ReleasePolicy,
) -> tuple[str, tuple[ReleaseResult, ...]]:
    """Simulate every target independently without changing or pushing the input repo."""
    repo = Path(repo_path).resolve()
    shallow = run_git(["rev-parse", "--is-shallow-repository"], cwd=repo, env=_GIT_ENV)
    if shallow.stdout.strip() == "true":
        raise FeasibilityError("Full Git history is required; fetch with fetch-depth: 0")
    source_sha = _resolve_commit(repo, source_ref)
    if policy.skip_reason:
        return source_sha, ()
    results = tuple(
        _check_release(
            repo, branch=branch, source_sha=source_sha, ignore_files=policy.ignore_files
        )
        for branch in policy.releases
    )
    return source_sha, results
