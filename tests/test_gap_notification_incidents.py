"""Unit tests for the pure GAP incident ledger."""

from __future__ import annotations

import copy
import json

import pytest

from lib.gap_notification_incidents import (
    INCIDENTS_KEY,
    IncidentError,
    apply_observation,
    incident_key,
)


def observation(*, environment="stage-redhat", repository="acme/gap", source_branch="feature",
                destination_branch="main", category="build", status="failure",
                observation_id="obs-1", evidence=None, component="api", pipeline=None):
    data = {
        "environment": environment,
        "repository": repository,
        "source_branch": source_branch,
        "destination_branch": destination_branch,
        "category": category,
        "status": status,
        "observation_id": observation_id,
        "evidence": evidence or {"summary": "failure"},
    }
    if component is not None:
        data["component"] = component
    if pipeline is not None:
        data["pipeline"] = pipeline
    return data


def test_incident_key_is_canonical_and_environment_scoped():
    base = observation(repository="acme/gap", pipeline="build-a")
    same_repo = observation(repository="github.com/acme/gap", pipeline="build-a")
    mixed_case = observation(repository="GitHub.com/AcMe/GaP", pipeline="build-a")
    other_env = observation(environment="stage-green", repository="github.com/acme/gap", pipeline="build-a")
    assert incident_key(base) == incident_key(same_repo) == incident_key(mixed_case)
    assert incident_key(base) != incident_key(other_env)


def test_branch_stream_separation_retains_destination_branch_for_all_categories():
    build = observation(destination_branch="main", pipeline="build-a")
    build_other_branch = observation(destination_branch="release-1", pipeline="build-a")
    conflict = observation(category="conflict", component=None, destination_branch="main")
    conflict_other_branch = observation(category="conflict", component=None, destination_branch="release-1")
    feasibility = observation(category="feasibility", component=None, destination_branch="rhoai-1")
    feasibility_other_branch = observation(category="feasibility", component=None, destination_branch="rhoai-2")

    assert incident_key(build) != incident_key(build_other_branch)
    assert incident_key(conflict) != incident_key(conflict_other_branch)
    assert incident_key(feasibility) != incident_key(feasibility_other_branch)


def test_apply_observation_tracks_open_update_recovery_and_reopen():
    ledger = {}
    failure1 = observation(observation_id="obs-1", evidence={"summary": "first failure"})
    ledger, transitions = apply_observation(ledger, failure1)
    key = incident_key(failure1)
    assert transitions == [{
        "kind": "opened",
        "incident_key": key,
        "episode": 1,
        "observation": {
            "environment": "stage-redhat",
            "repository": "github.com/acme/gap",
            "source_branch": "feature",
            "destination_branch": "main",
            "category": "build",
            "status": "failure",
            "observation_id": "obs-1",
            "evidence": {"summary": "first failure"},
            "component": "api",
        },
    }]
    record = ledger[INCIDENTS_KEY][key]
    assert record["state"] == "active"
    assert record["episode"] == 1
    assert record["active_episode"] == 1
    assert record["recovered_episode"] is None
    assert record["latest_observation"] == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "status": "failure",
        "observation_id": "obs-1",
        "evidence": {"summary": "first failure"},
        "component": "api",
    }

    before_duplicate = copy.deepcopy(ledger)
    duplicate = observation(observation_id="obs-1", evidence={"summary": "ignored duplicate"})
    ledger, transitions = apply_observation(ledger, duplicate)
    assert transitions == []
    assert ledger == before_duplicate

    failure2 = observation(observation_id="obs-2", evidence={"summary": "second failure"})
    ledger, transitions = apply_observation(ledger, failure2)
    assert transitions == [{
        "kind": "updated",
        "incident_key": key,
        "episode": 1,
        "observation": {
            "environment": "stage-redhat",
            "repository": "github.com/acme/gap",
            "source_branch": "feature",
            "destination_branch": "main",
            "category": "build",
            "status": "failure",
            "observation_id": "obs-2",
            "evidence": {"summary": "second failure"},
            "component": "api",
        },
    }]
    record = ledger[INCIDENTS_KEY][key]
    assert record["episode"] == 1
    assert record["state"] == "active"
    assert record["latest_observation"] == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "status": "failure",
        "observation_id": "obs-2",
        "evidence": {"summary": "second failure"},
        "component": "api",
    }

    success = observation(status="success", observation_id="obs-3", evidence={"summary": "recovered"})
    ledger, transitions = apply_observation(ledger, success)
    assert transitions == [{
        "kind": "recovered",
        "incident_key": key,
        "episode": 1,
        "observation": {
            "environment": "stage-redhat",
            "repository": "github.com/acme/gap",
            "source_branch": "feature",
            "destination_branch": "main",
            "category": "build",
            "status": "success",
            "observation_id": "obs-3",
            "evidence": {"summary": "recovered"},
            "component": "api",
        },
    }]
    record = ledger[INCIDENTS_KEY][key]
    assert record["state"] == "recovered"
    assert record["episode"] == 1
    assert record["active_episode"] is None
    assert record["recovered_episode"] == 1
    assert record["latest_observation"] == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "status": "success",
        "observation_id": "obs-3",
        "evidence": {"summary": "recovered"},
        "component": "api",
    }

    failure3 = observation(observation_id="obs-4", evidence={"summary": "failure after recovery"})
    ledger, transitions = apply_observation(ledger, failure3)
    assert transitions == [{
        "kind": "opened",
        "incident_key": key,
        "episode": 2,
        "observation": {
            "environment": "stage-redhat",
            "repository": "github.com/acme/gap",
            "source_branch": "feature",
            "destination_branch": "main",
            "category": "build",
            "status": "failure",
            "observation_id": "obs-4",
            "evidence": {"summary": "failure after recovery"},
            "component": "api",
        },
    }]
    record = ledger[INCIDENTS_KEY][key]
    assert record["state"] == "active"
    assert record["episode"] == 2
    assert record["active_episode"] == 2
    assert record["recovered_episode"] == 1
    assert record["latest_observation"] == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "status": "failure",
        "observation_id": "obs-4",
        "evidence": {"summary": "failure after recovery"},
        "component": "api",
    }


