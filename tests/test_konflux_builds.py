"""Real-shape identity, latest-attempt and API regression tests for Konflux."""
import base64
import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from lib.konflux_builds import (
    KonfluxClient, KonfluxQueryError, select_latest, build_status_source,
)
from lib.konflux_gate import evaluate_konflux_gate
from lib.github_check_runs import ExpectedCheck
from lib.state_file import StatePullRequest

SHA = "a" * 40
REPO = "acme/foo"
COMPONENT = "odh-foo-v3-7-ea-1"


def run(*, name="foo-pull-request-r1", created="2026-11-05T10:00:00Z",
        status="True", reason="Succeeded", component=COMPONENT):
    return {
        "apiVersion": "tekton.dev/v1", "kind": "PipelineRun",
        "metadata": {
            "name": name, "uid": "uid-" + name, "namespace": "rhoai-tenant",
            "creationTimestamp": created,
            "labels": {
                "appstudio.openshift.io/component": component,
                "pipelines.appstudio.openshift.io/type": "build",
                "pipelinesascode.tekton.dev/url-org": "acme",
                "pipelinesascode.tekton.dev/url-repository": "foo",
                "pipelinesascode.tekton.dev/pull-request": "123",
                "pipelinesascode.tekton.dev/sha": SHA,
                "pipelinesascode.tekton.dev/event-type": "on-comment",
            },
            "annotations": {
                "pipelinesascode.tekton.dev/repo-url": "https://github.com/acme/foo",
                "build.appstudio.openshift.io/repo": "https://github.com/acme/foo?rev=" + SHA,
                "build.appstudio.redhat.com/pull_request_number": "123",
                "build.appstudio.redhat.com/commit_sha": SHA,
            },
        },
        "spec": {"params": [{"name": "revision", "value": SHA},
                            {"name": "git-url", "value": "https://github.com/contributor/foo"}]},
        "status": {"conditions": [{"type": "Succeeded", "status": status, "reason": reason}],
                   **({"completionTime": "2026-11-05T10:01:00Z"} if status != "Unknown" else {})},
    }


def observe(runs, **kwargs):
    return select_latest(runs, repo=REPO, number=123, sha=SHA,
                         components={COMPONENT}, namespace="rhoai-tenant", **kwargs)


@pytest.mark.parametrize("value,reason,expected", [
    ("True", "Succeeded", "success"),
    ("False", "Failed", "failure"),
    ("False", "PipelineRunTimeout", "timed_out"),
    ("False", "PipelineRunCancelled", "cancelled"),
    ("Unknown", "PipelineRunPending", "pending"),
    ("Unknown", "Running", "pending"),
    ("???", "Unknown", "unknown"),
])
def test_status_comes_from_tekton_condition(value, reason, expected):
    actual = observe([run(status=value, reason=reason)])
    assert actual.status == expected
    assert actual.url.endswith("/pipelinerun/foo-pull-request-r1")
    assert actual.uid == "uid-foo-pull-request-r1"


@pytest.mark.parametrize("status,reason,expected", [
    ("False", "Failed", "failure"),
    ("Unknown", "PipelineRunPending", "pending"),
    ("False", "Cancelled", "cancelled"),
])
def test_newer_retry_supersedes_older_success(status, reason, expected):
    newer = run(name="foo-on-pull-request-r2", created="2026-11-05T11:00:00Z",
                status=status, reason=reason)
    assert observe([newer, run()]).status == expected


@pytest.mark.parametrize("field,value", [
    ("pipelinesascode.tekton.dev/url-repository", "other"),
    ("pipelinesascode.tekton.dev/pull-request", "999"),
    ("pipelinesascode.tekton.dev/sha", "b" * 40),
])
def test_conflicting_identity_never_falls_back_to_old_success(field, value):
    newer = run(name="r2", created="2026-11-05T11:00:00Z")
    newer["metadata"]["labels"][field] = value
    with pytest.raises(KonfluxQueryError, match="Conflicting"):
        observe([run(), newer])


