"""Unit tests for GAP Leader failure summary reporter."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import gap_failure_reporter as fr


# Report the explicit stable-branch restriction as the reason for closure.
def test_extract_close_reason_stable_restriction() -> None:
    comments = [
        {"body": "unrelated"},
        {
            "body": (
                "🚫 **PRs targeting `stable` are not allowed in this repo.**\n\n"
                "Please retarget your PR to `dev`.\n"
            )
        },
    ]
    reason = fr.extract_close_reason_from_comments(comments)
    assert reason is not None
    assert "stable" in reason.lower()
    assert "not allowed" in reason.lower()


# A generated summary marker alone must not be mistaken for a closure reason.
def test_extract_close_reason_ignores_summary_marker() -> None:
    comments = [
        {
            "body": fr.SUMMARY_MARKER
            + "\nPRs targeting `stable` are not allowed in this repo."
        }
    ]
    assert fr.extract_close_reason_from_comments(comments) is None


# Closed children remain reportable even when their stored status says success.
def test_should_report_closed_even_if_success_status() -> None:
    assert fr.should_report_child(state_status="success", pr_state="closed") is True
    assert fr.should_report_child(state_status="success", pr_state="open") is False
    assert fr.should_report_child(state_status="build-failure", pr_state="open") is True
    assert fr.should_report_child(state_status="build-pending", pr_state="open") is True


# Only completed failing pull-request builds should contribute diagnostic links.
def test_fetch_failed_links_checks_only_on_pull_request(monkeypatch) -> None:
    """Link completed failures with on-pull-request; drop on-push and success."""
    from lib.github_check_runs import CheckRun

    runs = [
        CheckRun(
            id=2,
            name="Konflux / odh-foo-on-pull-request-9",
            status="completed",
            conclusion="failure",
            head_sha="abc",
            html_url="https://github.com/acme/r/runs/2",
            details_url=(
                "https://konflux-ui.example/ns/t/pipelinerun/odh-foo-on-pull-request-9-abc"
            ),
            external_id=None,
            app_slug="konflux-internal-p02",
            app_id=None,
            started_at=None,
        ),
        CheckRun(
            id=3,
            name="Konflux / odh-foo-on-push",
            status="completed",
            conclusion="failure",
            head_sha="abc",
            html_url="https://github.com/acme/r/runs/3",
            details_url="https://konflux-ui.example",
            external_id=None,
            app_slug="konflux-internal-p02",
            app_id=None,
            started_at=None,
        ),
        CheckRun(
            id=4,
            name="Konflux / odh-bar-on-pull-request-9",
            status="completed",
            conclusion="success",
            head_sha="abc",
            html_url="https://github.com/acme/r/runs/4",
            details_url=(
                "https://konflux-ui.example/ns/t/pipelinerun/odh-bar-on-pull-request-9-abc"
            ),
            external_id=None,
            app_slug="konflux-internal-p02",
            app_id=None,
            started_at=None,
        ),
        CheckRun(
            id=5,
            name="Konflux / odh-baz-on-pull-request-9",
            status="in_progress",
            conclusion=None,
            head_sha="abc",
            html_url="https://github.com/acme/r/runs/5",
            details_url=(
                "https://konflux-ui.example/ns/t/pipelinerun/odh-baz-on-pull-request-9-abc"
            ),
            external_id=None,
            app_slug="konflux-internal-p02",
            app_id=None,
            started_at=None,
        ),
    ]

    monkeypatch.setattr(fr, "list_check_runs", lambda *a, **k: runs)

    links = fr.fetch_failed_build_check_links(
        "acme",
        "r",
        "abc",
        pr={"number": 9, "headRefOid": "abc"},
        gh_runner=lambda _args: "",
    )
    assert len(links) == 1
    assert links[0].name == "Konflux / odh-foo-on-pull-request-9"
    assert "pipelinerun" in links[0].url


# Ignore push builds and successful checks when selecting failed-build links.
def test_select_failed_build_checks_on_pull_request_only() -> None:
    runs = [
        {
            "name": "Red Hat Konflux / conforma-registry-rhoai-prod / odh-foo",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://konflux.example/application-pipeline",
            "html_url": "https://github.com/acme/r/runs/1",
            "app": {"slug": "konflux-internal-p02"},
        },
        {
            "name": "Konflux Production Internal / odh-foo-on-push",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://konflux-ui.example",
            "html_url": "https://github.com/acme/r/runs/9",
            "app": {"slug": "konflux-internal-p02"},
        },
        {
            "name": "Konflux Production Internal / odh-foo-on-pull-request-1",
            "status": "completed",
            "conclusion": "failure",
            "details_url": (
                "https://konflux-ui.example/ns/rhoai-tenant/pipelinerun/odh-foo-on-pull-request-1-abc"
            ),
            "html_url": "https://github.com/acme/r/runs/2",
            "app": {"slug": "konflux-internal-p02"},
        },
        {
            "name": "Konflux Production Internal / odh-bar-on-pull-request-1",
            "status": "completed",
            "conclusion": "cancelled",
            "details_url": (
                "https://konflux-ui.example/ns/rhoai-tenant/pipelinerun/odh-bar-on-pull-request-1-abc"
            ),
            "html_url": "https://github.com/acme/r/runs/3",
            "app": {"slug": "konflux-internal-p02"},
        },
        {
            "name": "github-actions / something",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://github.com/acme/r/actions/1",
            "app": {"slug": "github-actions"},
        },
    ]
    links = fr.select_failed_build_checks(runs, limit=5)
    assert len(links) == 1
    assert links[0].conclusion == "failure"
    assert "pipelinerun" in links[0].url
    assert "on-pull-request" in links[0].name


# Describe the first failed build and count additional failures without inventing details.
def test_format_build_failure_reason_matches_links() -> None:
    links = (
        fr.BuildCheckLink(
            name="Konflux / odh-foo-on-pull-request-9",
            conclusion="failure",
            url="https://example/pipelinerun/a",
        ),
        fr.BuildCheckLink(
            name="Konflux / odh-bar-on-pull-request-9",
            conclusion="failure",
            url="https://example/pipelinerun/b",
        ),
    )
    reason = fr.format_build_failure_reason("acme/r", 9, links)
    assert reason == (
        "acme/r#9 build-failure: odh-foo-on-pull-request-9 concluded failure (+1 more)"
    )
    assert fr.format_build_failure_reason("acme/r", 9, ()) == "acme/r#9 build-failure"


# Replace unexpanded feasibility expressions with readable explanations, preserving other
# reasons.
def test_humanize_merge_failure_reason_rewrites_unexpanded_feasibility_skip() -> None:
    raw = (
        "merge-failure: main-release-feasibility "
        "(${{ matrix.downstream_branch }}) concluded skipped"
    )
    out = fr.humanize_merge_failure_reason(raw)
    assert out.startswith("merge-failure:")
    assert "main-to-release feasibility failed" in out
    assert "automerge disabled" in out
    assert "${{" not in out
    # Non-feasibility / already-clear reasons stay untouched.
    assert fr.humanize_merge_failure_reason("merge-failure: conflict") == (
        "merge-failure: conflict"
    )
    assert fr.humanize_merge_failure_reason(
        "merge-failure: main-release-feasibility (rhoai-2.25) concluded failure"
    ).endswith("concluded failure")


# Fall back to the GitHub run page when no more specific build link is available.
def test_check_run_link_falls_back_to_github_html_url() -> None:
    url = fr._check_run_link(
        {
            "details_url": "https://konflux-ui.apps.example.com",
            "html_url": "https://github.com/acme/r/runs/123",
        }
    )
    assert url == "https://github.com/acme/r/runs/123"


# Display the build name as the link label rather than repeating its long URL.
def test_format_build_links_cell_is_name_only() -> None:
    cell = fr.format_build_links_cell(
        [
            fr.BuildCheckLink(
                name="Konflux Production Internal / odh-foo-on-pull-request-9",
                conclusion="failure",
                url="https://konflux-ui.example/ns/rhoai-tenant/pipelinerun/odh-foo-9-abc",
            )
        ]
    )
    assert cell == (
        "[odh-foo-on-pull-request-9]"
        "(https://konflux-ui.example/ns/rhoai-tenant/pipelinerun/odh-foo-9-abc)"
    )


# Render the summary marker and table with navigable child-PR and build links.
def test_render_summary_markdown_table() -> None:
    rows = [
        fr.ChildFailureRow(
            repo="kuberay",
            pr_url="https://github.com/acme/kuberay/pull/1",
            pr_number=1,
            state_status="build-failure",
            pr_state="closed",
            merged=False,
            reason="CLOSED (not merged) — PRs targeting stable are not allowed",
            build_links=(
                fr.BuildCheckLink(
                    name="Konflux Production Internal / odh-kuberay-on-pull-request-1",
                    conclusion="cancelled",
                    url="https://konflux-ui.example/ns/rhoai-tenant/pipelinerun/odh-kuberay-1",
                ),
            ),
        ),
        fr.ChildFailureRow(
            repo="ml-metadata",
            pr_url="https://github.com/acme/ml-metadata/pull/2",
            pr_number=2,
            state_status="build-failure",
            pr_state="open",
            merged=False,
            reason="ml-metadata#2 build-failure: odh-mlmd concluded failure",
            build_links=(),
        ),
    ]
    body = fr.render_summary_markdown(
        rows,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        generated_at="2026-11-04 10:00:00 UTC",
    )
    assert fr.SUMMARY_MARKER in body
    assert "| Repo | PR | State status | PR state | Reason | Build links |" in body
    assert "[#1](https://github.com/acme/kuberay/pull/1)" in body
    assert (
        "[odh-kuberay-on-pull-request-1](https://konflux-ui.example/ns/rhoai-tenant/pipelinerun/odh-kuberay-1)"
        in body
    )


# Tracking links must be current; standalone refreshes must not reuse stale incident links.
def test_tracking_column_standalone_and_creator_only_refresh(monkeypatch, tmp_path):
    row = fr.ChildFailureRow("repo", "https://github.com/acme/repo/pull/1", 1,
                             "build-failure", "open", False, "failed")
    tracking = {"status": "checked", "channel": "C0883L8FARE", "rows": {row.pr_url: [
        {"component": "cpu", "status": "slack_pending", "jira_key": "RHAI-1"},
        {"component": "gpu", "status": "confirmed", "jira_key": "RHAI-2",
         "slack_url": "https://redhat-internal.slack.com/archives/C0883L8FARE/p123000001"}]}}
    leader = "https://github.com/acme/gap/pull/9"
    body = fr.render_summary_markdown([row], leader_pr_url=leader, tracking=tracking)
    assert "| Jira / Slack |" in body and "cpu: [RHAI-1]" in body
    assert "gpu: [RHAI-2]" in body and "<br>" in body
    captured = []
    monkeypatch.setattr(fr, "list_issue_comments", lambda *a, **kw: [{"id": 1, "body": body}])
    monkeypatch.setattr(fr, "upsert_leader_summary_comment", lambda url, content, **kw: captured.append(content) or (1, False, True))
    fr.publish_creator_outcomes(leader, [], gh_runner=lambda a: "")
    assert body.rstrip() in captured[-1]  # Existing build table/links unchanged.
    monkeypatch.setattr(fr, "load_state", lambda path: {"pull-requests": []})
    monkeypatch.setattr(fr, "resolve_state_file", lambda **kw: tmp_path / "state.json")
    monkeypatch.setattr(fr, "collect_rows_from_state", lambda *a, **kw: [row])
    result = fr.report_for_leader(leader_pr_url=leader, repo_root=tmp_path,
                                  gh_runner=lambda a: "")
    assert "Not checked" in result.body and "RHAI-1" not in result.body
    unavailable = fr.report_for_leader(leader_pr_url=leader, repo_root=tmp_path,
                                       gh_runner=lambda a: "", tracking={"status": "unavailable"})
    assert "Unavailable" in unavailable.body and "No confirmed registry match" not in unavailable.body


# Healthy/unknown sibling observations must not produce registry-match warnings.
def test_tracking_summary_omits_expected_empty_matches(tmp_path):
    from gap_notification_fakes import Jira, Slack, observation
    from lib.gap_notifications.dispatch_incidents import Dispatcher, Ledger
    from lib.gap_notifications.lookup_tools import project_tracking

    pr = "https://github.com/acme/repo/pull/1"
    observations = [observation(status=status, component=name, oid=name, evidence={"pr_url": pr})
                    for name, status in [("failed", "failure"), ("healthy", "success"),
                                         ("unresolved", "unknown")]]
    jira = Jira()
    Dispatcher(Ledger(tmp_path / "journal"), jira, Slack(), trial_id="trial").run(observations)
    tracking = project_tracking(observations, jira, trial_id="trial")
    row = fr.ChildFailureRow("repo", pr, 1, "build-failure", "open", False, "failed")
    body = fr.render_summary_markdown([row], leader_pr_url="https://github.com/acme/gap/pull/9",
                                      tracking=tracking)
    assert "failed: [RHAI-1]" in body and "[Slack thread]" in body
    assert "No confirmed registry match" not in body
    assert "healthy:" not in body and "unresolved:" not in body


# Locate the existing managed summary by its marker rather than unrelated comments.
def test_find_summary_comment() -> None:
    comments = [
        {"id": 1, "body": "hello"},
        {"id": 2, "body": f"{fr.SUMMARY_MARKER}\nold table"},
    ]
    found = fr.find_summary_comment(comments)
    assert found is not None
    assert found["id"] == 2


# Refresh the managed summary in place instead of creating another comment.
def test_upsert_updates_existing_comment() -> None:
    def gh(args: list[str]) -> str:
        joined = " ".join(args)
        if "--paginate" in args and "comments" in joined and "-X" not in args:
            return json.dumps([{"id": 55, "body": f"{fr.SUMMARY_MARKER}\nold"}])
        if "PATCH" in args:
            assert "--input" in args
            path = args[args.index("--input") + 1]
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            assert fr.SUMMARY_MARKER in payload["body"]
            assert "new table" in payload["body"]
            return json.dumps({"id": 55})
        raise AssertionError(f"unexpected gh args: {args}")

    comment_id, created, updated = fr.upsert_leader_summary_comment(
        "https://github.com/acme/gap/pull/9",
        f"{fr.SUMMARY_MARKER}\nnew table",
        dry_run=False,
        gh_runner=gh,
    )
    assert comment_id == 55
    assert created is False
    assert updated is True


# Create a managed summary only when no existing summary can be found.
def test_upsert_creates_when_missing() -> None:
    def gh(args: list[str]) -> str:
        if "--paginate" in args:
            return "[]"
        if "POST" in args:
            return json.dumps({"id": 99})
        raise AssertionError(args)

    comment_id, created, updated = fr.upsert_leader_summary_comment(
        "https://github.com/acme/gap/pull/9",
        f"{fr.SUMMARY_MARKER}\ntable",
        dry_run=False,
        gh_runner=gh,
    )
    assert comment_id == 99
    assert created is True
    assert updated is False


# A dry run may describe a prospective summary but must not mutate comments.
def test_upsert_dry_run_no_mutate() -> None:
    def gh(args: list[str]) -> str:
        if "--paginate" in args:
            return "[]"
        raise AssertionError("dry-run must not mutate")

    comment_id, created, updated = fr.upsert_leader_summary_comment(
        "https://github.com/acme/gap/pull/9",
        f"{fr.SUMMARY_MARKER}\ntable",
        dry_run=True,
        gh_runner=gh,
    )
    assert comment_id is None
    assert created is True
    assert updated is False
