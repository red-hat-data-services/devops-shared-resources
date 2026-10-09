from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from lib.github_check_runs import (
    GATE_TIMEOUT,
    CheckQueryError,
    CheckRun,
    ExpectedCheck,
    evaluate_checks,
    list_check_runs,
    revision_deadline,
)
from lib.github_cli import GhCommandError

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40


def _run(
    *,
    run_id: int = 1,
    name: str = "Konflux Production Internal / odh-eval-hub-on-pull-request-65317",
    status: str = "completed",
    conclusion: str | None = "success",
    head_sha: str = SHA,
    app_slug: str = "konflux-internal-p02",
    app_id: int = 906404,
    started_at: str = "2026-10-06T11:00:00Z",
    html_url: str = "https://github.com/example/repo/runs/1",
    details_url: str = "https://konflux.example/ns/rhoai-tenant/pipelinerun/odh-eval-hub-on-pull-request-65317-s8lwr",
    external_id: str = "odh-eval-hub-on-pull-request-65317-s8lwr",
) -> CheckRun:
    return CheckRun(
        id=run_id,
        name=name,
        status=status,
        conclusion=conclusion,
        head_sha=head_sha,
        html_url=html_url,
        details_url=details_url,
        external_id=external_id,
        app_slug=app_slug,
        app_id=app_id,
        started_at=started_at,
    )


def _expected(suffix: str = "odh-eval-hub-on-pull-request-65317", key: str = "odh-eval-hub-v3-6") -> ExpectedCheck:
    return ExpectedCheck(key=key, name_suffix=suffix, app_slug="konflux-internal-p02", app_id=906404)


def test_revision_deadline_is_fixed_to_the_commit() -> None:
    committer = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    first = revision_deadline(committer)
    later = revision_deadline(committer)
    assert first == later == committer + GATE_TIMEOUT
    assert first == committer + timedelta(hours=6)


def test_only_success_passes_and_listed_conclusions_fail() -> None:
    expected = (_expected(),)
    passing = evaluate_checks(expected, [_run()], now=NOW, deadline=NOW + timedelta(hours=1))
    assert passing.outcome == "pass"

    for conclusion in (
        "failure",
        "cancelled",
        "skipped",
        "neutral",
        "timed_out",
        "action_required",
        "stale",
    ):
        result = evaluate_checks(
            expected,
            [_run(conclusion=conclusion)],
            now=NOW,
            deadline=NOW + timedelta(hours=5),
        )
        assert result.outcome == "fail", conclusion
        assert result.timed_out is False
        assert conclusion in result.findings[0].reason


def test_unfinished_and_missing_checks_stay_pending_until_the_deadline() -> None:
    expected = (_expected(),)
    deadline = NOW + timedelta(hours=1)
    for status in ("queued", "in_progress", "waiting", "pending"):
        result = evaluate_checks(
            expected,
            [_run(status=status, conclusion=None)],
            now=NOW,
            deadline=deadline,
        )
        assert result.outcome == "pending", status

    missing = evaluate_checks(expected, [], now=NOW, deadline=deadline)
    assert missing.outcome == "pending"
    assert missing.findings[0].reason == "missing"

    expired = evaluate_checks(expected, [], now=deadline, deadline=deadline)
    assert expired.outcome == "fail"
    assert expired.timed_out is True
    # A later poll uses the same deadline; it does not grant another six hours.
    again = evaluate_checks(
        expected,
        [],
        now=deadline + timedelta(hours=3),
        deadline=deadline,
    )
    assert again.outcome == "fail"
    assert again.timed_out is True


def test_completed_failure_does_not_wait_for_the_timeout() -> None:
    result = evaluate_checks(
        (_expected(),),
        [_run(conclusion="failure")],
        now=NOW,
        deadline=NOW + timedelta(hours=5),
    )
    assert result.outcome == "fail"
    assert result.timed_out is False


def test_query_error_never_passes_and_times_out() -> None:
    pending = evaluate_checks(
        (_expected(),),
        [_run()],
        now=NOW,
        deadline=NOW + timedelta(hours=1),
        query_error="failed to list check runs",
    )
    assert pending.outcome == "pending"
    assert "failed to list check runs" in pending.findings[0].reason

    failed = evaluate_checks(
        (),
        [],
        now=NOW + timedelta(hours=7),
        deadline=NOW,
        query_error="failed to list check runs",
    )
    assert failed.outcome == "fail"
    assert failed.timed_out is True


