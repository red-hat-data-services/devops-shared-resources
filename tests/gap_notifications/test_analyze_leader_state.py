"""Tests for analyze_leader_state responsibilities."""
from __future__ import annotations

from gap_notification_fakes import state, run, LEADER_URL
import copy
import json
from pathlib import Path
import pytest
from lib.gap_notifications.analyze_leader_state import (
    ObservationCollectionError,
    collect_build_observations,
)


# Observations

ENVIRONMENT = "stage-redhat.atlassian.net/RHAI/C0883L8FARE"
STATE_URL = "file:///tmp/leader-84/state.json"


# Normalize current component receipts deterministically with stream identity and provenance
# intact.
def test_collect_build_observations_returns_normalized_current_receipts():
    payload = state(
        expected_components=["api", "web"],
        runs=[run("api", "failure", reason="Failed"), run("web", "success")],
        gate_reason="real build failure",
    )
    observations = collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)
    assert len(observations) == 2
    by_component = {item["component"]: item for item in observations}
    assert set(by_component) == {"api", "web"}
    assert by_component["api"]["status"] == "failure"
    assert by_component["web"]["status"] == "success"
    for item in observations:
        assert item["environment"] == ENVIRONMENT
        assert item["repository"] == "acme/demo"
        assert item["source_branch"] == "feature/x"
        assert item["destination_branch"] == "main"
        assert item["category"] == "build"
        assert item["evidence"]["provenance"]["kind"] == "historical-snapshot"
        assert item["evidence"]["leader_url"] == LEADER_URL
        assert item["evidence"]["state_url"] == STATE_URL
        assert item["evidence"]["pr_url"] == "https://github.com/acme/demo/pull/42"
        assert item["evidence"]["build_url"] == item["evidence"]["selected-run"]["url"]
        assert item["evidence"]["source-branch"] == "feature/x"
        assert item["evidence"]["target-branch"] == "main"
        assert item["evidence"]["test_only"] is True
        assert item["evidence"]["historical_snapshot"] is True
        assert item["evidence"]["note"]
        assert item["evidence"]["selected-component"] == item["component"]
        assert item["observation_id"]
    assert observations == collect_build_observations(copy.deepcopy(payload), environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)


# Missing or ambiguous creator streams must not be guessed during collection.
@pytest.mark.parametrize("payload,match", [
    ({"pull-requests": [{"repo": "demo", "pr-url": "https://github.com/acme/demo/pull/42", "builds": [],
                          "build-observations": {"source": "konflux", "repository": "acme/demo", "pr": 42,
                                                  "head-sha": "a" * 40, "observed-at": "2026-11-06T15:12:46.242781+00:00",
                                                  "expected-components": ["api"], "runs": [], "pr-state": "OPEN"}}],
      "creator-outcomes": []}, "missing creator stream"),
    ({"pull-requests": [{"repo": "demo", "pr-url": "https://github.com/acme/demo/pull/42", "builds": [],
                          "build-observations": {"source": "konflux", "repository": "acme/demo", "pr": 42,
                                                  "head-sha": "a" * 40, "observed-at": "2026-11-06T15:12:46.242781+00:00",
                                                  "expected-components": ["api"], "runs": [], "pr-state": "OPEN"}}],
      "creator-outcomes": [
          {"repo": "acme/demo", "source-branch": "feature/x", "target-branch": "main", "outcome": "success",
           "reason": "one", "pr-url": "https://github.com/acme/demo/pull/41", "workflow-url": "https://github.com/acme/workflows/actions/runs/1", "observed-at": "2026-11-06T15:12:46.242781+00:00"},
          {"repo": "acme/demo", "source-branch": "feature/y", "target-branch": "main", "outcome": "success",
           "reason": "two", "pr-url": "https://github.com/acme/demo/pull/43", "workflow-url": "https://github.com/acme/workflows/actions/runs/2", "observed-at": "2026-11-06T15:12:46.242781+00:00"},
      ]}, "missing exact creator stream"),
])
def test_collect_build_observations_refuses_missing_or_ambiguous_creator_stream(payload, match):
    with pytest.raises(ObservationCollectionError, match=match):
        collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)


# Component build receipts, not aggregate PR status, determine build outcomes.
def test_collect_build_observations_maps_mixed_statuses_and_ignores_pr_status_for_build_receipts():
    payload = state(
        pr_status="merge-failure",
        gate_reason="placeholder feasibility conflict reason",
        expected_components=["api", "web", "worker"],
        runs=[
            run("api", "failure", reason="Failed"),
            run("web", "timed_out", reason="PipelineRunTimeout"),
            run("worker", "pending", reason="Running"),
        ],
    )
    observations = collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)
    by_component = {item["component"]: item for item in observations}
    assert by_component["api"]["status"] == "failure"
    assert by_component["web"]["status"] == "failure"
    assert by_component["worker"]["status"] == "unknown"
    assert all(item["category"] == "build" for item in observations)
    assert all(item["evidence"]["pr-status"] == "merge-failure" for item in observations)
    assert all("conflict" not in item["evidence"]["reason"].lower() for item in observations)
    assert all(item["evidence"]["build_url"] for item in observations)


