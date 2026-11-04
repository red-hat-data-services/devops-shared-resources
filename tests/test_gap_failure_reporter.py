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


def test_reporter_matches_stage1_selection_excludes_on_push() -> None:
    """Same select_build_checks as Stage-1: on-event=[push] is not expected on a PR."""
    from lib.gap_check_selection import PullRequestContext, select_build_checks
    from lib.github_check_runs import CheckRun, latest_matching

    pr_yaml = """
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: odh-foo-on-pull-request-{{pull_request_number}}
  labels:
    appstudio.openshift.io/component: odh-foo
  annotations:
    pipelinesascode.tekton.dev/on-event: "[pull_request]"
    pipelinesascode.tekton.dev/on-target-branch: "[{{target_branch}}]"
"""
    push_yaml = """
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: odh-foo-on-push
  labels:
    appstudio.openshift.io/component: odh-foo-push
  annotations:
    pipelinesascode.tekton.dev/on-event: "[push]"
    pipelinesascode.tekton.dev/on-target-branch: "[{{target_branch}}]"
"""
    ctx = PullRequestContext(
        number=9,
        base_ref="rhoai-2.25",
        head_sha="abc",
        labels=frozenset(),
        changed_files=("src/main.go",),
    )
    expected = select_build_checks(
        [pr_yaml, push_yaml], ctx, app_slug="konflux-internal-p02"
    )
    assert [item.name_suffix for item in expected] == ["odh-foo-on-pull-request-9"]

    runs = [
        CheckRun(
            id=1,
            name="Konflux / odh-foo-on-pull-request-9",
            status="completed",
            conclusion="failure",
            head_sha="abc",
            html_url="https://github.com/acme/r/runs/1",
            details_url=(
                "https://konflux-ui.example/ns/t/pipelinerun/odh-foo-on-pull-request-9-x"
            ),
            external_id=None,
            app_slug="konflux-internal-p02",
            app_id=None,
            started_at=None,
        ),
        CheckRun(
            id=2,
            name="Konflux / odh-foo-on-push",
            status="completed",
            conclusion="failure",
            head_sha="abc",
            html_url="https://github.com/acme/r/runs/2",
            details_url="https://konflux-ui.example",
            external_id=None,
            app_slug="konflux-internal-p02",
            app_id=None,
            started_at=None,
        ),
    ]
    # Same loop as fetch_failed_build_check_links after select_build_checks.
    linked = []
    for item in expected:
        matched = latest_matching(item, runs)
        if matched is None:
            continue
        if (matched.conclusion or "").lower() not in fr._FAILED_CHECK_CONCLUSIONS:
            continue
        linked.append(matched.name)
    assert linked == ["Konflux / odh-foo-on-pull-request-9"]


def test_fetch_failed_links_uses_expected_build_checks(monkeypatch) -> None:
    """Only failing checks that Stage-1 would expect are linked."""
    from lib.github_check_runs import CheckRun, ExpectedCheck

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
    ]
    expected = [
        ExpectedCheck(
            key="odh-foo",
            name_suffix="odh-foo-on-pull-request-9",
            app_slug="konflux-internal-p02",
        )
    ]

    monkeypatch.setattr(fr, "list_check_runs", lambda *a, **k: runs)
    monkeypatch.setattr(fr, "fetch_tekton_documents", lambda *a, **k: ["doc"])
    monkeypatch.setattr(fr, "fetch_changed_files", lambda *a, **k: ())
    monkeypatch.setattr(fr, "select_build_checks", lambda *a, **k: expected)

    links = fr.fetch_failed_build_check_links(
        "acme",
        "r",
        "abc",
        pr={
            "number": 9,
            "headRefOid": "abc",
            "baseRefName": "rhoai-2.25",
            "headRefName": "feature",
            "title": "t",
            "labels": [],
            "url": "https://github.com/acme/r/pull/9",
        },
        gh_runner=lambda _args: "",
    )
    assert len(links) == 1
    assert links[0].name == "Konflux / odh-foo-on-pull-request-9"
    assert "pipelinerun" in links[0].url


def test_select_failed_build_checks_prefers_pr_pipelinerun() -> None:
    runs = [
        {
            "name": "Red Hat Konflux / conforma-registry-rhoai-prod / odh-foo",
            "conclusion": "cancelled",
            "details_url": "https://konflux.example/application-pipeline",
            "html_url": "https://github.com/acme/r/runs/1",
            "app": {"slug": "konflux-internal-p02"},
        },
        {
            "name": "Konflux Production Internal / odh-foo-on-push",
            "conclusion": "failure",
            "details_url": "https://konflux-ui.example",
            "html_url": "https://github.com/acme/r/runs/9",
            "app": {"slug": "konflux-internal-p02"},
        },
        {
            "name": "Konflux Production Internal / odh-foo-on-pull-request-1",
            "conclusion": "failure",
            "details_url": (
                "https://konflux-ui.example/ns/rhoai-tenant/pipelinerun/odh-foo-on-pull-request-1-abc"
            ),
            "html_url": "https://github.com/acme/r/runs/2",
            "app": {"slug": "konflux-internal-p02"},
        },
        {
            "name": "github-actions / something",
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
