#!/usr/bin/env python3
"""GAP merge-orchestrator (Leader EG labels/comments → promotion-gates + children).

Also imported as the Early Gate results monitor. After Stage-1 sets
``component-builds=success`` and comments ``/early-gate``, Early Gate posts
terminal Leader comments and labels. This script wakes on those labels (or
comments), reconciles comment history, and updates ``promotion-gates``.

Merge contract (``promotion-gates`` is the source of truth):

1. Require successful component builds before triggering Early Gate.
2. Record EG build and test evidence from trusted comments and labels.
3. Only passing component builds and EG build/tests approve child merges and
   successful Leader archival. Failed outcomes remain active for retry until
   a verified newer Leader retires the batch.

Progress comments and untrusted authors are ignored. A label/comment wake-up
is only a signal; the script always reconciles current comment + label state
for **that** Leader.
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

from lib.leader_lifecycle import retired_outcome
from lib.leader_history import history_state_outcome, is_history_state

from lib.gap_promotion_gates import (
    GATE_COMPONENT_BUILDS,
    GATE_EARLY_GATE_BUILD,
    GATE_EARLY_GATE_TEST,
    GATE_STATUS_FAILURE,
    GATE_STATUS_PENDING,
    GATE_STATUS_SUCCESS,
    LABEL_EG_BUILD_FAILED,
    LABEL_EG_BUILD_SUCCESS,
    LABEL_EG_TEST_FAILED,
    LABEL_EG_TEST_SUCCESS,
    derive_legacy_overall_status,
    early_gate_concluded,
    early_gate_pair_overall,
    ensure_promotion_gates,
    gate_status_from_labels,
    get_gate_status,
    mark_eg_test_skipped_after_build_failure,
    migrate_legacy_overall_status,
    set_gate_status,
    strip_legacy_overall_status,
)
from lib.pr_status_updater import GhCommandError, PRStatusUpdater, parse_pr_url
from lib.state_file import (
    OVERALL_STATUS_FAILURE,
    OVERALL_STATUS_SUCCESS,
)

# Import via the ``scripts`` package so tests and CLI share one module identity.
from scripts import gap_pr_monitor as gpm  # noqa: E402

DEFAULT_CHECK_NAME = gpm.DEFAULT_CHECK_NAME
CHILD_PASS_PR_STATUSES = gpm.CHILD_PASS_PR_STATUSES
ENV_LEADER_PR_URL = gpm.ENV_LEADER_PR_URL
ENV_PR_LABELS = gpm.ENV_PR_LABELS
ENV_STATE_FILE = gpm.ENV_STATE_FILE
ENV_TRIGGER_ID = gpm.ENV_TRIGGER_ID
GapPrMonitorError = gpm.GapPrMonitorError
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

# Child check descriptions after both EG build + test are terminal (phase-1).
CHILD_EG_SUCCESS_DESCRIPTION = (
    "GAP: Early Gate build+tests passed — cleared for auto-merge"
)
CHILD_EG_FAILURE_DESCRIPTION = (
    "GAP: Early Gate build/tests failed — merge blocked"
)

# HTML markers used by rhods-ci-bot terminal EG reports.
EARLY_GATE_BUILD_COMPLETE_MARKER = "early-gate-build-complete"
EARLY_GATE_JOB_COMPLETE_MARKER = "early-gate-job-complete"

# Default Early Gate reporting identity (override via GAP_EG_TRUSTED_AUTHORS).
DEFAULT_TRUSTED_EG_AUTHORS = frozenset({"rhods-ci-bot"})
ENV_TRUSTED_AUTHORS = "GAP_EG_TRUSTED_AUTHORS"
EARLY_GATE_COMMIT_RE = re.compile(r"\bGAP_PR_COMMIT=([0-9a-f]{40})\b")

# Visible headlines / Status row — build comments (early-gate-build-complete).
_BUILD_PASSED_PATTERNS = (
    re.compile(r"\*\*Early Gate passed\*\*", re.IGNORECASE),
    re.compile(r"\*\*Early Gate Build [Pp]assed\*\*", re.IGNORECASE),
    re.compile(r"Early Gate passed", re.IGNORECASE),
    re.compile(
        r"\*\*Status\*\*\s*\|\s*:white_check_mark:\s*SUCCESS",
        re.IGNORECASE,
    ),
    re.compile(r"\|\s*\*\*Status\*\*\s*\|\s*:white_check_mark:\s*SUCCESS", re.IGNORECASE),
)
_BUILD_FAILED_PATTERNS = (
    re.compile(r"\*\*Early Gate failed\*\*", re.IGNORECASE),
    re.compile(r"\*\*Early Gate Build [Ff]ailed\*\*", re.IGNORECASE),
    re.compile(r"Early Gate failed", re.IGNORECASE),
    re.compile(r"\*\*Status\*\*\s*\|\s*:x:\s*FAILED", re.IGNORECASE),
    re.compile(r"\|\s*\*\*Status\*\*\s*\|\s*:x:\s*FAILED", re.IGNORECASE),
)

# Test comments (early-gate-job-complete).
_TEST_PASSED_PATTERNS = (
    re.compile(r"\*\*Early Gate Tests? Passed\*\*", re.IGNORECASE),
    re.compile(r"Early Gate Tests? Passed", re.IGNORECASE),
    re.compile(
        r"\*\*Status\*\*\s*\|\s*:white_check_mark:\s*SUCCESS",
        re.IGNORECASE,
    ),
    re.compile(r"\|\s*\*\*Status\*\*\s*\|\s*:white_check_mark:\s*SUCCESS", re.IGNORECASE),
)
_TEST_FAILED_PATTERNS = (
    re.compile(r"\*\*Early Gate Tests? Failed\*\*", re.IGNORECASE),
    re.compile(r"Early Gate Tests? Failed", re.IGNORECASE),
    re.compile(r"\*\*Early Gate Test - Complete\*\*", re.IGNORECASE),
    re.compile(r"\*\*Status\*\*\s*\|\s*:x:\s*FAILED", re.IGNORECASE),
    re.compile(r"\|\s*\*\*Status\*\*\s*\|\s*:x:\s*FAILED", re.IGNORECASE),
)

# Back-compat aliases used by older tests.
_PASSED_PATTERNS = _BUILD_PASSED_PATTERNS
_FAILED_PATTERNS = _BUILD_FAILED_PATTERNS

# Progress / non-terminal markers — never conclude from these alone.
_PROGRESS_MARKERS = (
    "eg-operator-build-status",
    "early-gate-queue-url",
)


@dataclass(frozen=True)
class EarlyGateCommentOutcome:
    """Parsed terminal Early Gate comment (build or test)."""

    overall_status: str  # success | failure
    comment_id: int | None = None
    created_at: str | None = None
    author: str | None = None
    gate: str = GATE_EARLY_GATE_BUILD


@dataclass(frozen=True)
class EarlyGateResultsResult:
    """Outcome of one merge-orchestrator / EG-results run."""

    state_path: Path
    previous_overall: str | None
    overall_status: str | None
    applied: bool
    leader_status_posted: bool
    published_sha: str | None
    dry_run: bool
    reason: str = ""
    child_urls_posted: tuple[str, ...] = ()
    child_check_state: str | None = None
    eg_build_status: str | None = None
    eg_test_status: str | None = None


def trusted_early_gate_authors(
    raw: str | None = None,
) -> frozenset[str]:
    """Return logins allowed to publish terminal Early Gate results."""
    text = raw if raw is not None else os.environ.get(ENV_TRUSTED_AUTHORS, "")
    text = (text or "").strip()
    if not text:
        return DEFAULT_TRUSTED_EG_AUTHORS
    authors = {part.strip() for part in text.split(",") if part.strip()}
    return frozenset(authors) if authors else DEFAULT_TRUSTED_EG_AUTHORS


def comment_author_login(comment: dict[str, Any]) -> str | None:
    """Extract the GitHub login from a comment API object."""
    author = comment.get("user") or comment.get("author") or {}
    if isinstance(author, dict):
        login = author.get("login")
        return str(login) if login else None
    if isinstance(author, str) and author.strip():
        return author.strip()
    return None


def is_trusted_early_gate_author(
    login: str | None,
    *,
    trusted: frozenset[str] | None = None,
) -> bool:
    """True when ``login`` is an allowed Early Gate reporter."""
    if not login:
        return False
    allowed = trusted if trusted is not None else trusted_early_gate_authors()
    return login in allowed


def comment_looks_like_early_gate_complete(body: str) -> bool:
    """True when a comment body is a candidate terminal EG build or test report."""
    text = (body or "").lower()
    return (
        EARLY_GATE_BUILD_COMPLETE_MARKER in text
        or EARLY_GATE_JOB_COMPLETE_MARKER in text
    )


def _parse_marked_outcome(
    body: str,
    *,
    marker: str,
    passed_patterns: Sequence[re.Pattern[str]],
    failed_patterns: Sequence[re.Pattern[str]],
) -> str | None:
    text = body or ""
    if not text.strip():
        return None
    stripped_lower = text.lower()
    if marker not in stripped_lower:
        return None
    if any(m in stripped_lower for m in _PROGRESS_MARKERS):
        return None
    passed = any(p.search(text) for p in passed_patterns)
    failed = any(p.search(text) for p in failed_patterns)
    if passed and not failed:
        return OVERALL_STATUS_SUCCESS
    if failed and not passed:
        return OVERALL_STATUS_FAILURE
    return None


def parse_early_gate_outcome_from_body(body: str) -> str | None:
    """Return ``success`` / ``failure`` from an EG **build** comment, else None."""
    return _parse_marked_outcome(
        body,
        marker=EARLY_GATE_BUILD_COMPLETE_MARKER,
        passed_patterns=_BUILD_PASSED_PATTERNS,
        failed_patterns=_BUILD_FAILED_PATTERNS,
    )


def parse_early_gate_test_outcome_from_body(body: str) -> str | None:
    """Return ``success`` / ``failure`` from an EG **test** comment, else None."""
    return _parse_marked_outcome(
        body,
        marker=EARLY_GATE_JOB_COMPLETE_MARKER,
        passed_patterns=_TEST_PASSED_PATTERNS,
        failed_patterns=_TEST_FAILED_PATTERNS,
    )


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
    *,
    trusted_authors: frozenset[str] | None = None,
    gate: str = GATE_EARLY_GATE_BUILD,
    leader_commit: str | None = None,
) -> EarlyGateCommentOutcome | None:
    """Pick the latest trusted comment for one EG gate (build or test)."""
    allowed = (
        trusted_authors
        if trusted_authors is not None
        else trusted_early_gate_authors()
    )
    parse = (
        parse_early_gate_test_outcome_from_body
        if gate == GATE_EARLY_GATE_TEST
        else parse_early_gate_outcome_from_body
    )
    best: EarlyGateCommentOutcome | None = None
    best_key: tuple[str, int] = ("", -1)
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        login = comment_author_login(comment)
        if not is_trusted_early_gate_author(login, trusted=allowed):
            continue
        body = str(comment.get("body") or "")
        if leader_commit is not None:
            match = EARLY_GATE_COMMIT_RE.search(body)
            if match is None or match.group(1) != leader_commit:
                continue
        outcome = parse(body)
        if outcome is None:
            continue
        key = _comment_sort_key(comment)
        if key >= best_key:
            best_key = key
            cid = comment.get("id")
            try:
                cid_int = int(cid) if cid is not None else None
            except (TypeError, ValueError):
                cid_int = None
            best = EarlyGateCommentOutcome(
                overall_status=outcome,
                comment_id=cid_int,
                created_at=str(
                    comment.get("created_at") or comment.get("createdAt") or ""
                )
                or None,
                author=login,
                gate=gate,
            )
    return best


def resolve_eg_gate_status(
    *,
    gate: str,
    comments: Sequence[dict[str, Any]],
    labels: Sequence[str],
    trusted_authors: frozenset[str] | None = None,
    leader_commit: str | None = None,
) -> tuple[str | None, str]:
    """Resolve one EG gate from comments (preferred) or Leader labels."""
    selected = select_latest_early_gate_outcome(
        comments,
        trusted_authors=trusted_authors,
        gate=gate,
        leader_commit=leader_commit,
    )
    if selected is not None:
        return (
            selected.overall_status,
            f"comment id={selected.comment_id} author={selected.author}",
        )
    if leader_commit is not None:
        return None, "no trusted terminal comment for current GAP PR commit"
    if gate == GATE_EARLY_GATE_BUILD:
        from_labels = gate_status_from_labels(
            labels,
            success_label=LABEL_EG_BUILD_SUCCESS,
            failed_label=LABEL_EG_BUILD_FAILED,
        )
    else:
        from_labels = gate_status_from_labels(
            labels,
            success_label=LABEL_EG_TEST_SUCCESS,
            failed_label=LABEL_EG_TEST_FAILED,
        )
    if from_labels is None:
        return None, "no trusted terminal comment or wake label"
    return from_labels, f"leader label ({from_labels})"


def _flatten_gh_paginate_slurp(data: Any) -> list[dict[str, Any]]:
    """Flatten ``gh api --paginate --slurp`` JSON into comment objects."""
    comments: list[dict[str, Any]] = []
    if isinstance(data, list):
        if not data:
            return []
        # --slurp: list of page arrays, or a single page array of objects.
        if all(isinstance(page, list) for page in data):
            for page in data:
                for item in page:
                    if isinstance(item, dict):
                        comments.append(item)
            return comments
        return [c for c in data if isinstance(c, dict)]
    raise GapPrMonitorError(
        f"Unexpected comments payload type: {type(data).__name__}"
    )


def list_leader_pr_comments(
    leader_pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> list[dict[str, Any]]:
    """List issue comments on the Leader PR (all pages via --paginate --slurp)."""
    run = gh_runner or _run_gh
    ref = parse_pr_url(leader_pr_url)
    endpoint = f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments"
    raw = run(["api", endpoint, "--paginate", "--slurp"])
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(
            f"Invalid JSON listing comments for {leader_pr_url}: {exc}"
        ) from exc
    return _flatten_gh_paginate_slurp(data)


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


def fetch_leader_head_sha(
    leader_pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> str:
    run = gh_runner or _run_gh
    ref = parse_pr_url(leader_pr_url)
    sha = run(["api", f"repos/{ref.slug}/pulls/{ref.number}", "--jq", ".head.sha"])
    sha = (sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GapPrMonitorError(f"Could not resolve current Leader commit for {leader_pr_url}.")
    return sha


def decide_overall_from_comments(
    *,
    current_overall: str | None = None,
    comments: Sequence[dict[str, Any]],
    trusted_authors: frozenset[str] | None = None,
    labels: Sequence[str] | None = None,
    payload: dict[str, Any] | None = None,
    eg_already_concluded: bool | None = None,
    leader_commit: str | None = None,
) -> tuple[str | None, str, dict[str, str | None]]:
    """Update EG gates from comments/labels; return derived overall when both done.

    ``promotion-gates`` on ``payload`` is mutated. Returns
    ``(derived_overall_or_None, reason, gate_snapshot)``.
    ``None`` means do not publish Leader/child conclusion yet (or already done).

    ``current_overall`` is ignored (legacy arg); gates are SoT.
    """
    if payload is not None and retired_outcome(payload):
        return None, "Leader retired; ignoring late Early Gate results", {}
    del current_overall  # legacy; gates are SoT
    label_list = list(labels or [])
    working = payload if payload is not None else {}
    ensure_promotion_gates(working)
    migrate_legacy_overall_status(working)

    builds = get_gate_status(working, GATE_COMPONENT_BUILDS)
    current_snap = {
        GATE_EARLY_GATE_BUILD: get_gate_status(working, GATE_EARLY_GATE_BUILD),
        GATE_EARLY_GATE_TEST: get_gate_status(working, GATE_EARLY_GATE_TEST),
    }
    # Normalize pending → None in snapshot for callers that expect unknown.
    for key, val in list(current_snap.items()):
        if val == "pending":
            current_snap[key] = None

    already = (
        early_gate_concluded(working)
        if eg_already_concluded is None
        else bool(eg_already_concluded)
    )
    if already and early_gate_pair_overall(working) == GATE_STATUS_SUCCESS:
        mark_eg_test_skipped_after_build_failure(working)
        return (
            None,
            "early-gate already concluded; no-op",
            {
                GATE_EARLY_GATE_BUILD: get_gate_status(working, GATE_EARLY_GATE_BUILD),
                GATE_EARLY_GATE_TEST: get_gate_status(working, GATE_EARLY_GATE_TEST),
            },
        )

    if builds != GATE_STATUS_SUCCESS:
        return (
            None,
            (
                f"component-builds={builds!r}; waiting for Stage-1 all-builds "
                "success before applying Early Gate results"
            ),
            current_snap,
        )

    build_status, build_src = resolve_eg_gate_status(
        gate=GATE_EARLY_GATE_BUILD,
        comments=comments,
        labels=label_list,
        trusted_authors=trusted_authors,
        leader_commit=leader_commit,
    )
    test_status, test_src = resolve_eg_gate_status(
        gate=GATE_EARLY_GATE_TEST,
        comments=comments,
        labels=label_list,
        trusted_authors=trusted_authors,
        leader_commit=leader_commit,
    )
    if build_status is not None:
        set_gate_status(working, GATE_EARLY_GATE_BUILD, build_status)
    if test_status is not None:
        set_gate_status(working, GATE_EARLY_GATE_TEST, test_status)
    # EG build failure ⇒ tests never start; mark test failure and conclude.
    if mark_eg_test_skipped_after_build_failure(working):
        test_status = GATE_STATUS_FAILURE
        test_src = "skipped (early-gate-build failed)"

    snapshot = {
        GATE_EARLY_GATE_BUILD: build_status
        if build_status is not None
        else (
            None
            if get_gate_status(working, GATE_EARLY_GATE_BUILD) == "pending"
            else get_gate_status(working, GATE_EARLY_GATE_BUILD)
        ),
        GATE_EARLY_GATE_TEST: test_status
        if test_status is not None
        else (
            None
            if get_gate_status(working, GATE_EARLY_GATE_TEST) == "pending"
            else get_gate_status(working, GATE_EARLY_GATE_TEST)
        ),
    }

    if not early_gate_concluded(working):
        return (
            None,
            (
                "waiting for early-gate conclusion "
                f"(build={build_status!r} via {build_src}; "
                f"test={test_status!r} via {test_src})"
            ),
            snapshot,
        )

    pair = early_gate_pair_overall(working)
    assert pair in {GATE_STATUS_SUCCESS, GATE_STATUS_FAILURE}
    new_overall = (
        OVERALL_STATUS_SUCCESS if pair == GATE_STATUS_SUCCESS else OVERALL_STATUS_FAILURE
    )
    reason = (
        f"EG concluded "
        f"(build={snapshot[GATE_EARLY_GATE_BUILD]} via {build_src}; "
        f"test={snapshot[GATE_EARLY_GATE_TEST]} via {test_src})"
    )
    return (new_overall, reason, snapshot)


def child_pass_pr_urls(payload: dict[str, Any]) -> list[str]:
    """Return pr-urls whose ``pr-status`` is a build/gate pass (EG may conclude them).

    Children that already failed their Stage-1 gate are omitted so EG-results
    does not overwrite their failing ``gated artifacts promoter`` check.
    Ignored children are also omitted so EG does not approve them.
    """
    from lib.state_file import child_entry_ignored

    if retired_outcome(payload):
        return []
    urls: list[str] = []
    for index, entry in enumerate(payload.get("pull-requests") or []):
        if not isinstance(entry, dict):
            raise GapPrMonitorError(f"pull-requests[{index}] must be an object.")
        if child_entry_ignored(entry):
            continue
        status = str(entry.get("pr-status") or "").strip()
        if status not in CHILD_PASS_PR_STATUSES:
            continue
        url = str(entry.get("pr-url") or "").strip()
        if not url:
            raise GapPrMonitorError(
                f"pull-requests[{index}] is missing a non-empty 'pr-url'."
            )
        urls.append(url)
    return urls


def child_check_for_overall(overall_status: str) -> tuple[str, str]:
    """Only a successful Early Gate may approve child merges."""
    if overall_status == OVERALL_STATUS_SUCCESS:
        return "success", CHILD_EG_SUCCESS_DESCRIPTION
    if overall_status == OVERALL_STATUS_FAILURE:
        # Preserve failure as a blocking child status.
        return "failure", CHILD_EG_FAILURE_DESCRIPTION
    raise GapPrMonitorError(
        f"Cannot post child Early Gate checks for overall-status={overall_status!r}"
    )


def post_child_statuses_for_early_gate(
    payload: dict[str, Any],
    *,
    overall_status: str,
    updater: PRStatusUpdater,
    continue_on_error: bool = True,
    pinned_only: bool = False,
) -> tuple[str, ...]:
    """Post blocking or passing EG conclusions onto build-passing children.

    Returns the tuple of child PR URLs that received a status post (not skipped).
    """
    urls = child_pass_pr_urls(payload)
    if not urls:
        return ()
    cli_status, description = child_check_for_overall(overall_status)
    posted: list[str] = []
    errors: list[tuple[str, str]] = []
    for url in urls:
        options = {}
        if pinned_only:
            eligible = [item for item in payload["pull-requests"]
                        if str(item.get("pr-status") or "").strip() in CHILD_PASS_PR_STATUSES]
            entry = next((item for item in eligible if item.get("pr-url") == url), None)
            if entry is None:
                entry = next((item for item in eligible
                              if str(item.get("pr-url") or "").strip() == url), {})
            sha = str(entry.get("head-sha") or (entry.get("build-observations") or {}).get("head-sha") or "")
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                print(f"WARNING: archived child {url} has no evaluated SHA; leaving its check unchanged.", file=sys.stderr)
                continue
            options = {"head_sha": sha, "defer_if_head_moved": True}
        try:
            result = updater.post_status_for_pr(
                url,
                cli_status,
                description=description,
                **options,
            )
        except (ValueError, RuntimeError, GhCommandError) as exc:
            errors.append((url, str(exc)))
            if not continue_on_error:
                raise GapPrMonitorError(
                    f"Failed to post Early Gate child status for {url}: {exc}"
                ) from exc
            continue
        if not result.skipped and not getattr(result, "deferred", False):
            posted.append(url)
    if errors and not posted:
        lines = [f"  - {u}: {e}" for u, e in errors]
        raise GapPrMonitorError(
            "Failed to post Early Gate status on child PRs:\n" + "\n".join(lines)
        )
    if errors:
        for url, err in errors:
            print(f"WARNING: child EG status failed for {url}: {err}", file=sys.stderr)
    return tuple(posted)


def run_early_gate_results(
    *,
    repo_root: Path,
    leader_pr_url: str,
    state_file: str | None = None,
    trigger_id: str | None = None,
    labels: Sequence[str] | None = None,
    publish: bool = False,
    check_name: str = DEFAULT_CHECK_NAME,
    dry_run: bool = False,
    remote: str = "origin",
    head_ref: str | None = None,
    trusted_authors: frozenset[str] | None = None,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    git_runner: Callable[..., str] | None = None,
    updater_factory: Callable[..., PRStatusUpdater] | None = None,
) -> EarlyGateResultsResult:
    """Apply EG comment outcome to state and optionally publish + green checks."""
    root = repo_root.expanduser().resolve()
    leader_url = (leader_pr_url or "").strip()
    if not leader_url:
        raise GapPrMonitorError("Leader PR URL is required.")

    # None = not provided (may fetch); explicit [] skips fetch (unit tests / CLI).
    if labels is not None:
        resolved_labels: list[str] | None = list(labels)
    elif not (trigger_id or "").strip() and not (state_file or "").strip():
        resolved_labels = fetch_leader_labels(leader_url, gh_runner=gh_runner)
    else:
        resolved_labels = None

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
    if publish and not dry_run:
        state_path = gpm.resume_unsuccessful_leader(root, state_path, git_runner=git_runner)
    state_path = gpm.resolve_finalized_state(root, state_path, git_runner=git_runner)
    payload = load_state(state_path)
    retired = retired_outcome(payload)
    if retired:
        return EarlyGateResultsResult(
            state_path, retired, retired, False, False, None, dry_run,
            reason="Leader retired; ignoring late Early Gate results",
        )
    archived = history_state_outcome(state_path)
    if archived:
        # Do not reinterpret late comments or reopen the run. A publish retry
        # can finish the original merge/status side effects from frozen state.
        if not publish or dry_run:
            return EarlyGateResultsResult(
                state_path, archived, archived, False, False, None, dry_run,
                reason="Leader archived; preserving its recorded Early Gate result",
            )
        published = publish_state(root, state_path, branch, remote=remote, git_runner=git_runner, archive=True)
        updater = (updater_factory or PRStatusUpdater)(check_name=check_name, dry_run=False)
        verify_leader_head(leader_url, published.sha, updater=updater)
        leader_result = post_leader_conclusion(
            leader_url, published.sha, overall_status=archived, updater=updater, gh_runner=gh_runner,
        )
        children = ()
        if early_gate_concluded(payload):
            children = post_child_statuses_for_early_gate(
                payload, overall_status=archived, updater=updater, pinned_only=True,
            )
        return EarlyGateResultsResult(
            state_path, archived, archived, False, not leader_result.skipped,
            published.sha, False, reason="Resumed archived Leader finalization",
            child_urls_posted=children,
        )
    ensure_promotion_gates(payload)
    migrate_legacy_overall_status(payload)
    previous = derive_legacy_overall_status(payload)
    eg_already = early_gate_concluded(payload)
    leader_commit = fetch_leader_head_sha(leader_url, gh_runner=gh_runner)
    trigger_commit = str(
        payload.get(gpm.EARLY_GATE_TRIGGERED_COMMIT_KEY) or ""
    ).strip()
    trigger_batch = payload.get(gpm.EARLY_GATE_TRIGGERED_BATCH_KEY)
    current_batch = gpm.child_batch_identity(payload)
    # The receipt commit may be newer than the commit that received /early-gate,
    # but it is evidence only for the exact child batch that was delivered.
    # A stale pending receipt must fall back to the current Leader commit.
    evidence_commit = (
        trigger_commit
        if (
            trigger_batch == current_batch
            and re.fullmatch(r"[0-9a-f]{40}", trigger_commit)
        )
        else leader_commit
    )
    if publish and not dry_run and (root / ".git").exists():
        run_git = git_runner or (lambda args, cwd=root: gpm._run_git(args, cwd=cwd))
        checked_out = run_git(["rev-parse", "HEAD"], cwd=root).strip()
        if checked_out != leader_commit:
            raise GapPrMonitorError(
                "Leader head moved while loading Early Gate results; rerun for the current commit."
            )

    # Always reconcile full comment + label history (wake-up may be stale).
    comments = list_leader_pr_comments(leader_url, gh_runner=gh_runner)
    if resolved_labels is not None:
        leader_labels = list(resolved_labels)
    else:
        leader_labels = fetch_leader_labels(leader_url, gh_runner=gh_runner)
    new_overall, reason, gate_snap = decide_overall_from_comments(
        comments=comments,
        trusted_authors=trusted_authors,
        labels=leader_labels,
        payload=payload,
        eg_already_concluded=eg_already,
        leader_commit=evidence_commit,
    )
    strip_legacy_overall_status(payload)
    derived = derive_legacy_overall_status(payload)

    if new_overall is None:
        # Persist partial promotion-gates progress (for example, a child SHA
        # changed while EG was running). A local save is insufficient when the
        # merge-orchestrator is running in a fresh checkout: the next monitor
        # cycle would lose the pending transition and may reconsider stale EG
        # results. Publish the pending state, but never post a merge/conclusion.
        published_sha = None
        if not dry_run:
            save_state(state_path, payload)
            # Only publish a pending state when Stage 1 itself is pending.
            # That is the child-SHA invalidation case. During a normal EG test
            # wait, component-builds is still successful; publishing a
            # bookkeeping commit there would advance the Leader SHA and make
            # the in-flight test result look stale.
            if publish and get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_PENDING:
                assert branch is not None
                published = publish_state(
                    root,
                    state_path,
                    branch,
                    remote=remote,
                    dry_run=False,
                    commit_message="Record GAP pending state after Early Gate invalidation",
                    rebase_on_conflict=False,
                    git_runner=git_runner,
                    archive=False,
                )
                state_path = published.state_path or state_path
                published_sha = published.sha
        return EarlyGateResultsResult(
            state_path=state_path,
            previous_overall=previous,
            overall_status=derived,
            applied=False,
            leader_status_posted=False,
            published_sha=published_sha,
            dry_run=dry_run,
            reason=reason,
            eg_build_status=gate_snap.get(GATE_EARLY_GATE_BUILD),
            eg_test_status=gate_snap.get(GATE_EARLY_GATE_TEST),
        )

    child_targets = child_pass_pr_urls(payload)
    child_check_state, child_desc = child_check_for_overall(new_overall)
    # The label belongs to the trigger batch only when the authoritative
    # Leader state records both the same semantic child batch and the commit
    # that received the trigger. Monitor-only/stale checkouts must not clear a
    # label that a newer Early Gate run may have set.
    trigger_commit = payload.get(gpm.EARLY_GATE_TRIGGERED_COMMIT_KEY)
    label_owned = (
        publish
        and not dry_run
        and isinstance(trigger_batch, str)
        and trigger_batch == current_batch
        and isinstance(trigger_commit, str)
        and trigger_commit == evidence_commit
    )
    if label_owned:
        try:
            gpm.set_early_gate_build_in_progress_label(
                leader_url, active=False, dry_run=False, gh_runner=gh_runner
            )
        except (GhCommandError, ValueError, RuntimeError) as exc:
            print(f"WARNING: failed to remove Early Gate in-progress label: {exc}", file=sys.stderr)

    if dry_run:
        print(
            f"[dry-run] would conclude EG as {new_overall!r} on {state_path} "
            f"({reason})"
        )
        print(
            f"[dry-run] would set promotion-gates "
            f"component-builds={get_gate_status(payload, GATE_COMPONENT_BUILDS)!r} "
            f"build={gate_snap.get(GATE_EARLY_GATE_BUILD)!r} "
            f"test={gate_snap.get(GATE_EARLY_GATE_TEST)!r}"
        )
        print(
            f"[dry-run] would post child check state={child_check_state!r} "
            f"({child_desc}) on {len(child_targets)} build-passing PR(s)"
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
            child_urls_posted=tuple(child_targets),
            child_check_state=child_check_state,
            eg_build_status=gate_snap.get(GATE_EARLY_GATE_BUILD),
            eg_test_status=gate_snap.get(GATE_EARLY_GATE_TEST),
        )

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
            child_urls_posted=tuple(child_targets),
            child_check_state=child_check_state,
            eg_build_status=gate_snap.get(GATE_EARLY_GATE_BUILD),
            eg_test_status=gate_snap.get(GATE_EARLY_GATE_TEST),
        )

    assert branch is not None
    published = publish_state(
        root,
        state_path,
        branch,
        remote=remote,
        dry_run=False,
        commit_message=(
            f"Update GAP promotion-gates from merge-orchestrator ({new_overall})"
        ),
        rebase_on_conflict=False,
        git_runner=git_runner,
        archive=new_overall == OVERALL_STATUS_SUCCESS,
    )

    state_path = published.state_path or state_path

    factory = updater_factory or PRStatusUpdater
    updater = factory(check_name=check_name, dry_run=False)
    verify_leader_head(leader_url, published.sha, updater=updater)
    leader_result = None
    if new_overall == OVERALL_STATUS_SUCCESS:
        leader_result = post_leader_conclusion(
            leader_url, published.sha, overall_status=new_overall,
            updater=updater, gh_runner=gh_runner,
        )
    # Reload after publish so child list matches committed state.
    payload = load_state(state_path)
    child_posted = post_child_statuses_for_early_gate(
        payload,
        overall_status=new_overall,
        updater=updater,
        # The first publication must honor the same frozen receipt as retries.
        pinned_only=True,
    )
    return EarlyGateResultsResult(
        state_path=state_path,
        previous_overall=previous,
        overall_status=new_overall,
        applied=True,
        leader_status_posted=bool(leader_result and not leader_result.skipped),
        published_sha=published.sha,
        dry_run=False,
        reason=reason,
        child_urls_posted=child_posted,
        child_check_state=child_check_state,
        eg_build_status=gate_snap.get(GATE_EARLY_GATE_BUILD),
        eg_test_status=gate_snap.get(GATE_EARLY_GATE_TEST),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply Early Gate Leader comment results to overall-status "
            "and optionally publish + green the Leader and build-passing children."
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
        "--publish-state",
        action="store_true",
        help=(
            "Commit/push state.json, green the Leader, and post success/failure "
            "on build-passing child PRs on EG success|failure."
        ),
    )
    parser.add_argument(
        "--check-name",
        default=DEFAULT_CHECK_NAME,
        help="GitHub commit status context for Leader and child checks.",
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
    parser.add_argument(
        "--trusted-authors",
        default="",
        help=(
            "Comma-separated GitHub logins allowed to publish EG results "
            f"(default rhods-ci-bot; or set {ENV_TRUSTED_AUTHORS})."
        ),
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

    trusted = trusted_early_gate_authors(args.trusted_authors or None)

    try:
        result = run_early_gate_results(
            repo_root=Path(args.repo_root),
            leader_pr_url=leader_pr_url,
            state_file=state_file,
            trigger_id=trigger_id,
            labels=labels,
            publish=bool(args.publish_state),
            check_name=args.check_name,
            dry_run=bool(args.dry_run),
            remote=args.remote,
            head_ref=(args.head_ref or "").strip() or None,
            trusted_authors=trusted,
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
    print(f"child_check_state: {result.child_check_state}")
    print(f"child_urls_posted: {list(result.child_urls_posted)}")
    print(f"eg_build_status: {result.eg_build_status}")
    print(f"eg_test_status: {result.eg_test_status}")
    print(f"dry_run: {result.dry_run}")
    print(f"reason: {result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
