#!/usr/bin/env python3
"""GAP child monitoring, promotion-gate publication and Leader reporting.

Resolve the Leader/state snapshot, evaluate child component builds and
main-to-release feasibility, and publish accepted current-head decisions.
Deferred/head-moved children cannot contribute stale evidence to the optional
notification hook. When state publication is requested, invoke that hook after
publication and before the best-effort Leader failure summary, passing fresh
validated tracking links separately from bounded notification result counts.

Promotion gates, not legacy overall-status, are authoritative. Good children
remain pending while siblings or Early Gate are outstanding; completed build
batches can signal /early-gate. EG gates currently have required: false: under
that policy EG conclusion, not a successful EG outcome, releases children (an
EG failure can still green them once concluded). Requiring EG success needs a
policy change. Notification delivery and cosmetic reporting failures do not
decide promotion outcomes.

The CLI can perform GitHub/Git/status/comment operations outside dry-run mode;
notification writes additionally require their own explicit opt-in. Neither a
successful Git publication nor a notification receipt makes these operations
transactional. State lives under GAP Leaders/<date>_<trigger_id>/state.json;
Leader selection, rollover guards and accepted-decision checks remain here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
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

from lib.gap_promotion_gates import (
    GATE_COMPONENT_BUILDS,
    GATE_EARLY_GATE_BUILD,
    GATE_EARLY_GATE_TEST,
    GATE_STATUS_PENDING,
    GATE_STATUS_SUCCESS,
    apply_component_builds_from_children,
    early_gate_concluded,
    children_ready_for_success_status,
    derive_legacy_overall_status,
    ensure_promotion_gates,
    get_gate_status,
    migrate_legacy_overall_status,
    should_trigger_early_gate,
    set_gate_status,
    strip_legacy_overall_status,
    waiting_for_early_gate,
)
from lib.gap_check_selection import (
    FeasibilityRun,
    TriggerParseError,
    default_build_producer,
    fetch_file_collections,
    fetch_feasibility_run,
    fetch_tekton_documents,
)
from lib.leader_lifecycle import retire_leader, retired_outcome, validate_replacement
from lib.konflux_builds import build_status_source
from lib.konflux_gate import evaluate_konflux_gate
from lib.gap_build_retries import BuildRetryManager, retries_enabled
from lib.gap_component_map import fetch_component_repo_map, repository_components
from lib.state_file import child_entry_ignored
from lib.gap_pr_gates import (
    PR_STATUS_BUILD_FAILURE,
    PR_STATUS_BUILD_PENDING,
    PR_STATUS_MERGE_FAILURE,
    PR_STATUS_SUCCESS,
    RepositoryGateDecision,
    RepositoryPullRequest,
    decide_repository_pr,
    expected_builds_for_pr,
)
from lib.github_check_runs import CheckQueryError, github_api_json, list_check_runs
from lib.pr_status_updater import (
    GhCommandError,
    PRStatusUpdater,
    PullRequestRef,
    StatusUpdateResult,
    parse_pr_url,
)
from lib.leader_history import (
    HISTORY_FOLDERS, canonical_state_path, history_state_outcome, is_history_state, state_outcome,
    prepare_leader_history_merge, resolve_existing_state_path, write_history_snapshot,
)
from lib.state_file import (
    OVERALL_STATUS_EARLY_GATE_TRIGGERED,
    OVERALL_STATUS_FAILURE,
    OVERALL_STATUS_SUCCESS,
    OVERALL_STATUSES,
)

DEFAULT_CHECK_NAME = "gated artifacts promoter"
DEFAULT_LEADER_LABEL = "gated-artifacts-promoter"
# workflow_dispatch may pass this instead of a concrete Leader PR URL.
LATEST_LEADER_PR_SENTINEL = "LATEST_LEADER_PR"
SUCCESS_PR_STATUS = PR_STATUS_SUCCESS
MERGE_FAILURE_PR_STATUS = PR_STATUS_MERGE_FAILURE
BUILD_PENDING_PR_STATUS = PR_STATUS_BUILD_PENDING
BUILD_FAILURE_PR_STATUS = PR_STATUS_BUILD_FAILURE
# Allowed in state_file; treat as a passing terminal if already present.
BUILD_SUCCESS_PR_STATUS = "build-success"
# Child statuses that conclude a repository PR. build-pending and new stay open.
STAGE1_TERMINAL_PR_STATUSES = frozenset(
    {
        SUCCESS_PR_STATUS,
        BUILD_SUCCESS_PR_STATUS,
        MERGE_FAILURE_PR_STATUS,
        BUILD_FAILURE_PR_STATUS,
    }
)
# Child passed its repo gate (we still write/post ``success`` when we evaluate pass).
CHILD_PASS_PR_STATUSES = frozenset({SUCCESS_PR_STATUS, BUILD_SUCCESS_PR_STATUS})
# RHAI-8261: hold green on good children until every sibling has concluded.
DEFERRED_CHILD_GREEN_DESCRIPTION = (
    "GAP: waiting for other component PRs to conclude (RHAI-8261)"
)
# A concluded failed batch remains open only for retry or replacement.
BATCH_FAILURE_CHILD_PENDING_DESCRIPTION = (
    "GAP: batch failed; merge held until a retry recovers it or a newer Leader replaces it"
)
# Hold child green after all-success until Early Gate concludes (EG status note).
WAITING_FOR_EARLY_GATE_DESCRIPTION = (
    "GAP: waiting for Early Gate (auto-merge held)"
)
EARLY_GATE_COMMENT_BODY = "/early-gate"
EARLY_GATE_COMMENT_RE = re.compile(r"^/early-gate(-build)?\s*$")
# overall-status values that allow Leader green (final).
FINAL_OVERALL_STATUSES = frozenset(
    {OVERALL_STATUS_SUCCESS, OVERALL_STATUS_FAILURE}
)
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
    early_gate_comment_posted: bool = False
    published_sha: str | None = None
    build_failure_urls: list[str] = field(default_factory=list)
    pending_urls: list[str] = field(default_factory=list)
    reports: tuple[str, ...] = ()
    child_head_changed: bool = False
    notification_urls: tuple[str, ...] = ()
    notification_result: dict[str, Any] | None = None


@dataclass(frozen=True)
class PublishResult:
    """Outcome of committing/pushing Leader state.json (RHOAIENG-97052)."""

    sha: str
    committed: bool
    dry_run: bool = False
    state_path: Path | None = None


@dataclass(frozen=True)
class OpenLeaderPr:
    """One open Leader PR discovered for scheduled monitor runs."""

    number: int
    url: str
    head_ref: str
    labels: tuple[str, ...]
    base_ref: str = ""


def state_path_for_trigger(trigger_id: str, *, root: Path | None = None) -> Path:
    """Return active or archived state.json for a trigger id.

    Prefer ``GAP Leaders/<YYYY-MM-DD>_<trigger_id>/state.json``.
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
    from lib.state_file import find_existing_state_path
    try:
        found = find_existing_state_path(base, tid)
    except ValueError as exc:
        raise GapPrMonitorError(str(exc)) from exc
    if found is None:
        raise GapPrMonitorError(f"State file not found for {tid!r} under {base / GAP_LEADERS_DIR}.")
    return (base / found).resolve()


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
        return resolve_existing_state_path(base, Path(state_file))

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


def record_child_head_shas(
    payload: dict[str, Any], decisions: Sequence[RepositoryGateDecision]
) -> bool:
    entries = payload.get("pull-requests")
    if not isinstance(entries, list):
        return False
    by_url = {
        str(entry.get("pr-url") or ""): entry
        for entry in entries
        if isinstance(entry, dict)
    }
    changed = False
    for decision in decisions:
        entry = by_url.get(decision.url)
        if entry is None:
            continue
        previous = str(entry.get("head-sha") or "")
        # Always record the live head; only non-ignored moves reset EG gates.
        if (
            previous
            and previous != decision.head_sha
            and not child_entry_ignored(entry)
        ):
            changed = True
        entry["head-sha"] = decision.head_sha
    if changed:
        ensure_promotion_gates(payload)
        for gate_id in (
            GATE_COMPONENT_BUILDS,
            GATE_EARLY_GATE_BUILD,
            GATE_EARLY_GATE_TEST,
        ):
            set_gate_status(payload, gate_id, GATE_STATUS_PENDING)
        strip_legacy_overall_status(payload)
    return changed


def apply_success_statuses(payload: dict[str, Any], urls: Sequence[str]) -> None:
    """Back-compat helper used by older tests."""
    apply_pr_statuses(payload, {u: SUCCESS_PR_STATUS for u in urls})


def compute_overall_status(payload: dict[str, Any]) -> str | None:
    """Derive a legacy overall string from ``promotion-gates`` (not persisted).

    Applies ``component-builds`` from children first when gates are missing/stale.
    Prefer reading ``promotion-gates`` directly; this exists for MonitorResult /
    older call sites.
    """
    ensure_promotion_gates(payload)
    migrate_legacy_overall_status(payload)
    apply_component_builds_from_children(payload)
    # Preserve EG-final conclusions already recorded on gates.
    return derive_legacy_overall_status(payload)


