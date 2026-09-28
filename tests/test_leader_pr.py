from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lib.leader_pr import GAP_LABEL, GhCommandError, LeaderPRManager
from lib.state_file import build_state


def _completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = ""
    return result


def _assert_dated_state_path(path: str, trigger_id: str) -> None:
    assert re.fullmatch(
        rf"GAP Leaders/\d{{4}}-\d{{2}}-\d{{2}}_{re.escape(trigger_id)}/state\.json",
        path,
    )


def _leader_branch(trigger_id: str) -> str:
    return f"gap-leader/{trigger_id}"


def test_dry_run_prints_gh_pr_create_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path | None) -> MagicMock:
        calls.append(list(command))
        if command[:3] == ["gh", "pr", "list"]:
            return _completed("[]\n")
        return _completed("")

    manager = LeaderPRManager(
        repo="red-hat-data-services/gated-artifacts-promoter",
        dry_run=True,
        runner=fake_runner,
    )
    state = build_state(pull_requests=[])
    result = manager.create_or_update(state, trigger_id="gap-testtrigger001")

    captured = capsys.readouterr().out
    assert "[dry-run] gh pr create" in captured
    assert "--dry-run" in captured
    assert result.dry_run is True
    assert result.pr_url is None
    assert result.branch == _leader_branch("gap-testtrigger001")
    _assert_dated_state_path(result.state_path, "gap-testtrigger001")
    assert any(cmd[:3] == ["gh", "pr", "list"] for cmd in calls)


def test_create_leader_pr_via_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[tuple[list[str], str | None]] = []

    def fake_runner(command: list[str], cwd: Path | None) -> MagicMock:
        calls.append((list(command), str(cwd) if cwd else None))
        if command[:3] == ["gh", "pr", "list"]:
            return _completed("[]\n")
        if command[:3] == ["gh", "repo", "clone"]:
            dest = Path(command[4])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            return _completed("")
        if command[0] == "git" and command[1] == "status":
            return _completed("A  GAP Leaders/2026-09-24_gap-testtrigger002/state.json\n")
        if command[:3] == ["gh", "pr", "create"]:
            return _completed(
                "https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/9\n"
            )
        return _completed("")

    manager = LeaderPRManager(
        repo="red-hat-data-services/gated-artifacts-promoter",
        runner=fake_runner,
    )
    state = build_state(
        pull_requests=[
            {
                "repo": "kserve",
                "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/2",
            }
        ],
    )
    result = manager.create_or_update(state, trigger_id="gap-testtrigger002")

    assert result.updated is False
    assert result.pr_number == 9
    assert result.pr_url.endswith("/pull/9")
    assert result.branch == _leader_branch("gap-testtrigger002")
    _assert_dated_state_path(result.state_path, "gap-testtrigger002")

    create_cmd = next(cmd for cmd, _ in calls if cmd[:3] == ["gh", "pr", "create"])
    assert "--label" in create_cmd
    assert "gap-testtrigger002" in create_cmd
    assert GAP_LABEL in create_cmd
    assert _leader_branch("gap-testtrigger002") in create_cmd
    push_cmd = next(cmd for cmd, _ in calls if cmd[:2] == ["git", "push"])
    assert "--force" in push_cmd


def test_update_existing_leader_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path | None) -> MagicMock:
        calls.append(list(command))
        if command[:3] == ["gh", "pr", "list"]:
            return _completed(
                json.dumps(
                    [
                        {
                            "number": 4,
                            "url": "https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/4",
                            "headRefName": _leader_branch("gap-existing"),
                            "title": "old",
                        }
                    ]
                )
            )
        if command[:3] == ["gh", "repo", "clone"]:
            dest = Path(command[4])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            return _completed("")
        if command[0] == "git" and command[1] == "checkout" and "-B" in command:
            # Simulate existing dated folder on the Leader branch.
            assert cwd is not None
            existing = (
                Path(cwd)
                / "GAP Leaders"
                / "2026-09-20_gap-existing"
            )
            existing.mkdir(parents=True, exist_ok=True)
            (existing / "state.json").write_text("{}", encoding="utf-8")
            return _completed("")
        if command[0] == "git" and command[1] == "status":
            return _completed("M  GAP Leaders/2026-09-20_gap-existing/state.json\n")
        return _completed("")

    manager = LeaderPRManager(
        repo="red-hat-data-services/gated-artifacts-promoter",
        runner=fake_runner,
    )
    state = build_state(pull_requests=[])
    result = manager.create_or_update(state, trigger_id="gap-existing")
    assert result.updated is True
    assert result.pr_number == 4
    assert result.branch == _leader_branch("gap-existing")
    assert result.state_path == "GAP Leaders/2026-09-20_gap-existing/state.json"

    label_cmd = next(
        cmd
        for cmd in calls
        if cmd[:2] == ["gh", "api"] and "issues/4/labels" in "/".join(cmd)
    )
    assert "labels[]=gap-existing" in label_cmd
    assert f"labels[]={GAP_LABEL}" in label_cmd
    push_cmd = next(cmd for cmd in calls if cmd[:2] == ["git", "push"])
    assert "--force" not in push_cmd


