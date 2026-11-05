"""Offline tests for Leader history retention and normal merge requests."""

import json
import subprocess
from pathlib import Path

import pytest

from lib.leader_history import (
    RESULT_LABELS,
    prepare_leader_history_merge,
    write_history_snapshot,
)
from scripts.gap_pr_monitor import publish_state

LEADER = "https://github.com/acme/gap/pull/9"
SHA = "a" * 40


def metadata():
    return {
        "state": "open", "merged": False, "title": "GAP leader: gap-test",
        "labels": [{"name": "gated-artifacts-promoter"}],
        "head": {"sha": SHA, "ref": "gap-leader/gap-test",
                 "repo": {"full_name": "acme/gap"}},
    }


@pytest.mark.parametrize("outcome,label,prefix", [
    ("failure", "result-gap-failure", "[GAP failed]"),
    ("success", "result-gap-success", "[GAP passed]"),
])
def test_terminal_leader_is_labelled_before_sha_pinned_auto_merge(outcome, label, prefix):
    calls = []
    pull = metadata()
    pull["title"] = "[GAP failed] GAP leader: gap-test"
    pull["labels"].append({"name": "result-gap-failure"})

    def gh(args):
        calls.append(list(args))
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(pull)
        return ""

    prepare_leader_history_merge(LEADER, SHA, overall_status=outcome, gh_runner=gh)
    title = next(c for c in calls if "PATCH" in c)
    assert f"title={prefix} GAP leader: gap-test" in title
    assert any(f"labels[]={label}" in c for c in calls)
    merge = calls[-1]
    assert merge == ["pr", "merge", "9", "--repo", "acme/gap", "--auto",
                     "--merge", "--match-head-commit", SHA]
    assert not any("--admin" in c or "--delete-branch" in c for c in calls)
    assert not any("statuses" in " ".join(c) for c in calls)
    if outcome == "success":
        assert any("DELETE" in c and c[-1].endswith("/result-gap-failure") for c in calls)
    else:
        assert not any("DELETE" in c for c in calls)


@pytest.mark.parametrize("change", ["head", "closed", "fork", "branch", "label"])
def test_wrong_or_moved_pr_is_never_mutated(change):
    pull = metadata()
    if change == "head":
        pull["head"]["sha"] = "b" * 40
    elif change == "closed":
        pull["state"] = "closed"
    elif change == "fork":
        pull["head"]["repo"]["full_name"] = "other/gap"
    elif change == "branch":
        pull["head"]["ref"] = "component-code"
    else:
        pull["labels"] = []
    calls = []

    def gh(args):
        calls.append(list(args))
        return json.dumps(pull)

    with pytest.raises(RuntimeError):
        prepare_leader_history_merge(LEADER, SHA, overall_status="failure", gh_runner=gh)
    assert calls == [["api", "repos/acme/gap/pulls/9"]]


@pytest.mark.parametrize("outcome", ["new", "early-gate-triggered", "", None])
def test_nonterminal_leader_is_not_merged(outcome):
    def gh(args):
        pytest.fail("Nonterminal validation must not contact GitHub")
    with pytest.raises(ValueError):
        prepare_leader_history_merge(LEADER, SHA, overall_status=outcome, gh_runner=gh)


def test_dry_run_never_calls_github():
    def gh(args):
        pytest.fail("Dry run must not contact GitHub")
    prepare_leader_history_merge(LEADER, SHA, overall_status="failure",
                                 dry_run=True, gh_runner=gh)


def test_blocked_merge_surfaces_and_never_closes_pr():
    calls = []
    def gh(args):
        calls.append(list(args))
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(metadata())
        if args[:2] == ["pr", "merge"]:
            raise RuntimeError("Auto-merge is not allowed")
        return ""
    with pytest.raises(RuntimeError, match="Auto-merge"):
        prepare_leader_history_merge(LEADER, SHA, overall_status="failure", gh_runner=gh)
    assert not any(c[:2] == ["pr", "close"] for c in calls)


def test_repeated_finalization_keeps_same_title_prefix():
    pull = metadata()
    calls = []
    def gh(args):
        calls.append(list(args))
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(pull)
        if "PATCH" in args:
            pull["title"] = next(a.removeprefix("title=") for a in args if a.startswith("title="))
        return ""
    for _ in range(2):
        prepare_leader_history_merge(LEADER, SHA, overall_status="failure", gh_runner=gh)
    assert pull["title"] == "[GAP failed] GAP leader: gap-test"