@pytest.mark.parametrize("kind", ["push", "other_component", "other_pr", "other_sha", "other_repo"])
def test_unrelated_attempts_do_not_count(kind):
    item = run()
    labels, annotations = item["metadata"]["labels"], item["metadata"]["annotations"]
    if kind == "push":
        labels["pipelinesascode.tekton.dev/event-type"] = "push"
    elif kind == "other_component":
        labels["appstudio.openshift.io/component"] = "different"
    elif kind == "other_pr":
        labels["pipelinesascode.tekton.dev/pull-request"] = "999"
        annotations["build.appstudio.redhat.com/pull_request_number"] = "999"
    elif kind == "other_sha":
        labels["pipelinesascode.tekton.dev/sha"] = "b" * 40
        annotations["build.appstudio.redhat.com/commit_sha"] = "b" * 40
        item["spec"]["params"][0]["value"] = "b" * 40
    else:
        labels["pipelinesascode.tekton.dev/url-repository"] = "other"
        annotations["pipelinesascode.tekton.dev/repo-url"] = "https://github.com/acme/other"
        annotations["build.appstudio.openshift.io/repo"] = "https://github.com/acme/other"
    assert observe([item]).status == "missing"


def test_tied_attempt_creation_is_unknown_not_arbitrary_uid_order():
    with pytest.raises(KonfluxQueryError, match="ambiguous"):
        observe([run(), run(name="second")])


@pytest.mark.parametrize("field,value", [("uid", None), ("creationTimestamp", "bad"),
                                        ("namespace", "other"), ("name", "../evil")])
def test_incomplete_matching_run_blocks_old_success(field, value):
    item = run(name="second", created="2026-11-05T11:00:00Z")
    item["metadata"][field] = value
    with pytest.raises(KonfluxQueryError):
        observe([run(), item])


def test_terminal_without_completion_time_cannot_pass():
    item = run()
    del item["status"]["completionTime"]
    assert observe([item]).status == "unknown"


def test_snapshot_key_resolves_unique_physical_component_only():
    # EG snapshot release keys do not determine which actual PR Component exists.
    actual = select_latest([run(component="odh-foo-v3-6")], repo=REPO, number=123, sha=SHA,
                           components={"odh-foo"}, namespace="rhoai-tenant", base_names=True)
    assert actual.component == "odh-foo-v3-6"
    with pytest.raises(KonfluxQueryError, match="version-specific"):
        select_latest([run(), run(name="r2", component="odh-foo-v3-6")],
                      repo=REPO, number=123, sha=SHA, components={"odh-foo"},
                      namespace="rhoai-tenant", base_names=True)
    assert observe([run(component="odh-foo-v3-6")]).status == "missing"


def record(item):
    return item


def client(fetch):
    return KonfluxClient("https://cluster.example:6443", "https://results.example/api",
                         "private-token", request_json=fetch)


def test_live_archive_merge_pagination_and_cache():
    calls = []
    old = run(status="False", reason="Failed")
    recent = run(name="r2", created="2026-11-05T11:00:00Z", status="Unknown", reason="Running")
    def fetch(url):
        calls.append(url)
        query = parse_qs(urlsplit(url).query)
        if "cluster.example" in url:
            if "continue" in query:
                return {"metadata": {"resourceVersion": "10"}, "items": [recent]}
            return {"metadata": {"resourceVersion": "10", "continue": "live-next"}, "items": []}
        if "continue" in query:
            return {"metadata": {}, "items": [record(old)]}
        return {"metadata": {"continue": "archive-next"}, "items": []}
    api = client(fetch)
    kwargs = dict(repo=REPO, number=123, sha=SHA, components={COMPONENT})
    assert api.observe(**kwargs).status == "pending"
    assert api.observe(**kwargs).status == "pending"
    assert len(calls) == 4
    assert "continue" in calls[-1]
    for url in calls:
        selector = parse_qs(urlsplit(url).query)["labelSelector"][0]
        assert "pipelinesascode.tekton.dev/sha=" + SHA in selector
        assert "pipelinesascode.tekton.dev/pull-request=123" in selector


