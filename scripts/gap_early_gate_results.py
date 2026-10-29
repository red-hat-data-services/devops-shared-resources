#!/usr/bin/env python3
"""GAP Early Gate results monitor (Leader comment → overall-status).

After Stage-1 sets ``overall-status=early-gate-triggered`` and comments
``/early-gate``, Early Gate posts a terminal comment on the **Leader** PR
(``<!-- early-gate-build-complete -->`` plus Early Gate passed/failed).

This script:

1. Reads Leader PR comments (or a single comment body from Actions).
2. Parses the latest terminal Early Gate outcome.
3. When state is ``early-gate-triggered``, writes ``success`` / ``failure``.
4. Optionally publishes ``state.json`` and greens the Leader (RHAI-8079).

Progress comments (``eg-operator-build-status``, queue notices, ``/early-gate``
triggers) are ignored. Final ``success`` / ``failure`` are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.pr_status_updater import GhCommandError, PRStatusUpdater, parse_pr_url
from lib.state_file import (
    OVERALL_STATUS_EARLY_GATE_TRIGGERED,
    OVERALL_STATUS_FAILURE,
    OVERALL_STATUS_SUCCESS,
)

# Import via the ``scripts`` package so tests and CLI share one module identity.
from scripts import gap_pr_monitor as gpm  # noqa: E402

DEFAULT_CHECK_NAME = gpm.DEFAULT_CHECK_NAME
ENV_LEADER_PR_URL = gpm.ENV_LEADER_PR_URL
ENV_PR_LABELS = gpm.ENV_PR_LABELS
ENV_STATE_FILE = gpm.ENV_STATE_FILE
ENV_TRIGGER_ID = gpm.ENV_TRIGGER_ID
FINAL_OVERALL_STATUSES = gpm.FINAL_OVERALL_STATUSES
GapPrMonitorError = gpm.GapPrMonitorError
apply_overall_status = gpm.apply_overall_status
checkout_leader_branch = gpm.checkout_leader_branch
load_state = gpm.load_state
post_leader_conclusion = gpm.post_leader_conclusion
publish_state = gpm.publish_state
resolve_inputs_for_cli = gpm.resolve_inputs_for_cli
resolve_state_file = gpm.resolve_state_file
save_state = gpm.save_state
split_labels_csv = gpm.split_labels_csv
verify_leader_head = gpm.verify_leader_head
_run_gh = gpm._run_gh

# HTML marker used by rhods-ci-bot terminal EG reports (may include digest lines).
EARLY_GATE_BUILD_COMPLETE_MARKER = "early-gate-build-complete"

# Visible headlines / Status row (screenshot contract).
_PASSED_PATTERNS = (
    re.compile(r"\*\*Early Gate passed\*\*", re.IGNORECASE),
    re.compile(r"Early Gate passed", re.IGNORECASE),
    re.compile(
        r"\*\*Status\*\*\s*\|\s*:white_check_mark:\s*SUCCESS",
        re.IGNORECASE,
    ),
    re.compile(r"\|\s*\*\*Status\*\*\s*\|\s*:white_check_mark:\s*SUCCESS", re.IGNORECASE),
)
_FAILED_PATTERNS = (
    re.compile(r"\*\*Early Gate failed\*\*", re.IGNORECASE),
    re.compile(r"Early Gate failed", re.IGNORECASE),
    re.compile(r"\*\*Status\*\*\s*\|\s*:x:\s*FAILED", re.IGNORECASE),
    re.compile(r"\|\s*\*\*Status\*\*\s*\|\s*:x:\s*FAILED", re.IGNORECASE),
)

# Progress / non-terminal markers — never conclude from these alone.
_PROGRESS_MARKERS = (
    "eg-operator-build-status",
    "early-gate-queue-url",
)


@dataclass(frozen=True)
class EarlyGateCommentOutcome:
    """Parsed terminal Early Gate comment."""

    overall_status: str  # success | failure
    comment_id: int | None = None
    created_at: str | None = None
    author: str | None = None


@dataclass(frozen=True)
class EarlyGateResultsResult:
    """Outcome of one EG-results run."""

    state_path: Path
    previous_overall: str | None
    overall_status: str | None
    applied: bool
    leader_status_posted: bool
    published_sha: str | None
    dry_run: bool
    reason: str = ""


def comment_looks_like_early_gate_complete(body: str) -> bool:
    """True when a comment body is a candidate terminal EG report.

    Used by the Actions job filter / early-exit path. Prefer the HTML marker;
    also accept the visible Early Gate passed/failed headline from screenshots.
    """
    text = body or ""
    if EARLY_GATE_BUILD_COMPLETE_MARKER in text:
        return True
    if re.search(r"Early Gate passed", text, re.IGNORECASE):
        return True
    if re.search(r"Early Gate failed", text, re.IGNORECASE):
        return True
    return False


def parse_early_gate_outcome_from_body(body: str) -> str | None:
    """Return ``success`` / ``failure`` from a terminal EG comment, else None.

    Requires the build-complete marker when present in mixed noise, but also
    accepts bodies that only show the screenshot headline + Status row (some
    older comments used a bare ``<!-- early-gate-build-complete -->``).
    """
    text = body or ""
    if not text.strip():
        return None

    # Never treat pure progress comments as terminal.
    stripped_lower = text.lower()
    if EARLY_GATE_BUILD_COMPLETE_MARKER not in stripped_lower:
        # Allow headline-only screenshot style without marker (rare).
        if not (
            re.search(r"Early Gate passed", text, re.IGNORECASE)
            or re.search(r"Early Gate failed", text, re.IGNORECASE)
        ):
            return None
        # Reject if this is only a progress marker comment.
        if any(m in stripped_lower for m in _PROGRESS_MARKERS):
            return None

    passed = any(p.search(text) for p in _PASSED_PATTERNS)
    failed = any(p.search(text) for p in _FAILED_PATTERNS)
    if passed and not failed:
        return OVERALL_STATUS_SUCCESS
    if failed and not passed:
        return OVERALL_STATUS_FAILURE
    # Ambiguous or re-seed stub with marker but no verdict.
    return None


def _comment_sort_key(comment: dict[str, Any]) -> tuple[str, int]:
    created = str(comment.get("created_at") or comment.get("createdAt") or "")
    cid = comment.get("id")
    try:
        cid_int = int(cid) if cid is not None else 0
    except (TypeError, ValueError):
        cid_int = 0
    return (created, cid_int)


def select_latest_early_gate_outcome(
    comments: Sequence[dict[str, Any]],
) -> EarlyGateCommentOutcome | None:
    """Pick the latest comment that parses to a definite EG overall status."""
    best: EarlyGateCommentOutcome | None = None
    best_key: tuple[str, int] = ("", -1)
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "")
        outcome = parse_early_gate_outcome_from_body(body)
        if outcome is None:
            continue
        key = _comment_sort_key(comment)
        if key >= best_key:
            best_key = key
            author = comment.get("user") or comment.get("author") or {}
            login = None
            if isinstance(author, dict):
                login = author.get("login")
            elif isinstance(author, str):
                login = author
            cid = comment.get("id")
            try:
                cid_int = int(cid) if cid is not None else None
            except (TypeError, ValueError):
                cid_int = None
            best = EarlyGateCommentOutcome(
                overall_status=outcome,
                comment_id=cid_int,
                created_at=str(comment.get("created_at") or comment.get("createdAt") or "")
                or None,
                author=str(login) if login else None,
            )
    return best


def list_leader_pr_comments(
    leader_pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> list[dict[str, Any]]:
    """List issue comments on the Leader PR (paginated)."""
    run = gh_runner or _run_gh
    ref = parse_pr_url(leader_pr_url)
    endpoint = f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments"
    raw = run(["api", endpoint, "--paginate"])
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(
            f"Invalid JSON listing comments for {leader_pr_url}: {exc}"
        ) from exc
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    raise GapPrMonitorError(
        f"Unexpected comments payload type for {leader_pr_url}: {type(data).__name__}"
    )


def fetch_leader_labels(
    leader_pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> list[str]:
    """Return label names on the Leader PR."""
    run = gh_runner or _run_gh
    ref = parse_pr_url(leader_pr_url)
    raw = run(
        [
            "pr",
            "view",
            str(ref.number),
            "-R",
            ref.slug,
            "--json",
            "labels",
        ]
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(
            f"Invalid JSON from gh pr view labels for {leader_pr_url}: {exc}"
        ) from exc
    labels: list[str] = []
    for item in data.get("labels") or []:
        if isinstance(item, dict) and item.get("name"):
            labels.append(str(item["name"]))
        elif isinstance(item, str) and item.strip():
            labels.append(item.strip())
    return labels


def fetch_leader_head_ref(
    leader_pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> str:
    """Return the Leader PR head branch name."""
    run = gh_runner or _run_gh
    ref = parse_pr_url(leader_pr_url)
    raw = run(
        [
            "pr",
            "view",
            str(ref.number),
            "-R",
            ref.slug,
            "--json",
            "headRefName",
        ]
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(
            f"Invalid JSON from gh pr view headRefName for {leader_pr_url}: {exc}"
        ) from exc
    head = str(data.get("headRefName") or "").strip()
    if not head:
        raise GapPrMonitorError(f"Leader PR has empty headRefName: {leader_pr_url}")
    return head


def decide_overall_from_comments(
    *,
    current_overall: str | None,
    comments: Sequence[dict[str, Any]],
    comment_body: str | None = None,
) -> tuple[str | None, str]:
    """Decide the next overall-status from EG comments and current state.

    Returns ``(new_overall_or_None, reason)``.
    ``None`` means do not change state.
    """
    existing = (current_overall or "").strip() or None
    if existing in FINAL_OVERALL_STATUSES:
        return None, f"overall-status already final ({existing}); no-op"

    if existing != OVERALL_STATUS_EARLY_GATE_TRIGGERED:
        return (
            None,
            f"overall-status={existing!r} is not {OVERALL_STATUS_EARLY_GATE_TRIGGERED!r}; "
            "waiting for Stage-1 Early Gate handoff",
        )

    if comment_body is not None and str(comment_body).strip():
        outcome = parse_early_gate_outcome_from_body(str(comment_body))
        if outcome is None:
            return None, "trigger comment is not a terminal Early Gate passed/failed report"
        return outcome, "applied from triggering comment body"

    selected = select_latest_early_gate_outcome(comments)
    if selected is None:
        return None, "no terminal Early Gate passed/failed comment found"
    return (
        selected.overall_status,
        f"applied from latest Early Gate comment id={selected.comment_id}",
    )


def run_early_gate_results(
    *,
    repo_root: Path,
    leader_pr_url: str,
    state_file: str | None = None,
    trigger_id: str | None = None,
    labels: Sequence[str] | None = None,
    comment_body: str | None = None,
    publish: bool = False,
    check_name: str = DEFAULT_CHECK_NAME,
    dry_run: bool = False,
    remote: str = "origin",
    head_ref: str | None = None,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    git_runner: Callable[..., str] | None = None,
    updater_factory: Callable[..., PRStatusUpdater] | None = None,
) -> EarlyGateResultsResult:
    """Apply EG comment outcome to state and optionally publish + green Leader."""
    root = repo_root.expanduser().resolve()
    leader_url = (leader_pr_url or "").strip()
    if not leader_url:
        raise GapPrMonitorError("Leader PR URL is required.")

    resolved_labels = list(labels) if labels else None
    if not resolved_labels and not (trigger_id or "").strip() and not (state_file or "").strip():
        resolved_labels = fetch_leader_labels(leader_url, gh_runner=gh_runner)

    branch = (head_ref or "").strip() or None
    if publish and not dry_run:
        if not branch:
            branch = fetch_leader_head_ref(leader_url, gh_runner=gh_runner)
        checkout_leader_branch(root, branch, remote=remote, git_runner=git_runner)

    state_path = resolve_state_file(
        state_file=state_file,
        trigger_id=trigger_id,
        labels=resolved_labels,
        root=root,
    )
    payload = load_state(state_path)
    previous = str(payload.get("overall-status") or "").strip() or None

    comments: list[dict[str, Any]] = []
    if comment_body is None or not str(comment_body).strip():
        comments = list_leader_pr_comments(leader_url, gh_runner=gh_runner)

    new_overall, reason = decide_overall_from_comments(
        current_overall=previous,
        comments=comments,
        comment_body=comment_body,
    )

    if new_overall is None:
        return EarlyGateResultsResult(
            state_path=state_path,
            previous_overall=previous,
            overall_status=previous,
            applied=False,
            leader_status_posted=False,
            published_sha=None,
            dry_run=dry_run,
            reason=reason,
        )

    if dry_run:
        print(
            f"[dry-run] would set overall-status={new_overall!r} on {state_path} "
            f"({reason})"
        )
        return EarlyGateResultsResult(
            state_path=state_path,
            previous_overall=previous,
            overall_status=new_overall,
            applied=True,
            leader_status_posted=False,
            published_sha=None,
            dry_run=True,
            reason=reason,
        )

    apply_overall_status(payload, new_overall)
    save_state(state_path, payload)

    if not publish:
        return EarlyGateResultsResult(
            state_path=state_path,
            previous_overall=previous,
            overall_status=new_overall,
            applied=True,
            leader_status_posted=False,
            published_sha=None,
            dry_run=False,
            reason=reason,
        )

    assert branch is not None
    published = publish_state(
        root,
        state_path,
        branch,
        remote=remote,
        dry_run=False,
        commit_message=(
            f"Update GAP overall-status from Early Gate results ({new_overall})"
        ),
        git_runner=git_runner,
    )

    factory = updater_factory or PRStatusUpdater
    updater = factory(check_name=check_name, dry_run=False)
    verify_leader_head(leader_url, published.sha, updater=updater)
    leader_result = post_leader_conclusion(
        leader_url,
        published.sha,
        overall_status=new_overall,
        updater=updater,
    )
    return EarlyGateResultsResult(
        state_path=state_path,
        previous_overall=previous,
        overall_status=new_overall,
        applied=True,
        leader_status_posted=not leader_result.skipped,
        published_sha=published.sha,
        dry_run=False,
        reason=reason,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply Early Gate Leader comment results to overall-status "
            "and optionally publish + green the Leader."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Checkout root of gated-artifacts-promoter (contains GAP Leaders/).",
    )
    parser.add_argument(
        "--leader-pr-url",
        default="",
        help=f"Leader PR URL (or set {ENV_LEADER_PR_URL}).",
    )
    parser.add_argument(
        "--state-file",
        default="",
        help=f"Explicit state.json path (or set {ENV_STATE_FILE}).",
    )
    parser.add_argument(
        "--trigger-id",
        default="",
        help=f"Leader trigger id gap-* (or set {ENV_TRIGGER_ID}).",
    )
    parser.add_argument(
        "--labels",
        default="",
        help=f"Comma-separated Leader labels (or set {ENV_PR_LABELS}).",
    )
    parser.add_argument(
        "--comment-body",
        default="",
        help=(
            "Optional single comment body (issue_comment event). "
            "When empty, all Leader comments are listed."
        ),
    )
    parser.add_argument(
        "--publish-state",
        action="store_true",
        help="Commit/push state.json and green the Leader on success|failure.",
    )
    parser.add_argument(
        "--check-name",
        default=DEFAULT_CHECK_NAME,
        help="GitHub commit status context for Leader green.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and decide without writing state or posting statuses.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote for checkout/publish.",
    )
    parser.add_argument(
        "--head-ref",
        default="",
        help="Leader head branch (optional; fetched from PR when publishing).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    state_file, trigger_id, labels, leader_pr_url = resolve_inputs_for_cli(
        state_file=args.state_file or None,
        trigger_id=args.trigger_id or None,
        labels=split_labels_csv(args.labels) or None,
        leader_pr_url=args.leader_pr_url or None,
    )
    if not leader_pr_url:
        parser.error(f"--leader-pr-url or {ENV_LEADER_PR_URL} is required")

    comment_body = (args.comment_body or "").strip() or None
    # Prefer env from Actions when not passed on CLI.
    if comment_body is None:
        comment_body = (os.environ.get("GAP_EG_COMMENT_BODY") or "").strip() or None

    try:
        result = run_early_gate_results(
            repo_root=Path(args.repo_root),
            leader_pr_url=leader_pr_url,
            state_file=state_file,
            trigger_id=trigger_id,
            labels=labels,
            comment_body=comment_body,
            publish=bool(args.publish_state),
            check_name=args.check_name,
            dry_run=bool(args.dry_run),
            remote=args.remote,
            head_ref=(args.head_ref or "").strip() or None,
        )
    except (GapPrMonitorError, GhCommandError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"state_path: {result.state_path}")
    print(f"previous_overall: {result.previous_overall}")
    print(f"overall_status: {result.overall_status}")
    print(f"applied: {result.applied}")
    print(f"leader_status_posted: {result.leader_status_posted}")
    print(f"published_sha: {result.published_sha}")
    print(f"dry_run: {result.dry_run}")
    print(f"reason: {result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
