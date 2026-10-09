"""Retrieve and evaluate GitHub check runs for a single commit.

Callers choose which checks matter. This module does not assume Konflux build
names or feasibility job names. A missing check, a query error, or any
completed conclusion other than ``success`` never produces a passing gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from lib.github_cli import GhCommandError

GhRunner = Callable[[Sequence[str]], str]

GATE_TIMEOUT = timedelta(hours=6)

OUTCOME_PASS = "pass"
OUTCOME_PENDING = "pending"
OUTCOME_FAIL = "fail"

# Only a completed check with this conclusion passes a gate.
PASSING_CONCLUSION = "success"


class CheckQueryError(RuntimeError):
    """GitHub check retrieval failed. Callers must not treat this as success."""


@dataclass(frozen=True)
class CheckRun:
    """One GitHub check run on a commit."""

    id: int
    name: str
    status: str
    conclusion: str | None
    head_sha: str
    html_url: str | None
    details_url: str | None
    external_id: str | None
    app_slug: str | None
    app_id: int | None
    started_at: str | None

    @property
    def link(self) -> str | None:
        """Prefer a PipelineRun details URL when GitHub provides one."""
        details = self.details_url or ""
        if "pipelinerun" in details.lower():
            return details
        return self.html_url or self.details_url or None


@dataclass(frozen=True)
class ExpectedCheck:
    """A check the caller requires on the current revision.

    ``name`` matches the full GitHub check-run name. ``name_suffix`` matches
    the pipeline or job portion after ``" / "`` (or the full name when the
    check has no application prefix). Producer filters are optional; when set,
    a check from a different GitHub App does not count.
    """

    key: str
    name: str | None = None
    name_suffix: str | None = None
    app_slug: str | None = None
    app_id: int | None = None


@dataclass(frozen=True)
class CheckFinding:
    """Evaluation of one expected check."""

    key: str
    outcome: str
    reason: str
    link: str | None = None
    revision: str | None = None
    status: str | None = None
    conclusion: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class GateResult:
    """Combined result for one set of expected checks."""

    outcome: str
    findings: tuple[CheckFinding, ...]
    timed_out: bool
    link: str | None


def revision_deadline(committer_at: datetime) -> datetime:
    """Return the fixed deadline for one commit.

    The anchor is that commit's committer timestamp. A new revision has a new
    committer timestamp and therefore a new deadline. Reruns of the same
    commit do not move it, and a later monitor invocation must pass the same
    timestamp rather than "now".
    """
    return _as_utc(committer_at) + GATE_TIMEOUT


def list_check_runs(
    owner: str,
    repo: str,
    sha: str,
    runner: GhRunner,
    *,
    page_size: int = 100,
) -> list[CheckRun]:
    """Return check runs for ``sha``, following GitHub pagination.

    Runs whose ``head_sha`` is a different commit are dropped so an older
    revision cannot satisfy the current one.
    """
    if page_size < 1:
        raise ValueError("page_size must be positive")
    endpoint = f"repos/{owner}/{repo}/commits/{sha}/check-runs"
    try:
        payload = collect_github_pages(runner, endpoint, "check_runs", page_size)
    except (GhCommandError, CheckQueryError) as exc:
        raise CheckQueryError(
            f"failed to list check runs for {owner}/{repo}@{sha}: {exc}"
        ) from exc

    runs: list[CheckRun] = []
    for item in payload:
        if not isinstance(item, dict):
            raise CheckQueryError(
                f"check run payload for {owner}/{repo}@{sha} was not an object"
            )
        run = parse_check_run(item)
        if run.head_sha and run.head_sha != sha:
            continue
        runs.append(run)
    return runs


def parse_check_run(data: dict[str, Any]) -> CheckRun:
    app = data.get("app") if isinstance(data.get("app"), dict) else {}
    app_id = app.get("id")
    return CheckRun(
        id=int(data.get("id") or 0),
        name=str(data.get("name") or ""),
        status=str(data.get("status") or "").lower(),
        conclusion=_optional_text(data.get("conclusion")),
        head_sha=str(data.get("head_sha") or ""),
        html_url=_optional_text(data.get("html_url")),
        details_url=_optional_text(data.get("details_url")),
        external_id=_optional_text(data.get("external_id")),
        app_slug=_optional_text(app.get("slug")),
        app_id=int(app_id) if isinstance(app_id, int) else None,
        started_at=_optional_text(data.get("started_at")),
    )


def matches_expected(expected: ExpectedCheck, run: CheckRun) -> bool:
    if expected.app_slug and run.app_slug != expected.app_slug:
        return False
    if expected.app_id is not None and run.app_id != expected.app_id:
        return False
    if expected.name and run.name != expected.name:
        return False
    if expected.name_suffix:
        portion = run.name.split(" / ")[-1] if " / " in run.name else run.name
        if portion != expected.name_suffix and run.name != expected.name_suffix:
            return False
    if not expected.name and not expected.name_suffix:
        return False
    return True


def latest_matching(expected: ExpectedCheck, runs: Sequence[CheckRun]) -> CheckRun | None:
    """Pick the newest run for this identity.

    An older success must not override a newer rerun, including a rerun that
    is still unfinished or has failed.
    """
    matches = [run for run in runs if matches_expected(expected, run)]
    if not matches:
        return None
    return max(matches, key=lambda run: (run.started_at or "", run.id))


def evaluate_checks(
    expected: Sequence[ExpectedCheck],
    runs: Sequence[CheckRun],
    *,
    now: datetime,
    deadline: datetime | None,
    query_error: str | None = None,
    revision: str | None = None,
) -> GateResult:
    """Evaluate caller-selected checks against the current revision.

    An empty expected set passes only when retrieval succeeded. Query errors
    and missing checks stay unresolved until ``deadline``, then fail. A
    completed non-success conclusion fails immediately. Runs for a different
    ``revision`` are ignored.
    """
    moment = _as_utc(now)
    limit = _as_utc(deadline) if deadline is not None else None
    if revision:
        runs = [run for run in runs if not run.head_sha or run.head_sha == revision]
    if query_error:
        finding = _unresolved_finding(
            key="check-query",
            reason=query_error,
            now=moment,
            deadline=limit,
            name=None,
        )
        return _result((finding,))

    findings = tuple(
        _evaluate_one(item, latest_matching(item, runs), now=moment, deadline=limit)
        for item in expected
    )
    return _result(findings)


def _evaluate_one(
    expected: ExpectedCheck,
    run: CheckRun | None,
    *,
    now: datetime,
    deadline: datetime | None,
) -> CheckFinding:
    if run is None:
        return _unresolved_finding(
            key=expected.key,
            reason="missing",
            now=now,
            deadline=deadline,
            name=expected.name or expected.name_suffix,
        )
    if run.status != "completed":
        state = run.status or "unfinished"
        return _unresolved_finding(
            key=expected.key,
            reason=f"status {state}",
            now=now,
            deadline=deadline,
            name=run.name,
            link=run.link,
            revision=run.head_sha,
            status=run.status,
            conclusion=run.conclusion,
        )
    conclusion = (run.conclusion or "").lower()
    if conclusion == PASSING_CONCLUSION:
        return CheckFinding(
            key=expected.key,
            outcome=OUTCOME_PASS,
            reason="concluded success",
            link=run.link,
            revision=run.head_sha,
            status=run.status,
            conclusion=conclusion,
            name=run.name,
        )
    shown = conclusion or "completed without success"
    return CheckFinding(
        key=expected.key,
        outcome=OUTCOME_FAIL,
        reason=f"concluded {shown}",
        link=run.link,
        revision=run.head_sha,
        status=run.status,
        conclusion=conclusion or None,
        name=run.name,
    )


def _unresolved_finding(
    *,
    key: str,
    reason: str,
    now: datetime,
    deadline: datetime | None,
    name: str | None,
    link: str | None = None,
    revision: str | None = None,
    status: str | None = None,
    conclusion: str | None = None,
) -> CheckFinding:
    if deadline is not None and now >= deadline:
        return CheckFinding(
            key=key,
            outcome=OUTCOME_FAIL,
            reason=f"timed out after 6h; {reason}",
            link=link,
            revision=revision,
            status=status,
            conclusion=conclusion,
            name=name,
        )
    return CheckFinding(
        key=key,
        outcome=OUTCOME_PENDING,
        reason=reason,
        link=link,
        revision=revision,
        status=status,
        conclusion=conclusion,
        name=name,
    )


def _result(findings: tuple[CheckFinding, ...]) -> GateResult:
    if any(item.outcome == OUTCOME_FAIL for item in findings):
        outcome = OUTCOME_FAIL
    elif any(item.outcome == OUTCOME_PENDING for item in findings):
        outcome = OUTCOME_PENDING
    else:
        outcome = OUTCOME_PASS
    timed_out = any(
        item.outcome == OUTCOME_FAIL and item.reason.startswith("timed out after 6h")
        for item in findings
    )
    link = next((item.link for item in findings if item.link and item.outcome != OUTCOME_PASS), None)
    if link is None:
        link = next((item.link for item in findings if item.link), None)
    return GateResult(outcome=outcome, findings=findings, timed_out=timed_out, link=link)


def github_api_json(runner: GhRunner, endpoint: str) -> Any:
    try:
        raw = runner(["api", endpoint])
    except GhCommandError:
        raise
    if raw is None or not str(raw).strip():
        raise CheckQueryError(f"empty response from gh api {endpoint}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckQueryError(f"invalid JSON from gh api {endpoint}: {exc}") from exc


def collect_github_pages(
    runner: GhRunner,
    endpoint: str,
    list_key: str | None,
    page_size: int,
) -> list[Any]:
    page = 1
    items: list[Any] = []
    total: int | None = None
    while page <= 1000:
        payload = github_api_json(runner, _with_page(endpoint, page, page_size))
        if list_key:
            if not isinstance(payload, dict):
                raise CheckQueryError(f"expected an object from {endpoint}")
            batch = payload.get(list_key)
            if not isinstance(batch, list):
                raise CheckQueryError(f"expected '{list_key}' array from {endpoint}")
            if total is None and isinstance(payload.get("total_count"), int):
                total = payload["total_count"]
        else:
            if not isinstance(payload, list):
                raise CheckQueryError(f"expected an array from {endpoint}")
            batch = payload
        items.extend(batch)
        if not batch or len(batch) < page_size:
            break
        if total is not None and len(items) >= total:
            break
        page += 1
    else:
        raise CheckQueryError(f"pagination exceeded 1000 pages for {endpoint}")
    return items


def _with_page(endpoint: str, page: int, page_size: int) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}per_page={page_size}&page={page}"


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
