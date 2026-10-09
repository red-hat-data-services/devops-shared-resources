"""Combine component-build and main-to-release feasibility gates for one PR.

The reusable check evaluator scores each selected check. This module applies
GAP's repository rules: every expected component build must succeed, a failed
feasibility job blocks the repository even when builds succeed, and an
unresolved result fails once the revision's six-hour deadline has passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from lib.gap_check_selection import (
    FeasibilityRun,
    PullRequestContext,
    TriggerParseError,
    select_build_checks,
    select_feasibility_checks,
)
from lib.github_check_runs import (
    OUTCOME_FAIL,
    OUTCOME_PASS,
    OUTCOME_PENDING,
    CheckFinding,
    CheckRun,
    GateResult,
    evaluate_checks,
    revision_deadline,
)
from lib.pr_status_updater import MAX_STATUS_DESCRIPTION_LENGTH

PR_STATUS_SUCCESS = "success"
PR_STATUS_MERGE_FAILURE = "merge-failure"
PR_STATUS_BUILD_PENDING = "build-pending"
PR_STATUS_BUILD_FAILURE = "build-failure"

_CONFLICT_MERGE_STATES = frozenset({"DIRTY", "CONFLICTING"})


@dataclass(frozen=True)
class RepositoryPullRequest:
    """Pull request facts the monitor has already loaded."""

    url: str
    owner: str
    repo: str
    number: int
    state: str
    mergeable: str | None
    merge_state_status: str | None
    base_ref: str
    head_sha: str
    labels: frozenset[str]
    changed_files: tuple[str, ...]
    committer_at: datetime


@dataclass(frozen=True)
class RepositoryGateDecision:
    """State-file status and commit status for one repository PR."""

    url: str
    pr_status: str
    cli_status: str
    description: str
    reason: str
    target_url: str | None = None
    skip_status_post: bool = False
    reports: tuple[str, ...] = ()


def decide_repository_pr(
    pull_request: RepositoryPullRequest,
    *,
    tekton_documents: Sequence[str] | None,
    tekton_error: str | None,
    check_runs: Sequence[CheckRun],
    check_query_error: str | None,
    feasibility: FeasibilityRun,
    now: datetime,
    build_app_slug: str,
    build_app_id: int | None = None,
) -> RepositoryGateDecision:
    """Decide the repository PR from mergeability, builds, and feasibility."""
    if pull_request.state == "MERGED":
        return RepositoryGateDecision(
            url=pull_request.url,
            pr_status=PR_STATUS_SUCCESS,
            cli_status="success",
            description=_clip(f"{_where(pull_request)} success: PR already merged"),
            reason="already merged",
            skip_status_post=True,
            reports=(f"success {pull_request.url} already merged",),
        )

    if _is_merge_conflict(pull_request):
        reason = (
            f"merge conflict mergeable={pull_request.mergeable} "
            f"mergeStateStatus={pull_request.merge_state_status}"
        )
        return _decision(
            pull_request,
            pr_status=PR_STATUS_MERGE_FAILURE,
            cli_status="failure",
            reason=reason,
            target_url=pull_request.url,
            reports=(f"merge-failure {pull_request.url} {reason}",),
        )

    deadline = revision_deadline(pull_request.committer_at)
    build_gate = _evaluate_builds(
        pull_request,
        tekton_documents=tekton_documents,
        tekton_error=tekton_error,
        check_runs=check_runs,
        check_query_error=check_query_error,
        now=now,
        deadline=deadline,
        app_slug=build_app_slug,
        app_id=build_app_id,
    )
    feasibility_gate = _evaluate_feasibility(
        feasibility,
        check_runs=() if check_query_error else check_runs,
        check_query_error=check_query_error,
        now=now,
        deadline=deadline,
    )
    return _combine(pull_request, build_gate, feasibility_gate)


def _evaluate_builds(
    pull_request: RepositoryPullRequest,
    *,
    tekton_documents: Sequence[str] | None,
    tekton_error: str | None,
    check_runs: Sequence[CheckRun],
    check_query_error: str | None,
    now: datetime,
    deadline: datetime,
    app_slug: str,
    app_id: int | None,
) -> GateResult:
    if check_query_error or tekton_error:
        return evaluate_checks(
            (),
            (),
            now=now,
            deadline=deadline,
            query_error=check_query_error or tekton_error,
            revision=pull_request.head_sha,
        )
    try:
        expected = select_build_checks(
            tekton_documents or (),
            PullRequestContext(
                number=pull_request.number,
                base_ref=pull_request.base_ref,
                head_sha=pull_request.head_sha,
                labels=pull_request.labels,
                changed_files=pull_request.changed_files,
            ),
            app_slug=app_slug,
            app_id=app_id,
        )
    except TriggerParseError as exc:
        return evaluate_checks(
            (),
            (),
            now=now,
            deadline=deadline,
            query_error=f"could not read PipelineRun configuration: {exc}",
        )
    return evaluate_checks(
        expected,
        check_runs,
        now=now,
        deadline=deadline,
        revision=pull_request.head_sha,
    )


def _evaluate_feasibility(
    feasibility: FeasibilityRun,
    *,
    check_runs: Sequence[CheckRun],
    check_query_error: str | None,
    now: datetime,
    deadline: datetime,
) -> GateResult:
    if check_query_error or feasibility.query_error:
        return evaluate_checks(
            (),
            (),
            now=now,
            deadline=deadline,
            query_error=check_query_error or feasibility.query_error,
            revision=feasibility.head_sha,
        )
    result = evaluate_checks(
        select_feasibility_checks(feasibility),
        check_runs,
        now=now,
        deadline=deadline,
        revision=feasibility.head_sha,
    )
    if result.outcome == OUTCOME_FAIL:
        return result
    status = (feasibility.status or "").lower()
    conclusion = (feasibility.conclusion or "").lower()
    if feasibility.found and status != "completed" and result.outcome == OUTCOME_PASS:
        return _force(
            result,
            outcome=OUTCOME_PENDING,
            reason="feasibility workflow still in progress",
        )
    if (
        feasibility.found
        and status == "completed"
        and conclusion not in ("", "success")
        and result.outcome == OUTCOME_PASS
    ):
        return _force(
            result,
            outcome=OUTCOME_FAIL,
            reason=f"feasibility workflow concluded {conclusion or 'without success'}",
        )
    return result


def _combine(
    pull_request: RepositoryPullRequest,
    builds: GateResult,
    feasibility: GateResult,
) -> RepositoryGateDecision:
    """Feasibility failure stays merge-failure even when every build succeeded."""
    if feasibility.outcome == OUTCOME_FAIL:
        decision = _from_gate(
            pull_request, PR_STATUS_MERGE_FAILURE, "failure", feasibility, "feasibility"
        )
        if builds.outcome != OUTCOME_FAIL:
            return decision
        build_reports = tuple(
            _report_line(PR_STATUS_BUILD_FAILURE, pull_request.url, finding)
            for finding in builds.findings
            if finding.outcome == OUTCOME_FAIL
        )
        return RepositoryGateDecision(
            url=decision.url,
            pr_status=decision.pr_status,
            cli_status=decision.cli_status,
            description=decision.description,
            reason=decision.reason,
            target_url=decision.target_url,
            reports=decision.reports + build_reports,
        )
    if builds.outcome == OUTCOME_FAIL:
        return _from_gate(pull_request, PR_STATUS_BUILD_FAILURE, "failure", builds, "build")
    if feasibility.outcome == OUTCOME_PENDING or builds.outcome == OUTCOME_PENDING:
        pending = _pending_result(builds, feasibility)
        return _from_gate(pull_request, PR_STATUS_BUILD_PENDING, "pending", pending, "pending")
    if not _has_build_expectation(builds):
        reason = "no component builds expected; feasibility checks passed"
    else:
        reason = "component builds and feasibility checks passed"
    return _decision(
        pull_request,
        pr_status=PR_STATUS_SUCCESS,
        cli_status="success",
        reason=reason,
        target_url=feasibility.link or builds.link,
        reports=(f"success {pull_request.url} {reason}",),
    )


def _from_gate(
    pull_request: RepositoryPullRequest,
    pr_status: str,
    cli_status: str,
    gate: GateResult,
    label: str,
) -> RepositoryGateDecision:
    reason = _gate_reason(gate)
    reports = tuple(
        _report_line(pr_status, pull_request.url, finding)
        for finding in gate.findings
        if finding.outcome != OUTCOME_PASS
    ) or (f"{pr_status} {pull_request.url} {label}: {reason}",)
    return _decision(
        pull_request,
        pr_status=pr_status,
        cli_status=cli_status,
        reason=reason,
        target_url=gate.link,
        reports=reports,
    )


def _decision(
    pull_request: RepositoryPullRequest,
    *,
    pr_status: str,
    cli_status: str,
    reason: str,
    target_url: str | None,
    reports: tuple[str, ...],
) -> RepositoryGateDecision:
    description = _clip(f"{_where(pull_request)} {pr_status}: {reason}")
    url = target_url if target_url and target_url.startswith(("http://", "https://")) else None
    return RepositoryGateDecision(
        url=pull_request.url,
        pr_status=pr_status,
        cli_status=cli_status,
        description=description,
        reason=reason,
        target_url=url,
        reports=reports,
    )


def _pending_result(builds: GateResult, feasibility: GateResult) -> GateResult:
    findings = tuple(
        finding
        for finding in (*builds.findings, *feasibility.findings)
        if finding.outcome == OUTCOME_PENDING
    )
    link = next((finding.link for finding in findings if finding.link), None)
    return GateResult(
        outcome=OUTCOME_PENDING,
        findings=findings,
        timed_out=False,
        link=link,
    )


def _force(result: GateResult, *, outcome: str, reason: str) -> GateResult:
    finding = CheckFinding(
        key="main-to-release feasibility",
        outcome=outcome,
        reason=reason,
        link=result.link,
        name="Main-to-release feasibility",
    )
    return GateResult(
        outcome=outcome,
        findings=result.findings + (finding,),
        timed_out=False,
        link=result.link,
    )


def _has_build_expectation(builds: GateResult) -> bool:
    return any(finding.key != "check-query" for finding in builds.findings)


def _gate_reason(gate: GateResult) -> str:
    notable = [finding for finding in gate.findings if finding.outcome != OUTCOME_PASS]
    if not notable:
        return "checks passed"
    first = notable[0]
    extra = f" (+{len(notable) - 1} more)" if len(notable) > 1 else ""
    return f"{first.key} {first.reason}{extra}"


def _report_line(pr_status: str, url: str, finding: CheckFinding) -> str:
    link = f" {finding.link}" if finding.link else ""
    revision = f" revision {finding.revision}" if finding.revision else ""
    return f"{pr_status} {url} {finding.key}: {finding.reason}{revision}{link}"


def _where(pull_request: RepositoryPullRequest) -> str:
    return f"{pull_request.repo}#{pull_request.number}"


def _clip(text: str) -> str:
    collapsed = " ".join(text.split())
    limit = MAX_STATUS_DESCRIPTION_LENGTH
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _is_merge_conflict(pull_request: RepositoryPullRequest) -> bool:
    return pull_request.mergeable == "CONFLICTING" or (
        pull_request.merge_state_status or ""
    ) in _CONFLICT_MERGE_STATES