# Without explicit expected components, unrelated recorded runs must not become observations.
def test_collect_build_observations_with_empty_expected_components_skips_runs():
    payload = state(expected_components=[], runs=[run("api", "success")])
    assert collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL) == []


# Multiple current receipts for one component must fail rather than choose an arbitrary run.
def test_collect_build_observations_rejects_duplicate_component_runs():
    payload = state(expected_components=["api"], runs=[run("api", "success", uid="u1"), run("api", "failure", uid="u2")])
    with pytest.raises(ObservationCollectionError, match="ambiguous current receipt"):
        collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)


# A head mismatch must become unknown, not a fabricated build failure or merge conflict.
def test_collect_build_observations_rejects_sha_mismatch_as_unknown_without_conflict_parsing():
    payload = state(
        expected_components=["api"],
        runs=[run("api", "success", head_sha="b" * 40, reason="Completed")],
        gate_reason="already merged or has a merge conflict",
    )
    observations = collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)
    assert len(observations) == 1
    observation = observations[0]
    assert observation["status"] == "unknown"
    assert observation["evidence"]["selected-run"]["head_sha"] == "b" * 40
    assert "conflict" not in observation["evidence"]["reason"].lower()


# A receipt for another PR must become unknown rather than authorize an outcome.
def test_collect_build_observations_rejects_pr_number_mismatch_as_unknown():
    payload = state(expected_components=["api"], runs=[run("api", "success")])
    payload["pull-requests"][0]["build-observations"]["pr"] = 99
    observations = collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)
    assert len(observations) == 1
    assert observations[0]["status"] == "unknown"
    assert "PR number" in observations[0]["evidence"]["reason"]


# Missing run identity must prevent a receipt from becoming a definitive outcome.
def test_collect_build_observations_rejects_missing_uid_as_unknown():
    payload = state(expected_components=["api"], runs=[run("api", "success")])
    payload["pull-requests"][0]["build-observations"]["runs"][0]["uid"] = ""
    observations = collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)
    assert len(observations) == 1
    assert observations[0]["status"] == "unknown"
    assert "UID" in observations[0]["evidence"]["reason"]


# Cosmetic snapshot changes must not create a new observation event.
def test_collect_build_observations_observation_id_is_stable_across_snapshot_noise():
    payload = state(expected_components=["api"], runs=[run("api", "success", uid="same-uid")])
    first = collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)[0]
    noisy = copy.deepcopy(payload)
    noisy["pull-requests"][0]["build-observations"]["observed-at"] = "2026-11-06T16:00:00+00:00"
    noisy["pull-requests"][0]["build-observations"]["gate-reason"] = "changed prose"
    noisy["pull-requests"][0]["build-observations"]["runs"][0]["completed_at"] = "2026-11-06T15:00:00Z"
    second = collect_build_observations(noisy, environment=ENVIRONMENT, leader_url=LEADER_URL + "?x=1", state_url=STATE_URL + "?x=1")[0]
    assert first["observation_id"] == second["observation_id"]
    assert "completed_at" not in first["evidence"]["selected-run"]
    assert second["evidence"]["selected-run"]["completed_at"] == "2026-11-06T15:00:00Z"


# A changed outcome on the same run must produce a distinct event identifier.
def test_collect_build_observations_observation_id_changes_when_status_changes_on_same_uid():
    success = state(expected_components=["api"], runs=[run("api", "pending", uid="same-uid")])
    failure = state(expected_components=["api"], runs=[run("api", "failure", uid="same-uid")])
    first = collect_build_observations(success, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)[0]
    second = collect_build_observations(failure, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=STATE_URL)[0]
    assert first["observation_id"] != second["observation_id"]
    assert first["status"] == "unknown"
    assert second["status"] == "failure"


# The checked-in snapshot must normalize to the expected components and retain historical
# provenance.
def test_collect_build_observations_fixture_smoke():
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "gap_notifications" / "leader-build-snapshot.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    observations = collect_build_observations(payload, environment=ENVIRONMENT, leader_url=LEADER_URL, state_url=str(fixture))
    by_component = {item["component"]: item for item in observations}
    assert set(by_component) == {"odh-model-controller-v3-7-ea-1", "odh-model-serving-api-v3-7-ea-1"}
    assert by_component["odh-model-controller-v3-7-ea-1"]["status"] == "success"
    assert by_component["odh-model-serving-api-v3-7-ea-1"]["status"] == "failure"
    assert all(item["environment"] == ENVIRONMENT for item in observations)
    assert all(item["category"] == "build" for item in observations)
    sample = observations[0]
    assert sample["evidence"]["historical_snapshot"] is True
    assert sample["evidence"]["test_only"] is True
    assert sample["evidence"]["provenance"]["kind"] == "historical-snapshot"
    assert sample["observation_id"]