def test_success_before_any_failure_and_unknown_record_seen_ids_without_transition():
    success = observation(status="success", observation_id="obs-1")
    ledger, transitions = apply_observation({}, success)
    assert transitions == []
    key = incident_key(success)
    assert ledger[INCIDENTS_KEY][key]["episode"] == 0
    assert ledger[INCIDENTS_KEY][key]["state"] == ""
    assert ledger[INCIDENTS_KEY][key]["seen_observation_ids"] == ["obs-1"]

    before_unknown = copy.deepcopy(ledger)
    unknown = observation(status="unknown", observation_id="obs-3", evidence={"summary": "unclear"})
    ledger, transitions = apply_observation(ledger, unknown)
    assert transitions == []
    assert ledger[INCIDENTS_KEY][key]["seen_observation_ids"] == ["obs-1", "obs-3"]
    assert ledger[INCIDENTS_KEY][key]["episode"] == 0
    assert before_unknown != ledger


def test_apply_observation_preserves_unrelated_ledger_and_record_metadata():
    first = observation(observation_id="obs-1", evidence={"summary": "first failure"})
    key = incident_key(first)
    ledger = {
        "pipeline": "shared",
        INCIDENTS_KEY: {
            key: {
                "incident_key": key,
                "environment": first["environment"],
                "repository": first["repository"],
                "source_branch": first["source_branch"],
                "destination_branch": first["destination_branch"],
                "category": first["category"],
                "status": "failure",
                "episode": 1,
                "state": "active",
                "active_episode": 1,
                "recovered_episode": None,
                "latest_observation": copy.deepcopy(first),
                "ticket": {"jira": "RHAIENG-1"},
                "delivery": {"slack": {"ts": "123.456"}},
            }
        },
    }
    ledger_before = copy.deepcopy(ledger)
    update = observation(observation_id="obs-2", evidence={"summary": "new evidence"})
    update_before = copy.deepcopy(update)
    updated, transitions = apply_observation(ledger, update)

    assert ledger == ledger_before
    assert update == update_before
    assert transitions == [{
        "kind": "updated",
        "incident_key": key,
        "episode": 1,
        "observation": {
            "environment": "stage-redhat",
            "repository": "github.com/acme/gap",
            "source_branch": "feature",
            "destination_branch": "main",
            "category": "build",
            "status": "failure",
            "observation_id": "obs-2",
            "evidence": {"summary": "new evidence"},
            "component": "api",
        },
    }]
    assert updated["pipeline"] == "shared"
    record = updated[INCIDENTS_KEY][key]
    assert record["ticket"] == {"jira": "RHAIENG-1"}
    assert record["delivery"] == {"slack": {"ts": "123.456"}}
    assert record["latest_observation"] == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "status": "failure",
        "observation_id": "obs-2",
        "evidence": {"summary": "new evidence"},
        "component": "api",
    }


@pytest.mark.parametrize("payload,match", [
    ({"repository": "acme/gap", "source_branch": "feature", "destination_branch": "main",
      "category": "build", "status": "failure", "observation_id": "obs-1", "evidence": {}},
     "environment"),
    ({"environment": "stage-redhat", "source_branch": "feature", "destination_branch": "main",
      "category": "build", "status": "failure", "observation_id": "obs-1", "evidence": {}},
     "repository"),
    ({"environment": "stage-redhat", "repository": "acme/gap", "destination_branch": "main",
      "category": "build", "status": "failure", "observation_id": "obs-1", "evidence": {}},
     "source_branch"),
    ({"environment": "stage-redhat", "repository": "acme/gap", "source_branch": "feature",
      "category": "build", "status": "failure", "observation_id": "obs-1", "evidence": {}},
     "destination_branch"),
    ({"environment": "stage-redhat", "repository": "acme/gap", "source_branch": "feature",
      "destination_branch": "main", "category": "build", "status": "failure",
      "observation_id": "obs-1", "evidence": {}},
     "component"),
    ({"environment": "stage-redhat", "repository": "acme/gap", "source_branch": "feature",
      "destination_branch": "main", "category": "build", "status": "bad",
      "observation_id": "obs-1", "evidence": {}, "component": "api"},
     "status"),
])
def test_validation_rejects_missing_or_invalid_observation_fields(payload, match):
    with pytest.raises(IncidentError, match=match):
        incident_key(payload)
    with pytest.raises(IncidentError, match=match):
        apply_observation({}, payload)


