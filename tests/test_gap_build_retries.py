"""Retry actions use fake APIs only; no live /retest comments or builds."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from lib.gap_build_retries import BuildRetryManager, classify_failure
from lib.gap_pr_gates import RepositoryPullRequest
from lib.github_check_runs import ExpectedCheck
from lib.konflux_builds import KonfluxClient, KonfluxQueryError, select_latest, PAC, COMPONENT as LABEL
from lib.konflux_gate import evaluate_konflux_gate
from lib.konflux_retry_evidence import recent_push_health, failure_details
from tests.test_konflux_builds import run, SHA, REPO, COMPONENT, observe

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
PIPELINE = "foo-on-pull-request"
PR = RepositoryPullRequest(url="https://github.com/acme/foo/pull/123", owner="acme", repo="foo", number=123,
    state="OPEN", mergeable="MERGEABLE", merge_state_status="CLEAN", base_ref="rhoai-3.7-ea.1", head_sha=SHA,
    labels=frozenset(), changed_files=(), committer_at=NOW)


def attempt(reason="PipelineRunTimeout", name="r1", created="2026-11-05T10:00:00Z", component=COMPONENT):
    item = run(status="False", reason=reason, name=name, created=created, component=component)
    item["metadata"]["annotations"][PAC + "original-prname"] = PIPELINE
    # Each retry must complete after its own creation, not the default first attempt.
    item["status"]["completionTime"] = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                                         + timedelta(minutes=1)).isoformat()
    return item


class FakeKonflux:
    api_url = "https://konflux.example.com"
    namespace = "rhoai-tenant"
    ui_url = "https://konflux.example.com"

    def __init__(self, item=None):
        self.items = [item or attempt()]
        self.tasks, self.pushes = [], []
        self.refreshes = 0

    def _request_json(self, url):
        return {"kind": "Component", "metadata": {"name": COMPONENT, "namespace": self.namespace},
                "spec": {"source": {"git": {"url": "https://github.com/" + REPO, "revision": PR.base_ref}}}}

    def observe(self, **query):
        self.refreshes += bool(query.pop("refresh", False))
        return select_latest(self.items, namespace=self.namespace, ui_url=self.ui_url, **query)

    def _list_resources(self, selector, resource="pipelineruns"):
        return self.tasks if resource == "taskruns" else self.pushes


class FakeGitHub:
    def __init__(self):
        self.comments, self.posts = [], []
        self.sha, self.state = SHA, "open"
        self.fail_after_post = False

    def __call__(self, args):
        endpoint = args[1]
        if "POST" in args:
            body = args[-1].removeprefix("body=")
            self.posts.append(body)
            comment = {"id": len(self.comments) + 1, "html_url": "https://github.com/acme/foo/pull/123#issuecomment-1",
                       "user": {"login": "rhods-devops-app[bot]"}, "body": body,
                       "created_at": NOW.isoformat()}
            self.comments.append(comment)
            if self.fail_after_post:
                raise RuntimeError("Connection lost after GitHub accepted comment")
            return json.dumps(comment)
        if "/comments" in endpoint:
            return json.dumps(self.comments)
        assert endpoint == "repos/acme/foo/pulls/123"
        return json.dumps({"state": self.state, "head": {"sha": self.sha}})


def evaluate(client):
    return evaluate_konflux_gate(repo=REPO, number=123, sha=SHA,
        expected=[ExpectedCheck(key=COMPONENT, name_suffix=PIPELINE)], client=client)


def manager(github, client, *, saved=None, dry_run=False, now=NOW):
    payload = {"pull-requests": [{"pr-url": PR.url, "build-observations": saved or {}}]}
    return BuildRetryManager(github, payload, dry_run=dry_run, client_factory=lambda: client, clock=lambda: now)


def apply(mgr, client, pr=PR):
    return mgr.apply(pr, *evaluate(client))


@pytest.mark.parametrize("reason", ["PipelineRunTimeout", "PipelineRunCancelled"])
def test_targeted_retry_holds_batch_and_keeps_original_build_truth(reason):
    gh, client = FakeGitHub(), FakeKonflux(attempt(reason))
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending"
    assert len(gh.posts) == 1 and gh.posts[0].startswith("/retest " + PIPELINE + "\n")
    record = receipt["runs"][0]
    assert record["status"] in {"timed_out", "cancelled"}  # EG never sees fabricated success
    assert record["retry"]["status"] == "build-retrigger-pending"
    assert record["retry"]["retrigger_count"] == 1
    assert receipt["retry-history"][0]["run_uid"] == record["uid"]
    assert client.refreshes == 1


@pytest.mark.parametrize("completion", ["invalid", "2026-11-05T09:00:00Z"])
def test_unusable_completion_cannot_authorize_automatic_retry(completion):
    """Malformed or pre-creation completion is unavailable, never retry authority."""
    gh, client = FakeGitHub(), FakeKonflux()
    client.items[0]["status"]["completionTime"] = completion
    gate, receipt = apply(manager(gh, client), client)
    assert not gh.posts
    assert gate.outcome == "fail"
    assert receipt["runs"][0]["completed_at"] == ""
    assert receipt["runs"][0]["status"] == "timed_out"


def test_crash_after_post_and_different_leader_cannot_reissue_same_attempt():
    gh, client = FakeGitHub(), FakeKonflux()
    gh.fail_after_post = True
    with pytest.raises(RuntimeError):
        apply(manager(gh, client), client)
    gh.fail_after_post = False
    # No local state survived; child comment is the cross-leader receipt.
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending" and len(gh.posts) == 1
    assert receipt["runs"][0]["retry"]["retrigger_count"] == 1


def test_retry_budget_is_per_component_sha_and_counts_new_attempts_only():
    gh, client = FakeGitHub(), FakeKonflux()
    _, saved = apply(manager(gh, client), client)
    client.items = [attempt(name="r2", created="2026-11-05T13:00:00Z")]
    _, saved = apply(manager(gh, client, saved=saved, now=NOW + timedelta(hours=2)), client)
    assert len(gh.posts) == 2
    client.items = [attempt(name="r3", created="2026-11-05T15:00:00Z")]
    gate, receipt = apply(manager(gh, client, saved=saved, now=NOW + timedelta(hours=4)), client)
    assert len(gh.posts) == 2 and gate.outcome == "fail"
    assert receipt["runs"][0]["retry"]["status"] == "retry-exhausted"


def test_missing_replacement_does_not_repeat_comment_forever():
    gh, client = FakeGitHub(), FakeKonflux()
    apply(manager(gh, client), client)
    gate, receipt = apply(manager(gh, client, now=NOW + timedelta(hours=2)), client)
    assert gate.outcome == "fail" and len(gh.posts) == 1
    assert receipt["runs"][0]["retry"]["status"] == "retry-not-started"


@pytest.mark.parametrize("status", ["True", "Unknown"])
def test_new_success_or_running_attempt_is_never_retried_and_keeps_history(status):
    gh, client = FakeGitHub(), FakeKonflux()
    _, saved = apply(manager(gh, client), client)
    item = attempt(name="r2", created="2026-11-05T13:00:00Z")
    item["status"]["conditions"][0].update(status=status, reason="Succeeded" if status == "True" else "Running")
    if status == "Unknown":
        item["status"].pop("completionTime")
    client.items.append(item)
    gate, receipt = apply(manager(gh, client, saved=saved), client)
    assert gate.outcome == ("pass" if status == "True" else "pending")
    assert len(gh.posts) == 1 and len(receipt["retry-history"]) == 1


def test_dry_run_has_no_comment_or_budget_side_effect():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, receipt = apply(manager(gh, client, dry_run=True), client)
    assert gate.outcome == "pending" and not gh.posts
    assert receipt["runs"][0]["retry"]["status"] == "would-retrigger"
    assert not receipt["retry-history"]


@pytest.mark.parametrize("state,sha", [("closed", SHA), ("open", "b" * 40)])
def test_head_change_or_closure_before_comment_is_fail_closed(state, sha):
    gh, client = FakeGitHub(), FakeKonflux()
    gh.state, gh.sha = state, sha
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending" and not gh.posts
    assert "changed head" in receipt["runs"][0]["retry"]["reason"]


def test_new_attempt_between_evaluation_and_comment_is_not_retried():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, receipt = evaluate(client)
    client.items.append(attempt(name="r2", created="2026-11-05T13:00:00Z"))
    gate, _ = manager(gh, client).apply(PR, gate, receipt)
    assert gate.outcome == "pending" and not gh.posts


@pytest.mark.parametrize("error", ["fips-check failed", "no such file or directory", "go.mod requires go >= 1.25", "syntax error"])
@pytest.mark.parametrize("reason", ["Failed", "PipelineRunTimeout", "Cancelled"])
def test_persistent_errors_override_timeout_and_healthy_pushes(error, reason):
    ob = observe([attempt(reason)]).to_dict()
    assert not classify_failure(ob, error, push_healthy=True, task_evidence=True)[0]


@pytest.mark.parametrize("details,healthy,tasks,eligible", [
    ("TLS handshake timeout", True, True, True),
    ("resource quota evaluation timed out", True, True, True),
    ("TLS handshake timeout", False, True, False),
    ("TLS handshake timeout", True, False, False),
    ("Step exited with code 1", True, True, False),
])
def test_generic_failure_requires_positive_infra_evidence_and_push_health(details, healthy, tasks, eligible):
    assert classify_failure(observe([attempt("Failed")]).to_dict(), details,
                            push_healthy=healthy, task_evidence=tasks)[0] is eligible


def push(name, created, *, component=COMPONENT, branch="rhoai-3.7-ea.1", status="True"):
    item = run(name=name, created=created, component=component, status=status)
    labels, annotations = item["metadata"]["labels"], item["metadata"]["annotations"]
    labels[PAC + "event-type"] = "push"
    labels.pop(PAC + "pull-request")
    annotations.pop("build.appstudio.redhat.com/pull_request_number")
    annotations[PAC + "branch"] = branch
    annotations[PAC + "source-branch"] = "refs/heads/" + branch
    return item


def pushes():
    return [push(f"push-{n}", f"2026-11-05T0{n}:00:00Z") for n in range(3)]


@pytest.mark.parametrize("change", ["none", "failed", "pending", "wrong-branch", "wrong-component", "old", "missing", "tied"])
def test_push_health_uses_exact_release_recent_complete_attempts(change):
    client = FakeKonflux()
    client.pushes = pushes()
    if change == "failed":
        client.pushes[-1]["status"]["conditions"][0]["status"] = "False"
    elif change == "pending":
        client.pushes[-1]["status"]["conditions"][0]["status"] = "Unknown"
    elif change == "wrong-branch":
        client.pushes[-1]["metadata"]["annotations"][PAC + "branch"] = "rhoai-3.6"
        client.pushes[-1]["metadata"]["annotations"][PAC + "source-branch"] = "refs/heads/rhoai-3.6"
    elif change == "wrong-component":
        client.pushes[-1]["metadata"]["labels"][LABEL] = "odh-foo-v3-6"
    elif change == "old":
        client.pushes[-1]["metadata"]["creationTimestamp"] = "2026-10-01T12:00:00Z"
    elif change == "missing":
        client.pushes.pop()
    elif change == "tied":
        client.pushes[-1]["metadata"]["creationTimestamp"] = client.pushes[0]["metadata"]["creationTimestamp"]
    if change == "tied":
        with pytest.raises(KonfluxQueryError):
            recent_push_health(client, repo=REPO, component=COMPONENT, branch=PR.base_ref, now=NOW)
    else:
        health = recent_push_health(client, repo=REPO, component=COMPONENT, branch=PR.base_ref, now=NOW)
        assert health["healthy"] is (change == "none")


def failed_task(message="TLS handshake timeout", name="build-container", uid="uid-r1"):
    return {"kind": "TaskRun", "metadata": {"name": name, "namespace": "rhoai-tenant", "uid": "task-1",
        "ownerReferences": [{"kind": "PipelineRun", "uid": uid}]},
        "status": {"conditions": [{"type": "Succeeded", "status": "False", "reason": "Failed", "message": message}]}}


@pytest.mark.parametrize("message,healthy,posted", [
    ("TLS handshake timeout", True, True), ("TLS handshake timeout", False, False),
    ("go.mod requires go >= 1.25", True, False), ("Step exited 1", True, False),
])
def test_full_generic_failure_path(message, healthy, posted):
    gh, client = FakeGitHub(), FakeKonflux(attempt("Failed"))
    client.tasks, client.pushes = [failed_task(message)], pushes() if healthy else []
    gate, receipt = apply(manager(gh, client), client)
    assert bool(gh.posts) is posted
    assert gate.outcome == ("pending" if posted else "fail")


def test_task_from_another_pipeline_uid_cannot_make_failure_retriable():
    client = FakeKonflux(attempt("Failed"))
    client.tasks = [failed_task(uid="unrelated")]
    _, found = failure_details(client, observe(client.items).to_dict())
    assert not found


def test_konflux_access_error_never_posts_or_authorizes_a_retry():
    gh, client = FakeGitHub(), FakeKonflux()
    def denied(*args, **kwargs):
        raise KonfluxQueryError("HTTP 403")
    client._list_resources = denied
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending" and not gh.posts
    assert receipt["runs"][0]["retry"]["status"] == "retry-evidence-unavailable"

@pytest.mark.usefixtures("pac_cel_binary")
@pytest.mark.parametrize("enabled,feasibility_fails", [(True, False), (False, False), (True, True)])
def test_real_monitor_integration_preserves_child_hold_and_gate_rules(tmp_path, monkeypatch, enabled, feasibility_fails):
    from lib.gap_promotion_gates import should_green_leader_for_build_failure, should_trigger_early_gate
    from tests.test_gap_repository_gates import (_GitHub, _state, _run, _saved, _passing_check_runs,
                                                _feasibility_success_payloads, _job, URL)
    github = _GitHub()
    github.check_runs = _passing_check_runs()
    if feasibility_fails:
        github.jobs[2] = _job("main-release-feasibility (rhoai-3.0)", "failure")
        github.workflow_conclusion = "failure"
        github.check_runs = _feasibility_success_payloads(fail_last=True)
    ledger = FakeGitHub()
    def runner(args):
        if "/issues/20/comments" in args[1]:
            return ledger(args)
        if args[:2] == ["api", "repos/red-hat-data-services/eval-hub/pulls/20"]:
            return json.dumps({"state": "open", "head": {"sha": SHA}})
        return github(args)
    item = attempt(component="odh-eval-hub-v3-6")
    meta = item["metadata"]
    meta["labels"].update({PAC + "url-org": "red-hat-data-services", PAC + "url-repository": "eval-hub", PAC + "pull-request": "20"})
    meta["annotations"].update({PAC + "repo-url": "https://github.com/red-hat-data-services/eval-hub",
        "build.appstudio.redhat.com/pull_request_number": "20", PAC + "original-prname": "odh-eval-hub-on-pull-request-65317"})
    client = FakeKonflux(item)
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setenv("GAP_AUTO_RETRY_BUILDS", str(enabled).lower())
    monkeypatch.setattr(KonfluxClient, "from_environment", lambda **_: client)
    path = _state(tmp_path)
    result, updater = _run(path, runner)
    saved = _saved(path)
    if enabled and not feasibility_fails:
        assert len(ledger.posts) == 1
        assert result.overall_status is None
        assert saved["pull-requests"][0]["pr-status"] == "build-pending"
        assert not should_green_leader_for_build_failure(saved)
        assert not should_trigger_early_gate(saved)
        assert updater.posts[0][1] == "pending"
        assert saved["pull-requests"][0]["build-observations"]["retry-history"]
        # Creator serialization and failure reporter both consume this receipt.
        from lib.state_file import StatePullRequest
        from scripts.gap_failure_reporter import build_child_row
        entry = saved["pull-requests"][0]
        assert StatePullRequest.from_mapping(entry, index=0).to_dict()["build-observations"] == entry["build-observations"]
        row = build_child_row(entry, gh_runner=runner, reuse_monitor_evidence=True)
        assert "build-retrigger-pending" in row.reason
        # Recovery proceeds to the existing EG hold, not a premature child green.
        item["status"]["conditions"][0].update(status="True", reason="Succeeded")
        result, updater = _run(path, runner)
        assert result.success_urls == [URL]
        assert updater.posts[0][1] == "pending"
        assert should_trigger_early_gate(_saved(path))
        assert _saved(path)["pull-requests"][0]["build-observations"]["retry-history"]
    else:
        assert not ledger.posts
        assert saved["pull-requests"][0]["pr-status"] == ("merge-failure" if feasibility_fails else "build-failure")


def test_new_sha_gets_its_own_budget(tmp_path):
    gh, client = FakeGitHub(), FakeKonflux()
    _, saved = apply(manager(gh, client), client)
    new_sha = "b" * 40
    gh.sha = new_sha
    item = attempt(name="new-head")
    item["metadata"]["labels"][PAC + "sha"] = new_sha
    item["metadata"]["annotations"]["build.appstudio.redhat.com/commit_sha"] = new_sha
    item["spec"]["params"][0]["value"] = new_sha
    client.items = [item]
    gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=new_sha,
        expected=[ExpectedCheck(key=COMPONENT, name_suffix=PIPELINE)], client=client)
    gate, evidence = manager(gh, client, saved=saved).apply(replace(PR, head_sha=new_sha), gate, evidence)
    assert gate.outcome == "pending" and len(gh.posts) == 2
    assert evidence["runs"][0]["retry"]["retrigger_count"] == 1
    assert len(evidence["retry-history"]) == 2


def test_retry_pending_with_another_persistent_failure_is_not_terminal():
    gh, client = FakeGitHub(), FakeKonflux()
    other = "odh-bad-v3-7-ea-1"
    item = attempt(reason="Failed", name="bad", component=other)
    item["status"]["conditions"][0]["message"] = "fips-check failed"
    client.items.append(item)
    gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=SHA,
        expected=[ExpectedCheck(key=c, name_suffix=PIPELINE) for c in (COMPONENT, other)], client=client)
    gate, _ = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pending"
    assert len(gh.posts) == 1
    assert [f.outcome for f in gate.findings] == ["pending", "fail"]


def test_ambiguous_pipeline_identity_does_not_send_a_broad_retest():
    gh, client = FakeGitHub(), FakeKonflux()
    item = client.items[0]
    item["metadata"]["annotations"].pop(PAC + "original-prname")
    # Missing pipeline identity is an unknown gate before retry policy sees it.
    gate, evidence = apply(manager(gh, client), client)
    assert gate.outcome == "pending" and not gh.posts


def test_wrong_retry_actor_cannot_cause_repeated_unrecorded_comments():
    gh, client = FakeGitHub(), FakeKonflux()
    apply(manager(gh, client), client)
    gh.comments[0]["user"].update(login="previous-app[bot]", type="Bot")
    with pytest.raises(ValueError, match="author differs"):
        apply(manager(gh, client), client)
    assert len(gh.posts) == 1


@pytest.mark.usefixtures("pac_cel_binary")
def test_archived_state_cannot_trigger_retry(tmp_path, monkeypatch):
    from tests.test_gap_repository_gates import _GitHub, _state, _Updater, _passing_check_runs
    from scripts.gap_pr_monitor import run_monitor
    from lib.konflux_builds import BuildObservation
    github = _GitHub()
    github.check_runs = _passing_check_runs()
    path = _state(tmp_path)
    archived = tmp_path / "GAP History" / "unsuccessful" / "record" / "state.json"
    archived.parent.mkdir(parents=True)
    payload = json.loads(path.read_text())
    payload["overall-status"] = "failure"
    path.write_text(json.dumps(payload))
    path.rename(archived)
    class Client:
        def observe(self, **kw):
            return BuildObservation("odh-eval-hub-v3-6", "timed_out", "PipelineRunTimeout", SHA)
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setenv("GAP_AUTO_RETRY_BUILDS", "true")
    monkeypatch.setattr(KonfluxClient, "from_environment", lambda **_: Client())
    # The fake GH runner rejects every unexpected comment/API mutation.
    run_monitor(archived, gh_runner=github, updater_factory=_Updater, now=NOW)


def test_pr_stable_branch_does_not_replace_component_release_branch():
    from lib.konflux_retry_evidence import component_push_branch
    client = FakeKonflux()
    assert component_push_branch(client, repo=REPO, component=COMPONENT) == "rhoai-3.7-ea.1"
    client._request_json = lambda _: {"kind": "Component", "metadata": {"name": COMPONENT, "namespace": client.namespace},
        "spec": {"source": {"git": {"url": "https://github.com/other/repo", "revision": PR.base_ref}}}}
    with pytest.raises(KonfluxQueryError):
        component_push_branch(client, repo=REPO, component=COMPONENT)


def test_next_poll_running_retry_keeps_mixed_failure_batch_pending():
    gh, client = FakeGitHub(), FakeKonflux()
    other = "odh-bad-v3-7-ea-1"
    bad = attempt(reason="Failed", name="bad", component=other)
    bad["status"]["conditions"][0]["message"] = "fips-check failed"
    client.items.append(bad)
    expected = [ExpectedCheck(key=c, name_suffix=PIPELINE) for c in (COMPONENT, other)]
    def poll(saved=None):
        gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=SHA, expected=expected, client=client)
        return manager(gh, client, saved=saved).apply(PR, gate, evidence)
    _, receipt = poll()
    newer = attempt(name="r2", created="2026-11-05T13:00:00Z")
    newer["status"]["conditions"][0].update(status="Unknown", reason="Running")
    newer["status"].pop("completionTime")
    client.items.append(newer)
    gate, receipt = poll(receipt)
    assert gate.outcome == "pending" and len(gh.posts) == 1
    # The remaining persistent defect becomes terminal only after the retry finishes.
    newer["status"]["conditions"][0].update(status="True", reason="Succeeded")
    newer["status"]["completionTime"] = "2026-11-05T14:00:00Z"
    gate, receipt = poll(receipt)
    assert gate.outcome == "fail" and len(gh.posts) == 1


def test_success_after_post_crash_still_recovers_state_audit():
    gh, client = FakeGitHub(), FakeKonflux()
    apply(manager(gh, client), client)
    client.items[0]["status"]["conditions"][0].update(status="True", reason="Succeeded")
    gate, evidence = apply(manager(gh, client), client)  # no saved local receipt
    assert gate.outcome == "pass" and len(evidence["retry-history"]) == 1


def test_human_quote_of_receipt_cannot_consume_the_retry_budget():
    gh, client = FakeGitHub(), FakeKonflux()
    apply(manager(gh, client), client)
    gh.comments[0]["user"] = {"login": "a-human", "type": "User"}
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending"
    assert receipt["runs"][0]["retry"]["retrigger_count"] == 1


def test_new_attempt_in_same_second_as_comment_is_not_treated_as_missing():
    gh, client = FakeGitHub(), FakeKonflux()
    apply(manager(gh, client), client)
    client.items = [attempt(name="r2", created=NOW.isoformat())]
    gate, receipt = apply(manager(gh, client, now=NOW + timedelta(minutes=20)), client)
    assert len(gh.posts) == 2
    assert receipt["runs"][0]["retry"]["retrigger_count"] == 2
    assert gate.outcome == "pending"


@pytest.mark.parametrize("reason", ["PipelineRunTimeout", "Cancelled"])
@pytest.mark.parametrize("retry_status", ["retry-exhausted", "retry-not-started"])
def test_leader_report_explains_terminal_retry_problem_and_links_attempt(reason, retry_status):
    from scripts.gap_failure_reporter import build_child_row
    gh, client = FakeGitHub(), FakeKonflux(attempt(reason))
    _, evidence = evaluate(client)
    evidence["pr-state"] = "OPEN"
    evidence["runs"][0]["url"] = evidence["runs"][0]["url"].replace(
        "https://konflux.example.com", "https://konflux-ui.apps.stone-prod-p02.hjvn.p1.openshiftapps.com")
    evidence["runs"][0]["retry"] = {"status": retry_status, "retrigger_count": 2,
        "reason": "Automatic retries stopped; manual investigation required"}
    row = build_child_row({"repo": "foo", "pr-url": PR.url, "pr-status": "build-failure",
                           "build-observations": evidence}, reuse_monitor_evidence=True)
    assert retry_status in row.reason
    assert "manual investigation required" in row.reason
    assert len(row.build_links) == 1


@pytest.mark.parametrize("initial,fresh_status,second_move", [
    ("build-failure", "success", False),
    ("build-failure", "build-pending", False),
    ("build-failure", "build-failure", True),
    ("success", "success", False),
])
def test_late_head_refresh_recomputes_hold_for_recovered_child_and_following_sibling(
        tmp_path, monkeypatch, initial, fresh_status, second_move):
    from scripts import gap_pr_monitor as monitor
    from tests.test_gap_pr_monitor import FakeUpdater, _gate
    from dataclasses import replace
    urls = ["https://github.com/acme/one/pull/1", "https://github.com/acme/two/pull/2"]
    path = tmp_path / "state.json"
    payload = {"pull-requests": [
        {"repo": str(n), "pr-url": url, "pr-status": "new", "head-sha": SHA}
        for n, url in enumerate(urls)]}
    if initial == "success":
        # A passing EG result belongs to the old heads and cannot release the new one.
        monitor.apply_overall_status(payload, "success")
    path.write_text(json.dumps(payload))
    calls = {url: 0 for url in urls}
    def decide(url, **kwargs):
        calls[url] += 1
        if url == urls[0] and calls[url] <= 2:
            return replace(_gate(url, initial, "failure" if initial == "build-failure" else "success"), head_sha=SHA)
        status = fresh_status if url == urls[0] else "success"
        cli_status = {"success": "success", "build-pending": "pending", "build-failure": "failure"}[status]
        return replace(_gate(url, status, cli_status), head_sha="b" * 40 if url == urls[0] else SHA)
    class MovedUpdater(FakeUpdater):
        def post_status_for_pr(self, url, status, **kwargs):
            result = super().post_status_for_pr(url, status, **kwargs)
            if url == urls[0] and (kwargs["head_sha"] == SHA or second_move):
                self.posts.pop()  # no stale status is actually published
                return replace(result, deferred=True)
            return result
    updater = MovedUpdater(check_name="gated artifacts promoter")
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setenv("GAP_AUTO_RETRY_BUILDS", "true")
    monkeypatch.setattr(monitor, "fetch_component_repo_map", lambda _: {})
    monkeypatch.setattr(monitor, "_decide_url", decide)
    result = monitor.run_monitor(path, gh_runner=lambda *_: "", updater_factory=lambda **_: updater)
    assert result.overall_status == ("early-gate-triggered" if fresh_status == "success" else None)
    assert len(updater.posts) == (1 if second_move else 2)
    assert all(status == "pending" for _, status, _ in updater.posts)
    saved = json.loads(path.read_text())
    gates = {gate["id"]: gate["status"] for gate in saved["promotion-gates"]}
    assert gates["early-gate-build"] == gates["early-gate-test"] == "pending"


@pytest.mark.parametrize("reason", ["PipelineRunTimeout", "Cancelled", "Failed"])
@pytest.mark.parametrize("age,waiting", [(timedelta(hours=1), True), (timedelta(hours=2), False), (timedelta(days=2), False)])
def test_optional_retry_evidence_failure_has_bounded_hold(reason, age, waiting):
    gh, client = FakeGitHub(), FakeKonflux(attempt(reason))
    completed = NOW - age
    client.items[0]["metadata"]["creationTimestamp"] = (completed - timedelta(minutes=1)).isoformat()
    client.items[0]["status"]["completionTime"] = completed.isoformat()
    def denied(*args, **kwargs):
        raise KonfluxQueryError("HTTP 403 reading TaskRuns")
    client._list_resources = denied
    gate, receipt = apply(manager(gh, client), client)
    assert not gh.posts
    assert gate.outcome == ("pending" if waiting else "fail")
    retry = receipt["runs"][0]["retry"]
    assert retry["status"] == "retry-evidence-unavailable"
    assert ("window expired" in retry["reason"]) is not waiting
    assert receipt["runs"][0]["status"] == observe(client.items).status


@pytest.mark.parametrize("problem", ["Archived PipelineRun pagination limit reached", "Cannot verify Component release branch"])
def test_persistent_push_health_error_cannot_hide_generic_build_failure(problem, monkeypatch):
    import lib.gap_build_retries as retries
    gh, client = FakeGitHub(), FakeKonflux(attempt("Failed"))
    client.tasks = [failed_task()]
    def unavailable(*args, **kwargs):
        raise KonfluxQueryError(problem)
    monkeypatch.setattr(retries, "component_push_branch", unavailable)
    gate, receipt = apply(manager(gh, client, now=NOW + timedelta(hours=1)), client)
    assert gate.outcome == "fail" and not gh.posts
    assert problem in receipt["runs"][0]["retry"]["reason"]


@pytest.mark.parametrize("move", ["head", "attempt"])
def test_new_identity_never_expires_into_failure_of_old_attempt(move):
    gh, client = FakeGitHub(), FakeKonflux()
    gate, receipt = evaluate(client)
    if move == "head":
        gh.sha = "b" * 40
    else:
        client.items.append(attempt(name="r2", created="2026-11-05T13:00:00Z"))
    gate, receipt = manager(gh, client, now=NOW + timedelta(days=3)).apply(PR, gate, receipt)
    assert gate.outcome == "pending" and not gh.posts
    assert receipt["runs"][0]["retry"]["status"] == "retry-observation-changed"


def test_malformed_post_response_does_not_archive_a_retry_that_was_delivered():
    gh, client = FakeGitHub(), FakeKonflux()
    def runner(args):
        response = gh(args)
        return "{" if "POST" in args else response
    # The observed failed attempt is older than the optional-evidence window.
    with pytest.raises(RuntimeError, match="Could not confirm retry delivery"):
        apply(manager(runner, client, now=NOW + timedelta(days=2)), client)
    assert len(gh.posts) == 1
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending" and len(gh.posts) == 1
    assert receipt["runs"][0]["retry"]["retrigger_count"] == 1


def test_lost_retry_response_during_late_refresh_holds_following_siblings(tmp_path, monkeypatch):
    from scripts import gap_pr_monitor as monitor
    from tests.test_gap_pr_monitor import FakeUpdater, _gate
    urls = ["https://github.com/acme/one/pull/1", "https://github.com/acme/two/pull/2"]
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"pull-requests": [
        {"repo": str(n), "pr-url": url, "pr-status": "new", "head-sha": SHA}
        for n, url in enumerate(urls)]}))
    calls = {url: 0 for url in urls}
    def decide(url, **kwargs):
        calls[url] += 1
        if url == urls[0]:
            if calls[url] == 3:
                raise RuntimeError("Could not confirm retry delivery")
            return replace(_gate(url, "build-failure", "failure"), head_sha=SHA)
        return replace(_gate(url, "success", "success"), head_sha=SHA)
    class MovedUpdater(FakeUpdater):
        def post_status_for_pr(self, url, status, **kwargs):
            result = super().post_status_for_pr(url, status, **kwargs)
            if url == urls[0]:
                self.posts.pop()
                return replace(result, deferred=True)
            return result
    updater = MovedUpdater(check_name="gated artifacts promoter")
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setenv("GAP_AUTO_RETRY_BUILDS", "true")
    monkeypatch.setattr(monitor, "fetch_component_repo_map", lambda _: {})
    monkeypatch.setattr(monitor, "_decide_url", decide)
    with pytest.raises(monitor.GapPrMonitorError, match="Could not confirm retry delivery"):
        monitor.run_monitor(path, gh_runner=lambda *_: "", updater_factory=lambda **_: updater,
                            continue_on_error=True)
    assert [(url, status) for url, status, _ in updater.posts] == [(urls[1], "pending")]
    saved = json.loads(path.read_text())
    assert saved["pull-requests"][0]["pr-status"] == "build-pending"
    assert monitor.compute_overall_status(saved) is None
