#!/usr/bin/env python3
"""Orchestrate Gated Artifacts Promoter sync runs and the Leader PR."""

from __future__ import annotations

import argparse
import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.config_parser import (  # noqa: E402
    ConfigError,
    filter_sync_entries,
    load_sync_config,
)
from lib.leader_pr import GAP_LABEL, LeaderPRManager, LeaderPRResult  # noqa: E402
from lib.state_file import (  # noqa: E402
    DEFAULT_LEADER_REPO,
    PR_STATUS_NEW,
    StateFileError,
    StatePullRequest,
    build_state,
    short_repo_name,
)
from lib.trigger_id import normalize_trigger_id  # noqa: E402
from scripts.sync_branches import SyncOutcome, resolve_github_token, run_sync_entry  # noqa: E402


@dataclass
class PromoterRunResult:
    trigger_id: str
    outcomes: list[tuple[dict[str, Any], SyncOutcome]]
    state_prs: list[StatePullRequest]
    leader: LeaderPRResult | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run branch syncs for the Gated Artifacts Promoter and open a Leader PR "
            "tracking all child PRs."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to sync config YAML (infra git: map or native syncs: format).",
    )
    parser.add_argument(
        "--trigger-id",
        default=None,
        help=(
            "Optional trigger ID for Leader state.json path and PR labels. "
            "Generated when omitted. Each new trigger ID opens a new Leader PR; "
            f"child sync PRs are tracked by the {GAP_LABEL!r} label."
        ),
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated sync entry names to run.",
    )
    parser.add_argument(
        "--leader-repo",
        default=DEFAULT_LEADER_REPO,
        help=f"Leader repository (default: {DEFAULT_LEADER_REPO}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan actions without creating PRs or writing the Leader state.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Pass verbose through to sync_branches merge conflict details.",
    )
    return parser


def prepare_entry_for_trigger(entry: dict[str, Any], trigger_id: str) -> dict[str, Any]:
    """Return a deep copy of entry with GAP defaults and labels injected.

    - Tracking label: ``gated-artifacts-promoter`` (re-runs update open PRs)
    - Extra labels: GAP label + trigger ID
    - Default PR head: source branch → stable (main→stable, no ignore-files)
    - ``ignore-files`` only kept when ``pr-head: sync-branch`` is set
    """
    prepared = copy.deepcopy(entry)
    pr = prepared.setdefault("pr", {})
    pr["tracking_label"] = GAP_LABEL
    pr["merge_when_ready"] = True
    head = pr.get("head_strategy") or "source"
    if head != "sync-branch":
        head = "source"
        prepared["ignore_files"] = []
        pr["branch"] = None
    pr["head_strategy"] = head
    labels = list(pr.get("labels") or [])
    for label in (GAP_LABEL, trigger_id):
        if label not in labels:
            labels.append(label)
    pr["labels"] = labels
    return prepared


def outcome_to_state_pr(
    entry: dict[str, Any],
    outcome: SyncOutcome,
) -> StatePullRequest | None:
    """Convert a successful sync PR outcome into a Stage 1 state.json PR record."""
    if not outcome.pr_url:
        return None
    repo = short_repo_name(str(entry.get("name") or entry["dest"]["url"]))
    return StatePullRequest(
        repo=repo,
        pr_url=outcome.pr_url,
        pr_status=PR_STATUS_NEW,
        builds=(),
    )


