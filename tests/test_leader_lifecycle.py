"""Retirement keeps history durable without granting child merge permission."""

import copy
import json
import subprocess
from types import SimpleNamespace

import pytest

from lib.leader_lifecycle import KEY, retire_leader, retired_outcome, validate_replacement
from lib.leader_history import write_history_snapshot
from lib.state_file import load_state_file
from lib.gap_promotion_gates import children_ready_for_success_status, should_trigger_early_gate
from scripts import gap_pr_monitor as monitor
from scripts import gap_early_gate_results as results
from scripts import gap_failure_reporter as reporter

OLD = "https://github.com/acme/gap/pull/9"
NEW = "https://github.com/acme/gap/pull/10"
SHA = "a" * 40

@pytest.fixture(autouse=True)
def enable_exclusive_history(monkeypatch):
    """Exercise the deployment mode that keeps finalized state only in history."""
    monkeypatch.setenv("GAP_MOVE_FINAL_STATE", "true")


def payload(status="build-pending"):
    return {
        "pull-requests": [{
            "repo": "component", "pr-url": "https://github.com/acme/component/pull/3",
            "pr-status": status, "builds": [{"component": "model", "image": "quay.io/acme/model@sha256:123"}],
            "build-observations": {"source": "konflux", "runs": [], "head-sha": "b" * 40},
        }],
        "creator-outcomes": [],
        "promotion-gates": [
            {"id": "component-builds", "status": "pending", "required": True},
            {"id": "early-gate-build", "status": "pending", "required": False},
            {"id": "early-gate-test", "status": "pending", "required": False},
        ],
    }


def retired(data=None):
    data = copy.deepcopy(data if data is not None else payload())
    retire_leader(data, replacement_url=NEW, source_sha=SHA)
    return data


def pull(number=9, sha=SHA):
    trigger = f"gap-{number}"
    return {
        "number": number, "state": "open", "merged": False, "title": f"GAP leader: {trigger}",
        "labels": [{"name": name} for name in ["gated-artifacts-promoter", trigger]],
        "head": {"sha": sha, "ref": f"gap-leader/{trigger}", "repo": {"full_name": "acme/gap"}},
        "base": {"ref": "main", "repo": {"full_name": "acme/gap"}},
    }


@pytest.mark.parametrize("statuses,expected,folder", [
    (["pending", "pending", "pending"], "superseded", "unsuccessful"),
    (["success", "success", "pending"], "superseded", "unsuccessful"),
    (["failure", "pending", "pending"], "failure", "unsuccessful"),
    (["success", "failure", "pending"], "failure", "unsuccessful"),
    (["success", "success", "success"], "success", "successful"),
])
def test_retirement_preserves_actual_results_and_history(tmp_path, statuses, expected, folder):
    """Retirement records the terminal outcome without altering child or gate evidence."""
    data = payload()
    for gate, status in zip(data["promotion-gates"], statuses):
        gate["status"] = status
    original = copy.deepcopy(data)
    assert retire_leader(data, replacement_url=NEW, source_sha=SHA) == expected
    assert {k: v for k, v in data.items() if k != KEY} == original
    assert data[KEY]["replacement-leader"] == NEW
    path = tmp_path / "GAP Leaders/2026-11-05_gap-9/state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))
    snapshot = write_history_snapshot(tmp_path, path)
    assert snapshot == tmp_path / f"GAP History/{folder}/2026-11-05_gap-9/state.json"
    assert json.loads(snapshot.read_text()) == data
    assert load_state_file(snapshot)[KEY] == data[KEY]
    assert load_state_file(snapshot)["promotion-gates"] == original["promotion-gates"]
    first = copy.deepcopy(data)
    # A later replacement or retry does not rewrite the original retirement.
    retire_leader(data, replacement_url=NEW.replace("/10", "/11"), source_sha="c" * 40)
    assert data == first


@pytest.mark.parametrize("bad", [None, {}, {"status": "active"},
    {"status": "retired", "outcome": "success"}, {"status": "retired", "outcome": []}])
def test_malformed_metadata_cannot_reactivate_leader(bad):
    with pytest.raises((ValueError, TypeError)):
        retired_outcome({KEY: bad})


@pytest.mark.parametrize("mutation", ["closed", "merged", "fork", "base", "trigger", "draft", "same-batch", "reversed", "other-repo"])
def test_replacement_validation_rejects_unrelated_or_stale_leaders(mutation):
    old, new, url = pull(), pull(10), NEW
    if mutation == "closed":
        new["state"] = "closed"
    elif mutation == "merged":
        new["merged"] = True
    elif mutation == "fork":
        new["head"]["repo"]["full_name"] = "outsider/gap"
    elif mutation == "base":
        new["base"]["ref"] = "release"
    elif mutation == "trigger":
        new["labels"].pop()
    elif mutation == "draft":
        new["draft"] = True
    elif mutation == "same-batch":
        new = copy.deepcopy(old)
    elif mutation == "reversed":
        url = OLD.replace("/9", "/8")
    else:
        url = NEW.replace("acme", "other")
    with pytest.raises(ValueError):
        validate_replacement(OLD, old, url, new)


def test_valid_replacement():
    validate_replacement(OLD, pull(), NEW, pull(10))


def test_late_events_cannot_change_retired_run(tmp_path):
    data = retired()
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data))
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Retired runs must not query builds, comments or post statuses")

    outcome = monitor.run_monitor(path, gh_runner=forbidden, updater_factory=forbidden)
    assert outcome.overall_status == "superseded"
    assert not outcome.updated_urls
    outcome = results.run_early_gate_results(
        repo_root=tmp_path, state_file=str(path), leader_pr_url=OLD,
        labels=[], gh_runner=forbidden, updater_factory=forbidden,
    )
    assert not outcome.applied
    assert "retired" in outcome.reason
    report = reporter.report_for_leader(
        repo_root=tmp_path, state_file=str(path), leader_pr_url=OLD, gh_runner=forbidden,
    )
    assert not report.created and not report.updated
    assert results.child_pass_pr_urls(retired(payload("success"))) == []
    assert not children_ready_for_success_status(data)
    assert not should_trigger_early_gate(data)
    assert results.decide_overall_from_comments(
        payload=data, comments=[{"body": "Early Gate passed"}], labels=["early-gate-build-success"],
    )[0] is None
    assert path.read_bytes() == before


@pytest.fixture
def git_leader(tmp_path):
    def git(args, cwd):
        run = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args], cwd=cwd,
            capture_output=True, text=True,
        )
        if run.returncode:
            raise monitor.GapPrMonitorError(run.stderr)
        return run.stdout.strip()
    remote, root = tmp_path / "origin.git", tmp_path / "checkout"
    git(["init", "--bare", str(remote)], tmp_path)
    git(["clone", str(remote), str(root)], tmp_path)
    git(["config", "user.name", "Test"], root)
    git(["config", "user.email", "test@example.com"], root)
    git(["checkout", "-b", "gap-leader/gap-9"], root)
    path = root / "GAP Leaders/2026-11-05_gap-9/state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload()))
    git(["add", "."], root)
    git(["commit", "-m", "Pending run"], root)
    git(["push", "-u", "origin", "HEAD"], root)
    return root, path, git


