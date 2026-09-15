"""Post GitHub commit statuses on pull request head commits."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from urllib.parse import urlparse

PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$"
)

VALID_GITHUB_STATES = frozenset({"error", "failure", "pending", "success"})

# https://docs.github.com/en/rest/commits/statuses#create-a-commit-status
MAX_STATUS_CONTEXT_LENGTH = 255
MAX_STATUS_DESCRIPTION_LENGTH = 140

# Caller-friendly aliases used by GAP state machine (RHOAIENG-93564).
STATUS_ALIASES = {
    "merge-failure": "failure",
    "build-failure": "failure",
    "test-failure": "failure",
    "build-pending": "pending",
    "test-pending": "pending",
    "build-success": "success",
}


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


@dataclass(frozen=True)
class StatusUpdateResult:
    pr: PullRequestRef
    head_sha: str
    state: str
    context: str
    dry_run: bool


class StatusUpdateBatchError(RuntimeError):
    """One or more PRs failed during a batch update."""

    def __init__(
        self,
        failures: Sequence[tuple[str, str]],
        successes: Sequence[StatusUpdateResult],
    ) -> None:
        self.failures = list(failures)
        self.successes = list(successes)
        lines = [f"  - {url}: {err}" for url, err in self.failures]
        super().__init__(
            f"{len(self.failures)} PR(s) failed, {len(self.successes)} succeeded.\n"
            + "\n".join(lines)
        )


class GhCommandError(RuntimeError):
    """Raised when a gh CLI command fails."""

    def __init__(self, command: Sequence[str], returncode: int, output: str) -> None:
        self.command = list(command)
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"gh command failed ({returncode}): {' '.join(self.command)}\n{output}"
        )


def parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Return (owner, repo) for a GitHub HTTPS or SSH URL."""
    repo_url = repo_url.strip()
    if repo_url.startswith("git@github.com:"):
        path = repo_url.split(":", 1)[1]
    else:
        path = urlparse(repo_url).path.lstrip("/")
    path = path.removesuffix(".git")
    if "/" not in path:
        raise ValueError(
            f"Invalid repository URL or slug '{repo_url}'. "
            "Expected owner/name or a github.com URL."
        )
    owner, name = path.split("/", 1)
    if not owner or not name:
        raise ValueError(f"Invalid repository URL or slug '{repo_url}'.")
    return owner, name


def validate_check_name(check_name: str) -> str:
    name = check_name.strip()
    if not name:
        raise ValueError("--check-name cannot be empty.")
    if len(name) > MAX_STATUS_CONTEXT_LENGTH:
        raise ValueError(
            f"--check-name exceeds {MAX_STATUS_CONTEXT_LENGTH} characters (GitHub context limit)."
        )
    return name


def validate_description(description: str | None) -> str | None:
    if description is None:
        return None
    text = description.strip()
    if not text:
        return None
    if len(text) > MAX_STATUS_DESCRIPTION_LENGTH:
        raise ValueError(
            f"--description exceeds {MAX_STATUS_DESCRIPTION_LENGTH} characters "
            "(GitHub commit status limit)."
        )
    return text


def validate_target_url(target_url: str | None) -> str | None:
    if target_url is None:
        return None
    url = target_url.strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("--target-url must be an http or https URL.")
    return url


def parse_pr_url(pr_url: str) -> PullRequestRef:
    match = PR_URL_PATTERN.match(pr_url.strip())
    if not match:
        raise ValueError(
            "Invalid PR URL. Expected https://github.com/<owner>/<repo>/pull/<number>"
        )
    return PullRequestRef(
        owner=match.group("owner"),
        repo=match.group("repo"),
        number=int(match.group("number")),
    )


def normalize_status(status: str) -> str:
    normalized = status.strip().lower()
    normalized = STATUS_ALIASES.get(normalized, normalized)
    if normalized not in VALID_GITHUB_STATES:
        allowed = ", ".join(sorted(VALID_GITHUB_STATES | set(STATUS_ALIASES)))
        raise ValueError(f"Unsupported status '{status}'. Allowed: {allowed}")
    return normalized


def normalize_repo_slug(repo: str) -> tuple[str, str]:
    repo = repo.strip()
    if repo.startswith("http://") or repo.startswith("https://"):
        return parse_github_repo(repo)
    if "/" not in repo:
        raise ValueError(f"Invalid repo slug '{repo}'. Expected owner/name or GitHub URL.")
    owner, name = repo.split("/", 1)
    return owner, name.removesuffix(".git")


