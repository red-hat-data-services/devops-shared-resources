"""Post GitHub commit statuses on pull request head commits."""

from __future__ import annotations

import json
import re
import subprocess
import warnings
from dataclasses import dataclass
from typing import Sequence

from urllib.parse import urlparse

from lib.github_cli import (
    GhCommandError,
    Runner,
    default_runner,
    run_gh as execute_gh,
)

PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$"
)

# Values sent to GitHub commit status API (POST .../statuses/{sha}).
VALID_GITHUB_STATES = frozenset({"error", "failure", "pending", "success"})

# https://docs.github.com/en/rest/commits/statuses#create-a-commit-status
MAX_STATUS_CONTEXT_LENGTH = 255
MAX_STATUS_DESCRIPTION_LENGTH = 140

# CLI accepts check-oriented names; map to commit-status `state` (see status checks docs).
# GAP-specific names are translated in RHOAIENG-93565 before calling this tool.
STATUS_INPUT_ALIASES = {
    "completed": "success",
    "in_progress": "pending",
    "queued": "pending",
}

ACCEPTED_STATUS_INPUTS = frozenset(VALID_GITHUB_STATES | STATUS_INPUT_ALIASES.keys())


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
class PullHeadInfo:
    sha: str
    status_owner: str
    status_repo: str
    pr_state: str


@dataclass(frozen=True)
class LatestCommitStatus:
    state: str
    description: str | None
    target_url: str | None


@dataclass(frozen=True)
class StatusUpdateResult:
    pr: PullRequestRef
    head_sha: str
    state: str
    context: str
    dry_run: bool
    skipped: bool = False


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
    if normalized not in ACCEPTED_STATUS_INPUTS:
        allowed = ", ".join(sorted(ACCEPTED_STATUS_INPUTS))
        raise ValueError(f"Unsupported status '{status}'. Allowed: {allowed}")
    return STATUS_INPUT_ALIASES.get(normalized, normalized)


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
        runner: Runner | None = None,
        dry_run: bool = False,
    ) -> None:
        self.check_name = validate_check_name(check_name)
        self.dry_run = dry_run
        self._runner = runner or self._default_runner

    def _default_runner(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return default_runner(command)

    def run_gh(self, gh_args: Sequence[str], *, mutate: bool = False) -> str:
        """Run gh. In dry-run mode, read-only calls execute; mutating calls are printed only."""
        command = ["gh", *gh_args]
        if self.dry_run and mutate:
            print(f"[dry-run] {' '.join(command)}")
            return ""

        if self.dry_run:
            print(f"[dry-run] {' '.join(command)}")

        return execute_gh(gh_args, runner=self._runner)

    def get_pull_head(self, pr: PullRequestRef) -> PullHeadInfo:
        """Return head SHA and repo that owns the head commit (fork PRs).

        Warns when the pull request is closed but still returns head metadata.
        """
        output = self.run_gh(
            [
                "api",
                f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}",
                "--jq",
                (
                    "{sha: .head.sha, owner: .head.repo.owner.login, "
                    "repo: .head.repo.name, state: .state}"
                ),
            ]
        )
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
        pr_state = payload.get("state")
        if not sha or not owner or not repo or not pr_state:
            raise RuntimeError(f"Could not resolve head for {pr.url}")
        if str(pr_state).lower() == "closed":
            warnings.warn(
                f"Pull request {pr.url} is closed; posting commit status anyway.",
                stacklevel=2,
            )
        return PullHeadInfo(
            sha=str(sha),
            status_owner=str(owner),
            status_repo=str(repo),
            pr_state=str(pr_state),
        )

    def get_head_sha(self, pr: PullRequestRef) -> str:
        return self.get_pull_head(pr).sha

    def get_latest_status_for_context(
        self,
        status_owner: str,
        status_repo: str,
        head_sha: str,
    ) -> LatestCommitStatus | None:
        """Return the latest commit status for this context on a commit, if any."""
        context_json = json.dumps(self.check_name)
        jq_filter = (
            f"[.[] | select(.context == {context_json})] | "
            ".[0] | {state: .state, description: .description, "
            "target_url: .target_url} // empty"
        )
        output = self.run_gh(
            [
                "api",
                f"repos/{status_owner}/{status_repo}/commits/{head_sha}/statuses",
                "--jq",
                jq_filter,
            ]
        )
        if not output:
            return None
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return None
        state = str(payload.get("state", "")).strip().lower()
        if state not in VALID_GITHUB_STATES:
            return None
        description = payload.get("description")
        target_url = payload.get("target_url")
        return LatestCommitStatus(
            state=state,
            description=str(description) if description is not None else None,
            target_url=str(target_url) if target_url is not None else None,
        )

    @staticmethod
    def _should_skip_status_update(
        existing: LatestCommitStatus,
        github_state: str,
        description: str | None,
        target_url: str | None,
    ) -> bool:
        if existing.state != github_state:
            return False
        if description is not None and (existing.description or "") != description:
            return False
        if target_url is not None and (existing.target_url or "") != target_url:
            return False
        return True

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
        head_sha: str | None = None,
    ) -> StatusUpdateResult:
        """Post a commit status for a PR.

        When ``head_sha`` is provided, the status is posted against that exact
        commit (RHOAIENG-97052 publish-then-signal). Otherwise the current PR
        head SHA is resolved at post time.
        """
        pr = parse_pr_url(pr_url)
        github_state = normalize_status(state)
        description = validate_description(description)
        target_url = validate_target_url(target_url)
        head = self.get_pull_head(pr)
        sha = (head_sha or "").strip() or head.sha
        existing = self.get_latest_status_for_context(
            head.status_owner, head.status_repo, sha
        )
        if existing and self._should_skip_status_update(
            existing, github_state, description, target_url
        ):
            return StatusUpdateResult(
                pr=pr,
                head_sha=sha,
                state=github_state,
                context=self.check_name,
                dry_run=self.dry_run,
                skipped=True,
            )

        self.run_gh(
            self.build_status_command(
                head.status_owner,
                head.status_repo,
                sha,
                github_state,
                description=description,
                target_url=target_url,
            ),
            mutate=True,
        )
        return StatusUpdateResult(
            pr=pr,
            head_sha=sha,
            state=github_state,
            context=self.check_name,
            dry_run=self.dry_run,
            skipped=False,
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