def test_archive_contains_newer_attempt_than_retained_live_success():
    failed = run(name="r2", created="2026-11-05T11:00:00Z", status="False", reason="Failed")
    api = client(lambda url: ({"metadata": {"resourceVersion": "1"}, "items": [run()]}
                             if "cluster.example" in url else {"metadata": {}, "items": [record(failed)]}))
    assert api.observe(repo=REPO, number=123, sha=SHA, components={COMPONENT}).status == "failure"


def test_live_copy_wins_over_lagging_archive_for_same_uid():
    archived = run(status="Unknown", reason="Running")
    api = client(lambda url: ({"metadata": {"resourceVersion": "1"}, "items": [run()]}
                             if "cluster.example" in url else {"metadata": {}, "items": [record(archived)]}))
    assert api.observe(repo=REPO, number=123, sha=SHA, components={COMPONENT}).status == "success"


@pytest.mark.parametrize("broken", ["archive_denied", "archive_malformed", "live_malformed", "archive_repeat", "live_repeat", "live_version"])
def test_incomplete_query_never_authorizes_a_live_success(broken):
    count = 0
    def fetch(url):
        nonlocal count
        count += 1
        if "cluster.example" in url:
            if broken == "live_malformed":
                return {"items": [run()]}
            if broken == "live_repeat":
                return {"metadata": {"resourceVersion": "1", "continue": "same"}, "items": [run()]}
            if broken == "live_version":
                return {"metadata": {"resourceVersion": str(count), "continue": "next" + str(count)}, "items": [run()]}
            return {"metadata": {"resourceVersion": "1"}, "items": [run()]}
        if broken == "archive_denied":
            raise KonfluxQueryError("Konflux lookup failed (HTTP 403)")
        if broken == "archive_malformed":
            return {"items": "invalid"}
        return {"metadata": {"continue": "same"}, "items": []}
    with pytest.raises(KonfluxQueryError):
        client(fetch).observe(repo=REPO, number=123, sha=SHA, components={COMPONENT})


def test_gate_error_is_pending_not_success_or_build_failure():
    class Missing:
        def observe(self, **kwargs):
            raise KonfluxQueryError("Archive unavailable")
    gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=SHA,
                                          expected=[ExpectedCheck(key=COMPONENT)], client=Missing())
    assert gate.outcome == "pending"
    assert evidence["query-error"] == "Archive unavailable"


def test_no_expected_build_needs_no_cluster_access():
    gate, evidence = evaluate_konflux_gate(repo=REPO, number=123, sha=SHA, expected=[])
    assert gate.outcome == "pass"
    assert evidence["runs"] == []


def test_explicit_rollout_source_has_no_silent_fallback(monkeypatch):
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    assert build_status_source() == "konflux"
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "typo")
    with pytest.raises(KonfluxQueryError):
        build_status_source()


def test_missing_credentials_are_an_error(monkeypatch):
    monkeypatch.delenv("KONFLUX_TOKEN", raising=False)
    monkeypatch.delenv("KONFLUX_TOKEN_FILE", raising=False)
    with pytest.raises(KonfluxQueryError):
        KonfluxClient.from_environment()


def test_creator_roundtrip_preserves_build_evidence():
    evidence = {"source": "konflux", "head-sha": SHA, "runs": [observe([run()]).to_dict()]}
    original = {"repo": "foo", "pr-url": "https://github.com/acme/foo/pull/123",
                "pr-status": "build-failure", "builds": [], "build-observations": evidence}
    assert StatePullRequest.from_mapping(original, index=0).to_dict() == original

