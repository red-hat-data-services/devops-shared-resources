#!/usr/bin/env python3
"""Best-effort Leader failure/pending/closed table, separate from promotion gates.

Resolve an explicit or scheduled Leader, load its state, and gather relevant
child rows using persisted monitor evidence when requested or existing GitHub
PR/check diagnostics otherwise. Keep reasons paired with build links, then
create/update one comment marked <!-- gap-failure-summary --> rather than
adding a new comment each run.

An optional structured tracking handoff adds component-labelled Jira/Slack
pairs from separately validated registry data. Standalone refresh does not
implicitly acquire notification credentials or look up tickets: absent handoff
means Not checked, lookup failure means Unavailable, never proof of no incident.
Creator-only updates replace their managed section and preserve the build table.

Reporting does not run notification lifecycle actions or determine recovery,
Leader authority or promotion gates. Normal execution can read/update GitHub;
dry-run suppresses comment mutation, not all diagnostic reads. Previous display
text is never notification routing authority.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.leader_lifecycle import retired_outcome
from lib.leader_history import history_state_outcome

from lib.gap_check_selection import DEFAULT_BUILD_APP_SLUG, default_build_producer
from lib.konflux_builds import build_status_source, DEFAULT_UI
from lib.github_check_runs import CheckQueryError, list_check_runs
from lib.pr_status_updater import GhCommandError, parse_pr_url
from scripts import gap_pr_monitor as gpm

GapPrMonitorError = gpm.GapPrMonitorError
_run_gh = gpm._run_gh
checkout_leader_branch = gpm.checkout_leader_branch
list_open_leader_prs = gpm.list_open_leader_prs
load_state = gpm.load_state
resolve_state_file = gpm.resolve_state_file
split_labels_csv = gpm.split_labels_csv
DEFAULT_LEADER_LABEL = gpm.DEFAULT_LEADER_LABEL
ENV_LEADER_PR_URL = gpm.ENV_LEADER_PR_URL
ENV_PR_LABELS = gpm.ENV_PR_LABELS
ENV_STATE_FILE = gpm.ENV_STATE_FILE
ENV_TRIGGER_ID = gpm.ENV_TRIGGER_ID

from lib.creator_outcomes import validate_outcomes

CREATOR_START = "<!-- gap-creator-outcomes -->"
CREATOR_END = "<!-- /gap-creator-outcomes -->"

SUMMARY_MARKER = "<!-- gap-failure-summary -->"
DEFAULT_CHECK_NAME = "gated artifacts promoter"
_CONFORMA_NAME_HINT = "conforma-registry"

REPORT_PR_STATUSES = frozenset(
    {
        "build-failure",
        "merge-failure",
        "build-pending",
        "new",
    }
)

_CLOSE_REASON_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"PRs targeting\s+`?stable`?\s+are not allowed", re.I),
    re.compile(r"not allowed in this repo", re.I),
    re.compile(r"retarget(?:ed|ing)?\s+(?:your\s+)?PR\s+to\s+`?\w+`?", re.I),
    re.compile(r"closed\s+(?:as|because|due to)", re.I),
    re.compile(r"superseded by", re.I),
    re.compile(r"closing (?:this|the) PR", re.I),
)

_PIPE = re.compile(r"\|")


@dataclass(frozen=True)
class BuildCheckLink:
    name: str
    conclusion: str
    url: str


@dataclass(frozen=True)
class ChildFailureRow:
    repo: str
    pr_url: str
    pr_number: int
    state_status: str
    pr_state: str
    merged: bool
    reason: str
    build_links: tuple[BuildCheckLink, ...] = ()


@dataclass(frozen=True)
class ReporterResult:
    leader_pr_url: str
    rows: tuple[ChildFailureRow, ...]
    comment_id: int | None
    created: bool
    updated: bool
    dry_run: bool
    body: str


def utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _clip(text: str, limit: int = 180) -> str:
    one = " ".join((text or "").split())
    if len(one) <= limit:
        return one
    return one[: max(0, limit - 1)].rstrip() + "…"


def _cell(text: str) -> str:
    return _PIPE.sub("\\|", _clip(text, 200))


def extract_close_reason_from_comments(comments: Sequence[dict[str, Any]]) -> str | None:
    scored: list[tuple[int, str]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "").strip()
        if not body or SUMMARY_MARKER in body:
            continue
        for idx, pattern in enumerate(_CLOSE_REASON_PATTERNS):
            match = pattern.search(body)
            if not match:
                continue
            start = max(0, match.start() - 40)
            end = min(len(body), match.end() + 160)
            scored.append((100 - idx, body[start:end]))
            break
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return _clip(scored[0][1], 220)


def list_issue_comments(
    pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> list[dict[str, Any]]:
    run = gh_runner or _run_gh
    ref = parse_pr_url(pr_url)
    endpoint = f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments"
    raw = run(["api", endpoint, "--paginate", "--slurp"])
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(f"Invalid JSON listing comments for {pr_url}: {exc}") from exc
    if isinstance(data, list) and data and all(isinstance(x, list) for x in data):
        out: list[dict[str, Any]] = []
        for page in data:
            out.extend(c for c in page if isinstance(c, dict))
        return out
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    return []


def fetch_pull_request(
    pr_url: str,
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> dict[str, Any]:
    run = gh_runner or _run_gh
    ref = parse_pr_url(pr_url)
    raw = run(
        [
            "pr",
            "view",
            str(ref.number),
            "-R",
            ref.slug,
            "--json",
            "number,url,state,mergedAt,title,headRefOid,closedAt,"
            "baseRefName,headRefName,labels,autoMergeRequest",
        ]
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GapPrMonitorError(f"Invalid JSON for PR view {pr_url}: {exc}") from exc
    if not isinstance(data, dict):
        raise GapPrMonitorError(f"PR view for {pr_url} did not return an object.")
    return data


def fetch_gap_status_description(
    owner: str,
    repo: str,
    head_sha: str,
    *,
    check_name: str = DEFAULT_CHECK_NAME,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> str | None:
    if not head_sha:
        return None
    run = gh_runner or _run_gh
    try:
        raw = run(["api", f"repos/{owner}/{repo}/commits/{head_sha}/status"])
    except GhCommandError as exc:
        print(
            f"WARNING: commit status query failed for {owner}/{repo}@{head_sha[:12]}: {exc}",
            file=sys.stderr,
        )
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for status in data.get("statuses") or []:
        if not isinstance(status, dict):
            continue
        if str(status.get("context") or "") == check_name:
            desc = str(status.get("description") or "").strip()
            return desc or None
    return None


def _is_generic_konflux_ui(url: str) -> bool:
    """True for Konflux home / application-pipeline pages (not a specific run)."""
    lowered = (url or "").strip().lower().rstrip("/")
    if not lowered:
        return True
    if "application-pipeline" in lowered:
        return True
    # Bare UI host with no /ns/.../pipelinerun/... path.
    if "konflux" in lowered and "/pipelinerun/" not in lowered:
        # Allow github.com check pages; those are not konflux UI roots.
        if "github.com" in lowered:
            return False
        return True
    return False


def _check_run_link(check: dict[str, Any]) -> str | None:
    """Prefer a specific PipelineRun URL, else the GitHub check run page.

    Many Konflux checks set ``details_url`` to the Konflux UI home page, which
    is useless for operators. In that case fall back to ``html_url``.
    """
    details = str(check.get("details_url") or "").strip()
    html = str(check.get("html_url") or "").strip()
    if details and "pipelinerun" in details.lower():
        return details
    if html:
        return html
    if details and not _is_generic_konflux_ui(details):
        return details
    return None


def _short_check_label(name: str) -> str:
    parts = [p.strip() for p in (name or "").split("/") if p.strip()]
    label = parts[-1] if parts else (name or "build")
    return _clip(label, 72)


def _is_on_pull_request_check(name: str) -> bool:
    """Leader failure links must be PR PipelineRuns only (never on-push)."""
    return "on-pull-request" in (name or "").lower()


def select_failed_build_checks(
    check_runs: Sequence[dict[str, Any]],
    *,
    app_slug: str = DEFAULT_BUILD_APP_SLUG,
    limit: int = 5,
) -> list[BuildCheckLink]:
    """Keep completed Konflux failures whose name includes on-pull-request."""
    candidates: list[BuildCheckLink] = []
    seen_names: set[str] = set()
    for check in check_runs:
        if not isinstance(check, dict):
            continue
        app = check.get("app") if isinstance(check.get("app"), dict) else {}
        if str(app.get("slug") or "") != app_slug:
            continue
        status = str(check.get("status") or "").lower()
        if status and status != "completed":
            continue
        conclusion = str(check.get("conclusion") or "").lower()
        if conclusion != "failure":
            continue
        name = str(check.get("name") or "").strip() or "unnamed-check"
        if _CONFORMA_NAME_HINT in name.lower():
            continue
        if not _is_on_pull_request_check(name):
            continue
        url = _check_run_link(check)
        if not url:
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        candidates.append(BuildCheckLink(name=name, conclusion=conclusion, url=url))

    candidates.sort(key=lambda link: link.name.lower())
    return candidates[: max(0, limit)]


def format_build_failure_reason(
    repo: str,
    pr_number: int,
    links: Sequence[BuildCheckLink],
) -> str:
    """Reason text that matches the Build links column (same names and count)."""
    prefix = f"{repo}#{pr_number} build-failure"
    if not links:
        return prefix
    first = _short_check_label(links[0].name)
    conc = links[0].conclusion or "failure"
    text = f"{prefix}: {first} concluded {conc}"
    extra = len(links) - 1
    if extra > 0:
        text += f" (+{extra} more)"
    return text


# Unexpanded Actions matrix placeholder — appears when main-release-feasibility
# skips the job (if: false), typically because main-to-release automerge is off.
_UNEXPANDED_MATRIX_PLACEHOLDER = re.compile(r"\$\{\{\s*matrix\.")

# Comment-only rewrite: keep merge-failure, make the reason human-readable.
_FEASIBILITY_AUTOMERGE_DISABLED_REASON = (
    "main-to-release feasibility failed — automerge disabled for this component "
    "(enable main-to-release automerge; feasibility job was skipped)"
)


def humanize_merge_failure_reason(reason: str) -> str:
    """Rewrite unexpanded feasibility skip noise for the Leader failure comment.

    When automerge is disabled, GitHub reports the skipped job name as
    ``main-release-feasibility (${{ matrix.downstream_branch }}) concluded skipped``.
    Keep ``merge-failure`` and state clearly that main-to-release feasibility
    failed because automerge is disabled (Chris / Deepak policy: mandatory).
    """
    text = (reason or "").strip()
    if not text:
        return text
    lower = text.lower()
    if "feasibility" not in lower:
        return text
    if not (
        _UNEXPANDED_MATRIX_PLACEHOLDER.search(text)
        or "matrix.downstream_branch" in lower
    ):
        return text
    if lower.startswith("merge-failure"):
        return f"merge-failure: {_FEASIBILITY_AUTOMERGE_DISABLED_REASON}"
    return _FEASIBILITY_AUTOMERGE_DISABLED_REASON


def fetch_failed_build_check_links(
    owner: str,
    repo: str,
    head_sha: str,
    *,
    pr: dict[str, Any] | None = None,
    app_slug: str | None = None,
    app_id: int | None = None,
    limit: int = 5,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> tuple[BuildCheckLink, ...]:
    """Failed on-pull-request Konflux checks on this SHA (GitHub Checks only).

    ``pr`` / ``app_id`` are accepted for call-site compatibility; selection does
    not use ``.tekton`` or CEL. Link a check when it is completed, conclusion
    is failure, and the name contains ``on-pull-request``.
    """
    del pr, app_id  # unused; kept so existing call sites stay valid
    if not head_sha:
        return ()
    run = gh_runner or _run_gh
    slug = app_slug
    if slug is None:
        slug, _ = default_build_producer()

    try:
        check_runs = list_check_runs(owner, repo, head_sha, run)
    except CheckQueryError as exc:
        print(
            f"WARNING: check-runs query failed for {owner}/{repo}@{head_sha[:12]}: {exc}",
            file=sys.stderr,
        )
        return ()

    raw_runs = [
        {
            "name": c.name,
            "status": c.status,
            "conclusion": c.conclusion,
            "details_url": c.details_url,
            "html_url": c.html_url,
            "app": {"slug": c.app_slug},
        }
        for c in check_runs
    ]
    return tuple(select_failed_build_checks(raw_runs, app_slug=slug, limit=limit))


def format_build_links_cell(links: Sequence[BuildCheckLink]) -> str:
    if not links:
        return "—"
    return "<br>".join(
        f"[{_short_check_label(link.name)}]({link.url})" for link in links
    )


def should_report_child(*, state_status: str, pr_state: str) -> bool:
    if (pr_state or "").lower() == "closed":
        return True
    return (state_status or "").strip() in REPORT_PR_STATUSES


def build_child_row(
    entry: dict[str, Any],
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    reuse_monitor_evidence: bool = False,
) -> ChildFailureRow | None:
    repo = str(entry.get("repo") or "").strip()
    pr_url = str(entry.get("pr-url") or entry.get("pr_url") or "").strip()
    state_status = str(entry.get("pr-status") or entry.get("pr_status") or "").strip()
    if not repo or not pr_url:
        return None
    if state_status in {"success", "build-success"}:
        return None

    ref = parse_pr_url(pr_url)
    evidence = entry.get("build-observations")
    konflux_evidence = isinstance(evidence, dict) and evidence.get("source") == "konflux"
    reusable = (
        reuse_monitor_evidence and konflux_evidence
        and evidence.get("repository", "").lower() == f"{ref.owner}/{ref.repo}".lower()
        and str(evidence.get("pr", "")) == str(ref.number)
        and evidence.get("pr-state") in {"OPEN", "CLOSED", "MERGED"}
        and re.fullmatch(r"[0-9a-f]{40}", str(evidence.get("head-sha", "")))
    )
    if reusable:
        # This invocation follows the monitor's publish/head guard. Render its
        # fresh receipt rather than scanning the same child PRs a second time.
        pr = {"state": evidence["pr-state"], "number": ref.number,
              "headRefOid": evidence["head-sha"],
              "mergedAt": evidence["pr-state"] == "MERGED"}
    else:
        # Standalone reports still check the current PR head before displaying
        # a persisted receipt, so old-head failures are not misrepresented.
        pr = fetch_pull_request(pr_url, gh_runner=gh_runner)
    pr_state = str(pr.get("state") or "").lower() or "unknown"
    merged = bool(pr.get("mergedAt"))
    number = int(pr.get("number") or parse_pr_url(pr_url).number)
    head_sha = str(pr.get("headRefOid") or "").strip()
    ref = parse_pr_url(pr_url)

    if not should_report_child(state_status=state_status, pr_state=pr_state):
        return None

    gap_desc = ""
    if reusable and state_status == "merge-failure":
        gap_desc = str(evidence.get("gate-reason") or "")
    elif state_status == "merge-failure" or not (konflux_evidence or build_status_source() == "konflux"):
        gap_desc = fetch_gap_status_description(ref.owner, ref.repo, head_sha, gh_runner=gh_runner)
    close_reason = None
    if pr_state == "closed":
        comments = list_issue_comments(pr_url, gh_runner=gh_runner)
        close_reason = extract_close_reason_from_comments(comments)

    # Build links only for build-failure. merge-failure / build-pending / new:
    # reason text only — never attach push or unrelated PR build links.
    status_key = (state_status or "").strip()
    evidence_reason = None
    if konflux_evidence or build_status_source() == "konflux":
        build_links = ()
        if (not konflux_evidence or evidence.get("head-sha") != head_sha
                or evidence.get("repository", "").lower() != f"{ref.owner}/{ref.repo}".lower()
                or str(evidence.get("pr", "")) != str(number)):
            evidence_reason = "Konflux evidence is missing or belongs to an older head; awaiting monitor refresh"
            if status_key != "merge-failure":
                status_key = state_status = "build-pending"
        else:
            if evidence.get("query-error"):
                evidence_reason = "Konflux lookup unavailable: " + _clip(str(evidence["query-error"]))
            elif status_key in {"build-pending", "new"}:
                pending = [str(r.get("component", "?")) + ": " + (str(r["retry"].get("status", "unknown")) + ": " + str(r["retry"].get("reason", ""))
                            if r.get("retry") else str(r.get("status", "unknown")))
                           for r in evidence.get("runs", []) if isinstance(r, dict) and r.get("status") != "success"]
                evidence_reason = "Konflux: " + "; ".join(pending) if pending else "Awaiting monitor refresh"
            if status_key == "build-failure":
                from urllib.parse import urlsplit
                expected_host = urlsplit(os.environ.get("KONFLUX_UI_URL", DEFAULT_UI)).netloc
                verified = []
                for record in evidence.get("runs", []):
                    if not isinstance(record, dict) or record.get("status") not in {"failure", "timed_out", "cancelled"}:
                        continue
                    if record.get("head_sha") != head_sha:
                        continue
                    link = str(record.get("url", ""))
                    parsed = urlsplit(link)
                    if (parsed.scheme == "https" and parsed.netloc == expected_host
                            and re.fullmatch(r"/ns/[a-z0-9.-]+/pipelinerun/[a-z0-9.-]+", parsed.path)
                            and not parsed.query and not parsed.fragment):
                        verified.append(BuildCheckLink(name=str(record.get("name") or record.get("component")),
                                                       conclusion=record["status"], url=link))
                build_links = tuple(verified)
                retry_details = [
                    str(record.get("component", "?")) + ": " + str(record["retry"].get("status", "unknown"))
                    + ": " + str(record["retry"].get("reason", ""))
                    for record in evidence.get("runs", [])
                    if isinstance(record, dict) and isinstance(record.get("retry"), dict)
                    and record.get("status") != "success"
                ]
                if retry_details:
                    evidence_reason = "; ".join(filter(None, [evidence_reason, *retry_details]))
    elif status_key in {"merge-failure", "build-pending", "new"}:
        build_links: tuple[BuildCheckLink, ...] = ()
    else:
        build_links = fetch_failed_build_check_links(
            ref.owner,
            ref.repo,
            head_sha,
            pr=pr,
            limit=20,
            gh_runner=gh_runner,
        )

    reason_parts: list[str] = []
    ignored = entry.get("ignore") is True
    if ignored:
        reason_parts.append("IGNORED (excluded from Early Gate gating)")
    if pr_state == "closed":
        reason_parts.append("CLOSED" + (" (merged)" if merged else " (not merged)"))
        if close_reason:
            reason_parts.append(close_reason)
    if status_key in {"build-pending", "new"}:
        reason_parts.append(evidence_reason or "builds pending")
    elif status_key == "merge-failure":
        reason_parts.append(
            humanize_merge_failure_reason(gap_desc or f"{repo}#{number} merge-failure")
        )
    elif status_key == "build-failure" or (
        pr_state == "closed"
        and status_key not in {"merge-failure", "build-pending", "new"}
    ):
        # Reason names/count must match Build links (not the Stage-1 status blurb).
        if build_links or status_key == "build-failure":
            reason_parts.append(
                format_build_failure_reason(repo, number, build_links)
            )
        elif gap_desc:
            reason_parts.append(gap_desc)
        else:
            reason_parts.append(f"{repo}#{number} build-failure")
    elif gap_desc:
        reason_parts.append(gap_desc)
    elif state_status:
        reason_parts.append(f"state pr-status={state_status}")

    if evidence_reason and status_key not in {"build-pending", "new"}:
        reason_parts.append(evidence_reason)
    reason = " — ".join(reason_parts) if reason_parts else state_status or pr_state
    status_label = state_status or "unknown"
    if ignored:
        status_label = f"{status_label} (ignored)"
    return ChildFailureRow(
        repo=repo,
        pr_url=pr_url,
        pr_number=number,
        state_status=status_label,
        pr_state=pr_state,
        merged=merged,
        reason=reason,
        build_links=build_links,
    )


def render_creator_outcomes(outcomes: Sequence[dict[str, str]]) -> str:
    records = [item for item in validate_outcomes(list(outcomes)) if item["outcome"] != "success"]
    def creator_cell(value):
        return html.escape(_cell(value), quote=False)
    lines = [CREATOR_START, "### Repository creation / auto-merge reports", "",
             "These creator observations are separate from component build failures.", ""]
    if not records:
        lines.append("No outstanding repository creation or auto-merge warnings recorded.")
    else:
        lines.extend(["| Repository | Source → target | Category | Child PR | Reason / action | Creator run | Observed at |",
                      "| --- | --- | --- | --- | --- | --- | --- |"])
        for item in records:
            repo = item["repo"]
            pr = item.get("pr-url", "")
            pr_cell = f"[#{pr.rsplit('/', 1)[-1]}]({pr})" if pr else "—"
            run = item.get("workflow-url", "")
            lines.append("| " + " | ".join([
                f"[{repo}](https://github.com/{repo})",
                creator_cell(item["source-branch"] + " → " + item["target-branch"]),
                creator_cell(item["outcome"]), pr_cell, creator_cell(item["reason"]),
                f"[Workflow run]({run})" if run else "—", creator_cell(item["observed-at"]),
            ]) + " |")
    lines.extend(["", CREATOR_END])
    return "\n".join(lines)


def current_creator_outcomes(outcomes, *, gh_runner=None):
    """Keep unresolved evidence on lookup errors; merged PRs need no manual action."""
    records = validate_outcomes(outcomes)
    result = []
    cache = {}
    for item in records:
        if item["outcome"] != "success" and item.get("pr-url"):
            url = item["pr-url"]
            if url not in cache:
                try:
                    cache[url] = fetch_pull_request(url, gh_runner=gh_runner)
                except (GapPrMonitorError, GhCommandError, ValueError):
                    cache[url] = None
            pr = cache[url]
            if pr is None:
                item = dict(item, reason=item["reason"] + " (Current PR lookup unavailable; recorded observation retained.)")
            if pr and (pr.get("mergedAt") or (
                    item["outcome"] == "automerge-unavailable" and pr.get("autoMergeRequest"))):
                continue
        result.append(item)
    return result


def publish_creator_outcomes(leader_pr_url, outcomes, *, dry_run=False, gh_runner=None):
    """Update only the creator section; preserve #45's current build summary."""
    run = gh_runner or _run_gh
    section = render_creator_outcomes(outcomes)
    comments = list_issue_comments(leader_pr_url, gh_runner=run)
    existing = find_summary_comment(comments)
    if existing:
        body = str(existing.get("body") or "")
        if CREATOR_START in body:
            start = body.index(CREATOR_START)
            end = body.find(CREATOR_END, start)
            if end < 0:
                raise GapPrMonitorError("Creator report section is incomplete; refusing to overwrite the summary")
            body = body[:start] + section + body[end + len(CREATOR_END):]
        else:
            body = body.rstrip() + "\n\n" + section + "\n"
    else:
        body = (SUMMARY_MARKER + "\n## GAP repository reports\n\n" + section
                + "\n\nComponent build status will be refreshed by the failure reporter.\n")
    return upsert_leader_summary_comment(
        leader_pr_url, body, dry_run=dry_run, gh_runner=run, comments=comments
    )