def test_new_trigger_merges_previous_leader_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new trigger merges prior open Leader PRs, then opens a new one."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    listed_labels: list[str] = []
    merge_calls: list[list[str]] = []
    calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path | None) -> MagicMock:
        calls.append(list(command))
        if command[:3] == ["gh", "pr", "list"]:
            idx = command.index("--label")
            label = command[idx + 1]
            listed_labels.append(label)
            if label == GAP_LABEL:
                return _completed(
                    json.dumps(
                        [
                            {
                                "number": 5,
                                "url": "https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/5",
                                "headRefName": _leader_branch("gap-old"),
                                "title": "old leader",
                            }
                        ]
                    )
                )
            return _completed("[]\n")
        if command[:3] == ["gh", "pr", "comment"]:
            return _completed("")
        if command[:3] == ["gh", "pr", "merge"]:
            merge_calls.append(list(command))
            assert "--delete-branch" in command
            return _completed("")
        if command[:3] == ["gh", "repo", "clone"]:
            dest = Path(command[4])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            return _completed("")
        if command[0] == "git" and command[1] == "status":
            return _completed("A  GAP Leaders/new/state.json\n")
        if command[:3] == ["gh", "pr", "create"]:
            assert _leader_branch("gap-newrun") in command
            return _completed(
                "https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/12\n"
            )
        return _completed("")

    manager = LeaderPRManager(
        repo="red-hat-data-services/gated-artifacts-promoter",
        runner=fake_runner,
    )
    result = manager.create_or_update(build_state(pull_requests=[]), trigger_id="gap-newrun")
    assert listed_labels[0] == "gap-newrun"
    assert GAP_LABEL in listed_labels
    assert result.updated is False
    assert result.pr_number == 12
    assert result.branch == _leader_branch("gap-newrun")
    _assert_dated_state_path(result.state_path, "gap-newrun")
    assert any(cmd[3] == "5" and "--merge" in cmd for cmd in merge_calls)
    push_cmd = next(cmd for cmd in calls if cmd[:2] == ["git", "push"])
    assert "--force" in push_cmd


def test_update_existing_does_not_merge_current_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    merge_calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path | None) -> MagicMock:
        if command[:3] == ["gh", "pr", "list"]:
            return _completed(
                json.dumps(
                    [
                        {
                            "number": 4,
                            "url": "https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/4",
                            "headRefName": _leader_branch("gap-existing"),
                            "title": "current",
                        }
                    ]
                )
            )
        if command[:3] == ["gh", "pr", "merge"]:
            merge_calls.append(list(command))
            return _completed("")
        if command[:3] == ["gh", "repo", "clone"]:
            dest = Path(command[4])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            return _completed("")
        if command[0] == "git" and command[1] == "checkout" and "-B" in command:
            assert cwd is not None
            existing = Path(cwd) / "GAP Leaders" / "2026-09-20_gap-existing"
            existing.mkdir(parents=True, exist_ok=True)
            (existing / "state.json").write_text("{}", encoding="utf-8")
            return _completed("")
        if command[0] == "git" and command[1] == "status":
            return _completed("M  GAP Leaders/2026-09-20_gap-existing/state.json\n")
        return _completed("")

    manager = LeaderPRManager(
        repo="red-hat-data-services/gated-artifacts-promoter",
        runner=fake_runner,
    )
    result = manager.create_or_update(build_state(pull_requests=[]), trigger_id="gap-existing")
    assert result.pr_number == 4
    assert merge_calls == []


def test_gh_failure_raises() -> None:
    def fake_runner(command: list[str], cwd: Path | None) -> MagicMock:
        return _completed("boom", returncode=1)

    manager = LeaderPRManager(runner=fake_runner)
    with pytest.raises(GhCommandError):
        manager.find_open_pr_by_label("gap-x")