def apply_overall_status(payload: dict[str, Any], overall_status: str | None) -> None:
    """Legacy helper: map overall into gates when set; always strip overall-status.

    New code should call ``apply_component_builds_from_children`` /
    ``set_gate_status`` instead. Kept so older tests that call this still
    leave state consistent with the gates SoT.
    """
    from lib.gap_promotion_gates import (
        GATE_COMPONENT_BUILDS,
        GATE_EARLY_GATE_BUILD,
        GATE_EARLY_GATE_TEST,
        GATE_STATUS_FAILURE,
        GATE_STATUS_SUCCESS,
        set_gate_status,
    )

    ensure_promotion_gates(payload)
    if overall_status is None:
        strip_legacy_overall_status(payload)
        return
    if overall_status not in OVERALL_STATUSES:
        raise GapPrMonitorError(
            f"Invalid overall-status {overall_status!r}; "
            f"expected one of {sorted(OVERALL_STATUSES)}."
        )
    if overall_status == OVERALL_STATUS_EARLY_GATE_TRIGGERED:
        set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
    elif overall_status == OVERALL_STATUS_SUCCESS:
        set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
        set_gate_status(payload, GATE_EARLY_GATE_BUILD, GATE_STATUS_SUCCESS)
        set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_SUCCESS)
    elif overall_status == OVERALL_STATUS_FAILURE:
        set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_FAILURE)
    strip_legacy_overall_status(payload)


def children_batch_concluded(pr_statuses: Sequence[str]) -> bool:
    """True when every child has a terminal gate status (RHAI-8261)."""
    if not pr_statuses:
        return False
    return all(status in STAGE1_TERMINAL_PR_STATUSES for status in pr_statuses)


def non_ignored_child_statuses(
    payload: dict[str, Any],
    status_overrides: dict[str, str] | None = None,
) -> list[str]:
    """Child ``pr-status`` values that still participate in Stage-1 gating."""
    overrides = status_overrides or {}
    statuses: list[str] = []
    for entry in payload.get("pull-requests") or []:
        if not isinstance(entry, dict) or child_entry_ignored(entry):
            continue
        url = str(entry.get("pr-url") or "")
        if url in overrides:
            statuses.append(str(overrides[url]).strip())
        else:
            statuses.append(str(entry.get("pr-status") or "").strip())
    return statuses


