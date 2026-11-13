"""Create or update the Gated Artifacts Promoter Leader PR via the gh CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from lib.git_utils import authenticated_clone_url
from lib.state_file import (
    DEFAULT_LEADER_REPO,
    PromoterState,
    StatePullRequest,
    find_existing_state_path,
    state_path_for_trigger,
    write_state_file,
    load_state_file,
)

from lib.creator_outcomes import ERROR_OUTCOMES, PR, merge_outcomes
from lib.leader_lifecycle import retired_outcome
from lib.leader_history import is_history_state

GAP_LABEL = "gated-artifacts-promoter"
LEADER_BRANCH_PREFIX = "gap-leader"


class GhCommandError(RuntimeError):
    """Raised when a gh CLI command fails."""

    def __init__(self, command: Sequence[str], returncode: int, output: str) -> None:
        self.command = list(command)
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"gh command failed ({returncode}): {' '.join(self.command)}\n{output}"
        )


@dataclass(frozen=True)
class LeaderPRResult:
    trigger_id: str
    repo: str
    branch: str
    state_path: str
    pr_url: str | None
    pr_number: int | None
    updated: bool
    dry_run: bool


class LeaderPRManager:
    """Manage the Leader PR that tracks child sync PRs for a trigger ID."""

    def __init__(
        self,
        *,
        repo: str = DEFAULT_LEADER_REPO,
        runner: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
        | None = None,
        dry_run: bool = False,
        base_branch: str = "main",
    ) -> None:
        self.repo = repo
        self.dry_run = dry_run
        self.base_branch = base_branch
        self._runner = runner or self._default_runner

    def _default_runner(
        self,
        command: Sequence[str],
        cwd: Path | None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=False,
        )

    def run_gh(
        self,
        gh_args: Sequence[str],
        *,
        cwd: Path | None = None,
        mutate: bool = False,
    ) -> str:
        command = ["gh", *gh_args]
        if self.dry_run and mutate:
            print(f"[dry-run] {' '.join(command)}")
            return ""

        if self.dry_run:
            print(f"[dry-run] {' '.join(command)}")

        result = self._runner(command, cwd)
        if result.returncode != 0:
            output = (result.stdout or "") + (result.stderr or "")
            raise GhCommandError(command, result.returncode, output)
        return (result.stdout or "").strip()

    def run_git(
        self,
        git_args: Sequence[str],
        *,
        cwd: Path,
        mutate: bool = False,
    ) -> str:
        command = ["git", *git_args]
        if self.dry_run and mutate:
            print(f"[dry-run] {' '.join(command)}")
            return ""

        result = self._runner(command, cwd)
        if result.returncode != 0:
            output = (result.stdout or "") + (result.stderr or "")
            raise RuntimeError(
                f"git command failed ({result.returncode}): {' '.join(command)}\n{output}"
            )
        return (result.stdout or "").strip()

    def leader_branch(self, trigger_id: str) -> str:
        return f"{LEADER_BRANCH_PREFIX}/{trigger_id}"

    def _resolve_token(self) -> str:
        token = (
            os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("SYNC_TOKEN")
        )
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN, GH_TOKEN, or SYNC_TOKEN is required to create the Leader PR"
            )
        return token

    def _configure_origin_auth(self, workdir: Path, token: str) -> None:
        """Point origin at an authenticated HTTPS URL so git push/fetch work in CI."""
        auth_url = authenticated_clone_url(f"https://github.com/{self.repo}.git", token)
        self.run_git(["remote", "set-url", "origin", auth_url], cwd=workdir, mutate=True)

    def ensure_labels(self, labels: Sequence[str]) -> None:
        """Create labels on the Leader repo when they are missing."""
        for label in labels:
            # gh label create fails if the label already exists; ignore that case.
            command = [
                "gh",
                "label",
                "create",
                label,
                "--repo",
                self.repo,
                "--force",
            ]
            if self.dry_run:
                print(f"[dry-run] {' '.join(command)}")
                continue
            result = self._runner(command, None)
            if result.returncode != 0:
                output = (result.stdout or "") + (result.stderr or "")
                # --force should upsert; still surface unexpected failures.
                if "already exists" not in output.lower():
                    raise GhCommandError(command, result.returncode, output)

    def add_labels_to_pr(self, number: int, labels: Sequence[str]) -> None:
        """Add labels to an existing Leader PR (idempotent)."""
        if not labels:
            return
        # Prefer REST — same constraint as title/body edit (avoid GraphQL org scopes).
        command: list[str] = [
            "api",
            "--method",
            "POST",
            f"repos/{self.repo}/issues/{number}/labels",
        ]
        for label in labels:
            command.extend(["-f", f"labels[]={label}"])
        self.run_gh(command, mutate=True)

    def find_open_pr_by_label(self, label: str) -> dict | None:
        """Return the first open PR with the given label, or None."""
        items = self.list_open_prs_by_label(label)
        return items[0] if items else None

    def list_open_prs_by_label(self, label: str) -> list[dict]:
        """Return all open PRs with the given label."""
        output = self.run_gh(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--label",
                label,
                "--json",
                "number,url,headRefName,title",
                "--limit",
                "50",
            ],
            mutate=False,
        )
        if self.dry_run and not output:
            return []
        items = json.loads(output or "[]")
        if not isinstance(items, list):
            return []
        return items

    def close_pr(self, number: int, *, comment: str | None = None) -> None:
        """Close an open Leader PR and delete its head branch."""
        if comment:
            self.run_gh(
                [
                    "pr",
                    "comment",
                    str(number),
                    "--repo",
                    self.repo,
                    "--body",
                    comment,
                ],
                mutate=True,
            )
        self.run_gh(
            [
                "pr",
                "close",
                str(number),
                "--repo",
                self.repo,
                "--delete-branch",
            ],
            mutate=True,
        )

    def create_or_update(
        self,
        state: PromoterState,
        *,
        trigger_id: str,
        title: str | None = None,
        body: str | None = None,
    ) -> LeaderPRResult:
        """Write state.json and open or update the Leader PR for the trigger."""
        branch = self.leader_branch(trigger_id)
        pr_title = title or f"GAP leader: {trigger_id}"
        # Labels: shared GAP marker + this run's trigger ID.
        # Lookup by trigger ID so each run opens a new Leader PR; only a re-run
        # with the same --trigger-id updates an existing one.
        labels = [GAP_LABEL, trigger_id]
        self.ensure_labels(labels)

        existing = self.find_open_pr_by_label(trigger_id)
        if existing is None:
            previous = json.loads(self.run_gh([
                "pr", "list", "--repo", self.repo, "--state", "all",
                "--label", trigger_id, "--json", "number,url", "--limit", "1",
            ]) or "[]")
            if previous:
                raise RuntimeError(
                    "This trigger already has a closed or merged Leader; use a new trigger ID"
                )
        preview_state_rel = state_path_for_trigger(trigger_id)

        if self.dry_run:
            pr_body = body or self._default_body(
                state, trigger_id=trigger_id, state_path=preview_state_rel
            )
            self.run_gh(
                [
                    "pr",
                    "create",
                    "--repo",
                    self.repo,
                    "--base",
                    self.base_branch,
                    "--head",
                    branch,
                    "--title",
                    pr_title,
                    "--body",
                    pr_body,
                    "--label",
                    labels[0],
                    "--label",
                    labels[1],
                    "--dry-run",
                ],
                mutate=True,
            )
            return LeaderPRResult(
                trigger_id=trigger_id,
                repo=self.repo,
                branch=branch,
                state_path=preview_state_rel,
                pr_url=None,
                pr_number=None,
                updated=existing is not None,
                dry_run=True,
            )

        # Other triggers retain their PRs and branches until their own monitors
        # conclude and merge their records. A new daily run must not erase them.

        workdir = Path(tempfile.mkdtemp(prefix="gap-leader-"))
        try:
            token = self._resolve_token()
            self.run_gh(
                [
                    "repo",
                    "clone",
                    self.repo,
                    str(workdir),
                    "--",
                    "--branch",
                    self.base_branch,
                    "--single-branch",
                ],
                mutate=True,
            )
            self._configure_origin_auth(workdir, token)

            # A failed PR-create can leave a valid state branch behind. Resume
            # it rather than force-pushing a new history over the existing data.
            resume_branch = bool(existing) or bool(self.run_git(
                ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
                cwd=workdir,
            ))
            if resume_branch:
                head = (existing or {}).get("headRefName") or branch
                self.run_git(
                    ["fetch", "origin", f"+refs/heads/{head}:refs/remotes/origin/{head}"],
                    cwd=workdir,
                    mutate=True,
                )
                self.run_git(
                    ["checkout", "-B", head, f"origin/{head}"],
                    cwd=workdir,
                    mutate=True,
                )
                branch = head
                state_rel = (
                    find_existing_state_path(workdir, trigger_id)
                    or state_path_for_trigger(trigger_id)
                )
            else:
                self.run_git(["checkout", "-B", branch], cwd=workdir, mutate=True)
                state_rel = find_existing_state_path(workdir, trigger_id) or state_path_for_trigger(trigger_id)

            if is_history_state(workdir / state_rel):
                raise RuntimeError("Cannot update an archived Leader; use a new trigger ID")
            if resume_branch and (workdir / state_rel).exists():
                previous_state = load_state_file(workdir / state_rel)
                if retired_outcome(previous_state):
                    raise RuntimeError("Cannot update a retired Leader; use a new trigger ID")
                previous_children = {
                    child["pr-url"]: child for child in previous_state["pull-requests"]
                }
                # Creator retries can return the same PR without new build data.
                # Preserve its receipt, but never transfer evidence to another PR.
                state.pull_requests[:] = [
                    replace(child, build_observations=previous_children[child.pr_url].get("build-observations"))
                    if child.build_observations is None and child.pr_url in previous_children
                    else child
                    for child in state.pull_requests
                ]
                attempted = {item["repo"].lower() for item in state.creator_outcomes}
                failed_without_pr = {
                    item["repo"].lower() for item in state.creator_outcomes
                    if item["outcome"] in ERROR_OUTCOMES and not item.get("pr-url")
                }
                known_pr_urls = {child.pr_url for child in state.pull_requests}
                if attempted:
                    # Keep untouched children and failed retries with no fresh PR
                    # identity. Successful no-change attempts still clear old children.
                    for index, child in enumerate(previous_state["pull-requests"]):
                        match = PR.fullmatch(child["pr-url"])
                        if (match and child["pr-url"] not in known_pr_urls and (
                                match[1].lower() not in attempted
                                or match[1].lower() in failed_without_pr)):
                            state.pull_requests.append(StatePullRequest.from_mapping(child, index=index))
                            known_pr_urls.add(child["pr-url"])
                state.creator_outcomes = merge_outcomes(
                    previous_state.get("creator-outcomes", []), state.creator_outcomes
                )
                state.early_gate_triggered_batch = previous_state.get(
                    "early-gate-triggered-batch", state.early_gate_triggered_batch
                )
                state.early_gate_triggered_commit = previous_state.get(
                    "early-gate-triggered-commit", state.early_gate_triggered_commit
                )
                state.early_gate_trigger_pending_batch = previous_state.get(
                    "early-gate-trigger-pending-batch", state.early_gate_trigger_pending_batch
                )
                state.early_gate_retry_count = int(previous_state.get(
                    "early-gate-retry-count", state.early_gate_retry_count
                ))
                state.early_gate_retry_batch = previous_state.get(
                    "early-gate-retry-batch", state.early_gate_retry_batch
                )

            pr_body = body or self._default_body(
                state, trigger_id=trigger_id, state_path=state_rel
            )

            write_state_file(workdir / state_rel, state)
            self.run_git(["add", "--", state_rel], cwd=workdir, mutate=True)
            status = self.run_git(["status", "--porcelain"], cwd=workdir, mutate=False)
            if status:
                self.run_git(
                    [
                        "-c",
                        "user.name=Openshift-AI DevOps",
                        "-c",
                        "user.email=openshift-ai-devops@redhat.com",
                        "commit",
                        "-m",
                        f"Update GAP state for {trigger_id}",
                    ],
                    cwd=workdir,
                    mutate=True,
                )
                # A leftover branch is evidence to preserve, never overwrite.
                self.run_git(["push", "-u", "origin", "HEAD"], cwd=workdir, mutate=True)

            if existing:
                number = int(existing["number"])
                self.run_gh(
                    [
                        "api",
                        "--method",
                        "PATCH",
                        f"repos/{self.repo}/pulls/{number}",
                        "-f",
                        f"title={pr_title}",
                        "-f",
                        f"body={pr_body}",
                    ],
                    mutate=True,
                )
                self.add_labels_to_pr(number, labels)
                return LeaderPRResult(
                    trigger_id=trigger_id,
                    repo=self.repo,
                    branch=branch,
                    state_path=state_rel,
                    pr_url=existing.get("url"),
                    pr_number=number,
                    updated=True,
                    dry_run=False,
                )

            create_out = self.run_gh(
                [
                    "pr",
                    "create",
                    "--repo",
                    self.repo,
                    "--base",
                    self.base_branch,
                    "--head",
                    branch,
                    "--title",
                    pr_title,
                    "--body",
                    pr_body,
                    "--label",
                    labels[0],
                    "--label",
                    labels[1],
                ],
                mutate=True,
            )
            pr_url = create_out.strip().splitlines()[-1] if create_out else None
            pr_number = _parse_pr_number(pr_url) if pr_url else None
            return LeaderPRResult(
                trigger_id=trigger_id,
                repo=self.repo,
                branch=branch,
                state_path=state_rel,
                pr_url=pr_url,
                pr_number=pr_number,
                updated=False,
                dry_run=False,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def publish_creator_outcomes(self, result: LeaderPRResult, state: PromoterState) -> None:
        """Use the reporter's existing summary marker, never a second comment."""
        if not result.pr_url:
            return
        from scripts.gap_failure_reporter import publish_creator_outcomes
        publish_creator_outcomes(
            result.pr_url, state.creator_outcomes,
            dry_run=self.dry_run, gh_runner=lambda args: self.run_gh(args),
        )

    def _default_body(
        self,
        state: PromoterState,
        *,
        trigger_id: str,
        state_path: str,
    ) -> str:
        lines = [
            "## Gated Artifacts Promoter leader",
            "",
            f"- Trigger ID: `{trigger_id}`",
            f"- State file: `{state_path}`",
            "",
            "### Child PRs",
            "",
        ]
        if not state.pull_requests:
            lines.append("_No child PRs were created (targets already up to date)._")
        else:
            for pr in state.pull_requests:
                lines.append(
                    f"- [{pr.repo}]({pr.pr_url}) (`{pr.pr_status}`)"
                )
        lines.append("")
        return "\n".join(lines)


def _parse_pr_number(url: str) -> int | None:
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (TypeError, ValueError):
        return None
