from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lib.gap_pr_monitor import (
    GapPrMonitorError,
    apply_success_statuses,
    extract_pr_urls,
    load_state,
    run_stage1_monitor,
    save_state,
)
from lib.pr_status_updater import PullRequestRef, StatusUpdateResult


def _sample_state() -> dict:
    return {
        "pull-requests": [
            {
                "repo": "kserve-branch",
                "pr-url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
                "pr-status": "new",
                "builds": [],
            },
            {
                "repo": "kubeflow",
                "pr-url": "https://github.com/rhoai-rhtap/kubeflow/pull/85",
                "pr-status": "new",
                "builds": [],
            },
        ]
    }


def test_load_and_extract_pr_urls(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())
    payload = load_state(path)
    assert extract_pr_urls(payload) == [
        "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
        "https://github.com/rhoai-rhtap/kubeflow/pull/85",
    ]


def test_load_state_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GapPrMonitorError, match="not found"):
        load_state(tmp_path / "missing.json")


def test_load_state_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(GapPrMonitorError, match="Invalid JSON"):
        load_state(path)


def test_extract_pr_urls_requires_pr_url() -> None:
    with pytest.raises(GapPrMonitorError, match="pr-url"):
        extract_pr_urls({"pull-requests": [{"repo": "x", "pr-status": "new"}]})


def test_apply_success_statuses() -> None:
    payload = _sample_state()
    apply_success_statuses(
        payload, ["https://github.com/rhoai-rhtap/kserve-branch/pull/15"]
    )
    assert payload["pull-requests"][0]["pr-status"] == "success"
    assert payload["pull-requests"][1]["pr-status"] == "new"


def test_run_stage1_monitor_posts_and_updates_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())
    calls: list[tuple] = []

    class FakeUpdater:
        def __init__(self, *, check_name: str, dry_run: bool = False) -> None:
            self.check_name = check_name
            self.dry_run = dry_run

        def post_status_for_many(self, pr_urls, status, **kwargs):
            calls.append((list(pr_urls), status, kwargs))
            results = []
            for url in pr_urls:
                owner, repo, number = (
                    url.split("github.com/")[1].split("/pull/")[0].split("/")[0],
                    url.split("github.com/")[1].split("/pull/")[0].split("/")[1],
                    int(url.rstrip("/").split("/")[-1]),
                )
                results.append(
                    StatusUpdateResult(
                        pr=PullRequestRef(owner, repo, number),
                        head_sha="abc",
                        state="success",
                        context=self.check_name,
                        dry_run=self.dry_run,
                        skipped=False,
                    )
                )
            return results

    result = run_stage1_monitor(path, updater_factory=FakeUpdater)
    assert calls[0][1] == "completed"
    assert len(result.updated_urls) == 2
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert all(e["pr-status"] == "success" for e in saved["pull-requests"])


def test_run_stage1_monitor_dry_run_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())

    class FakeUpdater:
        def __init__(self, *, check_name: str, dry_run: bool = False) -> None:
            self.check_name = check_name
            self.dry_run = dry_run

        def post_status_for_many(self, pr_urls, status, **kwargs):
            return [
                StatusUpdateResult(
                    pr=PullRequestRef("rhoai-rhtap", "kserve-branch", 15),
                    head_sha="abc",
                    state="success",
                    context=self.check_name,
                    dry_run=True,
                    skipped=False,
                ),
                StatusUpdateResult(
                    pr=PullRequestRef("rhoai-rhtap", "kubeflow", 85),
                    head_sha="def",
                    state="success",
                    context=self.check_name,
                    dry_run=True,
                    skipped=False,
                ),
            ]

    run_stage1_monitor(path, dry_run=True, updater_factory=FakeUpdater)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert all(e["pr-status"] == "new" for e in saved["pull-requests"])


def test_main_requires_state_file() -> None:
    from scripts import gap_pr_monitor

    with pytest.raises(SystemExit):
        gap_pr_monitor.main([])


def test_main_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import gap_pr_monitor

    path = tmp_path / "state.json"
    save_state(path, _sample_state())

    def fake_run(state_path, **kwargs):
        from lib.gap_pr_monitor import MonitorResult

        return MonitorResult(
            state_path=Path(state_path),
            updated_urls=["https://github.com/rhoai-rhtap/kserve-branch/pull/15"],
            skipped_urls=[],
            dry_run=False,
        )

    monkeypatch.setattr(gap_pr_monitor, "run_stage1_monitor", fake_run)
    code = gap_pr_monitor.main(["--state-file", str(path)])
    assert code == 0
    assert "Posted completed" in capsys.readouterr().out
