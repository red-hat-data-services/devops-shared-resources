from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.state_file import (
    PR_STATUS_NEW,
    StateFileError,
    StatePullRequest,
    build_state,
    load_state_file,
    short_repo_name,
    validate_state,
    write_state_file,
)


def test_build_and_validate_stage1_state() -> None:
    state = build_state(
        pull_requests=[
            {
                "repo": "kserve",
                "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/2",
            }
        ],
    )
    payload = state.to_dict()
    assert list(payload.keys()) == ["pull-requests"]
    entry = payload["pull-requests"][0]
    assert entry == {
        "repo": "kserve",
        "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/2",
        "pr-status": "new",
        "builds": [],
        "ignore": False,
    }
    assert validate_state(payload)["pull-requests"][0]["pr-status"] == PR_STATUS_NEW


def test_build_state_reduces_owner_repo_to_slug() -> None:
    state = build_state(
        pull_requests=[
            {
                "repo": "rhoai-rhtap/kubeflow",
                "pr-url": "https://github.com/rhoai-rhtap/kubeflow/pull/81",
            }
        ]
    )
    assert state.pull_requests[0].repo == "kubeflow"


def test_write_and_load_state_file(tmp_path: Path) -> None:
    state = build_state(
        pull_requests=[
            StatePullRequest(
                repo="kubeflow",
                pr_url="https://github.com/rhoai-rhtap/kubeflow/pull/81",
                pr_status="new",
                builds=(),
            )
        ],
    )
    path = write_state_file(tmp_path / "gap-deadbeef" / "state.json", state)
    loaded = load_state_file(path)
    assert loaded["pull-requests"][0]["pr-url"].endswith("/pull/81")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "pull-requests": [
            {
                "repo": "kubeflow",
                "pr-url": "https://github.com/rhoai-rhtap/kubeflow/pull/81",
                "pr-status": "new",
                "builds": [],
                "ignore": False,
            }
        ]
    }


def test_ignore_flag_round_trip_and_default() -> None:
    plain = validate_state(
        {
            "pull-requests": [
                {
                    "repo": "kserve",
                    "pr-url": "https://github.com/acme/kserve/pull/1",
                    "pr-status": "build-failure",
                }
            ]
        }
    )
    # Absent on input still normalizes to an explicit false for easy flipping.
    assert plain["pull-requests"][0]["ignore"] is False

    ignored = validate_state(
        {
            "pull-requests": [
                {
                    "repo": "kserve",
                    "pr-url": "https://github.com/acme/kserve/pull/1",
                    "pr-status": "build-failure",
                    "ignore": True,
                }
            ]
        }
    )
    assert ignored["pull-requests"][0]["ignore"] is True

    explicit_false = validate_state(
        {
            "pull-requests": [
                {
                    "repo": "kserve",
                    "pr-url": "https://github.com/acme/kserve/pull/1",
                    "pr-status": "success",
                    "ignore": False,
                }
            ]
        }
    )
    assert explicit_false["pull-requests"][0]["ignore"] is False

    with pytest.raises(StateFileError, match="ignore must be a boolean"):
        validate_state(
            {
                "pull-requests": [
                    {
                        "repo": "kserve",
                        "pr-url": "https://github.com/acme/kserve/pull/1",
                        "pr-status": "new",
                        "ignore": "yes",
                    }
                ]
            }
        )


def test_write_and_load_state_file_preserves_child_head_sha(tmp_path: Path) -> None:
    head_sha = "a" * 40
    state = build_state(
        pull_requests=[
            {
                "repo": "kubeflow",
                "pr-url": "https://github.com/rhoai-rhtap/kubeflow/pull/81",
                "head-sha": head_sha,
            }
        ]
    )
    path = write_state_file(tmp_path / "state.json", state)

    assert load_state_file(path)["pull-requests"][0]["head-sha"] == head_sha


def test_validate_state_rejects_legacy_prs_key() -> None:
    with pytest.raises(StateFileError, match="obsolete key 'prs'"):
        validate_state({"prs": []})


def test_validate_state_rejects_owner_repo_slug() -> None:
    with pytest.raises(StateFileError, match="short slug"):
        validate_state(
            {
                "pull-requests": [
                    {
                        "repo": "rhoai-rhtap/kserve",
                        "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/1",
                        "pr-status": "new",
                        "builds": [],
                    }
                ]
            }
        )


def test_validate_state_rejects_bad_status() -> None:
    with pytest.raises(StateFileError, match="pr-status"):
        validate_state(
            {
                "pull-requests": [
                    {
                        "repo": "kserve",
                        "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/1",
                        "pr-status": "nope",
                        "builds": [],
                    }
                ]
            }
        )


def test_validate_state_accepts_overall_status() -> None:
    payload = validate_state(
        {
            "overall-status": "success",
            "pull-requests": [
                {
                    "repo": "kserve",
                    "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/1",
                    "pr-status": "success",
                    "builds": [],
                }
            ],
        }
    )
    assert payload["overall-status"] == "success"
    assert "overall-status" in payload


def test_validate_state_accepts_early_gate_triggered() -> None:
    payload = validate_state(
        {
            "overall-status": "early-gate-triggered",
            "pull-requests": [
                {
                    "repo": "kserve",
                    "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/1",
                    "pr-status": "success",
                    "builds": [],
                }
            ],
        }
    )
    assert payload["overall-status"] == "early-gate-triggered"