@pytest.mark.parametrize("konflux_status,mirror_status,expected_status", [
    ("success", "in_progress", "success"),
    ("failure", "success", "build-failure"),
    ("pending", "success", "build-pending"),
    ("error", "success", "build-pending"),
])
def test_monitor_uses_konflux_not_mirrored_build_conclusion(tmp_path, monkeypatch, konflux_status, mirror_status, expected_status):
    from lib.konflux_builds import BuildObservation, KonfluxClient, KonfluxQueryError
    from tests.test_gap_repository_gates import (
        _GitHub, _state, _run, _saved, _passing_check_runs, SHA as HEAD, URL,
    )
    github = _GitHub()
    github.check_runs = _passing_check_runs()
    build = github.check_runs[0]
    if mirror_status == "in_progress":
        build.update(status="in_progress", conclusion=None)
    else:
        build.update(status="completed", conclusion=mirror_status)
    class Direct:
        def observe(self, **query):
            assert query["repo"] == "red-hat-data-services/eval-hub"
            assert query["components"] == {"odh-eval-hub-v3-6"}
            assert query["sha"] == HEAD
            if konflux_status == "error":
                raise KonfluxQueryError("KubeArchive unavailable")
            return BuildObservation(
                "odh-eval-hub-v3-6", konflux_status, "Observed directly", HEAD,
                "eval-hub-pr-r1", "verified-uid", "rhoai-tenant",
                "2026-11-05T11:00:00Z",
                "https://konflux-ui.apps.stone-prod-p02.hjvn.p1.openshiftapps.com/ns/rhoai-tenant/pipelinerun/eval-hub-pr-r1",
            )
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setattr(KonfluxClient, "from_environment", lambda **kwargs: Direct())
    path = _state(tmp_path)
    result, _ = _run(path, github)
    child = _saved(path)["pull-requests"][0]
    assert child["pr-status"] == expected_status
    assert child["build-observations"]["source"] == "konflux"
    assert child["build-observations"]["head-sha"] == HEAD
    if konflux_status == "error":
        assert child["build-observations"]["query-error"] == "KubeArchive unavailable"
    else:
        assert child["build-observations"]["runs"][0]["uid"] == "verified-uid"

def test_runtime_pac_identity_survives_stale_static_repository_name():
    item = run()
    item["metadata"]["annotations"]["build.appstudio.openshift.io/repo"] = "https://github.com/acme/old-name"
    assert observe([item]).status == "success"


@pytest.mark.parametrize("missing", ["sha", "pr", "event"])
def test_incomplete_new_attempt_cannot_authorize_old_success(missing):
    item = run(name="r2", created="2026-11-05T11:00:00Z")
    labels, annotations = item["metadata"]["labels"], item["metadata"]["annotations"]
    if missing == "sha":
        labels.pop("pipelinesascode.tekton.dev/sha")
        annotations.pop("build.appstudio.redhat.com/commit_sha")
        item["spec"]["params"] = []
    elif missing == "pr":
        labels.pop("pipelinesascode.tekton.dev/pull-request")
        annotations.pop("build.appstudio.redhat.com/pull_request_number")
    else:
        labels.pop("pipelinesascode.tekton.dev/event-type")
    with pytest.raises(KonfluxQueryError):
        observe([run(), item])

def test_distinct_expected_pipelines_for_one_component_are_not_collapsed():
    from lib.konflux_builds import BuildObservation
    seen = []
    class Direct:
        def observe(self, **query):
            origin = next(iter(query["pipeline_names"]))
            seen.append(origin)
            return BuildObservation(COMPONENT, "failure" if origin == "second" else "success",
                                    "Observed", SHA, origin)
    gate, evidence = evaluate_konflux_gate(
        repo=REPO, number=123, sha=SHA,
        expected=[ExpectedCheck(COMPONENT, name_suffix="first"), ExpectedCheck(COMPONENT, name_suffix="second")],
        client=Direct())
    assert seen == ["first", "second"]
    assert gate.outcome == "fail"
    assert len(evidence["runs"]) == 2


def test_template_identity_selects_its_latest_attempt_not_another_pipeline():
    first = run()
    first["metadata"]["labels"]["pipelinesascode.tekton.dev/original-prname"] = "expected"
    other = run(name="other", created="2026-11-05T11:00:00Z", status="False", reason="Failed")
    other["metadata"]["labels"]["pipelinesascode.tekton.dev/original-prname"] = "different"
    assert observe([first, other], pipeline_names={"expected"}).status == "success"
    with pytest.raises(KonfluxQueryError, match="pipeline identities"):
        observe([first, other])

