"""Unit tests for GAP Leader failure summary reporter."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import gap_failure_reporter as fr


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


def test_extract_close_reason_ignores_summary_marker() -> None:
    comments = [
        {
            "body": fr.SUMMARY_MARKER
            + "\nPRs targeting `stable` are not allowed in this repo."
        }
    ]
    assert fr.extract_close_reason_from_comments(comments) is None


def test_should_report_closed_even_if_success_status() -> None:
    assert fr.should_report_child(state_status="success", pr_state="closed") is True
    assert fr.should_report_child(state_status="success", pr_state="open") is False
    assert fr.should_report_child(state_status="build-failure", pr_state="open") is True
    assert fr.should_report_child(state_status="build-pending", pr_state="open") is True


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


def test_check_run_link_falls_back_to_github_html_url() -> None:
    url = fr._check_run_link(
        {
            "details_url": "https://konflux-ui.apps.example.com",
            "html_url": "https://github.com/acme/r/runs/123",
        }
    )
    assert url == "https://github.com/acme/r/runs/123"


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


def test_find_summary_comment() -> None:
    comments = [
        {"id": 1, "body": "hello"},
        {"id": 2, "body": f"{fr.SUMMARY_MARKER}\nold table"},
    ]
    found = fr.find_summary_comment(comments)
    assert found is not None
    assert found["id"] == 2


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