def render_summary_markdown(
    rows: Sequence[ChildFailureRow],
    *,
    leader_pr_url: str,
    generated_at: str | None = None,
    creator_outcomes: Sequence[dict[str, str]] = (),
    tracking: dict | None = None,
) -> str:
    from lib.gap_notifications.lookup_tools import tracking_cell
    stamp = generated_at or utc_now_stamp()
    lines = [
        SUMMARY_MARKER,
        "## GAP failure / pending / closed summary",
        "",
        f"_Updated: {stamp}_",
        f"_Leader: {leader_pr_url}_",
        "",
        "This comment is maintained by `gap-failure-reporter` "
        "(one comment, updated each run).",
        "",
        "Source: the latest child-monitoring observations in `state.json`.",
        "",
        "Konflux-enabled entries use the monitor's persisted current-head PipelineRun evidence "
        "for failure reasons and links, including both PR pipeline naming conventions. "
        "Legacy entries use GitHub check diagnostics until migrated. "
        "``merge-failure`` / ``build-pending`` rows leave Build links blank.",
        "",
    ]
    if creator_outcomes:
        lines.extend([render_creator_outcomes(creator_outcomes), ""])
    if not rows:
        lines.extend(
            [
                "No failing, pending, or closed child PRs in scope right now.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "| Repo | PR | State status | PR state | Reason | Build links | Jira / Slack |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        pr_link = f"[#{row.pr_number}]({row.pr_url})"
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row.repo),
                    pr_link,
                    _cell(row.state_status),
                    _cell(row.pr_state + ("/merged" if row.merged else "")),
                    _cell(row.reason),
                    format_build_links_cell(row.build_links),
                    tracking_cell(tracking, row.pr_url),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def find_summary_comment(
    comments: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if SUMMARY_MARKER in str(comment.get("body") or ""):
            return comment
    return None


def _gh_json_input(
    run: Callable[[Sequence[str]], str],
    args: Sequence[str],
    payload: dict[str, Any],
) -> str:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        json.dump(payload, handle)
        path = handle.name
    try:
        return run([*args, "--input", path])
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def upsert_leader_summary_comment(
    leader_pr_url: str,
    body: str,
    *,
    dry_run: bool = False,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    comments: Sequence[dict[str, Any]] | None = None,
) -> tuple[int | None, bool, bool]:
    run = gh_runner or _run_gh
    ref = parse_pr_url(leader_pr_url)
    if comments is None:
        comments = list_issue_comments(leader_pr_url, gh_runner=run)
    existing = find_summary_comment(comments)

    if dry_run:
        if existing and existing.get("id") is not None:
            print(f"[dry-run] would update comment id={existing['id']} on {leader_pr_url}")
            return int(existing["id"]), False, True
        print(f"[dry-run] would create summary comment on {leader_pr_url}")
        return None, True, False

    payload = {"body": body}
    if existing and existing.get("id") is not None:
        comment_id = int(existing["id"])
        _gh_json_input(
            run,
            [
                "api",
                "-X",
                "PATCH",
                f"repos/{ref.owner}/{ref.repo}/issues/comments/{comment_id}",
            ],
            payload,
        )
        return comment_id, False, True

    raw = _gh_json_input(
        run,
        [
            "api",
            "-X",
            "POST",
            f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments",
        ],
        payload,
    )
    try:
        created = json.loads(raw or "{}")
        comment_id = (
            int(created["id"]) if isinstance(created, dict) and "id" in created else None
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        comment_id = None
    return comment_id, True, False


def collect_rows_from_state(
    payload: dict[str, Any],
    *,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    continue_on_error: bool = True,
    reuse_monitor_evidence: bool = False,
) -> list[ChildFailureRow]:
    entries = payload.get("pull-requests")
    if not isinstance(entries, list):
        raise GapPrMonitorError("state.json missing pull-requests list")
    rows: list[ChildFailureRow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            options = {"reuse_monitor_evidence": True} if reuse_monitor_evidence else {}
            row = build_child_row(entry, gh_runner=gh_runner, **options)
        except (GapPrMonitorError, GhCommandError, ValueError) as exc:
            if not continue_on_error:
                raise
            repo = str(entry.get("repo") or "?")
            pr_url = str(entry.get("pr-url") or entry.get("pr_url") or "")
            print(f"WARNING: skipped {repo}: {exc}", file=sys.stderr)
            if pr_url:
                rows.append(
                    ChildFailureRow(
                        repo=repo,
                        pr_url=pr_url,
                        pr_number=0,
                        state_status=str(entry.get("pr-status") or ""),
                        pr_state="unknown",
                        merged=False,
                        reason=f"reporter error: {_clip(str(exc))}",
                        build_links=(),
                    )
                )
            continue
        if row is not None:
            rows.append(row)
    order = {"build-failure": 0, "merge-failure": 1, "build-pending": 2, "new": 3}
    rows.sort(
        key=lambda r: (
            0 if r.pr_state == "closed" and r.state_status not in order else 1,
            order.get(r.state_status, 9),
            r.repo.lower(),
        )
    )
    return rows


def report_for_leader(
    *,
    leader_pr_url: str,
    repo_root: Path,
    head_ref: str | None = None,
    state_file: str | None = None,
    trigger_id: str | None = None,
    labels: Sequence[str] | None = None,
    dry_run: bool = False,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    git_runner: Callable[..., str] | None = None,
    reuse_monitor_evidence: bool = False,
    tracking: dict | None = None,
    allow_archived: bool = False,
) -> ReporterResult:
    """Update the maintained summary, preserving finalized records unless explicitly requested."""
    run = gh_runner or _run_gh
    if head_ref:
        checkout_leader_branch(repo_root, head_ref, git_runner=git_runner)

    path = resolve_state_file(
        state_file=state_file,
        trigger_id=trigger_id,
        labels=list(labels or []),
        root=repo_root,
        allow_gap_dir_fallback=True,
    )
    payload = load_state(path)
    if retired_outcome(payload) or (history_state_outcome(path) and not allow_archived):
        return ReporterResult(
            leader_pr_url, (), None, False, False, dry_run,
            "Retired Leader: preserving the recorded failure summary.",
        )
    options = {"reuse_monitor_evidence": True} if reuse_monitor_evidence else {}
    rows = collect_rows_from_state(payload, gh_runner=run, **options)
    try:
        creator_outcomes = current_creator_outcomes(payload.get("creator-outcomes", []), gh_runner=run)
    except ValueError:
        # Optional creator diagnostics must not disable this build table or
        # abort reporting for unrelated Leaders. Do not log the invalid payload.
        print(f"WARNING: ignoring invalid creator-outcomes in {path}; creator section omitted.", file=sys.stderr)
        creator_outcomes = []
    body = render_summary_markdown(rows, leader_pr_url=leader_pr_url,
                                   creator_outcomes=creator_outcomes, tracking=tracking)
    comment_id, created, updated = upsert_leader_summary_comment(
        leader_pr_url, body, dry_run=dry_run, gh_runner=run
    )
    return ReporterResult(
        leader_pr_url=leader_pr_url,
        rows=tuple(rows),
        comment_id=comment_id,
        created=created,
        updated=updated,
        dry_run=dry_run,
        body=body,
    )


def run_scheduled_reporters(
    *,
    repo_root: Path,
    leader_repo: str,
    label: str = DEFAULT_LEADER_LABEL,
    dry_run: bool = False,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
    git_runner: Callable[..., str] | None = None,
) -> list[ReporterResult]:
    leaders = list_open_leader_prs(leader_repo, label=label, gh_runner=gh_runner)
    results: list[ReporterResult] = []
    for leader in leaders:
        print(f"Failure reporter: Leader #{leader.number} {leader.url}")
        try:
            result = report_for_leader(
                leader_pr_url=leader.url,
                repo_root=repo_root,
                head_ref=leader.head_ref,
                labels=leader.labels,
                dry_run=dry_run,
                gh_runner=gh_runner,
                git_runner=git_runner,
            )
            results.append(result)
            print(
                f"  rows={len(result.rows)} created={result.created} "
                f"updated={result.updated} dry_run={result.dry_run}"
            )
        except (GapPrMonitorError, GhCommandError) as exc:
            print(f"ERROR: Leader #{leader.number}: {exc}", file=sys.stderr)
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upsert GAP failure/pending/closed summary comment on Leader PR(s)."
    )
    parser.add_argument("--repo-root", default=".", help="Checkout of gated-artifacts-promoter")
    parser.add_argument("--leader-pr-url", default=None)
    parser.add_argument("--leader-repo", default=None)
    parser.add_argument("--leader-label", default=DEFAULT_LEADER_LABEL)
    parser.add_argument("--head-ref", default=None)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--trigger-id", default=None)
    parser.add_argument("--schedule", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    leader_url = (args.leader_pr_url or os.environ.get(ENV_LEADER_PR_URL) or "").strip()
    state_file = args.state_file or os.environ.get(ENV_STATE_FILE) or None
    trigger_id = args.trigger_id or os.environ.get(ENV_TRIGGER_ID) or None
    labels = split_labels_csv(os.environ.get(ENV_PR_LABELS) or "")
    schedule = args.schedule or os.environ.get("GITHUB_EVENT_NAME", "").strip() == "schedule"

    try:
        if schedule:
            leader_repo = (
                (args.leader_repo or "").strip()
                or os.environ.get("GAP_LEADER_REPO", "").strip()
                or os.environ.get("GITHUB_REPOSITORY", "").strip()
            )
            if not leader_repo:
                raise GapPrMonitorError(
                    "Schedule mode needs --leader-repo or GITHUB_REPOSITORY"
                )
            results = run_scheduled_reporters(
                repo_root=repo_root,
                leader_repo=leader_repo,
                label=args.leader_label,
                dry_run=args.dry_run,
            )
            print(f"failure_reporter_leaders={len(results)}")
            return 0

        if not leader_url:
            raise GapPrMonitorError("Provide --leader-pr-url or GAP_LEADER_PR_URL")

        head_ref = (args.head_ref or os.environ.get("LEADER_HEAD_REF") or "").strip() or None
        result = report_for_leader(
            leader_pr_url=leader_url,
            repo_root=repo_root,
            head_ref=head_ref,
            state_file=state_file,
            trigger_id=trigger_id,
            labels=labels,
            dry_run=args.dry_run,
        )
        print(f"failure_reporter_rows={len(result.rows)}")
        print(f"failure_reporter_created={result.created}")
        print(f"failure_reporter_updated={result.updated}")
        if result.comment_id is not None:
            print(f"failure_reporter_comment_id={result.comment_id}")
        return 0
    except (GapPrMonitorError, GhCommandError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