@pytest.mark.parametrize("block_merge", [False, True])
def test_rollover_publishes_durable_history_before_merge_and_can_retry(git_leader, block_merge):
    """Persist retirement before requesting a merge and allow a retry after merge rejection."""
    root, path, git = git_leader
    calls, status_posts = [], []
    initial = json.loads(path.read_text())
    original_sha = git(["rev-parse", "HEAD"], root)
    blocked = block_merge

    def head():
        return git(["rev-parse", "origin/gap-leader/gap-9"], root)

    def gh(args):
        """Stub GitHub metadata and merge requests while preserving publication ordering."""
        calls.append(args)
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(pull(sha=head()))
        if args == ["api", "repos/acme/gap/pulls/10"]:
            return json.dumps(pull(10))
        if args[:2] == ["pr", "merge"]:
            assert args[-1] == head()
            if blocked:
                raise RuntimeError("merge blocked by repository policy")
        return ""

    class Updater:
        dry_run = False
        def __init__(self, **kwargs): pass
        def get_pull_head(self, ref):
            return SimpleNamespace(sha=head())
        def post_status_for_pr(self, url, state, **kwargs):
            status_posts.append((url, state, kwargs))
            assert url == OLD
            return SimpleNamespace(skipped=False)

    kwargs = dict(
        repo_root=root, head_ref="gap-leader/gap-9", leader_pr_url=OLD,
        replacement_url=NEW, publish=True, updater_factory=Updater,
        gh_runner=gh, git_runner=git,
    )
    if blocked:
        with pytest.raises(RuntimeError, match="merge blocked"):
            monitor.run_monitor_publish_and_signal(path, **kwargs)
        assert not status_posts
    else:
        result = monitor.run_monitor_publish_and_signal(path, **kwargs)
        assert result.leader_status_posted
    archive = "GAP History/unsuccessful/2026-11-05_gap-9/state.json"
    assert not path.exists()
    data = json.loads((root / archive).read_text())
    assert data["pull-requests"] == initial["pull-requests"]
    assert data["promotion-gates"] == initial["promotion-gates"]
    assert data[KEY]["source-head"] == original_sha
    archive = "GAP History/unsuccessful/2026-11-05_gap-9/state.json"
    assert json.loads(git(["show", f"{head()}:{archive}"], root)) == data
    assert any("title=[GAP superseded] GAP leader: gap-9" in c for c in calls)
    assert not any("--admin" in c or "--delete-branch" in c or c[:2] == ["pr", "close"] for c in calls)
    first_sha = head()
    blocked = False
    kwargs["replacement_url"] = None
    monitor.run_monitor_publish_and_signal(path, **kwargs)
    assert head() == first_sha
    assert not path.exists()
    assert json.loads((root / archive).read_text()) == data
    assert all(url == OLD for url, _, _ in status_posts)


def test_moved_head_blocks_retirement_before_writing(git_leader):
    root, path, git = git_leader
    before = path.read_bytes()
    with pytest.raises(monitor.GapPrMonitorError, match="changed before rollover"):
        monitor.retire_previous_leader(
            path, repo_root=root, head_ref="gap-leader/gap-9",
            leader_pr_url=OLD, replacement_url=NEW, check_name="gap",
            dry_run=False, remote="origin", gh_runner=lambda _: json.dumps(pull()),
            git_runner=git,
        )
    assert path.read_bytes() == before
    assert not (root / "GAP History").exists()


def test_retirement_push_conflict_is_not_rebased_or_signalled(git_leader):
    root, path, git = git_leader
    commands = []
    old_sha = git(["rev-parse", "HEAD"], root)
    def conflicting_git(args, cwd):
        commands.append(args)
        if args[0] == "push":
            raise monitor.GapPrMonitorError("rejected: remote branch moved")
        return git(args, cwd)
    def gh(args):
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(pull(sha=old_sha))
        if args == ["api", "repos/acme/gap/pulls/10"]:
            return json.dumps(pull(10))
        if args[:2] == ["pr", "diff"]:
            return str(path.relative_to(root))
        pytest.fail(f"No signals are allowed before successful publication: {args}")
    with pytest.raises(monitor.GapPrMonitorError, match="rejected"):
        monitor.retire_previous_leader(
            path, repo_root=root, head_ref="gap-leader/gap-9",
            leader_pr_url=OLD, replacement_url=NEW, check_name="gap",
            dry_run=False, remote="origin", gh_runner=gh, git_runner=conflicting_git,
            updater_factory=lambda **kwargs: pytest.fail("No updater before a successful push"),
        )
    assert not any("pull" in c or "--rebase" in c for c in commands)
    assert git(["rev-parse", "origin/gap-leader/gap-9"], root) == old_sha


def test_merged_replacement_is_valid_but_closed_unmerged_is_not():
    new = pull(10)
    new.update(state="closed", merged=True)
    validate_replacement(OLD, pull(), NEW, new)
    new["merged"] = False
    with pytest.raises(ValueError):
        validate_replacement(OLD, pull(), NEW, new)


def test_latest_rollover_ignores_abandoned_draft_and_nonleader_prs():
    def row(number, state="OPEN", draft=False, branch=None, base="main", fork=False):
        return {
            "number": number, "url": f"https://github.com/acme/gap/pull/{number}",
            "state": state, "isDraft": draft, "baseRefName": base, "isCrossRepository": fork,
            "headRefName": branch or f"gap-leader/gap-{number}",
            "labels": [{"name": "gated-artifacts-promoter"}, {"name": f"gap-{number}"}],
        }
    calls = []
    def gh(args):
        calls.append(args)
        return json.dumps([None, 7, {"labels": 3}, row(16, base="rhoai-3.6"),
                           row(15, fork=True), row(14, base=""),
                           row(13, branch="feature"), row(12, draft=True),
                           row(11, state="CLOSED"), row(10, state="MERGED"), row(9)])
    newest = monitor.latest_rollover_leaders_by_base("acme/gap", gh_runner=gh)
    assert {base: leader.number for base, leader in newest.items()} == {"main": 10, "rhoai-3.6": 16}
    assert calls[0][calls[0].index("--state") + 1] == "all"


@pytest.mark.parametrize("enabled", [True, False])
def test_schedule_only_retires_older_leaders_when_enabled(tmp_path, monkeypatch, enabled):
    old = monitor.OpenLeaderPr(9, OLD, "gap-leader/gap-9", ("gated-artifacts-promoter", "gap-9"), "main")
    new = monitor.OpenLeaderPr(10, NEW, "gap-leader/gap-10", ("gated-artifacts-promoter", "gap-10"), "main")
    other = monitor.OpenLeaderPr(11, "https://github.com/acme/gap/pull/11",
                                 "gap-leader/gap-11", ("gated-artifacts-promoter", "gap-11"), "rhoai-3.6")
    calls = []
    monkeypatch.setenv("GAP_RETIRE_PREVIOUS_LEADERS", str(enabled).lower())
    monkeypatch.setattr(monitor, "list_open_leader_prs", lambda *a, **k: [new, old, other])
    monkeypatch.setattr(monitor, "latest_rollover_leaders_by_base",
                        lambda *a, **k: {"main": new, "rhoai-3.6": other})
    monkeypatch.setattr(monitor, "checkout_leader_branch", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "resolve_state_file", lambda **k: tmp_path / "state.json")
    def execute(path, **kwargs):
        calls.append(kwargs)
        return monitor.MonitorResult(path, [], [], [], [], False)
    monkeypatch.setattr(monitor, "run_monitor_publish_and_signal", execute)
    assert monitor.run_scheduled_monitors(tmp_path, leader_repo="acme/gap") == 0
    assert "replacement_url" not in calls[0]
    assert calls[1].get("replacement_url") == (NEW if enabled else None)
    assert "replacement_url" not in calls[2]


