"""Low-level git helpers used by merge and sync tooling."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse


@dataclass(frozen=True)
class GitCommit:
    """A commit returned by git log."""

    sha: str
    short_sha: str
    subject: str


class GitCommandError(RuntimeError):
    """Raised when a git command fails."""

    def __init__(self, command: Sequence[str], returncode: int, output: str) -> None:
        self.command = list(command)
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"git command failed ({returncode}): {' '.join(self.command)}\n{output}"
        )


def run_git(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=merged_env,
        check=False,
    )
    if check and result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        raise GitCommandError(command, result.returncode, output.strip())
    return result


def is_local_path(repo: str) -> bool:
    if repo.startswith(("/", "./", "../", "~")):
        return True
    return Path(repo).expanduser().exists()


def normalize_repo_url(repo: str) -> str:
    repo = repo.strip()
    if is_local_path(repo):
        return str(Path(repo).expanduser().resolve())
    if not repo.endswith(".git") and "github.com/" in repo:
        return f"{repo}.git" if not repo.endswith("/") else f"{repo.rstrip('/')}.git"
    return repo


def parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Return (owner, repo) for a GitHub HTTPS or SSH URL."""
    if repo_url.startswith("git@github.com:"):
        path = repo_url.split(":", 1)[1]
    else:
        parsed = urlparse(repo_url)
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    owner, name = path.split("/", 1)
    return owner, name


def authenticated_clone_url(repo_url: str, token: str | None) -> str:
    if is_local_path(repo_url):
        return normalize_repo_url(repo_url)
    if not token:
        return normalize_repo_url(repo_url)
    owner, name = parse_github_repo(repo_url)
    return f"https://x-access-token:{token}@github.com/{owner}/{name}.git"


def _git_config_value(repo_path: str | Path, key: str) -> str:
    result = run_git(["config", key], cwd=repo_path, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _default_git_identity_from_env() -> tuple[str, str]:
    name = os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GITHUB_ACTOR")
    email = os.environ.get("GIT_AUTHOR_EMAIL")
    if not email and os.environ.get("GITHUB_ACTOR"):
        actor = os.environ["GITHUB_ACTOR"]
        actor_id = os.environ.get("GITHUB_ACTOR_ID")
        if actor_id:
            email = f"{actor_id}+{actor}@users.noreply.github.com"
        else:
            email = f"{actor}@users.noreply.github.com"
    return name or "sync-branches-bot", email or "sync-branches-bot@users.noreply.github.com"


def ensure_git_identity(repo_path: str | Path) -> None:
    """Configure local git identity only when no effective user.name/email is set."""
    if _git_config_value(repo_path, "user.name") and _git_config_value(repo_path, "user.email"):
        return

    name, email = _default_git_identity_from_env()
    if not _git_config_value(repo_path, "user.name"):
        run_git(["config", "user.name", name], cwd=repo_path)
    if not _git_config_value(repo_path, "user.email"):
        run_git(["config", "user.email", email], cwd=repo_path)


def clone_repo(
    repo_url: str,
    destination: str | Path,
    *,
    token: str | None = None,
    branch: str | None = None,
) -> Path:
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    clone_url = authenticated_clone_url(repo_url, token)
    args = ["clone"]
    if branch:
        args.extend(["--branch", branch])
    args.extend([clone_url, str(destination)])
    run_git(args)
    ensure_git_identity(destination)
    return destination


def add_remote(
    repo_path: str | Path,
    name: str,
    repo_url: str,
    *,
    token: str | None = None,
) -> None:
    url = authenticated_clone_url(repo_url, token) if not is_local_path(repo_url) else normalize_repo_url(repo_url)
    existing = run_git(["remote"], cwd=repo_path, check=False).stdout.splitlines()
    if name in existing:
        run_git(["remote", "set-url", name, url], cwd=repo_path)
    else:
        run_git(["remote", "add", name, url], cwd=repo_path)


def fetch_remote(
    repo_path: str | Path,
    remote: str,
    *,
    refspec: str | None = None,
    tags: bool = False,
) -> None:
    args = ["fetch", remote]
    if tags:
        args.append("--tags")
    if refspec:
        args.append(refspec)
    run_git(args, cwd=repo_path)


def checkout_branch(repo_path: str | Path, branch: str, *, create: bool = False) -> None:
    args = ["checkout"]
    if create:
        args.append("-B")
    else:
        args.append("-B")
    args.append(branch)
    run_git(args, cwd=repo_path)


def checkout_remote_branch(repo_path: str | Path, branch: str) -> None:
    run_git(["fetch", "origin", branch], cwd=repo_path)
    run_git(["checkout", "-B", branch, f"origin/{branch}"], cwd=repo_path)


def current_branch(repo_path: str | Path) -> str:
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path).stdout.strip()