def test_validation_accepts_build_observation():
    payload = observation()
    assert incident_key(payload)
    ledger, transitions = apply_observation({}, payload)
    key = incident_key(payload)
    assert ledger[INCIDENTS_KEY][key]["episode"] == 1
    assert ledger[INCIDENTS_KEY][key]["component"] == "api"
    assert "pipeline" not in ledger[INCIDENTS_KEY][key]
    assert ledger[INCIDENTS_KEY][key]["seen_observation_ids"] == ["obs-1"]
    assert transitions[0]["kind"] == "opened"


def test_replayed_old_failure_after_recovery_does_not_open_new_episode_and_survives_reload():
    first = observation(observation_id="obs-a", evidence={"summary": "failure A"})
    second = observation(status="success", observation_id="obs-b", evidence={"summary": "success B"})
    third = observation(observation_id="obs-c", evidence={"summary": "failure C"})
    ledger, transitions = apply_observation({}, first)
    assert transitions[0]["kind"] == "opened"
    ledger, transitions = apply_observation(ledger, second)
    assert transitions[0]["kind"] == "recovered"
    frozen = json.loads(json.dumps(ledger))
    replay, transitions = apply_observation(frozen, first)
    assert transitions == []
    assert replay == frozen
    reopened, transitions = apply_observation(frozen, third)
    assert transitions[0]["kind"] == "opened"
    assert reopened[INCIDENTS_KEY][incident_key(first)]["episode"] == 2
    assert reopened[INCIDENTS_KEY][incident_key(first)]["seen_observation_ids"] == ["obs-a", "obs-b", "obs-c"]


def test_seen_only_successes_are_persisted_before_failure_and_after_recovery():
    initial_success = observation(status="success", observation_id="obs-s1", evidence={"summary": "success S"})
    failure = observation(observation_id="obs-f1", evidence={"summary": "failure F"})
    recovery = observation(status="success", observation_id="obs-s2", evidence={"summary": "success S2"})
    later_failure = observation(observation_id="obs-f2", evidence={"summary": "failure F2"})

    ledger, transitions = apply_observation({}, initial_success)
    assert transitions == []
    key = incident_key(initial_success)
    assert ledger[INCIDENTS_KEY][key]["episode"] == 0
    assert ledger[INCIDENTS_KEY][key]["seen_observation_ids"] == ["obs-s1"]

    ledger, transitions = apply_observation(ledger, failure)
    assert transitions[0]["kind"] == "opened"
    assert ledger[INCIDENTS_KEY][key]["episode"] == 1

    replay, transitions = apply_observation(ledger, initial_success)
    assert transitions == []
    assert replay[INCIDENTS_KEY][key]["seen_observation_ids"] == ["obs-s1", "obs-f1"]

    ledger, transitions = apply_observation(ledger, recovery)
    assert transitions[0]["kind"] == "recovered"
    assert ledger[INCIDENTS_KEY][key]["state"] == "recovered"

    replay, transitions = apply_observation(ledger, recovery)
    assert transitions == []
    assert replay[INCIDENTS_KEY][key]["seen_observation_ids"] == ["obs-s1", "obs-f1", "obs-s2"]

    ledger, transitions = apply_observation(ledger, later_failure)
    assert transitions[0]["kind"] == "opened"
    assert ledger[INCIDENTS_KEY][key]["episode"] == 2

    replay, transitions = apply_observation(ledger, recovery)
    assert transitions == []
    assert replay[INCIDENTS_KEY][key]["seen_observation_ids"] == ["obs-s1", "obs-f1", "obs-s2", "obs-f2"]


def test_validation_rejects_non_mapping_ledger_and_incidents():
    payload = observation(component=None, category="conflict")
    with pytest.raises(IncidentError, match="ledger"):
        apply_observation([], payload)
    with pytest.raises(IncidentError, match="incidents"):
        apply_observation({INCIDENTS_KEY: []}, payload)


@pytest.mark.parametrize("evidence", [
    {"summary": {"nested": set()}},
    {"summary": float("nan")},
])
def test_validation_rejects_non_json_evidence(evidence):
    payload = observation(evidence=evidence)
    with pytest.raises(IncidentError, match="evidence"):
        incident_key(payload)
    with pytest.raises(IncidentError, match="evidence"):
        apply_observation({}, payload)
