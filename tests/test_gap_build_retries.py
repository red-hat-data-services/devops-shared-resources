"""Retry actions use fake APIs only; no live /retest comments or builds."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from lib.gap_build_retries import BuildRetryManager
from lib.gap_pr_gates import RepositoryPullRequest
from lib.github_check_runs import CheckQueryError, ExpectedCheck
from lib.github_cli import GhCommandError
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

    def prefetch_prs_chunked(self, queries, **kwargs):
        pass

    def _list_resources(self, selector, resource="pipelineruns"):
        return self.tasks if resource == "taskruns" else self.pushes


class FakeGitHub:
    def __init__(self, *, post_login="rhods-devops-app[bot]"):
        self.comments, self.posts = [], []
        self.sha, self.state = SHA, "open"
        self.fail_after_post = False
        self.fail_check_lookup = False
        self.post_login = post_login

    def __call__(self, args):
        endpoint = args[1]
        if "POST" in args:
            body = args[-1].removeprefix("body=")
            self.posts.append(body)
            comment = {"id": len(self.comments) + 1, "html_url": "https://github.com/acme/foo/pull/123#issuecomment-1",
                       "user": {"login": self.post_login}, "body": body,
                       "created_at": NOW.isoformat()}
            self.comments.append(comment)
            if self.fail_after_post:
                raise RuntimeError("Connection lost after GitHub accepted comment")
            return json.dumps(comment)
        if "/comments" in endpoint:
            return json.dumps(self.comments)
        if "/check-runs" in endpoint:
            if self.fail_check_lookup:
                raise GhCommandError(["gh", *args], 1, "HTTP 503")
            return json.dumps({"check_runs": getattr(self, "check_runs", [])})
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
def test_failed_build_retry_uses_retest_and_keeps_original_build_truth(reason):
    gh, client = FakeGitHub(), FakeKonflux(attempt(reason))
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending"
    assert len(gh.posts) == 1 and gh.posts[0].startswith("/retest\n")
    assert PIPELINE in gh.posts[0]
    record = receipt["runs"][0]
    assert record["status"] in {"timed_out", "cancelled"}  # EG never sees fabricated success
    assert record["retry"]["status"] == "build-retrigger-pending"
    assert record["retry"]["retrigger_count"] == 1


@pytest.mark.parametrize("completion", ["invalid", "2026-11-05T09:00:00Z"])
def test_unusual_completion_still_retries_via_retest(completion):
    """Completion metadata does not gate /retest eligibility."""
    gh, client = FakeGitHub(), FakeKonflux()
    client.items[0]["status"]["completionTime"] = completion
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending"
    assert len(gh.posts) == 1 and gh.posts[0].startswith("/retest\n")
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
    for i in range(2, 6):  # retries 2 through 5
        client.items = [attempt(name=f"r{i}", created=f"2026-11-05T{9 + i}:00:00Z")]
        _, saved = apply(manager(gh, client, saved=saved, now=NOW + timedelta(hours=i * 2)), client)
    assert len(gh.posts) == 5
    client.items = [attempt(name="r6", created="2026-11-05T22:00:00Z")]
    gate, receipt = apply(manager(gh, client, saved=saved, now=NOW + timedelta(hours=14)), client)
    assert len(gh.posts) == 5 and gate.outcome == "fail"
    assert receipt["runs"][0]["retry"]["status"] == "retry-exhausted"


def test_final_retry_receipt_grace_precedes_retry_budget():
    gh, client = FakeGitHub(), FakeKonflux()
    _, saved = apply(manager(gh, client), client)
    for i in range(2, 6):  # retries 2 through 5
        client.items = [attempt(name=f"r{i}", created=f"2026-11-05T{9 + i}:00:00Z")]
        _, saved = apply(manager(gh, client, saved=saved, now=NOW + timedelta(hours=i * 2)), client)
    # The fake response uses a fixed timestamp; model GitHub's actual receipt time.
    gh.comments[-1]["created_at"] = (NOW + timedelta(hours=10)).isoformat()
    gate, receipt = apply(manager(gh, client, saved=saved, now=NOW + timedelta(hours=10, minutes=5)), client)
    assert gate.outcome == "pending"
    assert len(gh.posts) == 5
    assert receipt["runs"][0]["retry"]["status"] == "build-retrigger-pending"


def test_retry_grace_period_holds_pending_without_new_comment():
    gh, client = FakeGitHub(), FakeKonflux()
    _, saved = apply(manager(gh, client), client)
    gate, receipt = apply(manager(gh, client, saved=saved, now=NOW + timedelta(minutes=5)), client)
    assert gate.outcome == "pending"
    assert not len(gh.posts) > 1
    assert receipt["runs"][0]["retry"]["status"] == "build-retrigger-pending"


def test_failed_retry_uses_second_budget_slot_after_grace_expires():
    gh, client = FakeGitHub(), FakeKonflux()
    apply(manager(gh, client), client)
    # After grace expires with the same failing attempt, the second budget
    # slot is used rather than leaving the component permanently stuck.
    gate, receipt = apply(manager(gh, client, now=NOW + timedelta(hours=2)), client)
    assert gate.outcome == "pending" and len(gh.posts) == 2
    assert receipt["runs"][0]["retry"]["retrigger_count"] == 2


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


def test_retest_is_posted_even_when_new_attempt_appeared_since_evaluation():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, receipt = evaluate(client)
    client.items.append(attempt(name="r2", created="2026-11-05T13:00:00Z"))
    gate, _ = manager(gh, client).apply(PR, gate, receipt)
    # /retest is safe regardless of concurrent attempts; PaC deduplicates.
    assert gate.outcome == "pending" and len(gh.posts) == 1


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


def test_all_failures_are_retried_via_retest():
    gh, client = FakeGitHub(), FakeKonflux(attempt("Failed"))
    gate, receipt = apply(manager(gh, client), client)
    assert len(gh.posts) == 1 and gh.posts[0].startswith("/retest\n")
    assert gate.outcome == "pending"



def missing_evidence(client, *, cel=True):
    client.items = []
    gate, evidence = evaluate(client)
    evidence.update({
        "expected-components": [COMPONENT],
        "cel-evaluated": cel,
        "required-pipelines": {
            "pipelines": [{"component": COMPONENT, "pipeline_name": PIPELINE}],
        },
    })
    return gate, evidence


def test_expected_missing_build_is_retriggered_when_no_github_check_exists():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert len(gh.posts) == 1
    assert gh.posts[0].startswith("/retest\n\n")
    assert PIPELINE in gh.posts[0]
    assert evidence["runs"][0]["retry"]["status"] == "build-retrigger-pending"
    assert evidence["retry-history"][0]["run_uid"].startswith("missing:")


def test_multiple_ambiguous_missing_components_post_one_bare_retest_and_share_receipt():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    second = dict(evidence["runs"][0])
    second["component"] = "odh-other-v3-7-ea-1"
    evidence["runs"].append(second)
    evidence["expected-components"].append(second["component"])
    evidence["required-pipelines"]["pipelines"].append({
        "component": second["component"], "pipeline_name": "ambiguous-pipeline"
    })
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert len(gh.posts) == 1
    assert gh.posts[0].startswith("/retest\n\n")
    assert len(evidence["retry-history"]) == 2
    assert {r["comment_id"] for r in evidence["retry-history"]} == {1}
    restarted = manager(gh, client, now=NOW + timedelta(minutes=20))
    _, recovered = restarted.apply(PR, *missing_evidence(client))
    assert len(gh.posts) == 1
    assert len(recovered["retry-history"]) == 2




def test_failed_and_missing_components_share_one_retest_comment():
    gh, client = FakeGitHub(), FakeKonflux(attempt("PipelineRunTimeout"))
    gate, evidence = evaluate(client)
    second = {
        "component": "odh-bar-v3-6", "status": "missing",
        "reason": "No matching current-head Konflux PipelineRun found",
        "head_sha": SHA, "name": "", "uid": "", "namespace": "",
        "created_at": "", "url": "", "pipeline_name": "",
        "completed_at": "", "message": "",
    }
    evidence["runs"].append(second)
    evidence["expected-components"] = [COMPONENT, second["component"]]
    evidence["required-pipelines"] = {"pipelines": [
        {"component": COMPONENT, "pipeline_name": PIPELINE},
        {"component": second["component"], "pipeline_name": "bar-on-pull-request-123"},
    ]}
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pending"
    # Both failed and missing components share the /retest batch.
    assert all(post.startswith("/retest\n") for post in gh.posts)
    assert len(evidence["retry-history"]) >= 2
    assert {r["component"] for r in evidence["retry-history"]} == {COMPONENT, second["component"]}


def test_missing_component_always_posts_bare_retest_even_with_specific_pipeline():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    evidence["required-pipelines"]["pipelines"][0]["pipeline_name"] = "foo-on-pull-request-123"
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert gh.posts[0].startswith("/retest\n\n")


def test_bare_retest_receipt_is_reconciled_without_repeating_missing_retry():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, saved = manager(gh, client).apply(PR, *missing_evidence(client))
    assert gh.posts[0].startswith("/retest\n\n")
    gate, evidence = manager(gh, client, saved=saved, now=NOW + timedelta(minutes=20)).apply(PR, *missing_evidence(client))
    assert gate.outcome == "pass"  # missing build = green by default
    assert len(gh.posts) == 1
    assert evidence["runs"][0]["retry"]["status"] == "build-retrigger-pending"


def test_missing_build_waits_when_github_check_already_exists():
    gh, client = FakeGitHub(), FakeKonflux()
    gh.check_runs = [{"head_sha": SHA, "name": PIPELINE, "status": "queued"}]
    gate, evidence = missing_evidence(client)
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert not gh.posts
    assert evidence["runs"][0]["retry"]["status"] == "build-trigger-pending"


def test_missing_build_without_cel_evidence_stays_blocked_without_retry():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client, cel=False)
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert not gh.posts
    assert evidence["runs"][0]["retry"]["status"] == "not-retried"


def test_failed_build_retries_without_needing_konflux_evidence():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, receipt = apply(manager(gh, client), client)
    # /retest does not query Konflux for task evidence; it always retries.
    assert gate.outcome == "pending" and len(gh.posts) == 1
    assert receipt["runs"][0]["retry"]["status"] == "build-retrigger-pending"

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
    elif enabled and feasibility_fails:
        # Build retries fire even on merge-failure PRs: failed builds are
        # retried regardless of feasibility state so that build status is
        # current when the feasibility issue is resolved.
        assert len(ledger.posts) == 1
        assert saved["pull-requests"][0]["pr-status"] == "merge-failure"
        assert saved["pull-requests"][0]["build-observations"]["retry-history"]
    else:
        assert not ledger.posts
        assert saved["pull-requests"][0]["pr-status"] == "build-failure"


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


def test_all_failed_components_are_retried_regardless_of_error_message():
    gh, client = FakeGitHub(), FakeKonflux()
    other = "odh-bad-v3-7-ea-1"
    item = attempt(reason="Failed", name="bad", component=other)
    item["status"]["conditions"][0]["message"] = "fips-check failed"
    client.items.append(item)
    gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=SHA,
        expected=[ExpectedCheck(key=c, name_suffix=PIPELINE) for c in (COMPONENT, other)], client=client)
    gate, _ = manager(gh, client).apply(PR, gate, evidence)
    # All failures get /retest regardless of error message classification.
    assert gate.outcome == "pending"
    assert all(f.outcome == "pending" for f in gate.findings)


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


def test_next_poll_running_retry_keeps_batch_pending_until_completion():
    gh, client = FakeGitHub(), FakeKonflux()
    expected = [ExpectedCheck(key=COMPONENT, name_suffix=PIPELINE)]
    def poll(saved=None):
        gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=SHA, expected=expected, client=client)
        return manager(gh, client, saved=saved).apply(PR, gate, evidence)
    _, receipt = poll()
    newer = attempt(name="r2", created="2026-11-05T13:00:00Z")
    newer["status"]["conditions"][0].update(status="Unknown", reason="Running")
    newer["status"].pop("completionTime")
    client.items.append(newer)
    gate, receipt = poll(receipt)
    assert gate.outcome == "pending"
    # Once the retry succeeds, the batch completes.
    newer["status"]["conditions"][0].update(status="True", reason="Succeeded")
    newer["status"]["completionTime"] = "2026-11-05T14:00:00Z"
    gate, receipt = poll(receipt)
    assert gate.outcome == "pass"


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


def test_new_attempt_within_grace_period_does_not_trigger_second_retest():
    gh, client = FakeGitHub(), FakeKonflux()
    apply(manager(gh, client), client)
    client.items = [attempt(name="r2", created=NOW.isoformat())]
    gate, receipt = apply(manager(gh, client, now=NOW + timedelta(minutes=20)), client)
    # Within the 2-hour grace period, no second retry is posted.
    assert len(gh.posts) == 1
    assert receipt["runs"][0]["retry"]["status"] == "build-retrigger-pending"
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
def test_failed_build_retries_regardless_of_age(reason):
    gh, client = FakeGitHub(), FakeKonflux(attempt(reason))
    gate, receipt = apply(manager(gh, client), client)
    # Simplified /retest path does not check age or task evidence.
    assert len(gh.posts) == 1
    assert gate.outcome == "pending"
    assert receipt["runs"][0]["retry"]["status"] == "build-retrigger-pending"


def test_head_change_before_retry_prevents_stale_retest():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, receipt = evaluate(client)
    gh.sha = "b" * 40
    gate, receipt = manager(gh, client, now=NOW + timedelta(days=3)).apply(PR, gate, receipt)
    assert gate.outcome == "pending" and not gh.posts
    assert receipt["runs"][0]["retry"]["status"] == "retry-evidence-unavailable"


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


def test_similar_pipeline_check_does_not_suppress_missing_retry():
    gh, client = FakeGitHub(), FakeKonflux()
    gh.check_runs = [{"head_sha": SHA, "name": PIPELINE + "-extra", "status": "queued"}]
    gate, evidence = missing_evidence(client)
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert len(gh.posts) == 1


def test_missing_retry_receipts_use_unique_ids_and_exhaust_budget():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    _, saved = manager(gh, client).apply(PR, gate, evidence)
    assert saved["retry-history"][0]["run_uid"].endswith(":1")

    for i in range(2, 6):  # retries 2 through 5
        gate, evidence = missing_evidence(client)
        _, saved = manager(gh, client, saved=saved, now=NOW + timedelta(hours=i * 2)).apply(PR, gate, evidence)
        assert len(gh.posts) == i
        assert saved["retry-history"][i - 1]["run_uid"].endswith(f":{i}")

    gate, evidence = missing_evidence(client)
    gate, evidence = manager(gh, client, saved=saved, now=NOW + timedelta(hours=14)).apply(PR, gate, evidence)
    assert len(gh.posts) == 5
    assert evidence["runs"][0]["retry"]["status"] == "retry-exhausted"


def test_empty_runs_are_normalized_to_missing_expected_components():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    evidence["runs"] = []
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert len(gh.posts) == 1
    assert evidence["runs"][0]["status"] == "missing"


def test_no_build_expected_does_not_trigger_retest():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=SHA, expected=[], client=client)
    evidence.update({"runs": [], "expected-components": [], "cel-evaluated": True})
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert not gh.posts
    assert gate.outcome == "pass"


def test_multiple_missing_components_only_retrigger_the_component_without_a_check():
    gh, client = FakeGitHub(), FakeKonflux()
    gh.check_runs = [{"head_sha": SHA, "name": PIPELINE, "status": "queued"}]
    gate, evidence = missing_evidence(client)
    second_component = "odh-bar-v3-6"
    second_pipeline = "bar-on-pull-request"
    second = dict(evidence["runs"][0])
    second.update(component=second_component)
    evidence["runs"] = [evidence["runs"][0], second]
    evidence["expected-components"] = [COMPONENT, second_component]
    evidence["required-pipelines"]["pipelines"].append(
        {"component": second_component, "pipeline_name": second_pipeline}
    )
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert len(gh.posts) == 1
    assert gh.posts[0].startswith("/retest\n\n")
    assert second_pipeline in gh.posts[0]


def test_fresh_missing_component_defers_shared_retest_for_member_in_grace():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    second = dict(evidence["runs"][0])
    second["component"] = "odh-other-v3-7-ea-1"
    evidence["runs"] = [evidence["runs"][0], second]
    evidence["expected-components"] = [COMPONENT, second["component"]]
    evidence["required-pipelines"]["pipelines"].append({
        "component": second["component"], "pipeline_name": "other-on-pull-request-123"
    })
    existing = {
        "component": second["component"], "sha": PR.head_sha,
        "pipeline": "other-on-pull-request-123",
        "run_uid": "missing:other-on-pull-request-123:" + PR.head_sha + ":1",
        "requested_at": (NOW - timedelta(minutes=5)).isoformat(),
    }
    saved = {"pull-requests": [{"pr-url": PR.url, "build-observations": {"retry-history": [existing]}}]}
    mgr = BuildRetryManager(gh, saved, client_factory=lambda: client, clock=lambda: NOW)
    gate, evidence = mgr.apply(PR, gate, evidence)
    assert not gh.posts
    assert gate.outcome == "pass"  # missing build = green by default
    assert "grace period" in evidence["runs"][0]["retry"]["reason"]


def test_exhausted_missing_component_blocks_shared_retest_for_other_missing_component():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    second = dict(evidence["runs"][0])
    second["component"] = "odh-other-v3-7-ea-1"
    evidence["runs"] = [evidence["runs"][0], second]
    evidence["expected-components"] = [COMPONENT, second["component"]]
    evidence["required-pipelines"]["pipelines"].append({
        "component": second["component"], "pipeline_name": "other-on-pull-request-123"
    })
    existing = {
        "component": second["component"], "sha": PR.head_sha,
        "pipeline": "other-on-pull-request-123",
        "run_uid": "missing:other-on-pull-request-123:" + PR.head_sha + ":1",
        "requested_at": NOW.isoformat(),
    }
    saved = {"pull-requests": [{"pr-url": PR.url, "build-observations": {"retry-history": [existing]}}]}
    # Additional receipts reach the maximum for the other component.
    for n in range(2, 6):
        saved["pull-requests"][0]["build-observations"]["retry-history"].append(
            dict(existing, run_uid=existing["run_uid"].replace(":1", f":{n}"),
                 requested_at=(NOW + timedelta(hours=n - 1)).isoformat())
        )
    mgr = BuildRetryManager(gh, saved, client_factory=lambda: client, clock=lambda: NOW)
    gate, evidence = mgr.apply(PR, gate, evidence)
    assert not gh.posts
    assert gate.outcome == "pass"  # missing build = green by default


def test_ignored_child_never_retriggers_missing_or_failed_builds():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    payload = {"pull-requests": [{"pr-url": PR.url, "ignore": True}]}
    ignored = BuildRetryManager(gh, payload, client_factory=lambda: client, clock=lambda: NOW)
    gate, evidence = ignored.apply(PR, gate, evidence)
    assert not gh.posts


def test_missing_retry_check_lookup_failure_is_recorded_as_unavailable():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    retry_manager = manager(gh, client)

    def unavailable(_pr, _pipeline):
        raise CheckQueryError("GitHub check API unavailable")

    retry_manager._pipeline_check_exists = unavailable
    gate, evidence = retry_manager.apply(PR, gate, evidence)
    assert gate.outcome == "pending"  # evidence unavailable: stay pending, not green
    assert not gh.posts
    assert evidence["runs"][0]["retry"]["status"] == "retry-evidence-unavailable"


def test_missing_retry_cli_check_lookup_failure_is_recorded_as_unavailable():
    gh, client = FakeGitHub(), FakeKonflux()
    gh.fail_check_lookup = True
    gate, evidence = missing_evidence(client)
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pending"  # evidence unavailable: stay pending, not green
    assert not gh.posts
    assert evidence["runs"][0]["retry"]["status"] == "retry-evidence-unavailable"


def test_missing_build_with_evidence_unavailable_stays_pending_not_green():
    """When a missing observation hits CheckQueryError during retry, the finding
    must stay PENDING — not PASS — because we cannot verify whether a real build
    appeared.  Missing = green only applies when evidence is successfully checked."""
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    retry_manager = manager(gh, client)

    def raise_error(_pr, _pipeline):
        raise CheckQueryError("Transient API failure")

    retry_manager._pipeline_check_exists = raise_error
    gate, evidence = retry_manager.apply(PR, gate, evidence)
    assert gate.outcome == "pending"
    assert evidence["runs"][0]["status"] == "missing"
    assert evidence["runs"][0]["retry"]["status"] == "retry-evidence-unavailable"
    finding = gate.findings[0]
    assert finding.outcome == "pending"


def test_unmatched_running_observation_cannot_make_a_passing_gate():
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=SHA, expected=[], client=client)
    evidence["runs"] = [{
        "component": COMPONENT,
        "status": "running",
        "reason": "PipelineRun is still running",
        "head_sha": SHA,
        "name": "pipeline-run",
    }]
    gate, evidence = manager(gh, client).apply(PR, gate, evidence)
    assert gate.outcome == "pending"
    assert not gh.posts


def test_retry_comment_token_swaps_gh_token_for_post(monkeypatch):
    """When GAP_RETRY_COMMENT_TOKEN is set the POST uses it and restores the original."""
    monkeypatch.setenv("GAP_RETRY_COMMENT_TOKEN", "cibot-pat-token")
    monkeypatch.setenv("GH_TOKEN", "app-token-original")
    import os
    captured_tokens = []
    gh = FakeGitHub(post_login="rhods-ci-bot")
    original_call = gh.__call__

    def capturing_runner(args):
        if "POST" in args:
            captured_tokens.append(os.environ.get("GH_TOKEN"))
        return original_call(args)

    gh_wrapper = type(gh)()
    gh_wrapper.__class__ = type(gh)
    client = FakeKonflux()
    mgr = manager(capturing_runner, client)
    gate, receipt = apply(mgr, client)
    assert gate.outcome == "pending"
    assert captured_tokens == ["cibot-pat-token"]
    # Original token is restored after the POST.
    assert os.environ.get("GH_TOKEN") == "app-token-original"


def test_retry_comment_token_restores_absent_gh_token(monkeypatch):
    """When GH_TOKEN was not set originally, it is removed after the POST."""
    monkeypatch.setenv("GAP_RETRY_COMMENT_TOKEN", "cibot-pat-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    import os
    gh = FakeGitHub(post_login="rhods-ci-bot")
    client = FakeKonflux()
    mgr = manager(gh, client)
    gate, receipt = apply(mgr, client)
    assert gate.outcome == "pending"
    assert os.environ.get("GH_TOKEN") is None


def test_retry_comment_token_sets_actor_to_cibot(monkeypatch):
    """GAP_RETRY_COMMENT_TOKEN presence defaults the actor to rhods-ci-bot."""
    monkeypatch.setenv("GAP_RETRY_COMMENT_TOKEN", "cibot-pat-token")
    gh = FakeGitHub(post_login="rhods-ci-bot")
    client = FakeKonflux()
    mgr = manager(gh, client)
    assert mgr.actor == "rhods-ci-bot"


def test_old_actor_receipts_skipped_during_migration(monkeypatch):
    """Old-actor retry comments are ignored when GAP_RETRY_COMMENT_TOKEN is set."""
    monkeypatch.setenv("GAP_RETRY_COMMENT_TOKEN", "cibot-pat-token")
    gh = FakeGitHub(post_login="rhods-ci-bot")
    client = FakeKonflux()
    # First retry posted by old actor (simulate pre-migration comment).
    apply(manager(gh, client), client)
    # Rewrite the comment to look like it came from the old actor.
    gh.comments[0]["user"]["login"] = "rhods-devops-app[bot]"
    gh.comments[0]["user"]["type"] = "Bot"
    # Second poll should post a new retry (old receipt is not counted).
    gate, receipt = apply(manager(gh, client), client)
    assert gate.outcome == "pending"
    assert len(gh.posts) == 2
    assert receipt["runs"][0]["retry"]["retrigger_count"] == 1


def test_saved_records_cleared_during_actor_migration(monkeypatch):
    """Saved retry records from the old actor are ignored during migration.

    Unlike comment-based records (which carry an author and are filtered in
    _history), saved records from state.json don't store the author.  When
    GAP_RETRY_COMMENT_TOKEN is set and the actor differs from the old default,
    saved records are cleared so the new actor's budget starts fresh.
    """
    monkeypatch.setenv("GAP_RETRY_COMMENT_TOKEN", "cibot-pat-token")
    gh = FakeGitHub(post_login="rhods-ci-bot")
    client = FakeKonflux()
    # Simulate saved records that would exhaust the budget under the old actor.
    old_saved = {"retry-history": [
        {
            "component": COMPONENT, "sha": PR.head_sha,
            "pipeline": PIPELINE, "run_uid": f"old:{i}",
            "retrigger_count": i, "requested_at": (NOW + timedelta(hours=i)).isoformat(),
            "repository": f"{PR.owner}/{PR.repo}", "pr": PR.number,
        }
        for i in range(1, 6)
    ]}
    # With 5 old-actor saved records the budget would be exhausted, but during
    # migration saved records are ignored and the new actor can retry.
    mgr = manager(gh, client, saved=old_saved, now=NOW + timedelta(hours=12))
    gate, receipt = apply(mgr, client)
    assert gate.outcome == "pending"
    assert len(gh.posts) == 1
    assert receipt["runs"][0]["retry"]["retrigger_count"] == 1


def test_saved_records_counted_without_migration():
    """Without GAP_RETRY_COMMENT_TOKEN saved records count toward the budget."""
    gh, client = FakeGitHub(), FakeKonflux()
    old_saved = {"retry-history": [
        {
            "component": COMPONENT, "sha": PR.head_sha,
            "pipeline": PIPELINE, "run_uid": f"old:{i}",
            "retrigger_count": i, "requested_at": (NOW - timedelta(hours=10 - i)).isoformat(),
            "repository": f"{PR.owner}/{PR.repo}", "pr": PR.number,
        }
        for i in range(1, 6)
    ]}
    mgr = manager(gh, client, saved=old_saved, now=NOW + timedelta(hours=12))
    gate, receipt = apply(mgr, client)
    assert gate.outcome == "fail"
    assert not gh.posts
    assert receipt["runs"][0]["retry"]["status"] == "retry-exhausted"


# --- Change 1: build retries are decoupled from merge feasibility ---


def test_retry_applies_on_merge_failure_pr():
    """Retries fire for failed builds on merge-failure PRs (decoupled from feasibility)."""
    gh, client = FakeGitHub(), FakeKonflux()
    # Simulate a merge-failure PR (the retry manager should still apply).
    merge_failure_pr = replace(PR, mergeable="CONFLICTING", merge_state_status="DIRTY")
    gate, evidence = evaluate(client)
    mgr = manager(gh, client)
    # With the decoupling, retries should be applied even though the final
    # decision will be merge-failure.
    gate_out, evidence_out = mgr.apply(merge_failure_pr, gate, evidence)
    # The retry was posted (the build had a failure that was retried).
    assert len(gh.posts) == 1
    assert "/retest" in gh.posts[0]
    assert evidence_out["retry-history"]


def test_retry_skipped_for_merged_pr():
    """Retries should NOT fire for already-merged PRs."""
    gh, client = FakeGitHub(), FakeKonflux()
    merged_pr = replace(PR, state="MERGED")
    gate, evidence = evaluate(client)
    mgr = manager(gh, client)
    gate_out, evidence_out = mgr.apply(merged_pr, gate, evidence)
    # Merged PRs are skipped by the retry manager's own guard.
    assert not gh.posts


def test_merge_conflict_pr_gets_konflux_observations(tmp_path, monkeypatch):
    """Merge-conflicted PRs still get Konflux build observations for retries."""
    from tests.test_gap_repository_gates import _GitHub, _state, _run, _saved
    github = _GitHub()
    github.mergeable = "CONFLICTING"
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setattr(KonfluxClient, "from_environment", lambda **_: FakeKonflux())
    path = _state(tmp_path)
    result, _ = _run(path, github)
    saved = _saved(path)
    child = saved["pull-requests"][0]
    assert child["pr-status"] == "merge-failure"
    obs = child["build-observations"]
    assert obs["source"] == "konflux"
    # Check runs are fetched and Konflux gate is evaluated even for
    # merge-conflicted PRs.
    assert "head-sha" in obs


# --- retrigger-stale verification ---


def test_retrigger_stale_detected_when_same_uid_persists_after_verify_grace():
    """Same UID + >15min elapsed + failure status → waiting=False (OUTCOME_FAIL)."""
    gh, client = FakeGitHub(), FakeKonflux()
    # First apply: posts /retest, records retry with run_uid = observation uid.
    _, saved = apply(manager(gh, client), client)
    # Second apply: same observation (same uid), 20 min later (>15min verify grace, <2h START_GRACE).
    gate, receipt = apply(manager(gh, client, saved=saved, now=NOW + timedelta(minutes=20)), client)
    assert gate.outcome == "fail"
    assert receipt["runs"][0]["retry"]["status"] == "retrigger-stale"
    assert "No new PipelineRun after 20min" in receipt["runs"][0]["retry"]["reason"]
    assert receipt["runs"][0]["retry"]["run_uid"]
    assert len(gh.posts) == 1  # no second retry posted


def test_retrigger_stale_not_triggered_within_verify_grace():
    """Same UID but <15min elapsed → waiting=True (still in verify grace)."""
    gh, client = FakeGitHub(), FakeKonflux()
    _, saved = apply(manager(gh, client), client)
    # 10 min later: within the 15-min verify grace window.
    gate, receipt = apply(manager(gh, client, saved=saved, now=NOW + timedelta(minutes=10)), client)
    assert gate.outcome == "pending"
    assert receipt["runs"][0]["retry"]["status"] == "build-retrigger-pending"
    assert len(gh.posts) == 1


def test_retrigger_stale_not_triggered_when_new_pipelinerun_created():
    """Different UID (new PipelineRun created) → waiting=True."""
    gh, client = FakeGitHub(), FakeKonflux()
    _, saved = apply(manager(gh, client), client)
    # PaC created a new PipelineRun with a different UID (still failing).
    client.items = [attempt(name="r2", created="2026-11-05T12:05:00Z")]
    gate, receipt = apply(manager(gh, client, saved=saved, now=NOW + timedelta(minutes=20)), client)
    assert gate.outcome == "pending"
    assert receipt["runs"][0]["retry"]["status"] == "build-retrigger-pending"
    assert len(gh.posts) == 1


def test_retrigger_stale_not_triggered_with_empty_uids():
    """Empty UIDs → waiting=True (can't verify, stay pending)."""
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    _, saved = manager(gh, client).apply(PR, gate, evidence)
    # Missing observations have empty UIDs; stale check cannot apply.
    gate, evidence = missing_evidence(client)
    gate, evidence = manager(gh, client, saved=saved, now=NOW + timedelta(minutes=20)).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert evidence["runs"][0]["retry"]["status"] == "build-retrigger-pending"
    assert len(gh.posts) == 1


def test_retrigger_stale_not_triggered_for_missing_observation_status():
    """Observation status is 'missing' → waiting=True (not in STALE_VERIFIABLE_STATUSES)."""
    gh, client = FakeGitHub(), FakeKonflux()
    gate, evidence = missing_evidence(client)
    _, saved = manager(gh, client).apply(PR, gate, evidence)
    # Even if UIDs somehow matched, 'missing' status is not stale-verifiable.
    gate, evidence = missing_evidence(client)
    gate, evidence = manager(gh, client, saved=saved, now=NOW + timedelta(minutes=20)).apply(PR, gate, evidence)
    assert gate.outcome == "pass"  # missing build = green by default
    assert evidence["runs"][0]["retry"]["status"] == "build-retrigger-pending"
