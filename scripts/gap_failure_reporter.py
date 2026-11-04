#!/usr/bin/env python3
"""GAP failure reporter — upsert a Leader summary table for failing/closed children.

Separate from Stage-1 build gating. Each run (hourly schedule or dispatch):

1. Resolve open Leader PR(s) (or an explicit ``--leader-pr-url``).
2. Read that Leader's ``state.json``.
3. For build-failure / merge-failure / build-pending / closed children, gather
   GitHub PR state, GAP commit-status description, comment-derived close
   reasons, and failed Konflux checks selected like Stage-1
   (``select_build_checks``; PipelineRun / check URL; no cluster token).
4. Upsert **one** Leader issue comment marked ``<!-- gap-failure-summary -->``
   (create once, then PATCH the same comment) so the thread stays tidy.
"""

from __future__ import annotations

import argparse
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

from lib.gap_check_selection import (
    DEFAULT_BUILD_APP_SLUG,
    PullRequestContext,
    default_build_producer,
    fetch_changed_files,
    fetch_tekton_documents,
    select_build_checks,
)
from lib.github_check_runs import (
    CheckQueryError,
    CheckRun,
    latest_matching,
    list_check_runs,
)
from lib.pac_cel import TriggerParseError
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

_FAILED_CHECK_CONCLUSIONS = frozenset(
    {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
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
            "baseRefName,headRefName,labels",
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


def _check_priority(link: BuildCheckLink) -> tuple[int, int, str]:
    """Lower tuple sorts first: PR PipelineRuns before push/noise."""
    name = link.name.lower()
    url = link.url.lower()
    if _CONFORMA_NAME_HINT in name:
        bucket = 3
    elif "on-push" in name and "on-pull-request" not in name:
        bucket = 2
    elif "on-pull-request" in name or "/pipelinerun/" in url:
        bucket = 0
    else:
        bucket = 1
    # Prefer failure over cancelled within a bucket.
    conc = 0 if link.conclusion == "failure" else 1
    return (bucket, conc, name)


def select_failed_build_checks(
    check_runs: Sequence[dict[str, Any]],
    *,
    app_slug: str = DEFAULT_BUILD_APP_SLUG,
    limit: int = 5,
) -> list[BuildCheckLink]:
    candidates: list[BuildCheckLink] = []
    for check in check_runs:
        if not isinstance(check, dict):
            continue
        app = check.get("app") if isinstance(check.get("app"), dict) else {}
        if str(app.get("slug") or "") != app_slug:
            continue
        conclusion = str(check.get("conclusion") or "").lower()
        if conclusion not in _FAILED_CHECK_CONCLUSIONS:
            continue
        name = str(check.get("name") or "").strip() or "unnamed-check"
        # Skip conforma / registry noise; PR build failures matter for GAP.
        if _CONFORMA_NAME_HINT in name.lower():
            continue
        url = _check_run_link(check)
        if not url:
            continue
        candidates.append(BuildCheckLink(name=name, conclusion=conclusion, url=url))

    candidates.sort(key=_check_priority)
    # Drop bare on-push entries when an on-pull-request link exists for the row.
    has_pr_build = any("on-pull-request" in c.name.lower() for c in candidates)
    if has_pr_build:
        candidates = [
            c
            for c in candidates
            if not (
                "on-push" in c.name.lower() and "on-pull-request" not in c.name.lower()
            )
        ]
    return candidates[: max(0, limit)]


def _labels_from_pr_view(pr: dict[str, Any]) -> frozenset[str]:
    labels: set[str] = set()
    raw = pr.get("labels") or []
    if not isinstance(raw, list):
        return frozenset()
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name:
            labels.add(name)
    return frozenset(labels)


def _pr_context_for_builds(
    owner: str,
    repo: str,
    pr: dict[str, Any],
    *,
    gh_runner: Callable[[Sequence[str]], str],
) -> PullRequestContext:
    """Build the PR facts Stage-1 uses for PipelineRun selection."""
    number = int(pr.get("number") or 0)
    head_sha = str(pr.get("headRefOid") or "").strip()
    base_ref = str(pr.get("baseRefName") or "").strip() or "main"
    source_branch = str(pr.get("headRefName") or "").strip() or None
    title = str(pr.get("title") or "").strip() or None
    changed: tuple[str, ...] = ()
    try:
        changed = fetch_changed_files(owner, repo, number, gh_runner)
    except (CheckQueryError, GhCommandError, TriggerParseError) as exc:
        print(
            f"WARNING: changed-files query failed for {owner}/{repo}#{number}: {exc}",
            file=sys.stderr,
        )
    return PullRequestContext(
        number=number,
        base_ref=base_ref,
        head_sha=head_sha,
        labels=_labels_from_pr_view(pr),
        changed_files=changed,
        source_branch=source_branch,
        title=title,
        target_url=str(pr.get("url") or "") or None,
    )


def _link_from_check_run(run: CheckRun) -> str | None:
    """Prefer PipelineRun details_url; else GitHub check html_url (same as CheckRun.link)."""
    return run.link


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
    """Return failed checks Stage-1 expects for this child PR.

    Uses ``select_build_checks`` + tekton PipelineRun YAML (same as
    ``gap_pr_monitor``) so the summary links the builds that actually gate
    the PR — not every Konflux check on the SHA. Falls back to a
    Konflux-app filter when tekton selection is unavailable.
    """
    if not head_sha:
        return ()
    run = gh_runner or _run_gh
    slug = app_slug
    aid = app_id
    if slug is None:
        slug, aid = default_build_producer()

    try:
        check_runs = list_check_runs(owner, repo, head_sha, run)
    except CheckQueryError as exc:
        print(
            f"WARNING: check-runs query failed for {owner}/{repo}@{head_sha[:12]}: {exc}",
            file=sys.stderr,
        )
        return ()

    expected_links: list[BuildCheckLink] = []
    if pr is not None:
        try:
            documents = fetch_tekton_documents(owner, repo, head_sha, run)
            context = _pr_context_for_builds(owner, repo, pr, gh_runner=run)
            expected = select_build_checks(
                documents, context, app_slug=slug, app_id=aid
            )
            for item in expected:
                matched = latest_matching(item, check_runs)
                if matched is None:
                    continue
                conclusion = (matched.conclusion or "").lower()
                if conclusion not in _FAILED_CHECK_CONCLUSIONS:
                    continue
                url = _link_from_check_run(matched)
                if not url:
                    continue
                expected_links.append(
                    BuildCheckLink(
                        name=matched.name or item.name_suffix or item.key,
                        conclusion=conclusion,
                        url=url,
                    )
                )
        except (CheckQueryError, TriggerParseError, GhCommandError, ValueError) as exc:
            print(
                f"WARNING: expected-build selection failed for "
                f"{owner}/{repo}@{head_sha[:12]}: {exc}; falling back",
                file=sys.stderr,
            )
            expected_links = []

    if expected_links:
        return tuple(expected_links[: max(0, limit)])

    # Fallback: same Konflux-app filter used before tekton alignment.
    raw_runs = [
        {
            "name": c.name,
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
) -> ChildFailureRow | None:
    repo = str(entry.get("repo") or "").strip()
    pr_url = str(entry.get("pr-url") or entry.get("pr_url") or "").strip()
    state_status = str(entry.get("pr-status") or entry.get("pr_status") or "").strip()
    if not repo or not pr_url:
        return None
    if state_status in {"success", "build-success"}:
        return None

    pr = fetch_pull_request(pr_url, gh_runner=gh_runner)
    pr_state = str(pr.get("state") or "").lower() or "unknown"
    merged = bool(pr.get("mergedAt"))
    number = int(pr.get("number") or parse_pr_url(pr_url).number)
    head_sha = str(pr.get("headRefOid") or "").strip()
    ref = parse_pr_url(pr_url)

    if not should_report_child(state_status=state_status, pr_state=pr_state):
        return None

    gap_desc = fetch_gap_status_description(
        ref.owner, ref.repo, head_sha, gh_runner=gh_runner
    )
    build_links = fetch_failed_build_check_links(
        ref.owner, ref.repo, head_sha, pr=pr, gh_runner=gh_runner
    )
    close_reason = None
    if pr_state == "closed":
        comments = list_issue_comments(pr_url, gh_runner=gh_runner)
        close_reason = extract_close_reason_from_comments(comments)

    reason_parts: list[str] = []
    if pr_state == "closed":
        reason_parts.append("CLOSED" + (" (merged)" if merged else " (not merged)"))
        if close_reason:
            reason_parts.append(close_reason)
    if gap_desc:
        reason_parts.append(gap_desc)
    elif state_status:
        reason_parts.append(f"state pr-status={state_status}")

    reason = " — ".join(reason_parts) if reason_parts else state_status or pr_state
    return ChildFailureRow(
        repo=repo,
        pr_url=pr_url,
        pr_number=number,
        state_status=state_status or "unknown",
        pr_state=pr_state,
        merged=merged,
        reason=reason,
        build_links=build_links,
    )


def render_summary_markdown(
    rows: Sequence[ChildFailureRow],
    *,
    leader_pr_url: str,
    generated_at: str | None = None,
) -> str:
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
        "Build links are the failed Konflux checks selected the same way as Stage-1 "
        "(``.tekton`` PipelineRuns / ``select_build_checks``). URL prefers a "
        "PipelineRun page, else the GitHub check run — no cluster token required.",
        "",
    ]
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
            "| Repo | PR | State status | PR state | Reason | Build links |",
            "| --- | --- | --- | --- | --- | --- |",
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
) -> tuple[int | None, bool, bool]:
    run = gh_runner or _run_gh
    ref = parse_pr_url(leader_pr_url)
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
) -> list[ChildFailureRow]:
    entries = payload.get("pull-requests")
    if not isinstance(entries, list):
        raise GapPrMonitorError("state.json missing pull-requests list")
    rows: list[ChildFailureRow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            row = build_child_row(entry, gh_runner=gh_runner)
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
) -> ReporterResult:
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
    rows = collect_rows_from_state(payload, gh_runner=run)
    body = render_summary_markdown(rows, leader_pr_url=leader_pr_url)
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