def test_creator_rejects_reusing_closed_trigger_before_any_git_changes():
    from lib.leader_pr import LeaderPRManager
    from lib.state_file import build_state
    commands = []
    def run(command, cwd):
        commands.append(command)
        if command[:3] == ["gh", "pr", "list"]:
            result = [{"number": 9, "url": OLD}] if command[command.index("--state") + 1] == "all" else []
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    with pytest.raises(RuntimeError, match="closed or merged"):
        LeaderPRManager(repo="acme/gap", runner=run).create_or_update(
            build_state(pull_requests=[]), trigger_id="gap-9",
        )
    assert not any(command[0] == "git" or command[:3] == ["gh", "repo", "clone"] for command in commands)


def test_rollover_dry_run_makes_no_file_or_github_changes(git_leader):
    root, path, git = git_leader
    before = path.read_bytes()
    sha = git(["rev-parse", "HEAD"], root)
    calls = []
    def gh(args):
        calls.append(args)
        if args[:2] == ["pr", "diff"]:
            return str(path.relative_to(root))
        return json.dumps(pull(sha=sha) if args[-1].endswith("/9") else pull(10))
    result = monitor.retire_previous_leader(
        path, repo_root=root, head_ref="gap-leader/gap-9", leader_pr_url=OLD,
        replacement_url=NEW, check_name="gap", dry_run=True, remote="origin",
        gh_runner=gh, git_runner=git,
    )
    assert result.overall_status == "superseded" and result.dry_run
    assert path.read_bytes() == before
    assert not (root / "GAP History").exists()
    assert len(calls) == 3
    assert all((c[0] == "api" and len(c) == 2) or c[:2] == ["pr", "diff"] for c in calls)


def test_unrelated_code_changes_block_history_merge(git_leader):
    root, path, git = git_leader
    before = path.read_bytes()
    sha = git(["rev-parse", "HEAD"], root)
    def gh(args):
        if args[:2] == ["pr", "diff"]:
            return ".github/workflows/unreviewed.yml"
        return json.dumps(pull(sha=sha))
    with pytest.raises(monitor.GapPrMonitorError, match="outside its run record"):
        monitor.retire_previous_leader(
            path, repo_root=root, head_ref="gap-leader/gap-9",
            leader_pr_url=OLD, replacement_url=NEW, check_name="gap",
            dry_run=False, remote="origin", gh_runner=gh, git_runner=git,
        )
    assert path.read_bytes() == before
    assert not (root / "GAP History").exists()


def test_creator_resumes_orphaned_branch_without_force_push(monkeypatch):
    from pathlib import Path
    from lib.leader_pr import LeaderPRManager
    from lib.state_file import build_state
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    commands = []
    def run(command, cwd):
        commands.append(command)
        out = ""
        if command[:3] == ["gh", "pr", "list"]:
            out = "[]"
        elif command[:3] == ["gh", "repo", "clone"]:
            Path(command[4]).mkdir(exist_ok=True)
        elif command[:2] == ["git", "ls-remote"]:
            out = SHA + "\\trefs/heads/gap-leader/gap-9"
        elif command[:2] == ["git", "checkout"]:
            state = Path(cwd) / "GAP Leaders/2026-11-05_gap-9/state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps(payload()))
        elif command[:2] == ["git", "status"]:
            out = "M state.json"
        elif command[:3] == ["gh", "pr", "create"]:
            out = OLD
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    manager = LeaderPRManager(repo="acme/gap", runner=run)
    state = build_state(pull_requests=[{"repo": "component", "pr-url": "https://github.com/acme/component/pull/3"}])
    result = manager.create_or_update(state, trigger_id="gap-9")
    assert result.pr_url == OLD
    assert result.state_path == "GAP Leaders/2026-11-05_gap-9/state.json"
    assert any(c[:2] == ["git", "fetch"] for c in commands)
    assert state.pull_requests[0].build_observations["head-sha"] == "b" * 40
    assert not any("--force" in c for c in commands if c[0] == "git")


@pytest.mark.parametrize("response", [None, "not json", "{}"])
def test_discovery_failure_still_monitors_every_open_leader(tmp_path, monkeypatch, capsys, response):
    leaders = [
        monitor.OpenLeaderPr(n, f"https://github.com/acme/gap/pull/{n}",
                             f"gap-leader/gap-{n}", ("gated-artifacts-promoter", f"gap-{n}"), "main")
        for n in (9, 10)
    ]
    monkeypatch.setenv("GAP_RETIRE_PREVIOUS_LEADERS", "true")
    monkeypatch.setattr(monitor, "list_open_leader_prs", lambda *a, **k: leaders)
    monkeypatch.setattr(monitor, "checkout_leader_branch", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "resolve_state_file", lambda **k: tmp_path / "state.json")
    visited = []
    def gh(args):
        if response is None:
            raise monitor.GhCommandError(args, 1, "GitHub unavailable")
        return response
    def execute(path, **kwargs):
        assert "replacement_url" not in kwargs
        visited.append(kwargs["leader_pr_url"])
        return monitor.MonitorResult(path, [], [], [], [], False)
    monkeypatch.setattr(monitor, "run_monitor_publish_and_signal", execute)
    assert monitor.run_scheduled_monitors(tmp_path, leader_repo="acme/gap", gh_runner=gh) == 0
    assert visited == [leader.url for leader in leaders]
    assert "skipping daily rollover discovery" in capsys.readouterr().err


def test_failed_commit_cannot_carry_staged_history_to_next_leader(git_leader):
    root, path, git = git_leader
    data = retired(json.loads(path.read_text()))
    path.write_text(json.dumps(data))
    before = git(["rev-parse", "HEAD"], root)
    def fail_commit(args, cwd):
        if args[0] == "commit":
            raise monitor.GapPrMonitorError("commit hook failed")
        return git(args, cwd)
    with pytest.raises(monitor.GapPrMonitorError, match="hook failed"):
        monitor.publish_state(root, path, "gap-leader/gap-9", git_runner=fail_commit)
    assert "GAP History" in git(["diff", "--cached", "--name-only"], root)
    with pytest.raises(monitor.GapPrMonitorError, match="uncommitted"):
        monitor.checkout_leader_branch(root, "gap-leader/gap-10", git_runner=git)
    assert git(["rev-parse", "HEAD"], root) == before
    assert git(["branch", "--show-current"], root) == "gap-leader/gap-9"


def test_retirement_rejects_dirty_checkout_without_discarding_changes(git_leader):
    root, path, git = git_leader
    sha = git(["rev-parse", "HEAD"], root)
    path.write_text(path.read_text() + "\n")
    before = path.read_bytes()
    with pytest.raises(monitor.GapPrMonitorError, match="uncommitted"):
        monitor.retire_previous_leader(
            path, repo_root=root, head_ref="gap-leader/gap-9", leader_pr_url=OLD,
            replacement_url=NEW, check_name="gap", dry_run=False, remote="origin",
            gh_runner=lambda _: json.dumps(pull(sha=sha)), git_runner=git,
        )
    assert path.read_bytes() == before
    assert not (root / "GAP History").exists()


def test_publish_rejects_unrelated_staged_files(git_leader):
    root, path, git = git_leader
    unrelated = root / "other-leader.json"
    unrelated.write_text("{}")
    git(["add", "--", unrelated.name], root)
    before = git(["rev-parse", "HEAD"], root)
    with pytest.raises(monitor.GapPrMonitorError, match="outside this Leader record"):
        monitor.publish_state(root, path, "gap-leader/gap-9", git_runner=git)
    assert git(["rev-parse", "HEAD"], root) == before