def should_defer_child_success_status(
    *,
    pr_status: str,
    batch_concluded: bool,
    overall_status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Defer GitHub green on a good child until it is safe to auto-merge.

    - Batch still open → pending (RHAI-8261).
    - Builds success but EG not concluded → pending (final green carries EG note).
    - ``payload`` preferred; ``overall_status`` is legacy fallback only.
    """
    if pr_status not in CHILD_PASS_PR_STATUSES:
        return False
    if payload is not None:
        return not children_ready_for_success_status(payload)
    if not batch_concluded:
        return True
    return overall_status != OVERALL_STATUS_SUCCESS


def child_success_defer_description(
    *,
    batch_concluded: bool,
    overall_status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Human-readable pending description while child success is deferred."""
    if payload is not None and waiting_for_early_gate(payload):
        return WAITING_FOR_EARLY_GATE_DESCRIPTION
    if batch_concluded and overall_status == OVERALL_STATUS_EARLY_GATE_TRIGGERED:
        return WAITING_FOR_EARLY_GATE_DESCRIPTION
    if batch_concluded and overall_status != OVERALL_STATUS_SUCCESS:
        return BATCH_FAILURE_CHILD_PENDING_DESCRIPTION
    return DEFERRED_CHILD_GREEN_DESCRIPTION


def status_decision_for_post(
    decision: RepositoryGateDecision | PrDecision,
    *,
    batch_concluded: bool,
    overall_status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> RepositoryGateDecision | PrDecision:
    """Return the decision to POST; may downgrade success→pending.

    ``pr-status`` in state.json still records the evaluated outcome; only the
    GitHub commit status is deferred until siblings conclude and (on all-success)
    Early Gate has concluded so the green can include an EG note.
    """
    if getattr(decision, "skip_status_post", False):
        return decision
    if not should_defer_child_success_status(
        pr_status=decision.pr_status,
        batch_concluded=batch_concluded,
        overall_status=overall_status,
        payload=payload,
    ):
        return decision
    return replace(
        decision,
        cli_status="pending",
        description=child_success_defer_description(
            batch_concluded=batch_concluded,
            overall_status=overall_status,
            payload=payload,
        ),
    )


def provisional_overall_status(
    payload: dict[str, Any],
    status_updates: dict[str, str],
) -> str | None:
    """Derive legacy overall from a probe with ``status_updates`` applied."""
    probe = json.loads(json.dumps(payload))
    apply_pr_statuses(probe, status_updates)
    return compute_overall_status(probe)


def provisional_payload_with_updates(
    payload: dict[str, Any],
    status_updates: dict[str, str],
) -> dict[str, Any]:
    """Copy payload, apply PR statuses + component-builds gate (for defer)."""
    probe = json.loads(json.dumps(payload))
    ensure_promotion_gates(probe)
    migrate_legacy_overall_status(probe)
    apply_pr_statuses(probe, status_updates)
    apply_component_builds_from_children(probe)
    return probe


def early_gate_comment_already_present(
    leader_pr_url: str,
    *,
    leader_commit: str | None = None,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> bool:
    """True when the current Leader commit already has an Early Gate run."""
    run = gh_runner or _run_gh
    ref = parse_pr_url(leader_pr_url)
    if leader_commit:
        endpoint = (
            f"repos/{ref.owner}/{ref.repo}/commits/{leader_commit}/check-runs"
            "?per_page=100"
        )
        raw = run(
            [
                "api",
                endpoint,
                "--jq",
                '[.check_runs[]? | select(.app.slug == "konflux-internal-p02" '
                'and (.name | split(" / ")[-1] == "early-gate-ci-build")) | .id]',
            ]
        )
        try:
            matches = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise GapPrMonitorError(
                f"Invalid Early Gate check-run response for {leader_commit}: {exc}"
            ) from exc
        if not isinstance(matches, list):
            raise GapPrMonitorError(
                f"Unexpected Early Gate check-run response for {leader_commit}."
            )
        return bool(matches)
    endpoint = f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments"
    try:
        raw = run(["api", endpoint, "--paginate"])
    except GhCommandError:
        return False
    text = (raw or "").strip()
    if not text:
        return False
    try:
        comments = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(comments, list):
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "").strip()
        match = EARLY_GATE_COMMENT_RE.match(body)
        if not match:
            continue
        return True
    return False


def post_early_gate_trigger_comment(
    leader_pr_url: str,
    *,
    dry_run: bool = False,
    allow_repeat: bool = False,
    leader_commit: str | None = None,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> bool:
    """Comment ``/early-gate`` on the Leader once (RHAI-10942).

    Returns True when a comment was posted (or would be, in dry-run).
    Idempotent: skips if a matching comment already exists.
    """
    run = gh_runner or _run_gh
    body = EARLY_GATE_COMMENT_BODY
    if not allow_repeat and early_gate_comment_already_present(
        leader_pr_url, leader_commit=leader_commit, gh_runner=run
    ):
        return False
    if dry_run:
        print(
            f"[dry-run] would comment {body!r} on {leader_pr_url}"
        )
        return True
    ref = parse_pr_url(leader_pr_url)
    run(
        [
            "pr",
            "comment",
            str(ref.number),
            "-R",
            f"{ref.owner}/{ref.repo}",
            "--body",
            body,
        ]
    )
    return True


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

    # Evaluate batch + required promotion-gates before posting (8261).
    batch_concluded = children_batch_concluded(
        non_ignored_child_statuses(payload, status_updates)
    )
    ensure_promotion_gates(payload)
    migrate_legacy_overall_status(payload)
    probe = provisional_payload_with_updates(payload, status_updates)
    overall_status = derive_legacy_overall_status(probe)

    for decision in decisions:
        if decision.skip_status_post:
            skipped.append(decision.url)
            continue

        to_post = status_decision_for_post(
            decision,
            batch_concluded=batch_concluded,
            overall_status=overall_status,
            payload=probe,
        )
        desc = description if description is not None else to_post.description
        try:
            result: StatusUpdateResult = updater.post_status_for_pr(
                decision.url,
                to_post.cli_status,
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
                    apply_component_builds_from_children(payload)
                    strip_legacy_overall_status(payload)
                    save_state(path, payload)
                raise GapPrMonitorError(
                    f"Failed to post status for {decision.url}: {exc}"
                ) from exc

    if classify_errors or post_errors:
        if status_updates and not dry_run:
            apply_pr_statuses(payload, status_updates)
            apply_component_builds_from_children(payload)
            strip_legacy_overall_status(payload)
            save_state(path, payload)
        details = classify_errors + post_errors
        lines = [f"  - {u}: {e}" for u, e in details]
        raise GapPrMonitorError(
            f"{len(details)} PR(s) failed during monitor.\n" + "\n".join(lines)
        )

    apply_pr_statuses(payload, status_updates)
    apply_component_builds_from_children(payload)
    strip_legacy_overall_status(payload)
    overall_status = derive_legacy_overall_status(payload)

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


def _is_push_rejection(message: str) -> bool:
    """True when ``git push`` failed because the remote branch moved."""
    lower = message.lower()
    return (
        "rejected" in lower
        or "non-fast-forward" in lower
        or "fetch first" in lower
    )


def _push_head_with_rebase(
    repo_root: Path,
    head_ref: str,
    *,
    remote: str = "origin",
    git_runner: Callable[..., str],
    max_attempts: int = 3,
) -> None:
    """Push ``HEAD`` to ``head_ref``, rebasing onto the remote tip between attempts."""
    refspec = f"HEAD:{head_ref}"
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            git_runner(["fetch", remote, head_ref], cwd=repo_root)
        except GapPrMonitorError as exc:
            raise GapPrMonitorError(
                f"Could not fetch {remote}/{head_ref} before state publish: {exc}"
            ) from exc
        try:
            git_runner(["push", remote, refspec], cwd=repo_root)
            return
        except GapPrMonitorError as exc:
            last_error = str(exc)
            if attempt >= max_attempts or not _is_push_rejection(last_error):
                raise
            try:
                git_runner(
                    ["pull", "--rebase", remote, head_ref],
                    cwd=repo_root,
                )
            except GapPrMonitorError as rebase_exc:
                raise GapPrMonitorError(
                    f"State publish push was rejected and rebase failed "
                    f"({last_error}): {rebase_exc}"
                ) from rebase_exc
    raise GapPrMonitorError(last_error or "State publish push failed.")


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
            "number,url,headRefName,labels,baseRefName",
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
                base_ref=str(row.get("baseRefName") or ""),
            )
        )
    return leaders


def require_clean_leader_checkout(repo_root: Path, *, git_runner) -> None:
    """Do not carry another Leader's uncommitted files into a state publication."""
    if git_runner(["status", "--porcelain", "--untracked-files=no"], cwd=repo_root).strip():
        raise GapPrMonitorError(
            "Leader checkout has uncommitted tracked changes; refusing to switch or retire it"
        )


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
    require_clean_leader_checkout(repo_root, git_runner=run)
    run(["fetch", remote, f"+refs/heads/{ref}:refs/remotes/{remote}/{ref}"], cwd=repo_root)
    run(["checkout", "-B", ref, f"{remote}/{ref}"], cwd=repo_root)


def resume_unsuccessful_leader(repo_root, state_path, *, git_runner=None):
    """Resume a legacy failed archive that has no verified retirement receipt."""
    path = resolve_existing_state_path(repo_root, state_path)
    if not is_history_state(path) or history_state_outcome(path) != "failure":
        return path
    payload = load_state(path)
    if retired_outcome(payload):
        return path
    run = git_runner or (lambda args, cwd: _run_git(args, cwd=cwd))
    relative = state_path_for_output(path, repo_root=repo_root)
    if not run(["ls-files", "--", relative], cwd=repo_root).strip():
        raise GapPrMonitorError("Refusing to resume an untracked history snapshot")
    active = canonical_state_path(repo_root, path)
    if active.exists() and load_state(active) != payload:
        raise GapPrMonitorError("Active and archived Leader records disagree")
    active.parent.mkdir(parents=True, exist_ok=True)
    save_state(active, payload)
    return active


def resolve_finalized_state(
    repo_root: Path, state_path: Path, *, git_runner: Callable[..., str] | None = None,
) -> Path:
    """Recognize a tracked legacy conclusion before a scan can reset its gates.

    Only identical, terminal active/history records are migration candidates.
    A genuinely active retry may have stale history; conflicting terminal
    records instead require investigation without changing either copy.
    """
    path = resolve_existing_state_path(repo_root, state_path)
    if is_history_state(path) or os.environ.get("GAP_MOVE_FINAL_STATE", "").lower() != "true":
        return path
    payload = load_state(path)
    if state_outcome(payload) == "failure" and not retired_outcome(payload):
        return path  # Failure is retryable until a newer Leader replaces it.
    if retired_outcome(payload):
        return path  # Retirement may have just added metadata to the active record.
    folder = HISTORY_FOLDERS.get(state_outcome(payload))
    if folder is None:
        return path
    canonical = canonical_state_path(repo_root, path)
    snapshot = repo_root.resolve() / "GAP History" / folder / canonical.parent.name / "state.json"
    if not snapshot.is_file():
        return path
    run = git_runner or (lambda args, cwd: _run_git(args, cwd=cwd))
    relative = state_path_for_output(snapshot, repo_root=repo_root)
    if not run(["ls-files", "--", relative], cwd=repo_root).strip():
        raise GapPrMonitorError("Refusing to use an untracked history snapshot")
    history_state_outcome(snapshot)
    if load_state(snapshot) != payload:
        raise GapPrMonitorError("Active and archived Leader records disagree; refusing to change either")
    return snapshot


def publish_state(
    repo_root: Path,
    state_path: Path,
    head_ref: str,
    *,
    remote: str = "origin",
    dry_run: bool = False,
    commit_message: str = "Update GAP state from PR monitor (RHOAIENG-97052)",
    rebase_on_conflict: bool = True,
    archive: bool = False,
    git_runner: Callable[..., str] | None = None,
) -> PublishResult:
    """Commit and push state.json, returning the published commit SHA.

    When the state file is unchanged, no empty commit is created; the current
    ``HEAD`` SHA is returned so Leader signaling can still target it.
    """
    repo_root = repo_root.expanduser().resolve()
    run = git_runner or (lambda args, cwd=repo_root: _run_git(args, cwd=cwd))
    state_path = (resolve_existing_state_path(repo_root, state_path) if archive
                  else resolve_finalized_state(repo_root, state_path, git_runner=run))
    rel = state_path_for_output(state_path, repo_root=repo_root)
    archive = archive or bool(retired_outcome(load_state(state_path))) or is_history_state(state_path)
    if dry_run:
        sha = run(["rev-parse", "HEAD"], cwd=repo_root) or "dry-run-sha"
        print(f"[dry-run] would commit and push {rel} on {head_ref} (sha={sha})")
        return PublishResult(sha=sha, committed=True, dry_run=True, state_path=state_path)

    run(["config", "user.name", "github-actions[bot]"], cwd=repo_root)
    run(
        ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        cwd=repo_root,
    )
    # Activate together in the monitor and merge-orchestrator workflows. Older
    # pinned consumers cannot resolve a state file after it moves to history.
    exclusive = os.environ.get("GAP_MOVE_FINAL_STATE", "").lower() == "true" or is_history_state(state_path)
    if not exclusive:
        snapshot = write_history_snapshot(repo_root, state_path, move=False) if archive else None
        published_path = state_path
        paths = [rel]
        if snapshot is not None:
            paths.append(state_path_for_output(snapshot, repo_root=repo_root))
            if retired_outcome(load_state(state_path)):
                folder, *relative = snapshot.relative_to(repo_root / "GAP History").parts
                other = "unsuccessful" if folder == "successful" else "successful"
                obsolete = repo_root / "GAP History" / other / Path(*relative)
                if obsolete.exists():
                    old_rel = state_path_for_output(obsolete, repo_root=repo_root)
                    if not run(["ls-files", "--", old_rel], cwd=repo_root).strip():
                        raise GapPrMonitorError("Refusing to remove an untracked history snapshot")
                    obsolete.unlink()
                    paths.append(old_rel)
    else:
        canonical = canonical_state_path(repo_root, state_path)
        if is_history_state(state_path) and canonical.exists():
            canonical_rel = state_path_for_output(canonical, repo_root=repo_root)
            if not run(["ls-files", "--", canonical_rel], cwd=repo_root).strip():
                raise GapPrMonitorError("Refusing to remove an untracked active state")
        candidates = [repo_root / "GAP History" / folder / canonical.parent.name / "state.json"
                      for folder in ("successful", "unsuccessful")]
        # Check before touching any files. Legacy snapshots may exist while a run
        # is still active; finalization keeps exactly one, ordinary updates none.
        for candidate in candidates:
            if candidate.exists() and candidate != state_path:
                candidate_rel = state_path_for_output(candidate, repo_root=repo_root)
                if not run(["ls-files", "--", candidate_rel], cwd=repo_root).strip():
                    raise GapPrMonitorError("Refusing to remove an untracked history snapshot")
        snapshot = write_history_snapshot(repo_root, state_path) if archive else None
        if archive and snapshot is None:
            raise GapPrMonitorError("Cannot archive a non-terminal Leader")
        published_path = snapshot or state_path
        paths = [state_path_for_output(published_path, repo_root=repo_root)]
        canonical_rel = state_path_for_output(canonical, repo_root=repo_root)
        if snapshot and run(["ls-files", "--", canonical_rel], cwd=repo_root).strip():
            paths.append(canonical_rel)
        for candidate in candidates:
            if candidate != snapshot and candidate.exists():
                candidate.unlink()
                paths.append(state_path_for_output(candidate, repo_root=repo_root))
    run(["add", "--", *paths], cwd=repo_root)
    staged = run(["diff", "--cached", "--name-only"], cwd=repo_root)
    if set(staged.splitlines()) - set(paths):
        raise GapPrMonitorError("Refusing to publish staged changes outside this Leader record")
    committed = False
    if staged.strip():
        run(["commit", "-m", commit_message], cwd=repo_root)
        committed = True
        _push_head_with_rebase(
            repo_root,
            head_ref,
            remote=remote,
            git_runner=run,
            max_attempts=3 if rebase_on_conflict and not (archive and exclusive) else 1,
        )
        print(f"Pushed state update for Leader branch {head_ref}")
    else:
        print(f"No state file changes to commit for {head_ref}.")
    sha = run(["rev-parse", "HEAD"], cwd=repo_root)
    if not sha:
        raise GapPrMonitorError("Could not resolve published commit SHA after publish.")
    return PublishResult(sha=sha, committed=committed, dry_run=False, state_path=published_path)


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


def post_leader_conclusion(
    leader_pr_url: str,
    published_sha: str,
    *,
    overall_status: str,
    updater: PRStatusUpdater,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    replacement_verified: bool = False,
) -> StatusUpdateResult:
    """Authorize successful run history or verified replacement retirement."""
    if overall_status in {OVERALL_STATUS_FAILURE, "superseded"} and not replacement_verified:
        raise GapPrMonitorError("Unsuccessful Leader requires a verified newer replacement before merge")
    if overall_status == OVERALL_STATUS_SUCCESS:
        description = "GAP: overall success"
    elif overall_status == OVERALL_STATUS_FAILURE:
        description = "GAP: overall failure"
    elif overall_status == "superseded":
        description = "GAP: superseded run recorded; promotion not approved"
    else:
        raise GapPrMonitorError(
            f"Cannot post Leader conclusion for overall-status={overall_status!r}"
        )
    prepare_leader_history_merge(
        leader_pr_url, published_sha, overall_status=overall_status,
        dry_run=updater.dry_run, gh_runner=gh_runner,
    )
    return updater.post_status_for_pr(
        leader_pr_url,
        "completed",
        description=description,
        head_sha=published_sha,
    )


# Back-compat name used by older call sites/tests.
def post_leader_success(
    leader_pr_url: str,
    published_sha: str,
    *,
    updater: PRStatusUpdater,
    overall_status: str = OVERALL_STATUS_SUCCESS,
) -> StatusUpdateResult:
    return post_leader_conclusion(
        leader_pr_url,
        published_sha,
        overall_status=overall_status,
        updater=updater,
    )


def _post_leader_failure_summary(
    *,
    leader_pr_url: str,
    repo_root: Path,
    state_path: Path,
    head_ref: str,
    dry_run: bool,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    tracking: dict | None = None,
    finalized: bool = False,
) -> None:
    """Upsert the Leader failure/pending/closed summary comment (best-effort).

    Lazy-imports ``gap_failure_reporter`` to avoid an import cycle: that module
    reuses helpers from this file.
    """
    from scripts import gap_failure_reporter as gfr

    # State was just written on this checkout; pass the path explicitly and skip
    # a redundant branch checkout.
    summary = gfr.report_for_leader(
        leader_pr_url=leader_pr_url,
        repo_root=repo_root,
        head_ref=None,
        state_file=str(state_path),
        dry_run=dry_run,
        gh_runner=gh_runner,
        reuse_monitor_evidence=build_status_source() == "konflux",
        tracking=tracking,
        allow_archived=finalized,
    )
    prefix = "[dry-run] " if dry_run else ""
    action = "create" if summary.created else ("update" if summary.updated else "noop")
    print(
        f"{prefix}Leader failure summary: {action} "
        f"rows={len(summary.rows)} comment_id={summary.comment_id}"
    )


def retire_previous_leader(
    state_path: Path, *, repo_root: Path, head_ref: str | None,
    leader_pr_url: str, replacement_url: str | None,
    check_name: str, dry_run: bool, remote: str,
    updater_factory=None, gh_runner=None, git_runner=None,
) -> MonitorResult:
    """Archive a replaced batch, retrying only publication/merge for retired runs."""
    runner = gh_runner or _run_gh
    git = git_runner or (lambda args, cwd: _run_git(args, cwd=cwd))
    ref = parse_pr_url(leader_pr_url)
    old = json.loads(runner(["api", f"repos/{ref.slug}/pulls/{ref.number}"]))
    require_clean_leader_checkout(repo_root, git_runner=git)
    source_sha = git(["rev-parse", "HEAD"], cwd=repo_root)
    branch = head_ref or old.get("head", {}).get("ref")
    if (old.get("head", {}).get("sha") != source_sha
            or old.get("head", {}).get("ref") != branch
            or old.get("state") != "open" or old.get("merged")):
        raise GapPrMonitorError("Leader changed before rollover; reload its branch and retry")
    # Only this Leader record can be finalized or retried.
    trigger = str(branch).removeprefix("gap-leader/")
    labels = {item.get("name") for item in old.get("labels", []) if isinstance(item, dict)}
    if (old.get("draft") or not str(branch).startswith("gap-leader/gap-")
            or (old.get("head", {}).get("repo") or {}).get("full_name") != ref.slug
            or not {DEFAULT_LEADER_LABEL, trigger}.issubset(labels)):
        raise GapPrMonitorError("Refusing to retire a PR without its GAP Leader identity")
    relative = canonical_state_path(repo_root, state_path).relative_to(repo_root.resolve())
    if (len(relative.parts) != 3 or relative.parts[0] != GAP_LEADERS_DIR
            or relative.parts[2] != "state.json"
            or not relative.parts[1].endswith("_" + trigger)):
        raise GapPrMonitorError("Rollover state does not belong to this Leader branch")
    allowed = {
        relative.as_posix(),
        *(f"GAP History/{folder}/{relative.parts[1]}/state.json"
          for folder in ("successful", "unsuccessful")),
    }
    changed = runner(["pr", "diff", str(ref.number), "--repo", ref.slug, "--name-only"]).splitlines()
    if any(path not in allowed for path in changed):
        raise GapPrMonitorError(
            "Leader includes changes outside its run record; refusing automatic history merge"
        )
    payload = load_state(state_path)
    retired = retired_outcome(payload)
    outcome = retired or history_state_outcome(state_path)
    if retired is not None:
        expected = payload["leader-lifecycle"]["replacement-leader"]
        recorded_ref = parse_pr_url(expected)
        if recorded_ref.slug != ref.slug or recorded_ref.number <= ref.number:
            raise GapPrMonitorError("Invalid recorded replacement Leader identity")
    if outcome is None or (outcome != "success" and retired is None):
        if not replacement_url:
            raise GapPrMonitorError("Missing replacement Leader")
        replacement_ref = parse_pr_url(replacement_url)
        new = json.loads(runner(["api", f"repos/{replacement_ref.slug}/pulls/{replacement_ref.number}"]))
        validate_replacement(leader_pr_url, old, replacement_url, new)
        outcome = retire_leader(payload, replacement_url=replacement_url, source_sha=source_sha)
    if dry_run:
        print(f"[dry-run] would preserve {leader_pr_url} as {outcome}, without child statuses")
        return MonitorResult(state_path, [], [], [], [], True, overall_status=outcome)
    save_state(state_path, payload)
    published = publish_state(
        repo_root, state_path, branch, remote=remote, git_runner=git_runner,
        commit_message=f"Preserve replaced GAP Leader ({outcome})",
        # Never replay a retirement over a concurrently updated state.
        rebase_on_conflict=False,
        archive=True,
    )
    state_path = published.state_path or state_path
    factory = updater_factory or PRStatusUpdater
    updater = factory(check_name=check_name, dry_run=False)
    verify_leader_head(leader_pr_url, published.sha, updater=updater)
    posted = post_leader_conclusion(
        leader_pr_url, published.sha, overall_status=outcome,
        updater=updater, gh_runner=runner, replacement_verified=True,
    )
    if retired is None:
        if early_gate_concluded(payload) and not replacement_url:
            from scripts.gap_early_gate_results import post_child_statuses_for_early_gate
            post_child_statuses_for_early_gate(
                payload, overall_status=outcome, updater=updater, pinned_only=True,
            )
        try:
            _post_leader_failure_summary(
                leader_pr_url=leader_pr_url, repo_root=repo_root,
                state_path=state_path, head_ref=branch, dry_run=False,
                gh_runner=runner, finalized=True,
            )
        except Exception as exc:  # Best-effort, as on the first publication.
            print(f"WARNING: Leader failure summary retry failed: {exc}", file=sys.stderr)
    return MonitorResult(
        state_path, [], [], [], [], False, overall_status=outcome,
        leader_status_posted=not posted.skipped, published_sha=published.sha,
    )


def run_monitor_publish_and_signal(
    state_path: Path,
    *,
    repo_root: Path,
    head_ref: str | None = None,
    leader_pr_url: str | None = None,
    publish: bool = False,
    replacement_url: str | None = None,
    check_name: str = DEFAULT_CHECK_NAME,
    dry_run: bool = False,
    continue_on_error: bool = False,
    description: str | None = None,
    remote: str = "origin",
    updater_factory: Callable[..., PRStatusUpdater] | None = None,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    git_runner: Callable[..., str] | None = None,
    notification_config=None,
) -> MonitorResult:
    """Coordinator: monitor → publish state → EG comment and/or Leader green.

    Ordering (RHOAIENG-97052 / RHAI-8079 / RHAI-10942):
      evaluate children → write ``promotion-gates`` → commit/push →
      if ``component-builds`` success: comment ``/early-gate`` on the Leader once;
      if required builds failed: retain the active Leader for retry/replacement;
      always (when Leader URL known): upsert failure/pending/closed summary comment.
    """
    if publish and not dry_run and not replacement_url:
        state_path = resume_unsuccessful_leader(repo_root, state_path, git_runner=git_runner)
    state_path = resolve_finalized_state(repo_root, state_path, git_runner=git_runner)
    if publish and leader_pr_url and (
        replacement_url or is_history_state(state_path) or retired_outcome(load_state(state_path))
    ):
        return retire_previous_leader(
            state_path, repo_root=repo_root, head_ref=head_ref,
            leader_pr_url=leader_pr_url, replacement_url=replacement_url,
            check_name=check_name, dry_run=dry_run, remote=remote,
            updater_factory=updater_factory, gh_runner=gh_runner, git_runner=git_runner,
        )
    previous_payload = load_state(Path(state_path))
    ensure_promotion_gates(previous_payload)
    migrate_legacy_overall_status(previous_payload)
    result = run_monitor(
        state_path,
        check_name=check_name,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
        description=description,
        updater_factory=updater_factory,
        gh_runner=gh_runner,
    )

    if not publish:
        return result

    branch = (head_ref or "").strip() or current_git_branch(
        repo_root, git_runner=git_runner
    )
    finalizing = False  # Only replacement retirement may finalize a failed batch.
    published = publish_state(
        repo_root,
        result.state_path,
        branch,
        remote=remote,
        dry_run=dry_run,
        git_runner=git_runner,
        archive=finalizing,
    )

    result = replace(result, state_path=published.state_path or result.state_path)



    leader_url = (leader_pr_url or "").strip() or None
    leader_status_posted = False
    early_gate_comment_posted = False
    updated = list(result.updated_urls)
    skipped = list(result.skipped_urls)
    # Gate-driven: /early-gate only when all builds passed; failed Leaders stay
    # active until recovery or verified replacement (EG finishes successful Leaders).
    state_payload = load_state(result.state_path)
    ensure_promotion_gates(state_payload)
    migrate_legacy_overall_status(state_payload)
    # /early-gate only while builds passed and EG has not finished yet.
    early_gate = should_trigger_early_gate(
        state_payload
    ) and not early_gate_concluded(state_payload)
    # Keep MonitorResult.overall_status aligned with gates for callers/tests.
    result_overall = derive_legacy_overall_status(state_payload)

    if early_gate and leader_url:
        try:
            early_gate_comment_posted = post_early_gate_trigger_comment(
                leader_url,
                dry_run=dry_run,
                leader_commit=published.sha,
                gh_runner=gh_runner,
            )
        except (GhCommandError, ValueError, RuntimeError) as exc:
            raise GapPrMonitorError(
                f"Failed to post Early Gate trigger comment on {leader_url}: {exc}"
            ) from exc
    elif early_gate and not leader_url:
        print(
            f"WARNING: component-builds=success but no Leader PR URL "
            f"was provided (--leader-pr-url / {ENV_LEADER_PR_URL}); "
            "skipped Early Gate trigger comment.",
            file=sys.stderr,
        )

    notification_result = None
    notification_tracking = {"status": "not_checked"}
    if notification_config is not None and notification_config.mode != "disabled":
        try:
            from lib.gap_notifications.monitor_hook import notify_after_publish
            notification_result = notify_after_publish(
                load_state(result.state_path), accepted_urls=result.notification_urls,
                config=notification_config, leader_url=leader_pr_url,
                published_sha=published.sha, state_path=result.state_path,
                repo_root=repo_root, dry_run=dry_run, tracking=notification_tracking,
            )
        except Exception as exc:
            notification_tracking = {"status": "unavailable"}
            notification_result = {"status": "error", "error_type": type(exc).__name__}
            print("WARNING: GAP notification hook failed: " + type(exc).__name__, file=sys.stderr)


    # After state is published, refresh the single Leader failure/pending/closed
    # summary comment so the table matches this run's state.json.
    # Best-effort — never fail the gate run.
    if leader_url:
        try:
            _post_leader_failure_summary(
                leader_pr_url=leader_url,
                repo_root=repo_root,
                state_path=result.state_path,
                finalized=finalizing,
                head_ref=branch,
                dry_run=dry_run,
                gh_runner=gh_runner,
                tracking=notification_tracking,
            )
        except Exception as exc:  # noqa: BLE001 — summary must not fail the gate
            print(
                f"WARNING: Leader failure summary comment failed for {leader_url}: {exc}",
                file=sys.stderr,
            )

    return MonitorResult(
        state_path=result.state_path,
        updated_urls=updated,
        skipped_urls=skipped,
        merge_failure_urls=result.merge_failure_urls,
        success_urls=result.success_urls,
        dry_run=result.dry_run,
        overall_status=result_overall if result_overall is not None else result.overall_status,
        leader_status_posted=leader_status_posted,
        early_gate_comment_posted=early_gate_comment_posted,
        published_sha=published.sha,
        build_failure_urls=result.build_failure_urls,
        pending_urls=result.pending_urls,
        reports=result.reports,
        notification_urls=result.notification_urls,
        notification_result=notification_result,
    )


def latest_rollover_leaders_by_base(leader_repo, *, gh_runner=None):
    """Find the newest same-repository Leader for each target branch."""
    runner = gh_runner or _run_gh
    rows = json.loads(runner([
        "pr", "list", "--repo", leader_repo, "--state", "all",
        "--label", DEFAULT_LEADER_LABEL, "--limit", "100",
        "--json", "number,url,headRefName,baseRefName,labels,state,isDraft,isCrossRepository",
    ]))
    if not isinstance(rows, list):
        raise GapPrMonitorError("Invalid Leader inventory for daily rollover")
    newest = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        branch = str(row.get("headRefName") or "")
        base = row.get("baseRefName")
        raw_labels = row.get("labels")
        if not isinstance(raw_labels, list) or not isinstance(row.get("state"), str):
            continue
        labels = tuple(item["name"] for item in raw_labels
                       if isinstance(item, dict) and isinstance(item.get("name"), str))
        if (
            row.get("state") in {"OPEN", "MERGED"} and row.get("isDraft") is False
            and row.get("isCrossRepository") is False
            and isinstance(base, str) and base
            and DEFAULT_LEADER_LABEL in labels
            and type(row.get("number")) is int and row["number"] > 0
            and branch.startswith("gap-leader/gap-")
            and branch.removeprefix("gap-leader/") in labels
            and row.get("url") == f"https://github.com/{leader_repo}/pull/{row['number']}"
        ):
            candidate = OpenLeaderPr(row["number"], row["url"], branch, labels, base)
            if base not in newest or candidate.number > newest[base].number:
                newest[base] = candidate
    return newest


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
    notification_config=None,
) -> int:
    """Discover open Leaders and run gate evaluation, then publish-and-signal.

    Checkout stays in this script so the Actions workflow can stay a thin
    wrapper. Each Leader uses the same build and feasibility evaluation as a
    manual run, then publishes state and posts Leader success only after that
    commit (RHOAIENG-97050 / RHOAIENG-97052).
    """
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

    # Include a newer already-merged batch: a fast run must still retire older
    # pending Leaders. Closed, unmerged/abandoned PRs cannot be replacements.
    rollover = os.environ.get("GAP_RETIRE_PREVIOUS_LEADERS", "").lower() == "true"
    newest_by_base = {}
    if rollover:
        try:
            newest_by_base = latest_rollover_leaders_by_base(leader_repo, gh_runner=gh_runner)
        except (GapPrMonitorError, GhCommandError, ValueError) as exc:
            # Rollover is optional. A lookup outage must not prevent ordinary
            # build evaluation and reporting for every open Leader.
            print(f"WARNING: skipping daily rollover discovery: {exc}", file=sys.stderr)

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
            rollover_args = {}
            newest = newest_by_base.get(leader.base_ref)
            if newest and leader.number < newest.number:
                rollover_args["replacement_url"] = newest.url
            result = run_monitor_publish_and_signal(
                state_path,
                **rollover_args,
                **({"notification_config": notification_config} if notification_config is not None else {}),
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
                f"{len(result.success_urls) + len(result.merge_failure_urls) + len(result.build_failure_urls) + len(result.pending_urls)} "
                f"child PR(s) from {result.state_path}"
            )
            if result.published_sha:
                print(f"{prefix}published-sha: {result.published_sha}")
            if result.early_gate_comment_posted:
                print(f"{prefix}Posted Early Gate trigger comment (/early-gate)")
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


def is_latest_leader_pr_sentinel(value: str | None) -> bool:
    """True when the caller wants the newest open Leader PR discovered via gh."""
    return (value or "").strip().upper() == LATEST_LEADER_PR_SENTINEL


def resolve_leader_pr_url_input(
    leader_pr_url: str | None,
    *,
    leader_repo: str,
    label: str = DEFAULT_LEADER_LABEL,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> str | None:
    """Return a concrete Leader PR URL, resolving ``LATEST_LEADER_PR`` when set."""
    raw = (leader_pr_url or "").strip()
    if not raw:
        return None
    if not is_latest_leader_pr_sentinel(raw):
        return raw

    leaders = list_open_leader_prs(leader_repo, label=label, gh_runner=gh_runner)
    if not leaders:
        raise GapPrMonitorError(
            f"No open Leader PRs with label {label!r} in {leader_repo}; "
            f"cannot resolve {LATEST_LEADER_PR_SENTINEL}."
        )
    chosen = max(leaders, key=lambda leader: leader.number)
    if len(leaders) > 1:
        print(
            f"WARNING: {len(leaders)} open Leader PR(s); "
            f"{LATEST_LEADER_PR_SENTINEL} selected #{chosen.number} {chosen.url}",
            file=sys.stderr,
        )
    print(
        f"Resolved {LATEST_LEADER_PR_SENTINEL} to Leader "
        f"#{chosen.number} {chosen.url}"
    )
    return chosen.url


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


def fetch_repository_pull_request(
    pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    retry_unknown: bool = True,
) -> RepositoryPullRequest:
    """Load the PR revision, labels, and head committer timestamp."""
    runner = gh_runner or _run_gh
    pr = parse_pr_url(pr_url)
    data = _load_pr_view(pr_url, pr, runner)
    mergeable = data.get("mergeable")
    if retry_unknown and (mergeable is None or str(mergeable).upper() == "UNKNOWN"):
        time.sleep(1.5)
        data = _load_pr_view(pr_url, pr, runner)
        mergeable = data.get("mergeable")

    head_sha = str(data.get("headRefOid") or "").strip()
    base_ref = str(data.get("baseRefName") or "").strip()
    if not head_sha or not base_ref:
        raise GapPrMonitorError(f"Could not resolve head SHA or base branch for {pr_url}.")
    committer_at = _head_committer_at(pr.owner, pr.repo, head_sha, pr_url, runner)
    merge_state = data.get("mergeStateStatus")
    return RepositoryPullRequest(
        url=pr_url,
        owner=pr.owner,
        repo=pr.repo,
        number=pr.number,
        state=str(data.get("state") or "").upper(),
        mergeable=(str(mergeable).upper() if mergeable is not None else None),
        merge_state_status=(str(merge_state).upper() if merge_state is not None else None),
        base_ref=base_ref,
        head_sha=head_sha,
        labels=_label_names(data.get("labels")),
        changed_files=(),
        committer_at=committer_at,
        source_branch=str(data.get("headRefName") or "") or None,
        base_sha=str(data.get("baseRefOid") or "") or None,
        title=data.get("title"),
        target_url=f"https://github.com/{pr.owner}/{pr.repo}",
        source_url=_source_repository_url(data),

    )


def _source_repository_url(data: dict) -> str | None:
    repository = data.get("headRepository") or {}
    owner = data.get("headRepositoryOwner") or {}
    name, login = repository.get("name"), owner.get("login")
    return f"https://github.com/{login}/{name}" if name and login else None


def _load_pr_view(
    pr_url: str,
    pr: PullRequestRef,
    runner: Callable[[Sequence[str]], str],
) -> dict[str, Any]:
    raw = runner(
        [
            "pr",
            "view",
            str(pr.number),
            "-R",
            pr.slug,
            "--json",
            "state,mergeable,mergeStateStatus,baseRefName,baseRefOid,headRefName,headRefOid,labels,url,title,headRepository,headRepositoryOwner",
        ]
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(f"Invalid JSON from gh pr view for {pr_url}: {exc}") from exc
    if not isinstance(data, dict):
        raise GapPrMonitorError(f"Invalid JSON from gh pr view for {pr_url}.")
    return data


def _head_committer_at(
    owner: str,
    repo: str,
    head_sha: str,
    pr_url: str,
    runner: Callable[[Sequence[str]], str],
) -> datetime:
    """Return the committer timestamp of the already captured head SHA.

    ``gh pr view`` only returns the first 100 commits, so a long pull request
    can omit its head. The commit API addresses that SHA directly.
    """
    endpoint = f"repos/{owner}/{repo}/commits/{head_sha}"
    try:
        payload = github_api_json(runner, endpoint)
    except (GhCommandError, CheckQueryError) as exc:
        raise GapPrMonitorError(
            f"Could not read head commit {head_sha} on {pr_url}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise GapPrMonitorError(f"Could not read head commit {head_sha} on {pr_url}.")
    returned = str(payload.get("sha") or "")
    if returned and returned != head_sha:
        raise GapPrMonitorError(
            f"Head commit lookup for {head_sha} returned {returned} on {pr_url}."
        )
    commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
    committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
    raw = committer.get("date")
    if not raw:
        raise GapPrMonitorError(
            f"Head commit {head_sha} has no committer timestamp on {pr_url}."
        )
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise GapPrMonitorError(
            f"Invalid head commit timestamp for {pr_url}: {raw}"
        ) from exc


def _label_names(labels: Any) -> frozenset[str]:
    names: set[str] = set()
    if not isinstance(labels, list):
        return frozenset()
    for label in labels:
        if isinstance(label, str) and label.strip():
            names.add(label.strip())
        elif isinstance(label, dict) and str(label.get("name") or "").strip():
            names.add(str(label["name"]).strip())
    return frozenset(names)


def run_monitor(
    state_path: Path,
    *,
    check_name: str = DEFAULT_CHECK_NAME,
    dry_run: bool = False,
    continue_on_error: bool = False,
    description: str | None = None,
    updater_factory: Callable[..., PRStatusUpdater] | None = None,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    now: datetime | None = None,
    build_app_slug: str | None = None,
    build_app_id: int | None = None,
) -> MonitorResult:
    """Evaluate build and feasibility gates, post statuses, and update state."""
    path = state_path.expanduser().resolve()
    payload = load_state(path)
    retired = retired_outcome(payload) or history_state_outcome(path)
    if retired:
        return MonitorResult(path, [], [], [], [], dry_run, overall_status=retired,
                             reports=("Retired Leader: child evaluation is disabled",))
    pr_urls = extract_pr_urls(payload)
    if not pr_urls:
        raise GapPrMonitorError(f"No pull requests listed in {path}.")

    if build_app_slug is None:
        build_app_slug, env_app_id = default_build_producer()
        if build_app_id is None:
            build_app_id = env_app_id
    moment = now or datetime.now().astimezone()
    runner = gh_runner or _run_gh
    try:
        component_repo_map = fetch_component_repo_map(runner)
    except (GhCommandError, CheckQueryError) as exc:
        raise GapPrMonitorError(f"Could not load component repository mapping: {exc}") from exc
    factory = updater_factory or PRStatusUpdater
    updater = factory(check_name=check_name, dry_run=dry_run)

    decisions: list[RepositoryGateDecision] = []
    classify_errors: list[tuple[str, str]] = []
    for url in pr_urls:
        try:
            decisions.append(
                _decide_url(
                    url,
                    runner=runner,
                    now=moment,
                    build_app_slug=build_app_slug,
                    build_app_id=build_app_id,
                    retry_unknown=gh_runner is None,
                    component_repo_map=component_repo_map,
                )
            )
        except (GapPrMonitorError, GhCommandError, ValueError, RuntimeError) as exc:
            if not continue_on_error:
                raise GapPrMonitorError(f"Failed to inspect {url}: {exc}") from exc
            classify_errors.append((url, str(exc)))

    return _publish_decisions(
        path,
        payload,
        decisions,
        classify_errors=classify_errors,
        updater=updater,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
        description=description,
        runner=runner,
        now=moment,
        build_app_slug=build_app_slug,
        build_app_id=build_app_id,
        retry_unknown=gh_runner is None,
        component_repo_map=component_repo_map,
    )


def _decide_url(
    url: str,
    *,
    runner: Callable[[Sequence[str]], str],
    now: datetime,
    build_app_slug: str,
    build_app_id: int | None,
    retry_unknown: bool,
    component_repo_map: dict[str, frozenset[str]],
    retry_manager: BuildRetryManager | None = None,
) -> RepositoryGateDecision:
    pull_request = fetch_repository_pull_request(
        url, gh_runner=runner, retry_unknown=retry_unknown
    )
    blocking_merge = pull_request.state == "MERGED" or (
        pull_request.mergeable == "CONFLICTING"
        or (pull_request.merge_state_status or "") in CONFLICT_MERGE_STATES
    )
    check_runs: list = []
    check_query_error: str | None = None
    tekton_documents: list[str] | None = []
    tekton_error: str | None = None
    if blocking_merge:
        feasibility = FeasibilityRun(
            found=False,
            status=None,
            conclusion=None,
            head_sha=pull_request.head_sha,
            jobs=(),
        )
    else:
        try:
            if not pull_request.base_sha:
                raise CheckQueryError("PR snapshot has no pinned base SHA")
            file_collections = fetch_file_collections(
                pull_request.owner, pull_request.repo, pull_request.number, runner,
                base_sha=pull_request.base_sha, head_sha=pull_request.head_sha,
            )
            pull_request = replace(
                pull_request,
                changed_files=file_collections["all"],
                file_collections=file_collections,
            )
        except CheckQueryError as exc:
            tekton_error = str(exc)
        try:
            check_runs = list_check_runs(
                pull_request.owner,
                pull_request.repo,
                pull_request.head_sha,
                runner,
            )
        except CheckQueryError as exc:
            check_query_error = str(exc)
        if tekton_error is None:
            try:
                tekton_documents = fetch_tekton_documents(
                    pull_request.owner,
                    pull_request.repo,
                    pull_request.head_sha,
                    runner,
                )
            except CheckQueryError as exc:
                tekton_documents = None
                tekton_error = str(exc)
        feasibility = fetch_feasibility_run(
            pull_request.owner,
            pull_request.repo,
            pull_request.head_sha,
            runner,
        )

    build_gate_override, observations = None, None
    if not blocking_merge and build_status_source() == "konflux":
        configuration_error = None
        try:
            if tekton_error is not None:
                raise TriggerParseError(tekton_error)
            expected = expected_builds_for_pr(
                pull_request, tekton_documents, app_slug=build_app_slug, app_id=build_app_id,
                allowed_components=repository_components(component_repo_map, pull_request.repo),
            )
        except TriggerParseError as exc:
            expected, configuration_error = (), str(exc) or "Expected Konflux build configuration could not be verified"
        build_gate_override, observations = evaluate_konflux_gate(
            repo=f"{pull_request.owner}/{pull_request.repo}",
            number=pull_request.number, sha=pull_request.head_sha, expected=expected,
            query_error=configuration_error,
        )

    decision = decide_repository_pr(
        pull_request,
        tekton_documents=tekton_documents,
        tekton_error=tekton_error,
        check_runs=check_runs,
        check_query_error=check_query_error,
        feasibility=feasibility,
        now=now,
        build_app_slug=build_app_slug,
        build_app_id=build_app_id,
        allowed_components=repository_components(component_repo_map, pull_request.repo),
        build_gate_override=build_gate_override,
    )
    if (retry_manager is not None and observations is not None
            and decision.pr_status != PR_STATUS_MERGE_FAILURE):
        build_gate_override, observations = retry_manager.apply(pull_request, build_gate_override, observations)
        decision = decide_repository_pr(
            pull_request, tekton_documents=tekton_documents, tekton_error=tekton_error,
            check_runs=check_runs, check_query_error=check_query_error, feasibility=feasibility,
            now=now, build_app_slug=build_app_slug, build_app_id=build_app_id,
            allowed_components=repository_components(component_repo_map, pull_request.repo),
            build_gate_override=build_gate_override,
        )
    if observations is None and build_status_source() == "konflux":
        # Merge/conflict decisions need no build query, but the immediately
        # following comment can still reuse their freshly evaluated PR facts.
        observations = {
            "source": "konflux", "repository": f"{pull_request.owner}/{pull_request.repo}",
            "pr": pull_request.number, "head-sha": pull_request.head_sha,
            "observed-at": now.isoformat(), "runs": [], "expected-components": [],
            "query-skipped": "PR already merged or has a merge conflict",
        }
    if observations is not None:
        observations["pr-state"] = pull_request.state
        observations["gate-reason"] = decision.reason
    return replace(decision, build_observations=observations)


def _post_gate_status(
    updater: PRStatusUpdater,
    decision: RepositoryGateDecision,
    description: str | None,
) -> StatusUpdateResult:
    """Post one gate decision to the SHA that decision evaluated."""
    desc = description if description is not None else decision.description
    return updater.post_status_for_pr(
        decision.url,
        decision.cli_status,
        description=desc,
        target_url=decision.target_url,
        head_sha=decision.head_sha,
        defer_if_head_moved=True,
    )


def _publish_gate_decision(
    decision: RepositoryGateDecision,
    *,
    updater: PRStatusUpdater,
    description: str | None,
    runner: Callable[[Sequence[str]], str],
    now: datetime,
    build_app_slug: str,
    build_app_id: int | None,
    retry_unknown: bool,
    component_repo_map: dict[str, frozenset[str]],
    batch_concluded: bool,
    overall_status: str | None,
    payload: dict[str, Any] | None = None,
    retry_manager: BuildRetryManager | None = None,
) -> tuple[RepositoryGateDecision | None, StatusUpdateResult | None]:
    """Publish a decision, or re-evaluate once when the head has moved.

    A second move is left for the next poll. The stale decision is not posted
    to the untested commit. Success greens are deferred while required
    ``promotion-gates`` are not ready (RHAI-8261 + required checklist);
    ``pr_status`` on the returned decision stays evaluated.
    """
    if decision.skip_status_post:
        return decision, None
    to_post = status_decision_for_post(
        decision,
        batch_concluded=batch_concluded,
        overall_status=overall_status,
        payload=payload,
    )
    assert isinstance(to_post, RepositoryGateDecision)
    result = _post_gate_status(updater, to_post, description)
    if not result.deferred:
        return decision, result
    fresh = _decide_url(
        decision.url,
        runner=runner,
        now=now,
        build_app_slug=build_app_slug,
        build_app_id=build_app_id,
        retry_unknown=retry_unknown,
        component_repo_map=component_repo_map,
        retry_manager=retry_manager,
    )
    # The head changed after the batch refresh. Recompute the shared probe
    # before posting this decision or any following sibling: an old terminal
    # failure must not release greens when the new head is pending/recovered.
    if payload is not None:
        record_child_head_shas(payload, [fresh])
        apply_pr_statuses(payload, {fresh.url: fresh.pr_status})
        apply_component_builds_from_children(payload)
        overall_status = derive_legacy_overall_status(payload)
        batch_concluded = children_batch_concluded(
            non_ignored_child_statuses(payload)
        )
    else:
        batch_concluded, overall_status = False, None
    if fresh.skip_status_post:
        return fresh, None
    to_post = status_decision_for_post(
        fresh,
        batch_concluded=batch_concluded,
        overall_status=overall_status,
        payload=payload,
    )
    assert isinstance(to_post, RepositoryGateDecision)
    result = _post_gate_status(updater, to_post, description)
    if result.deferred:
        if payload is not None:
            apply_pr_statuses(payload, {fresh.url: BUILD_PENDING_PR_STATUS})
            apply_component_builds_from_children(payload)
        return None, result
    return fresh, result


def _publish_decisions(
    path: Path,
    payload: dict[str, Any],
    decisions: Sequence[RepositoryGateDecision],
    *,
    classify_errors: list[tuple[str, str]],
    updater: PRStatusUpdater,
    dry_run: bool,
    continue_on_error: bool,
    description: str | None,
    runner: Callable[[Sequence[str]], str],
    now: datetime,
    build_app_slug: str,
    build_app_id: int | None,
    retry_unknown: bool,
    component_repo_map: dict[str, frozenset[str]],
) -> MonitorResult:
    status_updates: dict[str, str] = {}
    updated: list[str] = []
    skipped: list[str] = []
    merge_failures: list[str] = []
    build_failures: list[str] = []
    pending: list[str] = []
    successes: list[str] = []
    reports: list[str] = []
    post_errors: list[tuple[str, str]] = []
    evaluated_decisions: list[RepositoryGateDecision] = []
    retry_manager = (BuildRetryManager(runner, payload, dry_run=dry_run)
                     if retries_enabled() and build_status_source() == "konflux"
                     and "GAP History" not in path.parts else None)
    ensure_promotion_gates(payload)
    migrate_legacy_overall_status(payload)
    if build_status_source() == "konflux":
        # Refresh the whole batch before deriving the child-green hold. A retry
        # can turn a terminal failure into pending/success; mixing fresh child
        # decisions with the old batch outcome could release a green before EG.
        # This is the existing second observation, moved ahead of all POSTs.
        try:
            decisions = tuple(
                decision if decision.skip_status_post else _decide_url(
                    decision.url, runner=runner, now=now, build_app_slug=build_app_slug,
                    build_app_id=build_app_id, retry_unknown=retry_unknown,
                    component_repo_map=component_repo_map,
                    retry_manager=retry_manager,
                )
                for decision in decisions
            )
        except (GapPrMonitorError, GhCommandError, ValueError, RuntimeError) as exc:
            # No commit status or state writes have happened: retain the prior
            # batch. Any retry comments already sent carry their own recovery
            # receipts, so the next poll cannot repeat them.
            raise GapPrMonitorError(f"Could not refresh child batch before publication: {exc}") from exc
    # One coherent batch decides both persisted outcomes and the child-green hold.
    child_head_changed = record_child_head_shas(payload, decisions)
    # Provisional statuses from first-pass decisions (before head-move refresh).
    provisional_updates = {d.url: d.pr_status for d in decisions}
    probe = provisional_payload_with_updates(payload, provisional_updates)
    batch_concluded = children_batch_concluded(
        non_ignored_child_statuses(probe, provisional_updates)
    )
    overall_status = derive_legacy_overall_status(probe)

    for original in decisions:
        url = original.url
        try:
            decision, result = _publish_gate_decision(
                original,
                updater=updater,
                description=description,
                runner=runner,
                now=now,
                build_app_slug=build_app_slug,
                build_app_id=build_app_id,
                retry_unknown=retry_unknown,
                component_repo_map=component_repo_map,
                batch_concluded=batch_concluded,
                overall_status=overall_status,
                payload=probe,
                retry_manager=retry_manager,
            )
        except (ValueError, RuntimeError, GhCommandError, GapPrMonitorError) as exc:
            post_errors.append((url, str(exc)))
            # A late refresh/action may have started a retry before its response
            # was lost. Keep subsequent siblings held while this child is unknown.
            status_updates[url] = BUILD_PENDING_PR_STATUS
            apply_pr_statuses(probe, {url: BUILD_PENDING_PR_STATUS})
            apply_component_builds_from_children(probe)
            if not continue_on_error:
                if status_updates and not dry_run:
                    apply_pr_statuses(payload, status_updates)
                    apply_component_builds_from_children(payload)
                    strip_legacy_overall_status(payload)
                    save_state(path, payload)
                raise GapPrMonitorError(f"Failed to post status for {url}: {exc}") from exc
            continue
        if decision is None:
            reports.append(f"deferred {url} head changed before status publication")
            skipped.append(url)
            status_updates[url] = BUILD_PENDING_PR_STATUS
            pending.append(url)
            continue

        evaluated_decisions.append(decision)
        status_updates[url] = decision.pr_status
        for entry in payload.get("pull-requests", []):
            if (entry.get("pr-url") or entry.get("pr_url")) == url:
                if decision.merge_observations is not None:
                    from lib.gap_notifications.merge_receipts import bind_receipt
                    try:
                        from types import SimpleNamespace
                        ref = parse_pr_url(url)
                        snapshot = _load_pr_view(url, ref, runner)
                        current = SimpleNamespace(
                            owner=ref.owner, repo=ref.repo, number=ref.number,
                            head_sha=snapshot.get("headRefOid"), base_sha=snapshot.get("baseRefOid"),
                            state=snapshot.get("state"), mergeable=snapshot.get("mergeable"),
                            merge_state_status=snapshot.get("mergeStateStatus"),
                            source_branch=snapshot.get("headRefName"), base_ref=snapshot.get("baseRefName"),
                        )
                    except (ValueError, RuntimeError, GhCommandError):
                        current = None
                    try:
                        entry["merge-observations"] = bind_receipt(
                            decision.merge_observations, current, entry.get("merge-observations"),
                        )
                    except (ValueError, TypeError, KeyError, AttributeError):
                        # Optional notification metadata must never change gate acceptance.
                        from lib.gap_notifications.merge_receipts import unavailable_receipt
                        entry["merge-observations"] = unavailable_receipt(entry.get("merge-observations"))
                else:
                    from lib.gap_notifications.merge_receipts import unavailable_receipt
                    entry["merge-observations"] = unavailable_receipt(entry.get("merge-observations"))
                if decision.build_observations is not None:
                    previous_history = (entry.get("build-observations") or {}).get("retry-history")
                    if previous_history and "retry-history" not in decision.build_observations:
                        decision.build_observations["retry-history"] = previous_history
                    entry["build-observations"] = decision.build_observations
                elif build_status_source() == "github":
                    # An explicit rollback must not leave obsolete Konflux
                    # receipts attached to newly evaluated GitHub statuses.
                    entry.pop("build-observations", None)
        reports.extend(decision.reports)
        if decision.pr_status == MERGE_FAILURE_PR_STATUS:
            merge_failures.append(url)
        elif decision.pr_status == BUILD_FAILURE_PR_STATUS:
            build_failures.append(url)
        elif decision.pr_status == BUILD_PENDING_PR_STATUS:
            pending.append(url)
        else:
            successes.append(url)

        if decision.skip_status_post or (result is not None and result.skipped):
            skipped.append(url)
        elif result is not None:
            updated.append(url)

    if classify_errors or post_errors:
        if status_updates and not dry_run:
            apply_pr_statuses(payload, status_updates)
            apply_component_builds_from_children(payload)
            strip_legacy_overall_status(payload)
            save_state(path, payload)
        details = classify_errors + post_errors
        lines = [f"  - {u}: {e}" for u, e in details]
        raise GapPrMonitorError(
            f"{len(details)} PR(s) failed during monitor.\n" + "\n".join(lines)
        )

    apply_pr_statuses(payload, status_updates)
    child_head_changed |= record_child_head_shas(payload, evaluated_decisions)
    apply_component_builds_from_children(payload)
    strip_legacy_overall_status(payload)
    overall_status = derive_legacy_overall_status(payload)
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
        build_failure_urls=build_failures,
        pending_urls=pending,
        reports=tuple(reports),
        child_head_changed=child_head_changed,
        # Exclude old persisted receipts from head-move deferral/post failures.
        notification_urls=tuple(d.url for d in evaluated_decisions),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "GAP PR monitor: read leader state.json, evaluate component build "
            "and main-to-release feasibility checks, post the gated artifacts "
            "promoter commit status, and update pr-status."
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
            "Leader PR URL, or "
            f"{LATEST_LEADER_PR_SENTINEL} to discover the newest open Leader "
            f"labeled {DEFAULT_LEADER_LABEL!r}. With --publish-state, after "
            "the state commit is pushed: comment /early-gate when "
            "overall-status is early-gate-triggered (RHAI-10942); failed Leaders "
            "remain active until replaced. Also accepted via "
            f"{ENV_LEADER_PR_URL}."
        ),
    )
    parser.add_argument(
        "--publish-state",
        action="store_true",
        help=(
            "RHOAIENG-97052 / RHAI-8079 / RHAI-10942: commit and push state.json, "
            "then comment /early-gate when overall-status is early-gate-triggered, "
            "or preserve unsuccessful Leaders until a newer batch replaces them "
            "(never while children are pending)."
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
            "monitor on each. Also selected automatically when "
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
    notifications = parser.add_argument_group("opt-in notifications (disabled; unset GAP_JIRA_URL defaults to staging)")
    notifications.add_argument("--notification-mode", choices=("disabled", "preview", "write"), default="disabled")
    notifications.add_argument("--notification-ledger", help="Isolated schema2 journal OUTSIDE monitored checkout")
    notifications.add_argument("--notification-trial-id", default="default")
    notifications.add_argument("--notification-transition-id", help="Explicit verified done transition for the selected Jira environment")
    notifications.add_argument("--notification-transition-fields", default="{}", help="JSON fields for recovery transition")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the scheduled or single-Leader monitor and expose its final published state path."""
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root)
    notification_config = None
    if args.notification_mode != "disabled":
        try:
            from lib.gap_notifications.monitor_hook import NotificationConfig
            from lib.gap_notifications.notify_tools import jira_target_from_url, STAGING_JIRA_ORIGIN
            if "GAP_JIRA_ENVIRONMENT" in os.environ:
                print("WARNING: Remove GAP_JIRA_ENVIRONMENT; use GAP_JIRA_URL. Notifications disabled.", file=sys.stderr)
                raise ValueError("legacy Jira selector")
            target = jira_target_from_url(os.environ.get("GAP_JIRA_URL", STAGING_JIRA_ORIGIN))
            notification_config = NotificationConfig(
                mode=args.notification_mode, ledger=args.notification_ledger,
                trial_id=args.notification_trial_id,
                jira_environment=target.name,
                channel=os.environ.get("GAP_SLACK_CHANNEL", ""),
                transition_id=args.notification_transition_id,
                transition_fields=json.loads(args.notification_transition_fields),
            )
        except Exception as exc:
            print("WARNING: Invalid notification configuration: " + type(exc).__name__, file=sys.stderr)

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
                **({"notification_config": notification_config} if notification_config is not None else {}),
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
    leader_label = (
        (args.leader_label or "").strip()
        or _env_nonempty(ENV_LEADER_LABEL)
        or DEFAULT_LEADER_LABEL
    )
    try:
        if leader_pr_url and is_latest_leader_pr_sentinel(leader_pr_url):
            leader_repo = resolve_leader_repo(args.leader_repo)
            leader_pr_url = resolve_leader_pr_url_input(
                leader_pr_url,
                leader_repo=leader_repo,
                label=leader_label,
            )
        state_path = resolve_state_file(
            state_file=state_file,
            trigger_id=trigger_id,
            labels=labels,
            root=repo_root,
            allow_gap_dir_fallback=args.allow_gap_dir_fallback,
        )
        result = run_monitor_publish_and_signal(
            state_path,
            repo_root=repo_root,
            leader_pr_url=leader_pr_url,
            **({"notification_config": notification_config} if notification_config is not None else {}),
            publish=args.publish_state,
            check_name=args.check_name,
            dry_run=args.dry_run,
            continue_on_error=args.continue_on_error,
            description=args.description,
        )
        # Finalization may have moved the state; expose the published location.
        append_github_output(
            "state_path",
            state_path_for_output(result.state_path, repo_root=repo_root),
        )
    except GapPrMonitorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    prefix = "[dry-run] " if result.dry_run else ""
    total = (
        len(result.success_urls)
        + len(result.merge_failure_urls)
        + len(result.build_failure_urls)
        + len(result.pending_urls)
    )
    print(f"{prefix}Processed {total} PR(s) from {result.state_path}")
    for url in result.success_urls:
        print(f"{prefix}success: {url}")
    for url in result.merge_failure_urls:
        print(f"{prefix}merge-failure: {url}")
    for url in result.build_failure_urls:
        print(f"{prefix}build-failure: {url}")
    for url in result.pending_urls:
        print(f"{prefix}build-pending: {url}")
    for line in result.reports:
        print(f"{prefix}{line}")
    if result.overall_status is not None:
        print(f"{prefix}overall-status: {result.overall_status}")
        append_github_output("overall_status", result.overall_status)
    if result.published_sha:
        print(f"{prefix}published-sha: {result.published_sha}")
        append_github_output("published_sha", result.published_sha)
    if result.early_gate_comment_posted:
        print(f"{prefix}Posted Early Gate trigger comment (/early-gate)")
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