class PRStatusUpdater:
    """Create or update commit statuses for PR head SHAs via the gh CLI."""

    def __init__(
        self,
        *,
        check_name: str,
        runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.check_name = validate_check_name(check_name)
        self.dry_run = dry_run
        self._runner = runner or self._default_runner

    def _default_runner(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def run_gh(self, gh_args: Sequence[str]) -> str:
        command = ["gh", *gh_args]
        if self.dry_run:
            print(f"[dry-run] {' '.join(command)}")
            return ""

        result = self._runner(command)
        if result.returncode != 0:
            output = (result.stdout or "") + (result.stderr or "")
            raise GhCommandError(command, result.returncode, output.strip())
        return (result.stdout or "").strip()

    def get_pull_head(self, pr: PullRequestRef) -> tuple[str, str, str]:
        """Return (head_sha, owner, repo) where owner/repo own the head commit.

        Statuses must be posted to the head repository (fork PRs), not only the
        repository named in the PR URL.
        """
        output = self.run_gh(
            [
                "api",
                f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}",
                "--jq",
                "{sha: .head.sha, owner: .head.repo.owner.login, repo: .head.repo.name}",
            ]
        )
        if self.dry_run:
            return "dry-run-sha", pr.owner, pr.repo
        if not output:
            raise RuntimeError(f"Could not resolve head for {pr.url}")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid JSON from GitHub for {pr.url}: {exc}"
            ) from exc
        sha = payload.get("sha")
        owner = payload.get("owner")
        repo = payload.get("repo")
        if not sha or not owner or not repo:
            raise RuntimeError(f"Could not resolve head for {pr.url}")
        return str(sha), str(owner), str(repo)

    def get_head_sha(self, pr: PullRequestRef) -> str:
        return self.get_pull_head(pr)[0]

    def find_open_pr_urls_by_label(self, repo_slug: str, label: str) -> list[str]:
        owner, repo = normalize_repo_slug(repo_slug)
        if self.dry_run:
            print(
                f"[dry-run] gh pr list --repo {owner}/{repo} "
                f"--state open --label {label} --json url --jq '.[].url'"
            )
            # Placeholder so dry-run can exercise repo+label without live gh list.
            return [f"https://github.com/{owner}/{repo}/pull/1"]

        output = self.run_gh(
            [
                "pr",
                "list",
                "--repo",
                f"{owner}/{repo}",
                "--state",
                "open",
                "--label",
                label,
                "--json",
                "url",
                "--jq",
                ".[].url",
            ]
        )
        if not output:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def build_status_command(
        self,
        status_owner: str,
        status_repo: str,
        head_sha: str,
        state: str,
        *,
        description: str | None = None,
        target_url: str | None = None,
    ) -> list[str]:
        github_state = normalize_status(state)
        command = [
            "api",
            "--method",
            "POST",
            f"repos/{status_owner}/{status_repo}/statuses/{head_sha}",
            "-f",
            f"state={github_state}",
            "-f",
            f"context={self.check_name}",
        ]
        if description:
            command.extend(["-f", f"description={description}"])
        if target_url:
            command.extend(["-f", f"target_url={target_url}"])
        return command

    def post_status_for_pr(
        self,
        pr_url: str,
        state: str,
        *,
        description: str | None = None,
        target_url: str | None = None,
    ) -> StatusUpdateResult:
        pr = parse_pr_url(pr_url)
        github_state = normalize_status(state)
        description = validate_description(description)
        target_url = validate_target_url(target_url)
        head_sha, status_owner, status_repo = self.get_pull_head(pr)
        self.run_gh(
            self.build_status_command(
                status_owner,
                status_repo,
                head_sha,
                github_state,
                description=description,
                target_url=target_url,
            )
        )
        return StatusUpdateResult(
            pr=pr,
            head_sha=head_sha,
            state=github_state,
            context=self.check_name,
            dry_run=self.dry_run,
        )

    def resolve_pr_urls(
        self,
        pr_urls: Sequence[str] | None,
        repos: Sequence[str] | None,
        label: str | None,
    ) -> list[str]:
        explicit_urls = [u.strip() for u in (pr_urls or []) if u and u.strip()]
        repo_list = [r.strip() for r in (repos or []) if r and r.strip()]
        label_value = (label or "").strip() or None

        if label_value and not repo_list:
            raise ValueError(
                "--label is only used with --repo to discover open PRs. "
                "Add --repo owner/name, or use --pr-url instead."
            )
        if repo_list and not label_value:
            raise ValueError("--label is required when --repo is used.")
        if not explicit_urls and not repo_list:
            raise ValueError(
                "Specify which PRs to update: provide one or more --pr-url values, "
                "or one or more --repo values together with --label."
            )

        for url in explicit_urls:
            parse_pr_url(url)

        resolved: list[str] = list(explicit_urls)
        if repo_list:
            for repo in repo_list:
                resolved.extend(
                    self.find_open_pr_urls_by_label(repo, label_value or "")
                )
        if not resolved:
            if repo_list and label_value and not explicit_urls:
                repos_text = ", ".join(repo_list)
                raise ValueError(
                    f"No open pull requests with label '{label_value}' in: {repos_text}."
                )
            raise ValueError("No PRs to update after resolving --pr-url and --repo/--label.")
        # Preserve order while removing duplicates.
        seen: set[str] = set()
        unique: list[str] = []
        for url in resolved:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    def post_status_for_many(
        self,
        pr_urls: Sequence[str],
        state: str,
        *,
        description: str | None = None,
        target_url: str | None = None,
        continue_on_error: bool = False,
    ) -> list[StatusUpdateResult]:
        normalize_status(state)
        description = validate_description(description)
        target_url = validate_target_url(target_url)

        results: list[StatusUpdateResult] = []
        failures: list[tuple[str, str]] = []
        for pr_url in pr_urls:
            try:
                results.append(
                    self.post_status_for_pr(
                        pr_url,
                        state,
                        description=description,
                        target_url=target_url,
                    )
                )
            except (ValueError, RuntimeError, GhCommandError) as exc:
                if continue_on_error:
                    failures.append((pr_url, str(exc)))
                    continue
                raise
        if failures:
            raise StatusUpdateBatchError(failures, results)
        return results