@pytest.mark.parametrize("old_outcome,new_outcome", [("failure", "success"), ("success", "failure")])
def test_retirement_keeps_one_current_history_record_but_preserves_git_history(git_leader, old_outcome, new_outcome):
    """Remove obsolete live copies while preserving prior outcomes in Git history."""
    root, path, git = git_leader
    data = json.loads(path.read_text())
    for gate in data["promotion-gates"]:
        gate["status"] = old_outcome
    path.write_text(json.dumps(data))
    # Simulate a legacy duplicate written by the pre-migration publisher.
    folders = {"success": "successful", "failure": "unsuccessful"}
    obsolete = root / f"GAP History/{folders[old_outcome]}/2026-11-05_gap-9/state.json"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text(path.read_text())
    git(["add", "."], root)
    git(["commit", "-m", "Legacy duplicate"], root)
    old_sha = git(["rev-parse", "HEAD"], root)
    git(["push", "origin", "HEAD"], root)
    for gate in data["promotion-gates"]:
        gate["status"] = new_outcome
    retire_leader(data, replacement_url=NEW, source_sha=old_sha)
    path.write_text(json.dumps(data))
    new = monitor.publish_state(root, path, "gap-leader/gap-9", git_runner=git)
    assert not obsolete.exists()
    current = root / f"GAP History/{folders[new_outcome]}/2026-11-05_gap-9/state.json"
    assert json.loads(current.read_text()) == data
    assert obsolete.relative_to(root).as_posix() not in git(["ls-tree", "-r", "--name-only", new.sha], root).splitlines()
    historical = json.loads(git(["show", f"{old_sha}:{obsolete.relative_to(root)}"], root))
    assert historical["promotion-gates"][0]["status"] == old_outcome
    assert KEY not in historical


def test_reset_active_updates_remove_stale_legacy_copy_without_finalizing(git_leader):
    """A run explicitly reset to pending must not inherit a previous attempt outcome."""
    root, path, git = git_leader
    data = json.loads(path.read_text())
    data["promotion-gates"][0]["status"] = "failure"
    path.write_text(json.dumps(data))
    legacy = write_history_snapshot(root, path, move=False)
    git(["add", "."], root)
    git(["commit", "-m", "Legacy snapshot"], root)
    data["promotion-gates"][0]["status"] = "pending"
    data["pull-requests"][0]["head-sha"] = "c" * 40
    path.write_text(json.dumps(data))
    result = monitor.publish_state(root, path, "gap-leader/gap-9", git_runner=git)
    assert result.state_path == path
    assert path.exists() and not legacy.exists()
    assert git(["ls-tree", "-r", "--name-only", result.sha], root).splitlines() == [path.relative_to(root).as_posix()]


def test_legacy_layout_stays_compatible_until_consumers_are_enabled(git_leader, monkeypatch):
    """Leave active paths readable until both consumers enable archive moves."""
    root, path, git = git_leader
    monkeypatch.delenv("GAP_MOVE_FINAL_STATE")
    data = retired(json.loads(path.read_text()))
    path.write_text(json.dumps(data))
    result = monitor.publish_state(root, path, "gap-leader/gap-9", git_runner=git, archive=True)
    archive = root / "GAP History/unsuccessful/2026-11-05_gap-9/state.json"
    assert result.state_path == path
    assert json.loads(path.read_text()) == json.loads(archive.read_text()) == data


def test_untracked_history_is_not_overwritten_or_deleted(git_leader):
    """Do not overwrite a local untracked archive during state publication."""
    root, path, git = git_leader
    archive = root / "GAP History/unsuccessful/2026-11-05_gap-9/state.json"
    archive.parent.mkdir(parents=True)
    archive.write_text("do not overwrite")
    path.write_text(json.dumps(retired()))
    before = path.read_bytes()
    with pytest.raises(monitor.GapPrMonitorError, match="untracked"):
        monitor.publish_state(root, path, "gap-leader/gap-9", git_runner=git)
    assert archive.read_text() == "do not overwrite"
    assert path.read_bytes() == before


@pytest.mark.parametrize("outcome", ["success"])
def test_eg_finalization_and_retry_use_one_frozen_record(git_leader, monkeypatch, outcome):
    """Retry a blocked history merge without reinterpreting comments or approving newer heads."""
    root, path, git = git_leader
    data = payload("success")
    data["pull-requests"][0]["head-sha"] = "b" * 40
    data["promotion-gates"][0]["status"] = "success"
    path.write_text(json.dumps(data))
    git(["add", "."], root)
    git(["commit", "-m", "Ready for EG"], root)
    git(["push", "origin", "HEAD"], root)
    monkeypatch.setattr(results, "checkout_leader_branch", lambda *a, **k: None)
    monkeypatch.setattr(results, "list_leader_pr_comments", lambda *a, **k: [])
    def decide(**kwargs):
        """Simulate terminal EG evidence without fetching live comments."""
        for gate in kwargs["payload"]["promotion-gates"][1:]:
            gate["status"] = outcome
        return outcome, "completed", {"early-gate-build": outcome, "early-gate-test": outcome}
    monkeypatch.setattr(results, "decide_overall_from_comments", decide)
    posts = []
    class Updater:
        dry_run = False
        def __init__(self, **kwargs): pass
        def get_pull_head(self, ref):
            return SimpleNamespace(sha=git(["rev-parse", "HEAD"], root))
        def post_status_for_pr(self, url, state, **kwargs):
            posts.append((url, state, kwargs))
            return SimpleNamespace(skipped=False, deferred=False)
    blocked = True
    def gh(args):
        """Stub GitHub metadata and merge requests while preserving publication ordering."""
        if args[:2] == ["api", "repos/acme/gap/pulls/9"] and "--jq" in args:
            return git(["rev-parse", "HEAD"], root)
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(pull(sha=git(["rev-parse", "HEAD"], root)))
        if args[:2] == ["pr", "merge"] and blocked:
            raise RuntimeError("merge blocked")
        return ""
    kwargs = dict(repo_root=root, state_file=str(path), leader_pr_url=OLD, labels=[],
                  head_ref="gap-leader/gap-9", publish=True,
                  git_runner=git, gh_runner=gh, updater_factory=Updater)
    with pytest.raises(RuntimeError, match="merge blocked"):
        results.run_early_gate_results(**kwargs)
    folder = "successful" if outcome == "success" else "unsuccessful"
    archive = root / f"GAP History/{folder}/2026-11-05_gap-9/state.json"
    assert not path.exists() and archive.exists()
    before, sha = archive.read_bytes(), git(["rev-parse", "HEAD"], root)
    assert not posts
    def forbidden(*args, **kwargs):
        """Fail if a finalized-record retry starts a fresh evaluation."""
        pytest.fail("An archive retry must not reinterpret comments or reevaluate builds")
    monkeypatch.setattr(results, "list_leader_pr_comments", forbidden)
    monkeypatch.setattr(results, "decide_overall_from_comments", forbidden)
    blocked = False
    result = results.run_early_gate_results(**kwargs)
    assert result.state_path == archive and result.published_sha == sha
    assert result.overall_status == outcome
    child_posts = [item for item in posts if item[0] != OLD]
    assert len(child_posts) == 1
    assert child_posts[0][2]["head_sha"] == "b" * 40
    assert child_posts[0][2]["defer_if_head_moved"] is True
    assert archive.read_bytes() == before
    assert not path.exists()
    assert monitor.run_monitor(archive, gh_runner=forbidden, updater_factory=forbidden).overall_status == outcome
    monkeypatch.setattr(monitor, "run_monitor", forbidden)
    monkeypatch.setattr(monitor, "_post_leader_failure_summary", lambda **k: None)
    resumed = monitor.run_monitor_publish_and_signal(
        path, repo_root=root, head_ref="gap-leader/gap-9", leader_pr_url=OLD,
        publish=True, git_runner=git, gh_runner=gh, updater_factory=Updater,
    )
    assert resumed.published_sha == sha and resumed.state_path == archive
    assert archive.read_bytes() == before and not path.exists()