def test_newest_rerun_wins_over_an_older_result() -> None:
    expected = (_expected(),)
    older_success = _run(run_id=1, conclusion="success", started_at="2026-10-06T10:00:00Z")
    newer_failure = _run(run_id=2, conclusion="failure", started_at="2026-10-06T11:00:00Z")
    failed = evaluate_checks(
        expected,
        [older_success, newer_failure],
        now=NOW,
        deadline=NOW + timedelta(hours=1),
    )
    assert failed.outcome == "fail"

    newer_pending = _run(
        run_id=3,
        status="in_progress",
        conclusion=None,
        started_at="2026-10-06T11:30:00Z",
    )
    pending = evaluate_checks(
        expected,
        [older_success, newer_pending],
        now=NOW,
        deadline=NOW + timedelta(hours=1),
    )
    assert pending.outcome == "pending"

    newer_success = _run(run_id=4, conclusion="success", started_at="2026-10-06T11:40:00Z")
    passed = evaluate_checks(
        expected,
        [newer_failure, newer_success],
        now=NOW,
        deadline=NOW + timedelta(hours=1),
    )
    assert passed.outcome == "pass"


def test_other_revision_and_other_producer_do_not_count() -> None:
    expected = (_expected(),)
    other_sha = _run(head_sha="b" * 40)
    other_app = _run(app_slug="someone-else", app_id=1)
    result = evaluate_checks(
        expected,
        [other_sha, other_app],
        now=NOW,
        deadline=NOW + timedelta(hours=1),
        revision=SHA,
    )
    assert result.outcome == "pending"
    assert result.findings[0].reason == "missing"


def test_one_failed_check_fails_the_whole_set() -> None:
    checks = (
        _expected("component-a-on-pull-request", "component-a"),
        _expected("component-b-on-pull-request", "component-b"),
    )
    runs = [
        _run(run_id=1, name="App / component-a-on-pull-request", conclusion="success"),
        _run(run_id=2, name="App / component-b-on-pull-request", conclusion="failure"),
    ]
    result = evaluate_checks(checks, runs, now=NOW, deadline=NOW + timedelta(hours=1))
    assert result.outcome == "fail"
    assert result.findings[1].key == "component-b"


def test_pipeline_details_link_is_preferred() -> None:
    result = evaluate_checks(
        (_expected(),),
        [_run(conclusion="failure")],
        now=NOW,
        deadline=NOW + timedelta(hours=1),
    )
    assert result.link is not None
    assert "pipelinerun" in result.link


def test_list_check_runs_paginates_and_drops_other_revisions() -> None:
    pages = {
        1: [_payload(1, SHA), _payload(2, "old")],
        2: [_payload(3, SHA)],
    }

    def runner(args: list[str]) -> str:
        assert args[0] == "api"
        assert "per_page=2" in args[1]
        page = 1 if "page=1" in args[1] else 2
        batch = pages[page]
        return json.dumps({"total_count": 3, "check_runs": batch})

    runs = list_check_runs("acme", "widget", SHA, runner, page_size=2)
    assert [run.id for run in runs] == [1, 3]
    assert all(run.head_sha == SHA for run in runs)


def test_list_check_runs_error_is_not_an_empty_success() -> None:
    def runner(args: list[str]) -> str:
        raise GhCommandError(["gh", *args], 1, "API rate limit")

    with pytest.raises(CheckQueryError, match="rate limit"):
        list_check_runs("acme", "widget", SHA, runner)


def _payload(run_id: int, sha: str) -> dict:
    return {
        "id": run_id,
        "name": f"App / pipeline-{run_id}",
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha,
        "html_url": f"https://github.com/acme/widget/runs/{run_id}",
        "details_url": f"https://konflux.example/pipelinerun/pipeline-{run_id}",
        "external_id": f"pipeline-{run_id}-rand",
        "started_at": "2026-10-06T11:00:00Z",
        "app": {"id": 906404, "slug": "konflux-internal-p02"},
    }