def test_same_head_retry_is_refreshed_before_status_publication(tmp_path, monkeypatch):
    from lib.konflux_builds import BuildObservation, KonfluxClient
    from tests.test_gap_repository_gates import (
        _GitHub, _state, _run, _saved, _passing_check_runs, SHA as HEAD,
    )
    github = _GitHub()
    github.check_runs = _passing_check_runs()
    calls = []
    class Direct:
        def observe(self, **query):
            calls.append(query)
            state = "success" if len(calls) == 1 else "pending"
            return BuildObservation("odh-eval-hub-v3-6", state, "new retry", HEAD,
                                    "latest", "latest-uid")
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setattr(KonfluxClient, "from_environment", lambda **kwargs: Direct())
    path = _state(tmp_path)
    _, updater = _run(path, github)
    assert len(calls) == 2
    child = _saved(path)["pull-requests"][0]
    assert child["pr-status"] == "build-pending"
    assert child["build-observations"]["runs"][0]["status"] == "pending"
    assert updater.posts[0][1] == "pending"

def test_prefetch_parallelism_deduplicates_pr_heads_and_reuses_cache():
    from threading import Barrier, Lock
    barrier, lock, calls = Barrier(2), Lock(), []
    def fetch(url):
        query = parse_qs(urlsplit(url).query)
        number = query["labelSelector"][0].split("pull-request=", 1)[1].split(",", 1)[0]
        with lock:
            calls.append(url)
        if "cluster.example" in url:
            # A serial implementation cannot satisfy this barrier.
            barrier.wait(timeout=5)
            item = run(name="pr-" + number)
            item["metadata"]["labels"]["pipelinesascode.tekton.dev/pull-request"] = number
            item["metadata"]["annotations"]["build.appstudio.redhat.com/pull_request_number"] = number
            return {"metadata": {"resourceVersion": "1"}, "items": [item]}
        return {"metadata": {}, "items": []}
    api = client(fetch)
    api.prefetch_prs([(REPO, 123, SHA), (REPO, 124, SHA), (REPO, 123, SHA)], max_workers=2)
    assert len(calls) == 4  # One live + archive read per unique PR/head.
    for number in (123, 124):
        assert api.observe(repo=REPO, number=number, sha=SHA, components={COMPONENT}).status == "success"
    assert len(calls) == 4


def test_prefetch_query_failure_is_cached_for_the_poll_and_reset_explicitly():
    calls = []
    def unavailable(url):
        calls.append(url)
        raise KonfluxQueryError("Konflux lookup failed (HTTP 403)")
    api = client(unavailable)
    api.prefetch_prs([(REPO, 123, SHA)])
    for _ in range(2):
        with pytest.raises(KonfluxQueryError, match="403"):
            api.observe(repo=REPO, number=123, sha=SHA, components={COMPONENT})
    assert len(calls) == 1
    api.clear_cache()
    with pytest.raises(KonfluxQueryError):
        api.observe(repo=REPO, number=123, sha=SHA, components={COMPONENT})
    assert len(calls) == 2


@pytest.mark.parametrize("workers", [0, 9, "unbounded"])
def test_prefetch_has_a_hard_concurrency_limit(workers):
    with pytest.raises(KonfluxQueryError):
        client(lambda url: {}).prefetch_prs([], max_workers=workers)

@pytest.mark.parametrize("initial,fresh,expected_overall", [
    (("success", "build-failure"), ("build-pending", "success"), None),
    (("build-failure", "build-failure"), ("success", "success"), "early-gate-triggered"),
])
def test_retry_refresh_recomputes_batch_hold_before_posting_child_greens(
        tmp_path, monkeypatch, initial, fresh, expected_overall):
    """Retry changes must not release a good child before its siblings or EG."""
    from scripts import gap_pr_monitor as monitor
    from tests.test_gap_pr_monitor import FakeUpdater, _gate
    urls = ["https://github.com/acme/one/pull/1", "https://github.com/acme/two/pull/2"]
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"pull-requests": [
        {"repo": repo, "pr-url": url, "pr-status": "new", "builds": []}
        for repo, url in zip(("one", "two"), urls)
    ]}))
    calls = {url: 0 for url in urls}
    def decide(url, **kwargs):
        index = urls.index(url)
        status = initial[index] if calls[url] == 0 else fresh[index]
        calls[url] += 1
        cli = {"success": "success", "build-failure": "failure", "build-pending": "pending"}[status]
        return _gate(url, status, cli)
    updater = FakeUpdater(check_name='gated artifacts promoter')
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setattr(monitor, "fetch_component_repo_map", lambda runner: {})
    monkeypatch.setattr(monitor, "_decide_url", decide)
    result = monitor.run_monitor(path, gh_runner=lambda *_: "",
                                 updater_factory=lambda **kwargs: updater)
    assert result.overall_status == expected_overall
    assert all(status == "pending" for _, status, _ in updater.posts)
    assert all(count == 2 for count in calls.values())