def test_archived_retry_never_grants_approval_without_a_pinned_child_revision():
    """Skip status updates when archived evidence cannot identify the evaluated revision."""
    data = payload("success")
    data["pull-requests"][0].pop("build-observations")
    class Updater:
        def post_status_for_pr(self, *args, **kwargs):
            pytest.fail("Missing evaluated SHA cannot approve today's PR head")
    assert results.post_child_statuses_for_early_gate(
        data, overall_status="success", updater=Updater(), pinned_only=True,
    ) == ()


def test_archived_retry_defers_changed_child_head():
    """Do not count a status deferred because the child revision changed as posted."""
    data = payload("success")
    class Updater:
        def post_status_for_pr(self, url, state, **kwargs):
            assert kwargs["defer_if_head_moved"]
            assert kwargs["head_sha"] == "b" * 40
            return SimpleNamespace(skipped=False, deferred=True)
    assert results.post_child_statuses_for_early_gate(
        data, overall_status="success", updater=Updater(), pinned_only=True,
    ) == ()


@pytest.mark.parametrize("branch_exists", [True, False])
def test_creator_cannot_recreate_archived_trigger(monkeypatch, branch_exists):
    """An archived trigger cannot be reused even after its Leader branch was deleted."""
    from pathlib import Path
    from lib.leader_pr import LeaderPRManager
    from lib.state_file import build_state
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls = []
    def run(command, cwd):
        """Simulate archived state discovery with or without the original remote branch."""
        calls.append(command)
        out = ""
        if command[:3] == ["gh", "pr", "list"]:
            out = "[]"
        elif command[:3] == ["gh", "repo", "clone"]:
            Path(command[4]).mkdir(exist_ok=True)
        elif command[:2] == ["git", "ls-remote"]:
            out = SHA if branch_exists else ""
        elif command[:2] == ["git", "checkout"]:
            state = Path(cwd) / "GAP History/unsuccessful/2026-11-05_gap-9/state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps(retired()))
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    manager = LeaderPRManager(repo="acme/gap", runner=run)
    state = build_state(pull_requests=[])
    with pytest.raises(RuntimeError, match="archived"):
        manager.create_or_update(state, trigger_id="gap-9")
    assert not any(c[:2] == ["git", "push"] or c[:3] == ["gh", "pr", "create"] for c in calls)


# Failed Leaders remain active while accepted notifications/tracking survive optional hook failures.
@pytest.mark.parametrize("hook_fails", [False, True])
def test_active_failure_summary_and_notifications_remain_visible(git_leader, monkeypatch, hook_fails):
    """Use published active evidence without prematurely archiving or concluding failure."""

    root, path, git = git_leader
    data = payload("build-failure")
    data["pull-requests"][0]["merge-observations"] = {
        "schema": 1, "conflict": {"status": "failure"}, "feasibility": {"status": "unknown"},
    }
    data["pull-requests"][0]["build-observations"] = {
        "source": "konflux", "repository": "acme/demo", "pr": 42, "head-sha": SHA,
        "runs": [], "expected-components": ["api"],
        "required-pipelines": {"schema": 1, "source": "revision-pinned-tekton",
            "repository": "acme/demo", "pr": 42, "head-sha": SHA,
            "pipelines": [{"component": "api", "pipeline_name": "build"},
                          {"component": "api", "pipeline_name": "test"}]},
    }
    data["promotion-gates"][0]["status"] = "failure"
    def evaluate(state, **kwargs):
        """Provide a failed component evaluation for the final-summary path."""
        state.write_text(json.dumps(data))
        return monitor.MonitorResult(state, [], [], [], [], False, overall_status="failure",
                                     notification_urls=(data["pull-requests"][0]["pr-url"],))
    monkeypatch.setattr(monitor, "run_monitor", evaluate)
    monkeypatch.setattr(monitor, "verify_leader_head", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "post_leader_conclusion", lambda *a, **k: SimpleNamespace(skipped=False))
    monkeypatch.setattr(reporter, "collect_rows_from_state", lambda saved, **k: [])
    monkeypatch.setattr(reporter, "current_creator_outcomes", lambda *a, **k: [])
    from lib.gap_notifications.monitor_hook import NotificationConfig
    notifications = []
    def notify(saved, **kwargs):
        """Observe exactly the accepted, already-published active evidence."""
        assert path.exists()
        archive = kwargs["state_path"]
        assert archive == path
        assert monitor.load_state(archive) == saved
        from lib.state_file import StatePullRequest
        child = saved["pull-requests"][0]
        assert StatePullRequest.from_mapping(child, index=0).to_dict()["merge-observations"] == child["merge-observations"]
        assert child["build-observations"] == data["pull-requests"][0]["build-observations"]
        assert StatePullRequest.from_mapping(child, index=0).to_dict()["build-observations"] == child["build-observations"]
        assert kwargs["accepted_urls"] == (data["pull-requests"][0]["pr-url"],)
        assert kwargs["published_sha"] == git(["rev-parse", "HEAD"], root)
        notifications.append(archive)
        if hook_fails:
            raise RuntimeError("optional failure")
        kwargs["tracking"].update(status="checked")
        return {"status": "preview"}
    monkeypatch.setattr("lib.gap_notifications.monitor_hook.notify_after_publish", notify)
    tracking = []
    original_render = reporter.render_summary_markdown
    def render(*args, **kwargs):
        tracking.append(kwargs["tracking"])
        return original_render(*args, **kwargs)
    monkeypatch.setattr(reporter, "render_summary_markdown", render)
    comments = []
    monkeypatch.setattr(reporter, "upsert_leader_summary_comment",
                        lambda url, body, **k: (comments.append(body) or (1, False, True)))
    result = monitor.run_monitor_publish_and_signal(
        path, repo_root=root, head_ref="gap-leader/gap-9", leader_pr_url=OLD,
        publish=True, git_runner=git, updater_factory=lambda **k: None,
        notification_config=NotificationConfig("preview"),
    )
    assert path.exists()
    assert result.state_path == path
    assert not result.leader_status_posted
    assert result.overall_status == "failure"
    assert notifications == [result.state_path]
    assert result.notification_result["status"] == ("error" if hook_fails else "preview")
    assert tracking == [{"status": "unavailable" if hook_fails else "checked"}]

    assert len(comments) == 1


def test_archive_retry_does_not_delete_an_untracked_active_copy(git_leader):
    """Preserve unexpected local active files when retrying archive publication."""
    root, path, git = git_leader
    path.write_text(json.dumps(retired()))
    result = monitor.publish_state(root, path, "gap-leader/gap-9", git_runner=git)
    archive = result.state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(archive.read_bytes())
    with pytest.raises(monitor.GapPrMonitorError, match="untracked active"):
        monitor.publish_state(root, archive, "gap-leader/gap-9", git_runner=git)
    assert path.exists() and archive.exists()