@pytest.mark.parametrize("outcome", [None, "early-gate-triggered"])
def test_incomplete_runs_have_no_history_snapshot(tmp_path, outcome):
    state = tmp_path / "state.json"
    payload = {"pull-requests": []}
    if outcome:
        payload["overall-status"] = outcome
    state.write_text(json.dumps(payload))
    assert write_history_snapshot(tmp_path, state) is None
    assert not (tmp_path / "GAP History").exists()


@pytest.mark.parametrize("outcome,folder", [("success", "successful"), ("failure", "unsuccessful")])
def test_snapshot_preserves_canonical_and_child_outcome(tmp_path, outcome, folder):
    state = tmp_path / "GAP Leaders" / "2026-11-04_gap-test" / "state.json"
    state.parent.mkdir(parents=True)
    payload = {"overall-status": outcome, "pull-requests": [
        {"repo": "component", "pr-url": "https://github.com/acme/component/pull/3",
         "pr-status": "success" if outcome == "success" else "build-failure", "builds": []}]}
    state.write_text(json.dumps(payload))
    snapshot = write_history_snapshot(tmp_path, state)
    assert snapshot == tmp_path / f"GAP History/{folder}/2026-11-04_gap-test/state.json"
    assert json.loads(snapshot.read_text()) == payload
    assert json.loads(state.read_text()) == payload
    first = snapshot.read_bytes()
    assert write_history_snapshot(tmp_path, state).read_bytes() == first


@pytest.mark.parametrize("legacy", [None, "success", "failure", "early-gate-triggered"])
@pytest.mark.parametrize("statuses,folder", [
    pytest.param([], None, id="empty"),
    pytest.param(["pending"], None, id="partial-pending"),
    pytest.param(["success"], None, id="partial-builds-success"),
    pytest.param(["pending", "pending", "pending"], None, id="pending"),
    pytest.param(["success", "success", "pending"], None, id="awaiting-tests"),
    pytest.param(["success", "success", "success"], "successful", id="success"),
    pytest.param(["success", "success", "failure"], "unsuccessful", id="test-failure"),
    pytest.param(["success", "failure", "pending"], "unsuccessful", id="eg-build-failure"),
    pytest.param(["failure", "pending", "pending"], "unsuccessful", id="component-failure"),
])
def test_snapshot_uses_canonical_gates_without_changing_payload(tmp_path, statuses, folder, legacy):
    state = tmp_path / "GAP Leaders" / "2026-11-04_gap-test" / "state.json"
    state.parent.mkdir(parents=True)
    gate_ids = ("component-builds", "early-gate-build", "early-gate-test")
    payload = {
        "promotion-gates": [
            {"id": gate_id, "status": status}
            for gate_id, status in zip(gate_ids, statuses)
        ],
        "pull-requests": [{"repo": "component", "pr-status": "success", "builds": []}],
    }
    if legacy is not None:
        payload["overall-status"] = legacy
    state.write_text(json.dumps(payload))
    original = state.read_bytes()

    snapshot = write_history_snapshot(tmp_path, state)

    assert state.read_bytes() == original
    if folder is None:
        assert snapshot is None
        assert not (tmp_path / "GAP History").exists()
    else:
        assert snapshot == tmp_path / f"GAP History/{folder}/2026-11-04_gap-test/state.json"
        assert json.loads(snapshot.read_text()) == payload
        assert write_history_snapshot(tmp_path, state).read_bytes() == snapshot.read_bytes()
        assert state.read_bytes() == original


