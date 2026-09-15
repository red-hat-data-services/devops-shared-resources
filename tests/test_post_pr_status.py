from __future__ import annotations

import pytest

from scripts import post_pr_status


def test_main_requires_status() -> None:
    with pytest.raises(SystemExit) as exc_info:
        post_pr_status.main([])
    assert exc_info.value.code != 0


def test_main_requires_pr_targets() -> None:
    assert post_pr_status.main(["--status", "success"]) == 1


def test_main_rejects_invalid_status() -> None:
    assert post_pr_status.main(["--status", "not-a-state", "--pr-url", "x"]) == 1


def test_main_rejects_invalid_pr_url_before_gh() -> None:
    assert (
        post_pr_status.main(
            ["--status", "success", "--pr-url", "https://example.com/nope"]
        )
        == 1
    )


def test_main_dry_run_multiple_pr_urls(capsys: pytest.CaptureFixture[str]) -> None:
    code = post_pr_status.main(
        [
            "--dry-run",
            "--status",
            "success",
            "--pr-url",
            "https://github.com/org/a/pull/1",
            "--pr-url",
            "https://github.com/org/b/pull/2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "org/a/pulls/1" in out
    assert "org/b/pulls/2" in out


def test_main_dry_run_repo_and_label(capsys: pytest.CaptureFixture[str]) -> None:
    code = post_pr_status.main(
        [
            "--dry-run",
            "--status",
            "success",
            "--repo",
            "org/repo",
            "--label",
            "gap-test",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "gh pr list --repo org/repo" in out
    assert "gap-test" in out