@pytest.mark.parametrize("consumer", ["monitor", "eg"])
@pytest.mark.parametrize("outcome", ["success"])
def test_rollout_preserves_completed_legacy_state_before_child_scan(git_leader, monkeypatch, consumer, outcome):
    """A copied terminal record is migrated before a newer child head can reset its gates."""
    root, path, git = git_leader
    data = payload("success")
    data["pull-requests"][0]["head-sha"] = "b" * 40
    data["promotion-gates"][0]["status"] = "success"
    for gate in data["promotion-gates"][1:]:
        gate["status"] = outcome
    path.write_text(json.dumps(data, indent=2) + "\n")
    archive = write_history_snapshot(root, path, move=False)
    git(["add", "."], root)
    git(["commit", "-m", "Legacy EG conclusion with duplicate history"], root)
    git(["push", "origin", "HEAD"], root)
    previous_sha = git(["rev-parse", "HEAD"], root)
    recorded = archive.read_bytes()
    posts = []

    def no_scan(*args, **kwargs):
        """Any rescan could observe a new child revision and reopen the finished run."""
        pytest.fail("A completed legacy record must not scan children or re-read EG comments")

    monkeypatch.setattr(monitor, "run_monitor", no_scan)
    monkeypatch.setattr(monitor, "_post_leader_failure_summary", lambda **k: None)
    monkeypatch.setattr(results, "list_leader_pr_comments", no_scan)

    class Updater:
        dry_run = False
        def __init__(self, **kwargs): pass
        def get_pull_head(self, ref):
            return SimpleNamespace(sha=git(["rev-parse", "HEAD"], root))
        def post_status_for_pr(self, url, state, **kwargs):
            if url != OLD:
                # The collaborator has moved since the archived receipt.
                assert kwargs["head_sha"] == "b" * 40
                assert kwargs["defer_if_head_moved"] is True
                return SimpleNamespace(skipped=False, deferred=True)
            posts.append((url, state, kwargs))
            return SimpleNamespace(skipped=False, deferred=False)

    def gh(args):
        """Keep all GitHub interactions offline while checking the published Leader head."""
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(pull(sha=git(["rev-parse", "HEAD"], root)))
        if args[:2] == ["pr", "diff"]:
            return "\n".join(str(item.relative_to(root)) for item in (path, archive))
        return ""

    common = dict(repo_root=root, leader_pr_url=OLD, head_ref="gap-leader/gap-9",
                  publish=True, git_runner=git, gh_runner=gh, updater_factory=Updater)
    if consumer == "monitor":
        result = monitor.run_monitor_publish_and_signal(path, **common)
    else:
        result = results.run_early_gate_results(state_file=str(path), labels=[], **common)
    assert result.overall_status == outcome
    assert result.state_path == archive
    assert result.published_sha != previous_sha
    assert archive.read_bytes() == recorded
    assert not path.exists()
    assert posts and posts[0][2]["head_sha"] == result.published_sha
    assert git(["rev-parse", "origin/gap-leader/gap-9"], root) == result.published_sha
    assert git(["ls-tree", "-r", "--name-only", result.published_sha], root).splitlines() == [
        archive.relative_to(root).as_posix()
    ]
    # The move must leave an archive-only retry able to finish the same outcome.
    retried = monitor.run_monitor_publish_and_signal(path, **common)
    assert retried.published_sha == result.published_sha
    assert archive.read_bytes() == recorded and not path.exists()


def test_archived_retry_normalizes_child_urls_and_processes_remaining_children():
    """Whitespace in a stored URL must not abort status publication for other children."""
    data = payload("success")
    first = data["pull-requests"][0]
    first["pr-url"] = "  https://github.com/acme/component/pull/3 \n"
    data["pull-requests"].append({
        "pr-url": "https://github.com/acme/other/pull/4", "pr-status": "success", "head-sha": "c" * 40,
    })
    posts = []
    class Updater:
        def post_status_for_pr(self, url, state, **kwargs):
            posts.append((url, kwargs["head_sha"]))
            return SimpleNamespace(skipped=False, deferred=False)
    result = results.post_child_statuses_for_early_gate(
        data, overall_status="success", updater=Updater(), pinned_only=True,
    )
    assert posts == [
        ("https://github.com/acme/component/pull/3", "b" * 40),
        ("https://github.com/acme/other/pull/4", "c" * 40),
    ]
    assert result == tuple(url for url, _ in posts)


def test_archived_retry_preserves_exact_passing_url_match():
    """A normalized alias must not override the SHA of an exact build-passing match."""
    data = payload("success")
    exact = data["pull-requests"][0]
    alias = copy.deepcopy(exact)
    alias["pr-url"] = " " + exact["pr-url"] + " "
    alias["head-sha"] = "c" * 40
    data["pull-requests"].insert(0, alias)
    class Updater:
        def post_status_for_pr(self, url, state, **kwargs):
            assert kwargs["head_sha"] == "b" * 40
            return SimpleNamespace(skipped=False, deferred=False)
    results.post_child_statuses_for_early_gate(
        data, overall_status="success", updater=Updater(), pinned_only=True,
    )


def test_archived_retry_missing_url_match_does_not_abort_remaining_children(monkeypatch):
    """If no stored revision matches a requested child, skip it and continue safely."""
    data = payload("success")
    known = data["pull-requests"][0]["pr-url"]
    monkeypatch.setattr(results, "child_pass_pr_urls", lambda _: ["https://github.com/acme/missing/pull/7", known])
    posts = []
    class Updater:
        def post_status_for_pr(self, url, state, **kwargs):
            posts.append(url)
            return SimpleNamespace(skipped=False, deferred=False)
    assert results.post_child_statuses_for_early_gate(
        data, overall_status="success", updater=Updater(), pinned_only=True,
    ) == (known,)
    assert posts == [known]


@pytest.mark.parametrize("mode", ["publish", "dry-run", "disabled", "disagree", "untracked"])
def test_legacy_migration_respects_rollout_and_preserves_conflicting_records(git_leader, monkeypatch, mode):
    """Migration moves only tracked matching conclusions, and dry runs never alter files."""
    root, path, git = git_leader
    data = payload("success")
    for gate in data["promotion-gates"]:
        gate["status"] = "success"
    path.write_text(json.dumps(data))
    archive = write_history_snapshot(root, path, move=False)
    git(["add", "--", str(path.relative_to(root))], root)
    if mode != "untracked":
        git(["add", "--", str(archive.relative_to(root))], root)
    git(["commit", "-m", "Legacy finalized record"], root)
    git(["push", "origin", "HEAD"], root)
    if mode == "disabled":
        monkeypatch.delenv("GAP_MOVE_FINAL_STATE")
    if mode == "disagree":
        data["pull-requests"][0]["head-sha"] = "c" * 40
        path.write_text(json.dumps(data))
    before = (path.read_bytes(), archive.read_bytes())
    sha = git(["rev-parse", "HEAD"], root)
    if mode in {"untracked", "disagree"}:
        with pytest.raises(monitor.GapPrMonitorError, match="untracked|disagree"):
            monitor.publish_state(root, path, "gap-leader/gap-9", git_runner=git)
        assert (path.read_bytes(), archive.read_bytes()) == before
        assert git(["rev-parse", "HEAD"], root) == sha
        return
    result = monitor.publish_state(
        root, path, "gap-leader/gap-9", git_runner=git, dry_run=mode == "dry-run",
    )
    if mode == "publish":
        assert result.state_path == archive and not path.exists()
        assert archive.read_bytes() == before[1] and result.sha != sha
    elif mode == "dry-run":
        assert (path.read_bytes(), archive.read_bytes()) == before and result.sha == sha
    else:
        assert result.state_path == path and json.loads(path.read_text()) == json.loads(archive.read_text())