def test_validate_state_rejects_bad_overall_status() -> None:
    with pytest.raises(StateFileError, match="overall-status"):
        validate_state(
            {
                "overall-status": "pending",
                "pull-requests": [],
            }
        )


def test_build_state_with_overall_status() -> None:
    state = build_state(
        pull_requests=[
            {
                "repo": "kserve",
                "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/2",
                "pr-status": "success",
            }
        ],
        overall_status="failure",
    )
    assert state.overall_status == "failure"
    assert state.to_dict()["overall-status"] == "failure"


def test_build_state_omits_overall_status_when_unset() -> None:
    state = build_state(
        pull_requests=[
            {
                "repo": "kserve",
                "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/2",
            }
        ]
    )
    assert state.overall_status is None
    assert list(state.to_dict().keys()) == ["pull-requests"]


def test_validate_state_accepts_overall_failure() -> None:
    payload = validate_state(
        {
            "overall-status": "failure",
            "pull-requests": [
                {
                    "repo": "kserve",
                    "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/1",
                    "pr-status": "merge-failure",
                    "builds": [],
                }
            ],
        }
    )
    assert payload["overall-status"] == "failure"


def test_write_and_load_state_file_preserves_overall_status(tmp_path: Path) -> None:
    state = build_state(
        pull_requests=[
            {
                "repo": "kubeflow",
                "pr-url": "https://github.com/rhoai-rhtap/kubeflow/pull/81",
                "pr-status": "success",
            }
        ],
        overall_status="success",
    )
    path = write_state_file(tmp_path / "state.json", state)
    loaded = load_state_file(path)
    assert loaded["overall-status"] == "success"
    assert loaded["pull-requests"][0]["pr-status"] == "success"


def test_short_repo_name() -> None:
    assert short_repo_name("https://github.com/org/repo.git") == "repo"
    assert short_repo_name("org/repo") == "repo"
    assert short_repo_name("repo") == "repo"


def test_state_path_for_trigger_is_dated() -> None:
    from datetime import datetime, timezone

    from lib.state_file import state_path_for_trigger

    when = datetime(2026, 9, 24, 14, 55, 32, tzinfo=timezone.utc)
    assert (
        state_path_for_trigger("gap-abc123", when=when)
        == "GAP Leaders/2026-09-24_gap-abc123/state.json"
    )


def test_find_existing_state_path(tmp_path: Path) -> None:
    from lib.state_file import find_existing_state_path

    folder = tmp_path / "GAP Leaders" / "2026-09-20_gap-abc123"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text("{}", encoding="utf-8")
    assert (
        find_existing_state_path(tmp_path, "gap-abc123")
        == "GAP Leaders/2026-09-20_gap-abc123/state.json"
    )
    assert find_existing_state_path(tmp_path, "gap-other") is None


def test_build_state_preserves_early_gate_trigger_metadata() -> None:
    batch = "a" * 64
    commit = "b" * 40
    pending = "c" * 64
    state = build_state(
        pull_requests=[
            {
                "repo": "kserve",
                "pr-url": "https://github.com/rhoai-rhtap/kserve/pull/2",
            }
        ],
        early_gate_triggered_batch=batch,
        early_gate_triggered_commit=commit,
        early_gate_trigger_pending_batch=pending,
    )
    payload = state.to_dict()
    assert payload["early-gate-triggered-batch"] == batch
    assert payload["early-gate-triggered-commit"] == commit
    assert payload["early-gate-trigger-pending-batch"] == pending
    normalized = validate_state(payload)
    assert normalized["early-gate-triggered-commit"] == commit
    assert normalized["early-gate-trigger-pending-batch"] == pending



def test_build_state_preserves_early_gate_retry_metadata() -> None:
    retry_batch = "b" * 64
    state = build_state(
        pull_requests=[],
        early_gate_retry_count=1,
        early_gate_retry_batch=retry_batch,
    )
    payload = state.to_dict()
    assert payload["early-gate-retry-count"] == 1
    assert payload["early-gate-retry-batch"] == retry_batch
    assert validate_state(payload)["early-gate-retry-batch"] == retry_batch


def test_validate_state_rejects_invalid_early_gate_retry_metadata() -> None:
    with pytest.raises(Exception, match="early-gate-retry-count"):
        validate_state({"pull-requests": [], "early-gate-retry-count": -1})
    with pytest.raises(Exception, match="early-gate-retry-batch"):
        validate_state({"pull-requests": [], "early-gate-retry-batch": "bad"})


def test_validate_state_rejects_invalid_early_gate_trigger_metadata() -> None:
    with pytest.raises(Exception, match="early-gate-triggered-commit"):
        validate_state({"pull-requests": [], "early-gate-triggered-commit": "bad"})
    with pytest.raises(Exception, match="early-gate-trigger-pending-batch"):
        validate_state({"pull-requests": [], "early-gate-trigger-pending-batch": "bad"})


def test_validate_state_rejects_invalid_early_gate_batch_identity() -> None:
    with pytest.raises(Exception, match="early-gate-triggered-batch"):
        validate_state(
            {
                "early-gate-triggered-batch": "not-a-sha256",
                "pull-requests": [],
            }
        )
