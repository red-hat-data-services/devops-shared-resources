"""Stage-1 GAP PR monitor: green-check PRs listed in a leader state file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from lib.pr_status_updater import (
    GhCommandError,
    PRStatusUpdater,
    StatusUpdateBatchError,
)

DEFAULT_CHECK_NAME = "gated artifacts promoter"
DEFAULT_CLI_STATUS = "completed"
SUCCESS_PR_STATUS = "success"


class GapPrMonitorError(RuntimeError):
    """Raised when the GAP state file cannot be processed."""


@dataclass(frozen=True)
class MonitorResult:
    state_path: Path
    updated_urls: list[str]
    skipped_urls: list[str]
    dry_run: bool


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


def apply_success_statuses(payload: dict[str, Any], urls: Sequence[str]) -> None:
    wanted = set(urls)
    for entry in payload.get("pull-requests") or []:
        if isinstance(entry, dict) and str(entry.get("pr-url", "")).strip() in wanted:
            entry["pr-status"] = SUCCESS_PR_STATUS


def run_stage1_monitor(
    state_path: Path,
    *,
    check_name: str = DEFAULT_CHECK_NAME,
    dry_run: bool = False,
    continue_on_error: bool = False,
    description: str | None = "GAP Stage 1 dummy gate",
    updater_factory: Callable[..., PRStatusUpdater] | None = None,
) -> MonitorResult:
    """Post green commit statuses for every PR in the state file and mark success.

    Stage 1 (RHOAIENG-93565): emit green gated-artifacts-promoter status for each
    listed PR, update pr-status to success, write the state file, then return.
    """
    path = state_path.expanduser().resolve()
    payload = load_state(path)
    pr_urls = extract_pr_urls(payload)
    if not pr_urls:
        raise GapPrMonitorError(f"No pull requests listed in {path}.")

    factory = updater_factory or PRStatusUpdater
    updater = factory(check_name=check_name, dry_run=dry_run)

    try:
        results = updater.post_status_for_many(
            pr_urls,
            DEFAULT_CLI_STATUS,
            description=description,
            continue_on_error=continue_on_error,
        )
    except StatusUpdateBatchError as exc:
        success_urls = [r.pr.url for r in exc.successes]
        if success_urls:
            apply_success_statuses(payload, success_urls)
            if not dry_run:
                save_state(path, payload)
        raise GapPrMonitorError(str(exc)) from exc
    except (ValueError, RuntimeError, GhCommandError) as exc:
        raise GapPrMonitorError(str(exc)) from exc

    updated = [r.pr.url for r in results if not r.skipped]
    skipped = [r.pr.url for r in results if r.skipped]
    touched = [r.pr.url for r in results]
    apply_success_statuses(payload, touched)
    if not dry_run:
        save_state(path, payload)

    return MonitorResult(
        state_path=path,
        updated_urls=updated,
        skipped_urls=skipped,
        dry_run=dry_run,
    )