def run_promoter(
    *,
    config_path: str | Path,
    trigger_id: str | None = None,
    only: str | None = None,
    leader_repo: str = DEFAULT_LEADER_REPO,
    dry_run: bool = False,
    verbose: bool = False,
    token: str | None = None,
    sync_runner: Callable[..., SyncOutcome] | None = None,
    leader_manager: LeaderPRManager | None = None,
    progress: Callable[[str], None] | None = None,
) -> PromoterRunResult:
    """Execute sync entries and create/update the Leader PR."""
    resolved_trigger = normalize_trigger_id(trigger_id)
    entries = filter_sync_entries(load_sync_config(config_path), only)
    resolved_token = token if token is not None else resolve_github_token()
    runner = sync_runner or run_sync_entry
    report = progress or (lambda _message: None)

    outcomes: list[tuple[dict[str, Any], SyncOutcome]] = []
    state_prs: list[StatePullRequest] = []
    entry_count = len(entries)
    entry_label = "entry" if entry_count == 1 else "entries"
    mode = " (dry run)" if dry_run else ""
    report(
        f"[promoter] Starting trigger {resolved_trigger}: "
        f"{entry_count} sync {entry_label}{mode}"
    )

    for index, entry in enumerate(entries, start=1):
        prepared = prepare_entry_for_trigger(entry, resolved_trigger)
        name = str(prepared.get("name") or prepared["dest"]["url"])
        source = prepared["src"]
        target = prepared["dest"]
        details = [prepared["sync_type"]]
        if prepared["sync_type"] == "pr":
            details.extend(
                [
                    f"head={prepared['pr']['head_strategy']}",
                    f"automerge={'on' if prepared['pr']['automerge'] else 'off'}",
                ]
            )
        report(
            f"[{index}/{entry_count}] Processing {name}: "
            f"{source['url']}:{source['branch']} -> "
            f"{target['url']}:{target['branch']} ({', '.join(details)})"
        )
        try:
            outcome = runner(
                prepared,
                token=resolved_token,
                dry_run=dry_run,
                verbose=verbose,
            )
        except Exception as exc:
            report(f"[{index}/{entry_count}] Failed {name}: {exc}")
            raise
        outcomes.append((prepared, outcome))
        state_pr = outcome_to_state_pr(prepared, outcome)
        if state_pr is not None:
            state_prs.append(state_pr)
        result = outcome.message
        if outcome.pr_url:
            result += f" ({outcome.pr_url})"
        report(f"[{index}/{entry_count}] Completed {name}: {result}")

    state = build_state(pull_requests=state_prs)

    manager = leader_manager or LeaderPRManager(repo=leader_repo, dry_run=dry_run)
    report(
        f"[leader] Processing {leader_repo} with "
        f"{len(state_prs)} child pull requests"
    )
    try:
        leader_result = manager.create_or_update(state, trigger_id=resolved_trigger)
    except Exception as exc:
        report(f"[leader] Failed {leader_repo}: {exc}")
        raise
    leader_location = leader_result.pr_url or leader_result.state_path
    report(f"[leader] Completed {leader_repo}: {leader_location}")
    report(
        f"[promoter] Completed trigger {resolved_trigger}: "
        f"{len(outcomes)} sync {entry_label}, {len(state_prs)} child pull requests"
    )

    return PromoterRunResult(
        trigger_id=resolved_trigger,
        outcomes=outcomes,
        state_prs=state_prs,
        leader=leader_result,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = run_promoter(
            config_path=args.config,
            trigger_id=args.trigger_id,
            only=args.only,
            leader_repo=args.leader_repo,
            dry_run=args.dry_run,
            verbose=args.verbose,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except (ConfigError, StateFileError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"trigger_id={result.trigger_id}")
    for entry, outcome in result.outcomes:
        name = entry.get("name") or entry["dest"]["url"]
        print(f"[{name}] {outcome.message}")
        if outcome.pr_url:
            print(outcome.pr_url)

    if result.leader:
        action = "Updated" if result.leader.updated else "Created"
        if result.leader.dry_run:
            print(
                f"Leader PR dry-run for {result.leader.repo} "
                f"(branch {result.leader.branch}, state {result.leader.state_path})"
            )
        elif result.leader.pr_url:
            print(f"{action} leader pull request: {result.leader.pr_url}")
        else:
            print(f"{action} leader pull request for {result.leader.repo}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