@pytest.mark.parametrize("outcome,folder", [("success", "successful"), ("failure", "unsuccessful")])
def test_state_and_snapshot_land_in_same_real_git_commit(tmp_path, outcome, folder):
    """Prove both copies survive a normal local push; no GitHub or mocks."""
    remote = tmp_path / "remote.git"
    root = tmp_path / "leader"
    def git(args, cwd):
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    git(["init", "--bare", str(remote)], tmp_path)
    git(["clone", str(remote), str(root)], tmp_path)
    git(["config", "user.name", "Test"], root)
    git(["config", "user.email", "test@example.com"], root)
    git(["checkout", "-b", "gap-leader/gap-test"], root)
    state = root / "GAP Leaders/2026-11-04_gap-test/state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"pull-requests": [], "overall-status": "early-gate-triggered"}))
    git(["add", "."], root)
    git(["commit", "-m", "Pending run"], root)
    git(["push", "-u", "origin", "HEAD"], root)
    state.write_text(json.dumps({"pull-requests": [], "overall-status": outcome}))
    result = publish_state(root, state, "gap-leader/gap-test", git_runner=git)
    canonical = git(["show", f"{result.sha}:GAP Leaders/2026-11-04_gap-test/state.json"], root)
    archive = git(["show", f"{result.sha}:GAP History/{folder}/2026-11-04_gap-test/state.json"], root)
    assert json.loads(canonical) == json.loads(archive)
    assert json.loads(archive)["overall-status"] == outcome
    assert git(["rev-parse", "origin/gap-leader/gap-test"], root) == result.sha


@pytest.mark.parametrize("outcome", ["failure", "success"])
def test_conclusion_prepares_history_before_the_leader_status(outcome):
    from scripts.gap_pr_monitor import post_leader_conclusion
    calls = []
    def gh(args):
        calls.append(list(args))
        return json.dumps(metadata()) if args == ["api", "repos/acme/gap/pulls/9"] else ""
    class Updater:
        dry_run = False
        def post_status_for_pr(self, url, status, **kwargs):
            assert calls[-1][:2] == ["pr", "merge"]
            calls.append(["status", url, status, kwargs["head_sha"], kwargs["description"]])
            return "posted"
    assert post_leader_conclusion(LEADER, SHA, overall_status=outcome,
                                  updater=Updater(), gh_runner=gh) == "posted"
    assert calls[-1] == ["status", LEADER, "completed", SHA, f"GAP: overall {outcome}"]


def test_moved_head_cannot_receive_a_leader_green_from_history_finalization():
    from scripts.gap_pr_monitor import post_leader_conclusion
    pull = metadata()
    pull["head"]["sha"] = "b" * 40
    class Updater:
        dry_run = False
        def post_status_for_pr(self, *args, **kwargs):
            pytest.fail("Must not green a moved Leader")
    with pytest.raises(RuntimeError, match="head moved"):
        post_leader_conclusion(LEADER, SHA, overall_status="failure",
                              updater=Updater(), gh_runner=lambda args: json.dumps(pull))


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_snapshot_preserves_creator_reporting_payload(tmp_path, outcome):
    state = tmp_path / "GAP Leaders/2026-11-04_gap-test/state.json"
    state.parent.mkdir(parents=True)
    payload = {"pull-requests": [], "overall-status": outcome, "creator-outcomes": [{
        "repo": "acme/foo", "source-branch": "main", "target-branch": "stable",
        "outcome": "creation-failure", "reason": "Access denied",
        "pr-url": "", "workflow-url": "", "observed-at": "2026-11-04T12:00:00+00:00"}]}
    state.write_text(json.dumps(payload))
    snapshot = write_history_snapshot(tmp_path, state)
    assert json.loads(snapshot.read_text()) == payload


def test_merged_leader_for_different_head_cannot_authorize_stale_status():
    pull = metadata()
    pull["merged"] = True
    pull["head"]["sha"] = "b" * 40
    with pytest.raises(RuntimeError, match="head moved"):
        prepare_leader_history_merge(LEADER, SHA, overall_status="failure",
                                     gh_runner=lambda args: json.dumps(pull))

@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize("result_first", [True, False])
def test_result_label_does_not_shadow_leader_trigger(outcome, result_first):
    """Finalization labels must not redirect the next scheduled monitor."""
    from scripts.gap_pr_monitor import extract_trigger_id_from_labels

    trigger = "gap-65c20b4a54fb4221ae161d227cd3402c"
    result_label = RESULT_LABELS[outcome]
    labels = [result_label, trigger] if result_first else [trigger, result_label]
    assert extract_trigger_id_from_labels(labels) == trigger
    assert extract_trigger_id_from_labels([result_label]) is None