def test_refresh_failure_cannot_partially_publish_an_old_batch(tmp_path, monkeypatch):
    """An unavailable second observation must leave statuses and state untouched."""
    from scripts import gap_pr_monitor as monitor
    from tests.test_gap_pr_monitor import FakeUpdater, _gate
    urls = ["https://github.com/acme/one/pull/1", "https://github.com/acme/two/pull/2"]
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"pull-requests": [
        {"repo": repo, "pr-url": url, "pr-status": "new"}
        for repo, url in zip(("one", "two"), urls)
    ]}))
    before = path.read_bytes()
    calls = []
    def decide(url, **kwargs):
        calls.append(url)
        if len(calls) == 4:
            raise RuntimeError("GitHub PR metadata unavailable during refresh")
        return _gate(url, "success", "success")
    updater = FakeUpdater(check_name="gated artifacts promoter")
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setattr(monitor, "fetch_component_repo_map", lambda runner: {})
    monkeypatch.setattr(monitor, "_decide_url", decide)
    with pytest.raises(monitor.GapPrMonitorError, match="refresh child batch"):
        monitor.run_monitor(path, gh_runner=lambda *_: "",
                            updater_factory=lambda **kwargs: updater)
    assert not updater.posts
    assert path.read_bytes() == before

def test_newer_matching_run_without_build_type_cannot_authorize_old_success():
    newer = run(name="r2", created="2026-11-05T11:00:00Z", status="Unknown", reason="Running")
    del newer["metadata"]["labels"]["pipelines.appstudio.openshift.io/type"]
    with pytest.raises(KonfluxQueryError, match="build type"):
        observe([run(), newer])


def test_nonbuild_run_with_explicit_type_does_not_replace_build_success():
    other = run(name="integration", created="2026-11-05T11:00:00Z", status="False")
    other["metadata"]["labels"]["pipelines.appstudio.openshift.io/type"] = "test"
    assert observe([run(), other]).status == "success"


def test_successful_explicit_refresh_clears_a_cached_query_error():
    denied = True
    def fetch(url):
        if denied:
            raise KonfluxQueryError("Temporary archive failure")
        return {"metadata": {"resourceVersion": "1"}, "items": [run()]}
    api = client(fetch)
    api.prefetch_prs([(REPO, 123, SHA)])
    denied = False
    query = dict(repo=REPO, number=123, sha=SHA, components={COMPONENT})
    assert api.observe(**query, refresh=True).status == "success"
    assert api.observe(**query).status == "success"

def test_archived_run_missing_type_is_not_hidden_by_server_side_filtering():
    newer = run(name="r2", created="2026-11-05T11:00:00Z", status="Unknown", reason="Running")
    del newer["metadata"]["labels"]["pipelines.appstudio.openshift.io/type"]
    def fetch(url):
        if "cluster.example" in url:
            return {"metadata": {"resourceVersion": "1"}, "items": [run()]}
        selector = parse_qs(urlsplit(url).query)["labelSelector"][0]
        # Model Kubernetes label filtering: a type selector would omit this run.
        return {"metadata": {}, "items": [] if "type=build" in selector else [newer]}
    with pytest.raises(KonfluxQueryError, match="build type"):
        client(fetch).observe(repo=REPO, number=123, sha=SHA, components={COMPONENT})