def test_archived_retry_never_uses_a_failed_exact_url_alias():
    """Only a build-passing entry may supply a revision, even when another URL matches exactly."""
    data = payload("success")
    passing = data["pull-requests"][0]
    failed = copy.deepcopy(passing)
    failed.update({"pr-status": "build-failure", "head-sha": "c" * 40})
    passing["pr-url"] = " " + passing["pr-url"] + " "
    data["pull-requests"].insert(0, failed)
    class Updater:
        def post_status_for_pr(self, url, state, **kwargs):
            assert kwargs["head_sha"] == "b" * 40
            return SimpleNamespace(skipped=False, deferred=False)
    assert results.post_child_statuses_for_early_gate(
        data, overall_status="success", updater=Updater(), pinned_only=True,
    ) == (failed["pr-url"],)


@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize("child_moved", [False, True])
def test_first_archive_finalization_uses_evaluated_child_revision(git_leader, outcome, child_moved):
    """The first archive publication must not approve code newer than its saved build receipt."""
    root, path, git = git_leader
    data = payload("success")
    data["pull-requests"][0]["head-sha"] = "b" * 40
    data["promotion-gates"][0]["status"] = "success"
    path.write_text(json.dumps(data))
    git(["add", "."], root)
    git(["commit", "-m", "Component ready for EG"], root)
    git(["push", "origin", "HEAD"], root)
    posts = []
    leader_commit = git(["rev-parse", "HEAD"], root)
    build_result = "failure" if outcome == "failure" else "success"
    comments = [
        {
            "id": 1,
            "created_at": "2026-11-05T12:00:00Z",
            "user": {"login": "rhods-ci-bot"},
            "body": (
                f"<!-- early-gate-build-complete GAP_PR_COMMIT={leader_commit} -->\n"
                + (":x: **Early Gate failed**" if build_result == "failure" else ":white_check_mark: **Early Gate passed**")
            ),
        }
    ]
    if outcome == "success":
        comments.append({
            "id": 2,
            "created_at": "2026-11-05T12:01:00Z",
            "user": {"login": "rhods-ci-bot"},
            "body": (
                f"<!-- early-gate-job-complete GAP_PR_COMMIT={leader_commit} -->\n"
                ":white_check_mark: **Early Gate Tests Passed**"
            ),
        })

    def gh(args):
        """Emulate GitHub heads and statuses while real updater code enforces revision checks."""
        if args[0] != "api":
            return ""
        endpoint = args[1] if args[1] != "--method" else args[3]
        if endpoint.endswith("/pulls/9"):
            if args[-1:] == [".head.sha"]:
                return git(["rev-parse", "HEAD"], root)
            if "--jq" in args:
                return json.dumps({
                    "sha": git(["rev-parse", "HEAD"], root),
                    "owner": "acme",
                    "repo": "gap",
                    "state": "open",
                })
            return json.dumps(pull(sha=git(["rev-parse", "HEAD"], root)))
        if endpoint.endswith("/pulls/3"):
            return json.dumps({"sha": ("c" if child_moved else "b") * 40,
                               "owner": "acme", "repo": "component", "state": "open"})
        if "/statuses/" in endpoint:
            posts.append(endpoint)
            return "{}"
        if "/commits/" in endpoint:
            return ""
        if "/comments" in endpoint:
            return json.dumps(comments)
        return ""

    def factory(**kwargs):
        """Use the actual status updater with an offline GitHub transport."""
        return results.PRStatusUpdater(
            **kwargs, runner=lambda command: subprocess.CompletedProcess(command, 0, gh(command[1:]), ""),
        )

    labels = ["early-gate-build-successful", "early-gate-tests-successful"] if outcome == "success" else [
        "early-gate-build-failed",
    ]
    result = results.run_early_gate_results(
        repo_root=root, leader_pr_url=OLD, state_file=str(path), labels=labels,
        head_ref="gap-leader/gap-9", publish=True, git_runner=git, gh_runner=gh,
        updater_factory=factory,
    )
    assert result.overall_status == outcome
    if outcome == "success":
        assert not path.exists() and result.state_path.parent.parent.name == "successful"
    else:
        assert path.exists() and result.state_path == path
        assert not result.leader_status_posted
    child_posts = [endpoint for endpoint in posts if "/component/" in endpoint]
    assert child_posts == ([] if child_moved else ["repos/acme/component/statuses/" + "b" * 40])
    assert result.child_urls_posted == (() if child_moved else (data["pull-requests"][0]["pr-url"],))
    assert json.loads(result.state_path.read_text())["pull-requests"][0]["head-sha"] == "b" * 40


def test_active_monitor_still_refreshes_receipts_and_retriggers_eg(git_leader, monkeypatch):
    """Exclusive history must not archive an active run or suppress its child-head retrigger."""
    root, path, git = git_leader
    child = payload()["pull-requests"][0]["pr-url"]
    stage = {"status": "build-pending", "sha": "b" * 40}
    comments, posts = [], []
    triggered_commits = set()
    monkeypatch.setenv("GAP_BUILD_STATUS_SOURCE", "konflux")
    monkeypatch.setattr(monitor, "fetch_component_repo_map", lambda _: {"component": frozenset({"model"})})
    monkeypatch.setattr(monitor, "_post_leader_failure_summary", lambda **kwargs: None)

    def observe(url, **kwargs):
        """Supply changing Konflux observations while keeping the monitor coordinator real."""
        return monitor.RepositoryGateDecision(
            url=url, pr_status=stage["status"], cli_status="completed" if stage["status"] == "success" else "pending",
            description="build observation", reason="build observation", head_sha=stage["sha"],
            build_observations={"source": "konflux", "head-sha": stage["sha"], "runs": []},
        )

    monkeypatch.setattr(monitor, "_decide_url", observe)

    def gh(args):
        """Record status and trigger writes without any real GitHub changes."""
        if args[:2] == ["pr", "comment"]:
            comments.append(args[args.index("--body") + 1])
            triggered_commits.add(git(["rev-parse", "HEAD"], root))
            return ""
        if args[0] != "api":
            raise AssertionError(args)
        endpoint = args[1] if args[1] != "--method" else args[3]
        if "/check-runs" in endpoint:
            leader_commit = endpoint.split("/commits/", 1)[1].split("/", 1)[0]
            return json.dumps(["run"] if leader_commit in triggered_commits else [])
        if endpoint.endswith("/pulls/3"):
            return json.dumps({"sha": stage["sha"], "owner": "acme", "repo": "component", "state": "open"})
        if "/statuses/" in endpoint:
            posts.append(args)
            return "{}"
        if "/commits/" in endpoint:
            return ""
        if "/comments" in endpoint:
            return json.dumps([{"body": body} for body in comments])
        raise AssertionError(args)

    def factory(**kwargs):
        """Exercise revision checks and the child-green hold in the real updater."""
        return results.PRStatusUpdater(
            **kwargs, runner=lambda command: subprocess.CompletedProcess(command, 0, gh(command[1:]), ""),
        )

    for status, sha, count in [("build-pending", "b" * 40, 0), ("success", "b" * 40, 1),
                               ("success", "b" * 40, 1), ("success", "c" * 40, 2)]:
        stage.update(status=status, sha=sha)
        result = monitor.run_monitor_publish_and_signal(
            path, repo_root=root, leader_pr_url=OLD, head_ref="gap-leader/gap-9",
            publish=True, git_runner=git, gh_runner=gh, updater_factory=factory,
        )
        saved = json.loads(path.read_text())
        assert result.state_path == path and not result.leader_status_posted
        assert not list((root / "GAP History").rglob("state.json"))
        assert saved["pull-requests"][0]["head-sha"] == sha
        assert saved["pull-requests"][0]["build-observations"]["head-sha"] == sha
        assert len(comments) == count
        assert git(["rev-parse", "origin/gap-leader/gap-9"], root) == result.published_sha
    assert all("state=pending" in command for command in posts)
    assert all("/component/statuses/" in " ".join(command) for command in posts)


