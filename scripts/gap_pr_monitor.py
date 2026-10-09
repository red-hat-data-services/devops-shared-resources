#!/usr/bin/env python3
"""Stage-1 GAP PR monitor (RHOAIENG-93565).

Read leader state.json → classify each child PR (merge-failure vs success) →
post the matching commit status via PRStatusUpdater → update pr-status →
write state.json.

State path (RHOAIENG-93564 / sync layout):
GAP Leaders/<YYYY-MM-DD>_<trigger_id>/state.json
where trigger_id looks like gap-<uuid>
(e.g. GAP Leaders/2026-09-25_gap-60907cdcc8e64561aa75bc326fe22899/state.json).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

# CI env vars read by the thin GitHub Actions wrapper (RHOAIENG-93565).
ENV_STATE_FILE = "GAP_STATE_FILE"
ENV_TRIGGER_ID = "GAP_TRIGGER_ID"
ENV_PR_LABELS = "GAP_PR_LABELS"
ENV_LEADER_REPO = "GAP_LEADER_REPO"
ENV_LEADER_LABEL = "GAP_LEADER_LABEL"
ENV_LEADER_PR_URL = "GAP_LEADER_PR_URL"

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.pr_status_updater import (
    GhCommandError,
    PRStatusUpdater,
    PullRequestRef,
    StatusUpdateResult,
    parse_pr_url,
)
from lib.state_file import (
    OVERALL_STATUS_FAILURE,
    OVERALL_STATUS_SUCCESS,
    OVERALL_STATUSES,
)

DEFAULT_CHECK_NAME = "gated artifacts promoter"
DEFAULT_LEADER_LABEL = "gated-artifacts-promoter"
SUCCESS_PR_STATUS = "success"
MERGE_FAILURE_PR_STATUS = "merge-failure"
# Child statuses that mean Stage-1 classification is done for that PR.
STAGE1_TERMINAL_PR_STATUSES = frozenset({SUCCESS_PR_STATUS, MERGE_FAILURE_PR_STATUS})
TRIGGER_ID_RE = re.compile(r"^gap-[A-Za-z0-9._-]+$")
# Parent directory for per-trigger Leader state files (sync / RHOAIENG-93564).
GAP_LEADERS_DIR = "GAP Leaders"
# Folder name: 2026-09-25_gap-<uuid>
DATED_TRIGGER_DIR_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<trigger_id>gap-[A-Za-z0-9._-]+)$"
)

# GitHub mergeStateStatus values that mean the PR cannot be merged cleanly.
CONFLICT_MERGE_STATES = frozenset({"DIRTY", "CONFLICTING"})

CommandRunner = Callable[[Sequence[str]], str]


class GapPrMonitorError(RuntimeError):
    """Raised when the GAP state file cannot be processed."""


@dataclass(frozen=True)
class PrMergeInfo:
    """Mergeability snapshot for one child PR."""

    url: str
    pr: PullRequestRef
    state: str  # OPEN, MERGED, CLOSED, …
    mergeable: str | None  # MERGEABLE, CONFLICTING, UNKNOWN, or None
    merge_state_status: str | None


@dataclass(frozen=True)
class PrDecision:
    """Stage-1 decision for one child PR."""

    url: str
    pr_status: str  # success | merge-failure
    cli_status: str  # completed | failure  (aliases for PRStatusUpdater)
    description: str
    skip_status_post: bool = False
    reason: str = ""


@dataclass(frozen=True)
class MonitorResult:
    state_path: Path
    updated_urls: list[str]
    skipped_urls: list[str]
    merge_failure_urls: list[str]
    success_urls: list[str]
    dry_run: bool
    overall_status: str | None = None
    leader_status_posted: bool = False
    published_sha: str | None = None


@dataclass(frozen=True)
class PublishResult:
    """Outcome of committing/pushing Leader state.json (RHOAIENG-97052)."""

    sha: str
    committed: bool
    dry_run: bool = False


@dataclass(frozen=True)
class OpenLeaderPr:
    """One open Leader PR discovered for scheduled monitor runs."""

    number: int
    url: str
    head_ref: str
    labels: tuple[str, ...]


def state_path_for_trigger(trigger_id: str, *, root: Path | None = None) -> Path:
    """Return state.json for a trigger id under GAP Leaders/.

    Looks for ``GAP Leaders/<YYYY-MM-DD>_<trigger_id>/state.json``.
    If several dated folders exist for the same trigger id, the latest
    folder name (lexicographic date prefix) wins.
    """
    tid = (trigger_id or "").strip()
    if not TRIGGER_ID_RE.match(tid):
        raise GapPrMonitorError(
            f"Invalid trigger id {trigger_id!r}; expected gap-<id> "
            "(e.g. gap-4e997b5f8c224668b51d2fc8b4677495)."
        )
    base = root if root is not None else Path(".")
    leaders = base / GAP_LEADERS_DIR
    if not leaders.is_dir():
        raise GapPrMonitorError(
            f"State directory not found: {leaders} "
            f"(expected {GAP_LEADERS_DIR}/<YYYY-MM-DD>_{tid}/state.json)."
        )

    matches: list[Path] = []
    for child in sorted(leaders.iterdir()):
        if not child.is_dir():
            continue
        m = DATED_TRIGGER_DIR_RE.match(child.name)
        if not m or m.group("trigger_id") != tid:
            continue
        candidate = child / "state.json"
        if candidate.is_file():
            matches.append(candidate)

    if not matches:
        raise GapPrMonitorError(
            f"State file not found for {tid!r} under {leaders} "
            f"(expected {GAP_LEADERS_DIR}/<YYYY-MM-DD>_{tid}/state.json)."
        )
    # Date-prefixed names sort chronologically; take the newest.
    return matches[-1].resolve()


def extract_trigger_id_from_labels(labels: Sequence[str] | None) -> str | None:
    """Return the first gap-* label (Leader trigger id), or None.

    Leader PRs carry a gap-<uuid> label; state lives at
    GAP Leaders/<YYYY-MM-DD>_<gap-uuid>/state.json.
    """
    for raw in labels or []:
        label = str(raw).strip()
        if TRIGGER_ID_RE.match(label):
            return label
    return None


def split_labels_csv(raw: str | None) -> list[str]:
    """Split a comma-separated label list from GitHub Actions ``join(...)``."""
    if not raw or not str(raw).strip():
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _env_nonempty(name: str) -> str | None:
    value = os.environ.get(name, "")
    value = value.strip() if value else ""
    return value or None


def resolve_inputs_for_cli(
    *,
    state_file: str | None,
    trigger_id: str | None,
    labels: Sequence[str] | None,
    leader_pr_url: str | None = None,
) -> tuple[str | None, str | None, list[str] | None, str | None]:
    """Merge CLI flags with GAP_* CI env vars (CLI wins when set)."""
    resolved_state = (state_file or "").strip() or _env_nonempty(ENV_STATE_FILE)
    resolved_trigger = (trigger_id or "").strip() or _env_nonempty(ENV_TRIGGER_ID)
    if labels:
        resolved_labels: list[str] | None = [str(x).strip() for x in labels if str(x).strip()]
    else:
        from_env = split_labels_csv(os.environ.get(ENV_PR_LABELS))
        resolved_labels = from_env or None
    resolved_leader = (leader_pr_url or "").strip() or _env_nonempty(ENV_LEADER_PR_URL)
    return resolved_state, resolved_trigger, resolved_labels, resolved_leader


def append_github_output(key: str, value: str, *, output_file: str | None = None) -> None:
    """Append ``key=value`` to ``GITHUB_OUTPUT`` when running in Actions."""
    path = output_file if output_file is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def state_path_for_output(state_path: Path, *, repo_root: Path) -> str:
    """Prefer a repo-relative path for ``git add`` in the workflow."""
    resolved = state_path.expanduser().resolve()
    try:
        return str(resolved.relative_to(repo_root.expanduser().resolve()))
    except ValueError:
        return str(resolved)


def find_gap_state_files(root: Path) -> list[Path]:
    """Find <root>/GAP Leaders/<YYYY-MM-DD>_gap-*/state.json."""
    base = root.expanduser().resolve()
    leaders = base / GAP_LEADERS_DIR
    if not leaders.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(leaders.glob("*/state.json")):
        if DATED_TRIGGER_DIR_RE.match(path.parent.name):
            found.append(path.resolve())
    return found


def resolve_state_file(
    *,
    state_file: str | None = None,
    trigger_id: str | None = None,
    labels: Sequence[str] | None = None,
    root: Path | None = None,
    allow_gap_dir_fallback: bool = False,
) -> Path:
    """Resolve leader state.json path (RHOAIENG-93564 / RHOAIENG-93565).

    Priority:
      1. Explicit ``state_file`` — manual override (workflow_dispatch / local)
      2. Explicit ``trigger_id`` →
         ``GAP Leaders/<YYYY-MM-DD>_<trigger_id>/state.json``
      3. ``gap-*`` label from the Leader PR → same path
      4. Optional fallback: exactly one dated folder under ``GAP Leaders/``

    Arbitrary discovery of unrelated ``state.json`` files is not used.
    """
    base = root if root is not None else Path(".")

    if state_file and str(state_file).strip():
        return Path(state_file).expanduser().resolve()

    if trigger_id and str(trigger_id).strip():
        return state_path_for_trigger(str(trigger_id).strip(), root=base)

    from_labels = extract_trigger_id_from_labels(labels)
    if from_labels:
        return state_path_for_trigger(from_labels, root=base)

    if allow_gap_dir_fallback:
        candidates = find_gap_state_files(base)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            listed = ", ".join(str(p) for p in candidates)
            raise GapPrMonitorError(
                f"Multiple {GAP_LEADERS_DIR}/<date>_gap-*/state.json files "
                "found and no trigger id/label was provided; refuse to guess. "
                f"Candidates: {listed}"
            )

    raise GapPrMonitorError(
        "Cannot locate state.json. Provide --state-file, or --trigger-id "
        f"(gap-<id> → {GAP_LEADERS_DIR}/<YYYY-MM-DD>_gap-<id>/state.json), "
        "or a Leader PR gap-* label."
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise GapPrMonitorError(f"State file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GapPrMonitorError(f"State file root must be an object: {path}")
    pull_requests = payload.get("pull-requests")
    if not isinstance(pull_requests, list):
        raise GapPrMonitorError(
            f"State file {path} must contain a 'pull-requests' array."
        )
    return payload


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def extract_pr_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for index, entry in enumerate(payload.get("pull-requests") or []):
        if not isinstance(entry, dict):
            raise GapPrMonitorError(f"pull-requests[{index}] must be an object.")
        url = entry.get("pr-url")
        if not url or not str(url).strip():
            raise GapPrMonitorError(
                f"pull-requests[{index}] is missing a non-empty 'pr-url'."
            )
        urls.append(str(url).strip())
    return urls


def apply_pr_statuses(
    payload: dict[str, Any], updates: dict[str, str]
) -> None:
    """Set pr-status for matching pr-url entries."""
    for entry in payload.get("pull-requests") or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("pr-url", "")).strip()
        if url in updates:
            entry["pr-status"] = updates[url]


def apply_success_statuses(payload: dict[str, Any], urls: Sequence[str]) -> None:
    """Back-compat helper used by older tests."""
    apply_pr_statuses(payload, {u: SUCCESS_PR_STATUS for u in urls})


def compute_overall_status(payload: dict[str, Any]) -> str | None:
    """Derive overall-status once every child PR has a Stage-1 final status.

    Stage-1 finals: ``success`` / ``merge-failure`` (RHOAIENG-97052).

    - all ``success`` → ``success``
    - any ``merge-failure`` (and none still in progress) → ``failure``
    - any non-terminal child (e.g. ``new``) → ``None`` (do not conclude yet)
    """
    entries = payload.get("pull-requests") or []
    if not entries:
        return None
    statuses: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise GapPrMonitorError(f"pull-requests[{index}] must be an object.")
        status = str(entry.get("pr-status") or "").strip()
        if status not in STAGE1_TERMINAL_PR_STATUSES:
            return None
        statuses.append(status)
    if any(status == MERGE_FAILURE_PR_STATUS for status in statuses):
        return OVERALL_STATUS_FAILURE
    return OVERALL_STATUS_SUCCESS


def apply_overall_status(payload: dict[str, Any], overall_status: str | None) -> None:
    """Set or clear top-level overall-status on the state payload."""
    if overall_status is None:
        payload.pop("overall-status", None)
        return
    if overall_status not in OVERALL_STATUSES:
        raise GapPrMonitorError(
            f"Invalid overall-status {overall_status!r}; "
            f"expected one of {sorted(OVERALL_STATUSES)}."
        )
    payload["overall-status"] = overall_status


def _run_gh(args: Sequence[str]) -> str:
    command = ["gh", *args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GhCommandError(command, 127, "gh executable not found") from exc
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout or "").strip()
        raise GhCommandError(command, completed.returncode, output)
    return completed.stdout


def fetch_pr_merge_info(
    pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> PrMergeInfo:
    """Load mergeable / mergeStateStatus for a PR via gh."""
    pr = parse_pr_url(pr_url)
    runner = gh_runner or _run_gh
    raw = runner(
        [
            "pr",
            "view",
            str(pr.number),
            "-R",
            pr.slug,
            "--json",
            "state,mergeable,mergeStateStatus,url",
        ]
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(
            f"Invalid JSON from gh pr view for {pr_url}: {exc}"
        ) from exc
    mergeable = data.get("mergeable")
    merge_state = data.get("mergeStateStatus")
    return PrMergeInfo(
        url=pr_url,
        pr=pr,
        state=str(data.get("state") or "").upper(),
        mergeable=(str(mergeable).upper() if mergeable is not None else None),
        merge_state_status=(
            str(merge_state).upper() if merge_state is not None else None
        ),
    )


def classify_pr(
    info: PrMergeInfo,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    retry_unknown: bool = True,
) -> PrDecision:
    """Decide Stage-1 pr-status + commit-status for one PR."""
    if info.state == "MERGED":
        return PrDecision(
            url=info.url,
            pr_status=SUCCESS_PR_STATUS,
            cli_status="completed",
            description="GAP Stage 1: PR already merged",
            skip_status_post=True,
            reason="already merged",
        )

    mergeable = info.mergeable
    merge_state = info.merge_state_status
    # GitHub sometimes returns UNKNOWN while computing; retry once.
    if retry_unknown and mergeable in (None, "UNKNOWN"):
        time.sleep(1.5)
        info = fetch_pr_merge_info(info.url, gh_runner=gh_runner)
        mergeable = info.mergeable
        merge_state = info.merge_state_status

    if mergeable == "CONFLICTING" or (merge_state or "") in CONFLICT_MERGE_STATES:
        return PrDecision(
            url=info.url,
            pr_status=MERGE_FAILURE_PR_STATUS,
            cli_status="failure",
            description="GAP Stage 1: merge conflict",
            reason=f"mergeable={mergeable} mergeStateStatus={merge_state}",
        )

    return PrDecision(
        url=info.url,
        pr_status=SUCCESS_PR_STATUS,
        cli_status="completed",
        description="GAP Stage 1 dummy gate",
        reason=f"mergeable={mergeable} mergeStateStatus={merge_state}",
    )


def run_stage1_monitor(
    state_path: Path,
    *,
    check_name: str = DEFAULT_CHECK_NAME,
    dry_run: bool = False,
    continue_on_error: bool = False,
    description: str | None = None,
    updater_factory: Callable[..., PRStatusUpdater] | None = None,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> MonitorResult:
    """Classify children, post child statuses, write overall-status, save state.

    Leader success signaling is intentionally **not** done here. Callers that
    need auto-merge must publish the saved state first, then post Leader green
    against that published SHA (see ``run_monitor_publish_and_signal``).
    """
    path = state_path.expanduser().resolve()
    payload = load_state(path)
    pr_urls = extract_pr_urls(payload)
    if not pr_urls:
        raise GapPrMonitorError(f"No pull requests listed in {path}.")

    factory = updater_factory or PRStatusUpdater
    updater = factory(check_name=check_name, dry_run=dry_run)

    decisions: list[PrDecision] = []
    classify_errors: list[tuple[str, str]] = []
    for url in pr_urls:
        try:
            info = fetch_pr_merge_info(url, gh_runner=gh_runner)
            decisions.append(
                classify_pr(info, gh_runner=gh_runner, retry_unknown=gh_runner is None)
            )
        except (GapPrMonitorError, GhCommandError, ValueError, RuntimeError) as exc:
            if not continue_on_error:
                raise GapPrMonitorError(f"Failed to inspect {url}: {exc}") from exc
            classify_errors.append((url, str(exc)))

    status_updates: dict[str, str] = {}
    updated: list[str] = []
    skipped: list[str] = []
    merge_failures: list[str] = []
    successes: list[str] = []
    post_errors: list[tuple[str, str]] = []

    for decision in decisions:
        status_updates[decision.url] = decision.pr_status
        if decision.pr_status == MERGE_FAILURE_PR_STATUS:
            merge_failures.append(decision.url)
        else:
            successes.append(decision.url)

        if decision.skip_status_post:
            skipped.append(decision.url)
            continue

        desc = description if description is not None else decision.description
        try:
            result: StatusUpdateResult = updater.post_status_for_pr(
                decision.url,
                decision.cli_status,
                description=desc,
            )
            if result.skipped:
                skipped.append(decision.url)
            else:
                updated.append(decision.url)
        except (ValueError, RuntimeError, GhCommandError) as exc:
            post_errors.append((decision.url, str(exc)))
            if not continue_on_error:
                if status_updates and not dry_run:
                    apply_pr_statuses(payload, status_updates)
                    # Invalidate any stale overall success from a prior run.
                    apply_overall_status(payload, None)
                    save_state(path, payload)
                raise GapPrMonitorError(
                    f"Failed to post status for {decision.url}: {exc}"
                ) from exc

    if classify_errors or post_errors:
        if status_updates and not dry_run:
            apply_pr_statuses(payload, status_updates)
            # Partial runs stay in progress — clear stale overall-status.
            apply_overall_status(payload, None)
            save_state(path, payload)
        details = classify_errors + post_errors
        lines = [f"  - {u}: {e}" for u, e in details]
        raise GapPrMonitorError(
            f"{len(details)} PR(s) failed during monitor.\n" + "\n".join(lines)
        )

    apply_pr_statuses(payload, status_updates)

    # RHOAIENG-97052: conclude only when every component PR is Stage-1 final.
    overall_status = compute_overall_status(payload)
    apply_overall_status(payload, overall_status)

    if not dry_run:
        save_state(path, payload)

    return MonitorResult(
        state_path=path,
        updated_urls=updated,
        skipped_urls=skipped,
        merge_failure_urls=merge_failures,
        success_urls=successes,
        dry_run=dry_run,
        overall_status=overall_status,
        leader_status_posted=False,
    )


def _run_git(args: Sequence[str], *, cwd: Path) -> str:
    """Run a git command in ``cwd`` and return stripped stdout."""
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(cwd),
        )
    except FileNotFoundError as exc:
        raise GapPrMonitorError("git executable not found") from exc
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout or "").strip()
        raise GapPrMonitorError(
            f"git {' '.join(args)} failed ({completed.returncode}): {output}"
        )
    return (completed.stdout or "").strip()


def list_open_leader_prs(
    leader_repo: str,
    *,
    label: str = DEFAULT_LEADER_LABEL,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> list[OpenLeaderPr]:
    """List open Leader PRs that carry the GAP label (RHOAIENG-97050)."""
    repo = (leader_repo or "").strip()
    if not repo or "/" not in repo:
        raise GapPrMonitorError(
            f"Invalid leader repo {leader_repo!r}; expected OWNER/REPO."
        )
    label_name = (label or "").strip() or DEFAULT_LEADER_LABEL
    runner = gh_runner or _run_gh
    raw = runner(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--label",
            label_name,
            "--json",
            "number,url,headRefName,labels",
            "--limit",
            "100",
        ]
    )
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(
            f"Invalid JSON from gh pr list for {repo}: {exc}"
        ) from exc
    if not isinstance(rows, list):
        raise GapPrMonitorError(f"gh pr list for {repo} did not return an array.")

    leaders: list[OpenLeaderPr] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = row.get("number")
        url = str(row.get("url") or "").strip()
        head_ref = str(row.get("headRefName") or "").strip()
        if not isinstance(number, int) or not url or not head_ref:
            continue
        label_names: list[str] = []
        for item in row.get("labels") or []:
            if isinstance(item, dict) and item.get("name"):
                label_names.append(str(item["name"]))
            elif isinstance(item, str) and item.strip():
                label_names.append(item.strip())
        leaders.append(
            OpenLeaderPr(
                number=number,
                url=url,
                head_ref=head_ref,
                labels=tuple(label_names),
            )
        )
    return leaders


def checkout_leader_branch(
    repo_root: Path,
    head_ref: str,
    *,
    remote: str = "origin",
    git_runner: Callable[..., str] | None = None,
) -> None:
    """Fetch and check out a Leader PR head branch under ``repo_root``."""
    ref = (head_ref or "").strip()
    if not ref:
        raise GapPrMonitorError("Leader head ref is empty.")
    run = git_runner or (lambda args, cwd=repo_root: _run_git(args, cwd=cwd))
    run(["fetch", remote, f"+refs/heads/{ref}:refs/remotes/{remote}/{ref}"], cwd=repo_root)
    run(["checkout", "-B", ref, f"{remote}/{ref}"], cwd=repo_root)


def publish_state(
    repo_root: Path,
    state_path: Path,
    head_ref: str,
    *,
    remote: str = "origin",
    dry_run: bool = False,
    commit_message: str = "Update GAP state from PR monitor (RHOAIENG-97052)",
    git_runner: Callable[..., str] | None = None,
) -> PublishResult:
    """Commit and push state.json, returning the published commit SHA.

    When the state file is unchanged, no empty commit is created; the current
    ``HEAD`` SHA is returned so Leader signaling can still target it.
    """
    run = git_runner or (lambda args, cwd=repo_root: _run_git(args, cwd=cwd))
    rel = state_path_for_output(state_path, repo_root=repo_root)
    if dry_run:
        sha = run(["rev-parse", "HEAD"], cwd=repo_root) or "dry-run-sha"
        print(f"[dry-run] would commit and push {rel} on {head_ref} (sha={sha})")
        return PublishResult(sha=sha, committed=True, dry_run=True)

    run(["config", "user.name", "github-actions[bot]"], cwd=repo_root)
    run(
        ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        cwd=repo_root,
    )
    run(["add", "--", rel], cwd=repo_root)
    staged = run(["diff", "--cached", "--name-only"], cwd=repo_root)
    committed = False
    if staged.strip():
        run(["commit", "-m", commit_message], cwd=repo_root)
        committed = True
        run(["push", remote, f"HEAD:{head_ref}"], cwd=repo_root)
        print(f"Pushed state update for Leader branch {head_ref}")
    else:
        print(f"No state file changes to commit for {head_ref}.")
    sha = run(["rev-parse", "HEAD"], cwd=repo_root)
    if not sha:
        raise GapPrMonitorError("Could not resolve published commit SHA after publish.")
    return PublishResult(sha=sha, committed=committed, dry_run=False)


# Back-compat alias used by older schedule helpers/tests.
def commit_and_push_state(
    repo_root: Path,
    state_path: Path,
    head_ref: str,
    *,
    remote: str = "origin",
    dry_run: bool = False,
    git_runner: Callable[..., str] | None = None,
) -> bool:
    """Commit/push state; returns True when a new commit was created."""
    result = publish_state(
        repo_root,
        state_path,
        head_ref,
        remote=remote,
        dry_run=dry_run,
        git_runner=git_runner,
    )
    return result.committed


def current_git_branch(
    repo_root: Path,
    *,
    git_runner: Callable[..., str] | None = None,
) -> str:
    """Return the current branch name under ``repo_root``."""
    run = git_runner or (lambda args, cwd=repo_root: _run_git(args, cwd=cwd))
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    if not branch or branch == "HEAD":
        raise GapPrMonitorError(
            "Checked-out Leader tree is not on a branch; cannot publish state."
        )
    return branch


def verify_leader_head(
    leader_pr_url: str,
    expected_sha: str,
    *,
    updater: PRStatusUpdater,
) -> None:
    """Fail if the Leader PR head is no longer the published SHA."""
    head = updater.get_pull_head(parse_pr_url(leader_pr_url))
    if head.sha != expected_sha:
        raise GapPrMonitorError(
            f"Leader head moved after state publication: expected {expected_sha}, "
            f"found {head.sha}. Re-run the monitor; not posting Leader success."
        )


def post_leader_success(
    leader_pr_url: str,
    published_sha: str,
    *,
    updater: PRStatusUpdater,
) -> StatusUpdateResult:
    """Post Leader green against an explicit published commit SHA."""
    description = (
        "GAP overall-status=success (every component PR final/success); "
        "auto-merge Leader for history (RHOAIENG-97052)"
    )
    return updater.post_status_for_pr(
        leader_pr_url,
        "completed",
        description=description,
        head_sha=published_sha,
    )


def run_monitor_publish_and_signal(
    state_path: Path,
    *,
    repo_root: Path,
    head_ref: str | None = None,
    leader_pr_url: str | None = None,
    publish: bool = False,
    check_name: str = DEFAULT_CHECK_NAME,
    dry_run: bool = False,
    continue_on_error: bool = False,
    description: str | None = None,
    remote: str = "origin",
    updater_factory: Callable[..., PRStatusUpdater] | None = None,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    git_runner: Callable[..., str] | None = None,
) -> MonitorResult:
    """Coordinator: monitor → publish state → Leader green on published SHA.

    Ordering (Chris review / RHOAIENG-97052):
      classify children → save state → commit/push → post Leader success on that SHA
    """
    result = run_stage1_monitor(
        state_path,
        check_name=check_name,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
        description=description,
        updater_factory=updater_factory,
        gh_runner=gh_runner,
    )

    if not publish:
        if (
            result.overall_status == OVERALL_STATUS_SUCCESS
            and not (leader_pr_url or "").strip()
        ):
            # Local/non-publish runs still warn when a Leader URL is missing.
            pass
        return result

    branch = (head_ref or "").strip() or current_git_branch(
        repo_root, git_runner=git_runner
    )
    published = publish_state(
        repo_root,
        result.state_path,
        branch,
        remote=remote,
        dry_run=dry_run,
        git_runner=git_runner,
    )

    leader_url = (leader_pr_url or "").strip() or None
    leader_status_posted = False
    updated = list(result.updated_urls)
    skipped = list(result.skipped_urls)

    if result.overall_status == OVERALL_STATUS_SUCCESS and leader_url:
        factory = updater_factory or PRStatusUpdater
        updater = factory(check_name=check_name, dry_run=dry_run)
        if not dry_run:
            verify_leader_head(leader_url, published.sha, updater=updater)
        try:
            leader_result = post_leader_success(
                leader_url, published.sha, updater=updater
            )
            leader_status_posted = not leader_result.skipped
            if leader_status_posted:
                updated.append(leader_url)
            else:
                skipped.append(leader_url)
        except (ValueError, RuntimeError, GhCommandError) as exc:
            raise GapPrMonitorError(
                f"Failed to post Leader conclusion status for {leader_url} "
                f"on {published.sha}: {exc}"
            ) from exc
    elif result.overall_status == OVERALL_STATUS_SUCCESS and not leader_url:
        print(
            "WARNING: overall-status=success but no Leader PR URL was provided "
            f"(--leader-pr-url / {ENV_LEADER_PR_URL}); skipped Leader self-status.",
            file=sys.stderr,
        )

    return MonitorResult(
        state_path=result.state_path,
        updated_urls=updated,
        skipped_urls=skipped,
        merge_failure_urls=result.merge_failure_urls,
        success_urls=result.success_urls,
        dry_run=result.dry_run,
        overall_status=result.overall_status,
        leader_status_posted=leader_status_posted,
        published_sha=published.sha,
    )


def run_scheduled_monitors(
    repo_root: Path,
    *,
    leader_repo: str,
    label: str = DEFAULT_LEADER_LABEL,
    check_name: str = DEFAULT_CHECK_NAME,
    dry_run: bool = False,
    continue_on_error: bool = True,
    description: str | None = None,
    remote: str = "origin",
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    git_runner: Callable[..., str] | None = None,
    updater_factory: Callable[..., PRStatusUpdater] | None = None,
) -> int:
    """Discover open Leaders and run publish-then-signal on each (RHOAIENG-97050)."""
    leaders = list_open_leader_prs(
        leader_repo, label=label, gh_runner=gh_runner
    )
    if not leaders:
        print(
            f"No open Leader PRs with label {label!r} in {leader_repo}; "
            "nothing to do."
        )
        return 0

    print(f"Scheduled monitor: {len(leaders)} open Leader PR(s) in {leader_repo}")
    failures: list[str] = []
    root = repo_root.expanduser().resolve()

    for leader in leaders:
        print(f"--- Leader #{leader.number} {leader.url} ({leader.head_ref}) ---")
        try:
            checkout_leader_branch(
                root,
                leader.head_ref,
                remote=remote,
                git_runner=git_runner,
            )
            state_path = resolve_state_file(
                labels=list(leader.labels),
                root=root,
                allow_gap_dir_fallback=True,
            )
            result = run_monitor_publish_and_signal(
                state_path,
                repo_root=root,
                head_ref=leader.head_ref,
                leader_pr_url=leader.url,
                publish=True,
                check_name=check_name,
                dry_run=dry_run,
                continue_on_error=continue_on_error,
                description=description,
                remote=remote,
                updater_factory=updater_factory,
                gh_runner=gh_runner,
                git_runner=git_runner,
            )
            prefix = "[dry-run] " if result.dry_run else ""
            print(
                f"{prefix}Processed "
                f"{len(result.success_urls) + len(result.merge_failure_urls)} "
                f"child PR(s) from {result.state_path}"
            )
            if result.published_sha:
                print(f"{prefix}published-sha: {result.published_sha}")
            if result.leader_status_posted:
                print(f"{prefix}Posted Leader conclusion status (auto-merge signal)")
        except (GapPrMonitorError, GhCommandError, ValueError, RuntimeError) as exc:
            message = f"Leader #{leader.number} ({leader.url}): {exc}"
            print(f"ERROR: {message}", file=sys.stderr)
            failures.append(message)
            if not continue_on_error:
                return 1

    if failures:
        print(
            f"ERROR: {len(failures)}/{len(leaders)} Leader PR(s) failed "
            "during scheduled monitor.",
            file=sys.stderr,
        )
        return 1
    return 0


def should_run_schedule_mode(*, schedule_flag: bool) -> bool:
    """True when CLI ``--schedule`` is set or Actions event is ``schedule``."""
    if schedule_flag:
        return True
    return os.environ.get("GITHUB_EVENT_NAME", "").strip() == "schedule"


def resolve_leader_repo(explicit: str | None) -> str:
    """Resolve OWNER/REPO for Leader discovery (CLI, GAP_LEADER_REPO, GITHUB_REPOSITORY)."""
    for candidate in (
        (explicit or "").strip(),
        _env_nonempty(ENV_LEADER_REPO) or "",
        os.environ.get("GITHUB_REPOSITORY", "").strip(),
    ):
        if candidate:
            return candidate
    raise GapPrMonitorError(
        "Cannot resolve Leader repo for schedule mode. Pass --leader-repo, "
        f"set {ENV_LEADER_REPO}, or run in Actions (GITHUB_REPOSITORY)."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stage-1 GAP PR monitor: read leader state.json, post success or "
            "merge-failure commit statuses, and update pr-status in the state file."
        )
    )
    parser.add_argument(
        "--state-file",
        default=None,
        metavar="PATH",
        help="Path to leader state.json (alternative to --trigger-id).",
    )
    parser.add_argument(
        "--trigger-id",
        default=None,
        metavar="ID",
        help="Leader trigger id (resolves to GAP Leaders/<date>_<id>/state.json).",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=None,
        dest="labels",
        metavar="NAME",
        help=(
            "Leader PR label (repeatable). A gap-* label selects "
            "GAP Leaders/<date>_<gap-id>/state.json when --trigger-id is omitted. "
            "In Actions, prefer GAP_PR_LABELS instead."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repo root used with --trigger-id / labels (default: current directory).",
    )
    parser.add_argument(
        "--allow-gap-dir-fallback",
        action="store_true",
        help=(
            "If no trigger id/label, use exactly one GAP Leaders/<date>_gap-*/state.json "
            "under --repo-root (fail if zero or many)."
        ),
    )
    parser.add_argument(
        "--leader-pr-url",
        default=None,
        metavar="URL",
        help=(
            "Leader PR URL. With --publish-state, after the state commit is "
            "pushed and overall-status is success, post a green "
            f"{DEFAULT_CHECK_NAME!r} status on the published SHA "
            f"(RHOAIENG-97052). Also accepted via {ENV_LEADER_PR_URL}."
        ),
    )
    parser.add_argument(
        "--publish-state",
        action="store_true",
        help=(
            "RHOAIENG-97052: commit and push state.json from Python, then post "
            "Leader success against that published commit SHA (never before)."
        ),
    )
    parser.add_argument(
        "--check-name",
        default=DEFAULT_CHECK_NAME,
        help=f"Commit status context (default: {DEFAULT_CHECK_NAME!r}).",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Override description on commit statuses (default depends on outcome).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Inspect PRs and plan updates without POSTing statuses or writing "
            "the state file."
        ),
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep processing remaining PRs if one inspection/update fails.",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help=(
            "RHOAIENG-97050: discover open Leader PRs (GAP label) and run the "
            "Stage-1 monitor on each. Also selected automatically when "
            "GITHUB_EVENT_NAME=schedule."
        ),
    )
    parser.add_argument(
        "--leader-repo",
        default=None,
        metavar="OWNER/REPO",
        help=(
            "Leader repository for --schedule / schedule events "
            f"(default: {ENV_LEADER_REPO} or GITHUB_REPOSITORY)."
        ),
    )
    parser.add_argument(
        "--leader-label",
        default=None,
        metavar="NAME",
        help=(
            "Label that identifies Leader PRs for schedule discovery "
            f"(default: {ENV_LEADER_LABEL} or {DEFAULT_LEADER_LABEL!r})."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root)

    if should_run_schedule_mode(schedule_flag=args.schedule):
        label = (
            (args.leader_label or "").strip()
            or _env_nonempty(ENV_LEADER_LABEL)
            or DEFAULT_LEADER_LABEL
        )
        try:
            leader_repo = resolve_leader_repo(args.leader_repo)
            return run_scheduled_monitors(
                repo_root,
                leader_repo=leader_repo,
                label=label,
                check_name=args.check_name,
                dry_run=args.dry_run,
                # Schedule should not stop the whole batch on one Leader failure.
                continue_on_error=True,
                description=args.description,
            )
        except GapPrMonitorError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    state_file, trigger_id, labels, leader_pr_url = resolve_inputs_for_cli(
        state_file=args.state_file,
        trigger_id=args.trigger_id,
        labels=args.labels,
        leader_pr_url=args.leader_pr_url,
    )
    try:
        state_path = resolve_state_file(
            state_file=state_file,
            trigger_id=trigger_id,
            labels=labels,
            root=repo_root,
            allow_gap_dir_fallback=args.allow_gap_dir_fallback,
        )
        # Emit path for thin Actions workflows (optional follow-up steps).
        append_github_output(
            "state_path",
            state_path_for_output(state_path, repo_root=repo_root),
        )
        result = run_monitor_publish_and_signal(
            state_path,
            repo_root=repo_root,
            leader_pr_url=leader_pr_url,
            publish=args.publish_state,
            check_name=args.check_name,
            dry_run=args.dry_run,
            continue_on_error=args.continue_on_error,
            description=args.description,
        )
    except GapPrMonitorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    prefix = "[dry-run] " if result.dry_run else ""
    total = len(result.success_urls) + len(result.merge_failure_urls)
    print(f"{prefix}Processed {total} PR(s) from {result.state_path}")
    for url in result.success_urls:
        print(f"{prefix}success: {url}")
    for url in result.merge_failure_urls:
        print(f"{prefix}merge-failure: {url}")
    if result.overall_status is not None:
        print(f"{prefix}overall-status: {result.overall_status}")
        append_github_output("overall_status", result.overall_status)
    if result.published_sha:
        print(f"{prefix}published-sha: {result.published_sha}")
        append_github_output("published_sha", result.published_sha)
    if result.leader_status_posted:
        print(f"{prefix}Posted Leader conclusion status (auto-merge signal)")
    for url in result.updated_urls:
        print(f"{prefix}Posted status for {url}")
    for url in result.skipped_urls:
        print(f"{prefix}Skipped status post for {url}")
    if not result.dry_run:
        print(f"Updated state file: {result.state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