def rev_parse(repo_path: str | Path, ref: str) -> str:
    return run_git(["rev-parse", ref], cwd=repo_path).stdout.strip()


def remote_branch_exists(
    repo_url: str,
    branch: str,
    *,
    token: str | None = None,
) -> bool:
    """Return whether ``branch`` exists on ``repo_url``.

    A missing ref is false. A failed lookup is raised so a transport or
    repository error is not treated as "the branch needs creating".
    """
    ref = f"refs/heads/{branch}"
    if is_local_path(repo_url):
        repo = normalize_repo_url(repo_url)
        result = run_git(["show-ref", "--verify", "--quiet", ref], cwd=repo, check=False)
        if result.returncode in (0, 1):
            return result.returncode == 0
        output = (result.stdout or "") + (result.stderr or "")
        raise GitCommandError(
            ["git", "show-ref", "--verify", "--quiet", ref],
            result.returncode,
            output.strip(),
        )

    url = authenticated_clone_url(repo_url, token)
    result = run_git(["ls-remote", "--heads", url, ref], check=False)
    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        raise GitCommandError(
            ["git", "ls-remote", "--heads", ref],
            result.returncode,
            output.strip(),
        )
    return bool(result.stdout.strip())


def resolve_branch_ref(
    repo_path: str | Path,
    branch: str,
    *,
    remote: str = "origin",
) -> str:
    """Return the best available ref for a branch in a local clone."""
    remote_ref = f"{remote}/{branch}"
    if run_git(["rev-parse", "--verify", remote_ref], cwd=repo_path, check=False).returncode == 0:
        return remote_ref
    if run_git(["rev-parse", "--verify", branch], cwd=repo_path, check=False).returncode == 0:
        return branch
    raise GitCommandError(
        ["rev-parse", "--verify", remote_ref],
        128,
        f"could not resolve branch ref for {branch}",
    )


def list_commits_between(
    repo_path: str | Path,
    base_ref: str,
    head_ref: str,
) -> list[GitCommit]:
    """Return commits reachable from head_ref but not base_ref, newest first."""
    result = run_git(
        ["log", f"{base_ref}..{head_ref}", "--pretty=format:%H%x1f%h%x1f%s"],
        cwd=repo_path,
    )
    commits: list[GitCommit] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        sha, short_sha, subject = line.split("\x1f", 2)
        commits.append(GitCommit(sha=sha, short_sha=short_sha, subject=subject))
    return commits


def format_commit_url(repo_url: str, sha: str) -> str:
    """Return a GitHub commit URL, or the SHA for local repositories."""
    if is_local_path(repo_url):
        return sha
    owner, name = parse_github_repo(repo_url)
    return f"https://github.com/{owner}/{name}/commit/{sha}"


def push_branch(
    repo_path: str | Path,
    branch: str,
    *,
    remote: str = "origin",
    force: bool = False,
    dry_run: bool = False,
) -> None:
    args = ["push", remote, branch]
    if force:
        args.insert(1, "--force")
    if dry_run:
        args.insert(1, "--dry-run")
    run_git(args, cwd=repo_path)


def file_matches_pattern(path: str, pattern: str) -> bool:
    """Match a file path against a glob-style exclusion pattern."""
    import fnmatch

    normalized = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if fnmatch.fnmatch(normalized, pattern):
        return True
    if normalized == pattern or normalized.startswith(f"{pattern}/"):
        return True
    return False


def paths_matching_patterns(paths: Iterable[str], patterns: Sequence[str]) -> set[str]:
    matched: set[str] = set()
    for path in paths:
        for pattern in patterns:
            if file_matches_pattern(path, pattern):
                matched.add(path)
                break
    return matched


def extract_repo_slug(repo_url: str) -> str:
    if is_local_path(repo_url):
        return Path(repo_url).name
    owner, name = parse_github_repo(repo_url)
    return f"{owner}/{name}"
