"""Reporting must consume the monitor's exact Konflux evidence, not GH checks."""
import copy
import json

import pytest
from scripts import gap_failure_reporter as reporter
from tests.test_konflux_builds import SHA, REPO, COMPONENT, run, observe


@pytest.fixture
def entry(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Konflux build diagnostics must not query GitHub checks/statuses")
    monkeypatch.setattr(reporter, "fetch_failed_build_check_links", forbidden)
    monkeypatch.setattr(reporter, "fetch_gap_status_description", forbidden)
    monkeypatch.setattr(reporter, "fetch_pull_request", lambda *args, **kwargs: {
        "state": "OPEN", "number": 123, "headRefOid": SHA, "mergedAt": None,
    })
    failed = observe([run(status="False", reason="Failed")]).to_dict()
    return {
        "repo": "foo", "pr-url": "https://github.com/acme/foo/pull/123",
        "pr-status": "build-failure",
        "build-observations": {"source": "konflux", "head-sha": SHA,
                               "repository": REPO, "pr": 123, "runs": [failed]},
    }


def test_failure_links_match_persisted_konflux_attempt_including_pull_request_name(entry):
    row = reporter.build_child_row(entry)
    assert row.state_status == "build-failure"
    assert len(row.build_links) == 1
    assert row.build_links[0].name == "foo-pull-request-r1"
    assert row.build_links[0].url.endswith("/pipelinerun/foo-pull-request-r1")


@pytest.mark.parametrize("field,value", [("head-sha", "b" * 40), ("repository", "acme/other"), ("pr", 999)])
def test_stale_or_wrong_identity_is_not_reported_as_current_failure(entry, field, value):
    entry["build-observations"][field] = value
    row = reporter.build_child_row(entry)
    assert row.state_status == "build-pending"
    assert row.build_links == ()
    assert "awaiting monitor refresh" in row.reason


def test_konflux_mode_does_not_fall_back_to_github_for_unmigrated_state(entry, monkeypatch):
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    del entry["build-observations"]
    row = reporter.build_child_row(entry)
    assert row.state_status == "build-pending"
    assert row.build_links == ()


def test_lookup_problem_is_visible_as_unknown_not_failed(entry):
    entry["pr-status"] = "build-pending"
    entry["build-observations"]["query-error"] = "Konflux lookup failed (HTTP 403)"
    entry["build-observations"]["runs"] = []
    row = reporter.build_child_row(entry)
    assert "HTTP 403" in row.reason
    assert row.state_status == "build-pending"


@pytest.mark.parametrize("field,value", [("url", "https://evil.example/ns/rhoai-tenant/pipelinerun/run"),
                                        ("head_sha", "c" * 40)])
def test_untrusted_or_old_head_link_is_not_rendered(entry, field, value):
    entry["build-observations"]["runs"][0][field] = value
    assert reporter.build_child_row(entry).build_links == ()

def test_comment_after_monitor_reuses_pr_and_build_receipt_without_second_scan(entry, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A report immediately after monitoring must not query the child again")
    monkeypatch.setattr(reporter, "fetch_pull_request", forbidden)
    entry["build-observations"]["pr-state"] = "OPEN"
    row = reporter.build_child_row(entry, reuse_monitor_evidence=True)
    assert row.state_status == "build-failure"
    assert row.pr_number == 123
    assert row.build_links[0].name == "foo-pull-request-r1"


def test_standalone_report_still_checks_current_head(entry, monkeypatch):
    calls = []
    def fetch(*args, **kwargs):
        calls.append(args)
        return {"state": "OPEN", "number": 123, "headRefOid": "b" * 40}
    monkeypatch.setattr(reporter, "fetch_pull_request", fetch)
    entry["build-observations"]["pr-state"] = "OPEN"
    row = reporter.build_child_row(entry)
    assert len(calls) == 1
    assert row.state_status == "build-pending"
    assert not row.build_links

def test_merge_conflict_summary_reuses_monitor_facts_without_another_pr_scan(tmp_path, monkeypatch):
    from lib.konflux_builds import KonfluxClient
    from tests.test_gap_repository_gates import _GitHub, _state, _run, _saved
    def forbidden(*args, **kwargs):
        pytest.fail("Merge-conflict reporting must reuse the monitor receipt")
    github = _GitHub()
    github.mergeable = "CONFLICTING"
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setattr(KonfluxClient, "from_environment", forbidden)
    path = _state(tmp_path)
    result, _ = _run(path, github)
    child = _saved(path)["pull-requests"][0]
    assert child["pr-status"] == "merge-failure"
    assert child["build-observations"]["query-skipped"]
    monkeypatch.setattr(reporter, "fetch_pull_request", forbidden)
    monkeypatch.setattr(reporter, "fetch_gap_status_description", forbidden)
    row = reporter.build_child_row(child, reuse_monitor_evidence=True)
    assert row.state_status == "merge-failure"
    assert "merge conflict" in row.reason

@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_build_receipt_survives_eg_result_and_history_snapshot(tmp_path, monkeypatch, outcome):
    """Stage-2 result handling and history retain Stage-1 evidence and image data."""
    from scripts import gap_early_gate_results as eg
    from lib.leader_history import write_history_snapshot
    from tests.test_gap_early_gate_results import (
        _bot_comment, PASSED_BARE_MARKER, FAILED_BARE_MARKER, TESTS_PASSED,
    )
    receipt = {"source": "konflux", "repository": REPO, "pr": 123,
               "head-sha": SHA, "pr-state": "OPEN", "runs": [observe([run()]).to_dict()]}
    images = [{"component": "odh-foo", "image": "quay.io/acme/foo@sha256:" + "b" * 64}]
    folder = tmp_path / "GAP Leaders" / "2026-11-05_gap-audit"
    folder.mkdir(parents=True)
    path = folder / "state.json"
    path.write_text(json.dumps({"overall-status": "early-gate-triggered", "pull-requests": [{
        "repo": "foo", "pr-url": "https://github.com/acme/foo/pull/123",
        "pr-status": "success", "builds": images, "build-observations": receipt,
    }]}))
    comment = PASSED_BARE_MARKER if outcome == "success" else FAILED_BARE_MARKER
    comments = [_bot_comment(comment)]
    if outcome == "success":
        comments.append(_bot_comment(TESTS_PASSED))
    monkeypatch.setattr(eg, "list_leader_pr_comments", lambda *a, **k: comments)
    result = eg.run_early_gate_results(
        repo_root=tmp_path, leader_pr_url="https://github.com/acme/gap/pull/9",
        state_file=str(path), labels=[], publish=False)
    assert result.overall_status == outcome
    for saved in (path, write_history_snapshot(tmp_path, path)):
        child = json.loads(saved.read_text())["pull-requests"][0]
        assert child["build-observations"] == receipt
        assert child["builds"] == images
        assert child["pr-status"] == "success"
