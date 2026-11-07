"""Producer integration: independent receipts never change ordinary gate verdicts."""
from datetime import datetime, timedelta, timezone

import pytest

from lib.gap_check_selection import FEASIBILITY_SETUP_JOB, FeasibilityRun, WorkflowJob
from lib.gap_pr_gates import RepositoryPullRequest, decide_repository_pr
from lib.github_check_runs import CheckRun, GateResult, CheckFinding
from lib.gap_notifications.merge_receipts import bind_receipt

SHA, BASE = "a" * 40, "b" * 40
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def evaluate(*, conclusion="success", build="pass", query_error=None, conflict=False, age=1):
    pr = RepositoryPullRequest("https://github.com/acme/demo/pull/42", "acme", "demo", 42,
                               "OPEN", "CONFLICTING" if conflict else "MERGEABLE",
                               "DIRTY" if conflict else "CLEAN", "main", SHA, frozenset(), (),
                               NOW - timedelta(hours=age), source_branch="sync-main", base_sha=BASE)
    check = CheckRun(1, FEASIBILITY_SETUP_JOB, "completed", conclusion, SHA,
                     None, None, None, "github-actions", 1, None)
    run = FeasibilityRun(True, "completed", "success", SHA,
                         (WorkflowJob(FEASIBILITY_SETUP_JOB, "completed", conclusion, None, None),),
                         query_error=query_error, run_id=42, attempt=2, source_head_sha=SHA)
    build_gate = GateResult(build, (CheckFinding("api", build, "build result"),), False, None)
    d = decide_repository_pr(pr, tekton_documents=[], tekton_error=None, check_runs=[check],
                             check_query_error=None, feasibility=run, now=NOW,
                             build_app_slug="konflux", build_gate_override=build_gate)
    return d, bind_receipt(d.merge_observations, pr)


@pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled", "neutral", "timed_out"])
def test_required_nonpassing_conclusion_keeps_merge_failure(conclusion):
    decision, receipt = evaluate(conclusion=conclusion, build="fail")
    assert decision.pr_status == "merge-failure"
    assert receipt["feasibility"]["status"] == "failure"
    assert any("build-failure" in report for report in decision.reports)


def test_build_failure_cannot_mask_feasibility_recovery_receipt():
    decision, receipt = evaluate(build="fail")
    assert decision.pr_status == "build-failure"
    assert receipt["feasibility"]["status"] == "success"


def test_conflict_short_circuit_is_not_completed_skipped():
    decision, receipt = evaluate(conflict=True)
    assert decision.pr_status == "merge-failure"
    assert receipt["conflict"]["status"] == "failure"
    assert receipt["feasibility"] == {"status": "unknown", "query-skipped": True}


def test_query_error_timeout_stays_ordinary_gate_failure_but_notification_unknown():
    decision, receipt = evaluate(query_error="offline fake query failure", age=7)
    assert decision.pr_status == "merge-failure"
    assert receipt["feasibility"]["status"] == "unknown"
    assert receipt["feasibility"]["query-error"] is True
