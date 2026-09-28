"""Create and update GitHub pull requests."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Sequence
import requests

from lib.git_utils import GitCommit, extract_repo_slug, format_commit_url, parse_github_repo

MAX_COMMITS_IN_BODY = 100


@dataclass(frozen=True)
class PRResult:
    """Details about a created or updated pull request."""

    number: int
    url: str
    branch: str
    created: bool
    updated: bool


class PRCreator:
    """General-purpose GitHub pull request helper."""

    def __init__(self, token: str, *, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, f"{self.api_url}{path}", **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(
                f"GitHub API {method} {path} failed ({response.status_code}): {response.text}"
            )
        if response.text:
            return response.json()
        return None

    def find_open_pr_by_label(
        self,
        repo_url: str,
        *,
        base_branch: str,
        label: str,
    ) -> dict[str, Any] | None:
        owner, repo = parse_github_repo(repo_url)
        pulls = self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open", "base": base_branch, "per_page": 100},
        )
        for pull in pulls:
            labels = [item["name"] for item in pull.get("labels", [])]
            if label in labels:
                return pull
        return None

    def find_open_pr_by_head(
        self,
        repo_url: str,
        *,
        head_branch: str,
        base_branch: str,
    ) -> dict[str, Any] | None:
        """Return the open PR for this head→base pair, if any.

        GitHub allows only one open PR per head branch in a repo, so a
        main→stable sync must update that PR instead of creating another.
        """
        owner, repo = parse_github_repo(repo_url)
        pulls = self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={
                "state": "open",
                "base": base_branch,
                "head": f"{owner}:{head_branch}",
                "per_page": 10,
            },
        )
        if pulls:
            return pulls[0]
        # Fallback without owner prefix (some API contexts).
        pulls = self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open", "base": base_branch, "per_page": 100},
        )
        for pull in pulls:
            if pull.get("head", {}).get("ref") == head_branch:
                return pull
        return None

    def ensure_delete_branch_on_merge(self, repo_url: str) -> None:
        """Enable repository setting to delete head branches after PR merge."""
        owner, repo = parse_github_repo(repo_url)
        repo_data = self._request("GET", f"/repos/{owner}/{repo}")
        if not repo_data.get("delete_branch_on_merge"):
            self._request(
                "PATCH",
                f"/repos/{owner}/{repo}",
                json={"delete_branch_on_merge": True},
            )

    def create_pull_request(
        self,
        repo_url: str,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        labels: list[str] | None = None,
        reviewers: list[str] | None = None,
        automerge: bool = False,
        draft: bool = False,
        delete_branch_on_merge: bool = True,
    ) -> PRResult:
        owner, repo = parse_github_repo(repo_url)
        if delete_branch_on_merge:
            self.ensure_delete_branch_on_merge(repo_url)
        payload = {
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
            "draft": draft,
        }
        pull = self._request("POST", f"/repos/{owner}/{repo}/pulls", json=payload)
        number = pull["number"]
        url = pull["html_url"]

        if labels:
            self.add_labels(repo_url, number=number, labels=labels)
        if reviewers:
            self.request_reviewers(repo_url, number=number, reviewers=reviewers)
        if automerge:
            self.enable_automerge(repo_url, number=number)

        return PRResult(
            number=number,
            url=url,
            branch=head_branch,
            created=True,
            updated=False,
        )

    def update_pull_request(
        self,
        repo_url: str,
        *,
        number: int,
        title: str | None = None,
        body: str | None = None,
    ) -> None:
        owner, repo = parse_github_repo(repo_url)
        payload = {key: value for key, value in {"title": title, "body": body}.items() if value}
        if payload:
            self._request("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json=payload)

    def add_labels(self, repo_url: str, *, number: int, labels: list[str]) -> None:
        owner, repo = parse_github_repo(repo_url)
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/labels",
            json={"labels": labels},
        )

    def request_reviewers(self, repo_url: str, *, number: int, reviewers: list[str]) -> None:
        owner, repo = parse_github_repo(repo_url)
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/requested_reviewers",
            json={"reviewers": reviewers},
        )

    def enable_automerge(self, repo_url: str, *, number: int) -> None:
        owner, repo = parse_github_repo(repo_url)
        pull = self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        node_id = pull["node_id"]
        mutation = """
        mutation($pullRequestId: ID!) {
          enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: MERGE}) {
            pullRequest { number }
          }
        }
        """
        response = self.session.post(
            "https://api.github.com/graphql",
            json={"query": mutation, "variables": {"pullRequestId": node_id}},
        )
        context = f" for {owner}/{repo} pull request #{number}"
        if response.status_code >= 400:
            raise RuntimeError(f"Failed to enable automerge{context}: {response.text}")
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(
                f"Failed to enable automerge{context}: {json.dumps(payload['errors'])}"
            )

    def create_or_update_tracking_pr(
        self,
        repo_url: str,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        tracking_label: str | None,
        labels: list[str] | None = None,
        reviewers: list[str] | None = None,
        automerge: bool = False,
        delete_branch_on_merge: bool = True,
    ) -> PRResult:
        labels = list(labels or [])
        if tracking_label and tracking_label not in labels:
            labels.append(tracking_label)

        # Prefer an existing PR with the same head→base (e.g. master→stable).
        # GitHub rejects a second open PR from the same head (422).
        existing = self.find_open_pr_by_head(
            repo_url,
            head_branch=head_branch,
            base_branch=base_branch,
        )
        if existing is None and tracking_label:
            existing = self.find_open_pr_by_label(
                repo_url,
                base_branch=base_branch,
                label=tracking_label,
            )

        if existing:
            number = existing["number"]
            existing_branch = existing["head"]["ref"]
            self.update_pull_request(repo_url, number=number, title=title, body=body)
            if labels:
                self.add_labels(repo_url, number=number, labels=labels)
            if reviewers:
                self.request_reviewers(repo_url, number=number, reviewers=reviewers)
            if automerge:
                self.enable_automerge(repo_url, number=number)
            return PRResult(
                number=number,
                url=existing["html_url"],
                branch=existing_branch,
                created=False,
                updated=True,
            )

        try:
            return self.create_pull_request(
                repo_url,
                title=title,
                body=body,
                head_branch=head_branch,
                base_branch=base_branch,
                labels=labels,
                reviewers=reviewers,
                automerge=automerge,
                delete_branch_on_merge=delete_branch_on_merge,
            )
        except RuntimeError as exc:
            # Race or missed lookup: reuse the open head→base PR.
            if "422" not in str(exc) or "already exists" not in str(exc).lower():
                raise
            existing = self.find_open_pr_by_head(
                repo_url,
                head_branch=head_branch,
                base_branch=base_branch,
            )
            if existing is None:
                raise
            number = existing["number"]
            self.update_pull_request(repo_url, number=number, title=title, body=body)
            if labels:
                self.add_labels(repo_url, number=number, labels=labels)
            if reviewers:
                self.request_reviewers(repo_url, number=number, reviewers=reviewers)
            if automerge:
                self.enable_automerge(repo_url, number=number)
            return PRResult(
                number=number,
                url=existing["html_url"],
                branch=existing["head"]["ref"],
                created=False,
                updated=True,
            )


def format_default_pr_title(
    source_branch: str,
    target_branch: str,
    *,
    source_repo: str,
    target_repo: str,
) -> str:
    source_slug = extract_repo_slug(source_repo)
    target_slug = extract_repo_slug(target_repo)
    if source_slug == target_slug:
        return f"Automated Sync from {source_branch} to {target_branch}"
    return (
        f"Automated Sync from {source_slug}:{source_branch} "
        f"to {target_slug}:{target_branch}"
    )


def _format_commit_line(commit: GitCommit, *, repo_url: str) -> str:
    url = format_commit_url(repo_url, commit.sha)
    return f"- [`{commit.short_sha}`]({url}) {commit.subject}"


def format_default_pr_body(
    source_branch: str,
    target_branch: str,
    *,
    source_repo: str,
    target_repo: str,
    commits: list[GitCommit] | None = None,
    head_branch: str | None = None,
    automerge: bool = False,
    conflict_files: Sequence[str] | None = None,
) -> str:
    sync_commits = list(commits or [])
    lines = [
        f"## Automated Sync from {source_branch} to {target_branch}",
        "",
        (
            f"This PR automatically syncs the `{source_branch}` branch to the "
            f"`{target_branch}` branch"
        ),
    ]
    if head_branch and head_branch != source_branch:
        lines[-1] += (
            f" by merging `{source_branch}` into temporary branch "
            f"`{head_branch}` and opening a pull request into `{target_branch}`."
        )
    elif head_branch == source_branch:
        lines[-1] += (
            f" by opening a pull request from `{source_branch}` into `{target_branch}`."
        )
    else:
        lines[-1] += "."

    lines.extend(["", "### Sync Summary", ""])

    if sync_commits:
        latest = sync_commits[0]
        latest_url = format_commit_url(source_repo, latest.sha)
        lines.append(
            f"- **Latest commit:** [`{latest.short_sha}`]({latest_url}) {latest.subject}"
        )
        lines.append(f"- **Total commits to sync:** {len(sync_commits)}")
    else:
        lines.append("- **Total commits to sync:** 0")

    lines.extend(
        [
            "",
            f"- **Source:** `{source_repo}` @ `{source_branch}`",
            f"- **Target:** `{target_repo}` @ `{target_branch}`",
        ]
    )
    if head_branch and head_branch != source_branch:
        lines.append(f"- **Sync branch:** `{head_branch}`")
    elif head_branch == source_branch:
        lines.append(f"- **PR head:** `{head_branch}`")

    lines.extend(["", "### Commits to be synced", ""])
    if sync_commits:
        visible_commits = sync_commits[:MAX_COMMITS_IN_BODY]
        lines.extend(_format_commit_line(commit, repo_url=source_repo) for commit in visible_commits)
        remaining = len(sync_commits) - len(visible_commits)
        if remaining > 0:
            lines.append(f"- _...and {remaining} more commit(s)_")
    else:
        lines.append("_No new commits to sync._")

    if conflict_files:
        lines.extend(["", "### Merge conflicts", ""])
        lines.append(
            "This pull request was opened with unresolved merge conflicts that must be "
            "resolved before it can be merged:"
        )
        lines.extend(f"- `{file_path}`" for file_path in conflict_files)

    lines.extend(["", "### Merging", ""])
    if conflict_files:
        lines.append(
            "Resolve the merge conflicts above before merging this pull request into "
            f"`{target_branch}`."
        )
    elif automerge:
        lines.append(
            "GitHub automerge is enabled for this pull request once required checks pass."
        )
    else:
        lines.append(
            "Review the commits above before merging this pull request into "
            f"`{target_branch}`."
        )

    return "\n".join(lines)


def try_gh_pr_create(
    repo_slug: str,
    *,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str,
    labels: list[str] | None = None,
    reviewers: list[str] | None = None,
) -> str:
    """Optional helper that shells out to the GitHub CLI when available."""
    command = [
        "gh",
        "pr",
        "create",
        "--repo",
        repo_slug,
        "--title",
        title,
        "--body",
        body,
        "--base",
        base_branch,
        "--head",
        head_branch,
    ]
    for label in labels or []:
        command.extend(["--label", label])
    for reviewer in reviewers or []:
        command.extend(["--reviewer", reviewer])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()
