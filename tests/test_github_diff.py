"""Pinned comparison tests: API fast path and real local Git fallback, no network."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from lib import github_diff as diff
from lib.github_check_runs import CheckQueryError
from lib.github_cli import GhCommandError

BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40


def response(files):
    return {"base_commit": {"sha": BASE}, "merge_base_commit": {"sha": MERGE}, "files": files}


def test_compare_fast_path_is_pinned_and_preserves_statuses(monkeypatch):
    calls = []
    files = [{"filename": name, "status": status} for name, status in [
        ("new", "added"), ("gone", "removed"), ("edit", "modified"),
        ("renamed", "renamed"), ("copy", "copied"), ("type", "changed"),
    ]]
    def runner(args):
        calls.append(args)
        return json.dumps(response(files))
    monkeypatch.setattr(diff, "_git_file_collections", lambda *args: pytest.fail("unexpected fallback"))
    result = diff.fetch_comparison_files("owner", "repo", BASE, HEAD, runner)
    assert calls == [["api", f"repos/owner/repo/compare/{BASE}...{HEAD}?per_page=1&page=1"]]
    assert result == {"all": tuple(f["filename"] for f in files), "added": ("new", "copy"),
                      "deleted": ("gone",), "modified": ("edit", "type"), "renamed": ("renamed",)}


def test_empty_diff_is_complete(monkeypatch):
    monkeypatch.setattr(diff, "_git_file_collections", lambda *args: pytest.fail("unexpected fallback"))
    result = diff.fetch_comparison_files("owner", "repo", BASE, HEAD, lambda args: json.dumps(response([])))
    assert all(not paths for paths in result.values())


@pytest.mark.parametrize("count, fallback", [(299, False), (300, True), (301, True)])
def test_cap_is_conservative(monkeypatch, count, fallback):
    files = [{"filename": f"file-{i}", "status": "added"} for i in range(count)]
    calls = []
    def local(*args):
        calls.append(args)
        return {"all": ("complete-local-result",)}
    monkeypatch.setattr(diff, "_git_file_collections", local)
    result = diff.fetch_comparison_files("owner", "repo", BASE, HEAD, lambda args: json.dumps(response(files)))
    assert bool(calls) is fallback
    if fallback:
        assert calls == [("https://github.com/owner/repo.git", BASE, HEAD, MERGE)]
        assert result["all"] == ("complete-local-result",)


@pytest.mark.parametrize("payload", [
    "not json", [], {}, response(None), response([{}]),
    response([{"filename": "file", "status": "unexpected"}]),
    {"base_commit": {"sha": HEAD}, "merge_base_commit": {"sha": MERGE}, "files": []},
])
def test_bad_api_response_uses_local_fallback(monkeypatch, payload):
    calls = []
    def local(*args):
        calls.append(args)
        return {"all": ("local",)}
    monkeypatch.setattr(diff, "_git_file_collections", local)
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    assert diff.fetch_comparison_files("owner", "repo", BASE, HEAD, lambda args: raw)["all"] == ("local",)
    assert calls[0][1:3] == (BASE, HEAD)


def test_api_error_computes_merge_base_locally(monkeypatch):
    def runner(args):
        raise GhCommandError(["gh", *args], 1, "unavailable")
    def local(url, base, head, merge_base):
        assert (base, head, merge_base) == (BASE, HEAD, None)
        return {"all": ("local",)}
    monkeypatch.setattr(diff, "_git_file_collections", local)
    assert diff.fetch_comparison_files("owner", "repo", BASE, HEAD, runner)["all"] == ("local",)


@pytest.mark.parametrize("error", [OSError(), subprocess.TimeoutExpired("git", 1), CheckQueryError("failed")])
def test_fallback_failure_is_unresolved(monkeypatch, error):
    def local(*args):
        raise error
    monkeypatch.setattr(diff, "_git_file_collections", local)
    with pytest.raises(CheckQueryError, match="complete pinned file comparison"):
        diff.fetch_comparison_files("owner", "repo", BASE, HEAD, lambda args: "{}")


@pytest.mark.parametrize("base, head", [("main", HEAD), (BASE, "--upload-pack=evil"), (None, HEAD)])
def test_requires_immutable_shas(base, head):
    with pytest.raises(CheckQueryError, match="immutable"):
        diff.fetch_comparison_files("owner", "repo", base, head, lambda args: pytest.fail("must not query"))


@pytest.mark.parametrize("data", [b"M\0incomplete", b"R100\0old\0", b"X\0file\0"])
def test_incomplete_or_unknown_git_output_is_unresolved(data):
    with pytest.raises(CheckQueryError):
        diff._parse_name_status(data)


def git(repo, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(repo):
    git(repo, "add", "--all")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--template=", "-b", "main")
    for name in ["rename-old", "edit", "gone"]:
        (repo / name).write_text((name + "\n") * 100)
    merge = commit(repo)
    git(repo, "checkout", "-b", "feature")
    (repo / "rename-old").rename(repo / "rename-new")
    (repo / "edit").write_text("updated\n" * 100)
    (repo / "gone").unlink()
    (repo / "space\ttab\nnewline").write_text("new file")
    for i in range(301):
        (repo / f"added-{i:03}").write_text(str(i))
    head = commit(repo)
    git(repo, "checkout", "main")
    (repo / "base-only").write_text("must not appear in PR diff")
    base = commit(repo)
    return repo, base, head, merge


@pytest.mark.parametrize("known_merge", [True, False])
def test_real_git_diff_is_complete_and_uses_merge_base(history, known_merge):
    repo, base, head, merge = history
    result = diff._git_file_collections(repo.as_uri(), base, head, merge if known_merge else None)
    assert len(result["all"]) == 305
    assert "base-only" not in result["all"]
    assert result["renamed"] == ("rename-new",)
    assert result["deleted"] == ("gone",)
    assert result["modified"] == ("edit",)
    assert "space\ttab\nnewline" in result["added"]
    assert "rename-old" not in result["all"]
    assert git(repo, "status", "--porcelain") == ""


def test_truncated_api_to_real_git_fallback(history):
    repo, base, head, merge = history
    from unittest.mock import patch
    local = diff._git_file_collections
    def fallback(url, b, h, m):
        assert (b, h, m) == (base, head, merge)
        return local(repo.as_uri(), b, h, m)
    payload = {"base_commit": {"sha": base}, "merge_base_commit": {"sha": merge},
               "files": [{"filename": str(i), "status": "added"} for i in range(300)]}
    with patch.object(diff, "_git_file_collections", side_effect=fallback):
        result = diff.fetch_comparison_files("owner", "repo", base, head, lambda args: json.dumps(payload))
    assert len(result["all"]) == 305


@pytest.mark.parametrize("known_merge", [True, False])
def test_git_commands_are_no_checkout_pinned_and_noninteractive(monkeypatch, known_merge):
    from types import SimpleNamespace
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "must-not-run")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        env = kwargs["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_VALUE_0" not in env
        assert "credential.helper=" in command
        assert "core.hooksPath=/dev/null" in command
        assert 0 < kwargs["timeout"] <= diff.GIT_TIMEOUT_SECONDS
        if "merge-base" in command:
            kwargs["stdout"].write((MERGE + "\n").encode())
        if "diff" in command:
            kwargs["stdout"].write(b"A\0new\0")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(diff.subprocess, "run", run)
    result = diff._git_file_collections("https://github.com/owner/repo.git", BASE, HEAD, MERGE if known_merge else None)
    assert result["added"] == ("new",)
    fetch = next(command for command in commands if "fetch" in command)
    assert BASE in fetch and HEAD in fetch and "--filter=blob:none" in fetch
    assert ("--depth=1" in fetch) is known_merge
    assert not any("checkout" in command for command in commands)
    command = next(command for command in commands if "diff" in command)
    assert command[-3:] == [MERGE, HEAD, "--"]
    assert "--no-ext-diff" in command and "--no-textconv" in command and "-z" in command


def test_git_output_limit_is_unresolved(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(diff, "MAX_DIFF_BYTES", 1)
    def run(command, **kwargs):
        kwargs["stdout"].write(b"too much output")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(diff.subprocess, "run", run)
    with pytest.raises(CheckQueryError, match="output limit"):
        diff._git_file_collections("https://github.com/owner/repo.git", BASE, HEAD)


def test_git_total_time_budget_is_unresolved(monkeypatch):
    times = iter([0, diff.GIT_TIMEOUT_SECONDS + 1])
    monkeypatch.setattr(diff.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(diff.subprocess, "run", lambda *args, **kwargs: pytest.fail("budget already exceeded"))
    with pytest.raises(CheckQueryError, match="time budget"):
        diff._git_file_collections("https://github.com/owner/repo.git", BASE, HEAD)


def test_git_timeout_propagates_without_partial_files(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 1)
    monkeypatch.setattr(diff.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        diff._git_file_collections("https://github.com/owner/repo.git", BASE, HEAD)
