"""Unit tests for the pure GAP incident identity helpers."""

from __future__ import annotations

import copy
import json

import pytest

from lib.gap_notifications.dispatch_incidents import IncidentError, incident_key


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


def test_incident_key_is_stable_and_does_not_mutate_payload():
    payload = observation(
        repository="GitHub.com/AcMe/GaP",
        pipeline="build-a",
        evidence={"summary": "failure", "details": ["x", "y"]},
    )
    before = copy.deepcopy(payload)
    key = incident_key(payload)
    assert payload == before
    assert json.loads(key) == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "component": "api",
        "pipeline": "build-a",
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


def test_validation_accepts_build_observation():
    payload = observation()
    before = copy.deepcopy(payload)
    key = incident_key(payload)
    assert payload == before
    assert json.loads(key) == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "component": "api",
    }
    assert "pipeline" not in json.loads(key)


@pytest.mark.parametrize("evidence", [
    {"summary": {"nested": set()}},
    {"summary": float("nan")},
])
def test_validation_rejects_non_json_evidence(evidence):
    payload = observation(evidence=evidence)
    with pytest.raises(IncidentError, match="evidence"):
        incident_key(payload)