@pytest.mark.parametrize("builds,eg_build,eg_test", [
    ("failure", "pending", "pending"),
    ("pending", "success", "success"),
    ("success", "failure", "pending"),
    ("success", "success", "failure"),
    ("success", "success", "pending"),
    ("success", "success", "success"),
])
def test_child_approval_requires_successful_batch_and_eg(builds, eg_build, eg_test):
    data = payload("success")
    for gate, status in zip(data["promotion-gates"], [builds, eg_build, eg_test]):
        gate["status"] = status
    assert children_ready_for_success_status(data) is (builds == eg_build == eg_test == "success")


def test_unsuccessful_leader_cannot_request_merge_without_replacement():
    def forbidden(*args, **kwargs):
        pytest.fail("No GitHub operation is allowed for unverified failure finalization")
    with pytest.raises(monitor.GapPrMonitorError, match="verified newer replacement"):
        monitor.post_leader_conclusion(OLD, SHA, overall_status="failure", updater=forbidden,
                                       gh_runner=forbidden)


def test_legacy_failed_archive_resumes_active_monitoring(git_leader, monkeypatch):
    """Replay #94: a failed archive alone must never authorize a history merge."""
    root, path, git = git_leader
    data = payload("build-failure")
    data["promotion-gates"][0]["status"] = "failure"
    path.write_text(json.dumps(data))
    archive = write_history_snapshot(root, path)
    git(["add", "."], root)
    git(["commit", "-m", "Legacy prematurely archived failure"], root)
    before = archive.read_bytes()
    def evaluate(state, **kwargs):
        assert state == path and state.read_bytes() == before
        return monitor.MonitorResult(state, [], [], [], [], False, overall_status="failure")
    def forbidden(*args, **kwargs):
        pytest.fail("Failed Leader must not merge or trigger EG")
    monkeypatch.setattr(monitor, "run_monitor", evaluate)
    monkeypatch.setattr(monitor, "post_leader_conclusion", forbidden)
    monkeypatch.setattr(monitor, "post_early_gate_trigger_comment", forbidden)
    monkeypatch.setattr(monitor, "_post_leader_failure_summary", lambda **kwargs: None)
    result = monitor.run_monitor_publish_and_signal(
        path, repo_root=root, head_ref="gap-leader/gap-9", leader_pr_url=OLD,
        publish=True, git_runner=git, gh_runner=forbidden,
    )
    assert result.state_path == path and path.read_bytes() == before
    assert not archive.exists() and not result.leader_status_posted
    # A repeated pass must also remain active and must not recreate history.
    monitor.run_monitor_publish_and_signal(
        path, repo_root=root, head_ref="gap-leader/gap-9", leader_pr_url=OLD,
        publish=True, git_runner=git, gh_runner=forbidden,
    )
    assert path.exists() and not archive.exists()


def test_failed_archive_retirement_requires_verified_replacement(git_leader):
    root, path, git = git_leader
    data = payload("build-failure")
    data["promotion-gates"][0]["status"] = "failure"
    path.write_text(json.dumps(data))
    archive = write_history_snapshot(root, path)
    git(["add", "."], root)
    git(["commit", "-m", "Legacy failed archive"], root)
    writes = []
    def gh(args):
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(pull(sha=git(["rev-parse", "HEAD"], root)))
        if args == ["api", "repos/acme/gap/pulls/10"]:
            return json.dumps(pull(10))
        if args[:2] == ["pr", "diff"]:
            return str(archive.relative_to(root))
        writes.append(args)
        return ""
    with pytest.raises(monitor.GapPrMonitorError, match="Missing replacement"):
        monitor.retire_previous_leader(archive, repo_root=root, head_ref="gap-leader/gap-9",
            leader_pr_url=OLD, replacement_url=None, check_name="gate", dry_run=False,
            remote="origin", git_runner=git, gh_runner=gh)
    assert not writes
    # Dry-run with a genuine newer batch validates the replacement first.
    result = monitor.retire_previous_leader(archive, repo_root=root, head_ref="gap-leader/gap-9",
        leader_pr_url=OLD, replacement_url=NEW, check_name="gate", dry_run=True,
        remote="origin", git_runner=git, gh_runner=gh)
    assert result.overall_status == "failure" and not writes


def test_legacy_failed_archive_can_retire_only_after_new_leader(git_leader, monkeypatch):
    """Publish replacement metadata before merge; retries never approve children."""
    root, path, git = git_leader
    data = payload("build-failure")
    data["promotion-gates"][0]["status"] = "failure"
    path.write_text(json.dumps(data))
    archive = write_history_snapshot(root, path)
    git(["add", "."], root)
    git(["commit", "-m", "Legacy failure"], root)
    calls, posts = [], []
    def gh(args):
        if args == ["api", "repos/acme/gap/pulls/9"]:
            return json.dumps(pull(sha=git(["rev-parse", "HEAD"], root)))
        if args == ["api", "repos/acme/gap/pulls/10"]:
            calls.append("replacement-validated")
            return json.dumps(pull(10))
        if args[:2] == ["pr", "diff"]:
            return str(archive.relative_to(root))
        if args[:2] == ["pr", "merge"]:
            saved = json.loads(archive.read_text())
            assert saved[KEY]["replacement-leader"] == NEW
            assert "replacement-validated" in calls
            calls.append("merge")
        return ""
    class Updater:
        def __init__(self, **kwargs): self.dry_run = False
        def get_pull_head(self, ref):
            return SimpleNamespace(sha=git(["rev-parse", "HEAD"], root))
        def post_status_for_pr(self, url, state, **kwargs):
            assert url == OLD
            posts.append((url, state))
            return SimpleNamespace(skipped=False)
    monkeypatch.setattr(monitor, "_post_leader_failure_summary", lambda **kwargs: None)
    kwargs = dict(repo_root=root, head_ref="gap-leader/gap-9", leader_pr_url=OLD,
                  check_name="gate", dry_run=False, remote="origin", git_runner=git,
                  gh_runner=gh, updater_factory=Updater)
    first = monitor.retire_previous_leader(archive, replacement_url=NEW, **kwargs)
    assert first.overall_status == "failure" and calls[-1] == "merge"
    assert not path.exists() and retired_outcome(json.loads(archive.read_text())) == "failure"
    assert monitor.resume_unsuccessful_leader(root, archive, git_runner=git) == archive
    before = archive.read_bytes()
    retry = monitor.retire_previous_leader(archive, replacement_url=None, **kwargs)
    assert retry.published_sha == first.published_sha and archive.read_bytes() == before
    assert len(posts) == 2
