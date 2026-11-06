"""Pure incident ledger for GAP failure notifications."""

from __future__ import annotations

import copy
import json
from typing import Any

INCIDENTS_KEY = "incidents"
VALID_CATEGORIES = frozenset({"build", "conflict", "feasibility"})
VALID_STATUSES = frozenset({"failure", "success", "unknown"})
ACTIVE_STATE = "active"
RECOVERED_STATE = "recovered"


class IncidentError(ValueError):
    """Raised when an observation or ledger payload is invalid."""


def _mapping(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IncidentError(f"{path} must be a mapping")
    return value


def _jsonable(value: Any, *, path: str) -> Any:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IncidentError(f"{path} must be JSON-compatible") from exc
    return value


def _text(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise IncidentError(f"{path} must be a string")
    text = value.strip()
    if not text:
        raise IncidentError(f"{path} is required")
    return text


def _repository(value: Any) -> str:
    repo = _text(value, path="repository")
    parts = [part.strip().lower() for part in repo.split("/")]
    if len(parts) == 2:
        owner, name = parts
        if not owner or not name:
            raise IncidentError("repository must be owner/repo or host/owner/repo")
        return f"github.com/{owner}/{name}"
    if len(parts) == 3:
        host, owner, name = parts
        if not host or not owner or not name:
            raise IncidentError("repository must be owner/repo or host/owner/repo")
        return f"{host}/{owner}/{name}"
    raise IncidentError("repository must be owner/repo or host/owner/repo")


def _normalize_observation(observation: dict[str, Any]) -> dict[str, Any]:
    data = _mapping(observation, path="observation")
    environment = _text(data.get("environment"), path="environment")
    repository = _repository(data.get("repository"))
    source_branch = _text(data.get("source_branch"), path="source_branch")
    destination_branch = _text(data.get("destination_branch"), path="destination_branch")
    category = _text(data.get("category"), path="category")
    if category not in VALID_CATEGORIES:
        raise IncidentError(f"category must be one of {sorted(VALID_CATEGORIES)}")
    status = _text(data.get("status"), path="status")
    if status not in VALID_STATUSES:
        raise IncidentError(f"status must be one of {sorted(VALID_STATUSES)}")
    observation_id = _text(data.get("observation_id"), path="observation_id")
    evidence = _jsonable(_mapping(data.get("evidence"), path="evidence"), path="evidence")

    normalized: dict[str, Any] = {
        "environment": environment,
        "repository": repository,
        "source_branch": source_branch,
        "destination_branch": destination_branch,
        "category": category,
        "status": status,
        "observation_id": observation_id,
        "evidence": copy.deepcopy(evidence),
    }
    if category == "build":
        component = _text(data.get("component"), path="component")
        normalized["component"] = component
        pipeline = data.get("pipeline")
        if pipeline is not None:
            normalized["pipeline"] = _text(pipeline, path="pipeline")
    return normalized


def _incident_identity(observation: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "environment": observation["environment"],
        "repository": observation["repository"],
        "source_branch": observation["source_branch"],
        "destination_branch": observation["destination_branch"],
        "category": observation["category"],
    }
    if observation["category"] == "build":
        identity["component"] = observation["component"]
        if "pipeline" in observation:
            identity["pipeline"] = observation["pipeline"]
    return identity


def incident_key(observation: dict) -> str:
    """Return a deterministic, human-inspectable incident identity."""
    normalized = _normalize_observation(observation)
    return json.dumps(_incident_identity(normalized), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _incident_state(record: dict[str, Any]) -> str:
    state = record.get("state")
    if isinstance(state, str) and state in {ACTIVE_STATE, RECOVERED_STATE}:
        return state
    if isinstance(record.get("active_episode"), int):
        return ACTIVE_STATE
    if isinstance(record.get("recovered_episode"), int):
        return RECOVERED_STATE
    return ""


def _current_episode(record: dict[str, Any]) -> int:
    for field in ("episode", "active_episode", "recovered_episode"):
        value = record.get(field)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def _build_metadata(observation: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if observation["category"] == "build":
        metadata["component"] = observation["component"]
        if "pipeline" in observation:
            metadata["pipeline"] = observation["pipeline"]
    return metadata


def _seen_observation_ids(record: dict[str, Any]) -> list[str]:
    seen = record.get("seen_observation_ids")
    if seen is None:
        return []
    if not isinstance(seen, list) or not all(isinstance(item, str) and item for item in seen):
        raise IncidentError("ledger incident records must keep seen_observation_ids as a list of strings")
    return seen


def _record_with_seen(observation: dict[str, Any], key: str, *, seen_ids: list[str]) -> dict[str, Any]:
    return {
        "incident_key": key,
        "environment": observation["environment"],
        "repository": observation["repository"],
        "source_branch": observation["source_branch"],
        "destination_branch": observation["destination_branch"],
        "category": observation["category"],
        "status": observation["status"],
        **_build_metadata(observation),
        "seen_observation_ids": seen_ids,
        "episode": 0,
        "state": "",
        "active_episode": None,
        "recovered_episode": None,
        "latest_observation": copy.deepcopy(observation),
    }


def _base_record(observation: dict[str, Any], key: str) -> dict[str, Any]:
    return _record_with_seen(observation, key, seen_ids=[observation["observation_id"]])


def _transition(record: dict[str, Any], observation: dict[str, Any], *, kind: str, episode: int, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(record)
    updated.update({
        "incident_key": key,
        "environment": observation["environment"],
        "repository": observation["repository"],
        "source_branch": observation["source_branch"],
        "destination_branch": observation["destination_branch"],
        "category": observation["category"],
        "status": observation["status"],
        **_build_metadata(observation),
        "episode": episode,
        "latest_observation": copy.deepcopy(observation),
    })
    return updated, {
        "kind": kind,
        "incident_key": key,
        "episode": episode,
        "observation": copy.deepcopy(observation),
    }


def apply_observation(ledger: dict, observation: dict) -> tuple[dict, list[dict]]:
    """Apply one observation to an incident ledger without mutating inputs."""
    source_ledger = _mapping(ledger, path="ledger")
    normalized = _normalize_observation(observation)
    key = incident_key(normalized)
    result = copy.deepcopy(source_ledger)

    incidents = result.get(INCIDENTS_KEY)
    if incidents is not None and not isinstance(incidents, dict):
        raise IncidentError(f"{INCIDENTS_KEY} must be a mapping when present")

    current = incidents.get(key) if isinstance(incidents, dict) else None
    if current is None:
        record = _base_record(normalized, key)
        if not isinstance(incidents, dict):
            incidents = {}
            result[INCIDENTS_KEY] = incidents
        incidents[key] = record
        if normalized["status"] != "failure":
            return result, []
        record["episode"] = 1
        record["state"] = ACTIVE_STATE
        record["active_episode"] = 1
        return result, [{
            "kind": "opened",
            "incident_key": key,
            "episode": 1,
            "observation": copy.deepcopy(normalized),
        }]

    if not isinstance(current, dict):
        raise IncidentError("ledger incident records must be mappings")

    seen = _seen_observation_ids(current)
    if normalized["observation_id"] in seen:
        return result, []

    state = _incident_state(current)
    episode = _current_episode(current)
    seen_ids = [*seen, normalized["observation_id"]]
    if normalized["status"] == "failure":
        if state == RECOVERED_STATE:
            episode = episode + 1 if episode > 0 else 1
            kind = "opened"
        else:
            episode = episode if episode > 0 else 1
            kind = "opened" if state == "" else "updated"
        updated, transition = _transition(current, normalized, kind=kind, episode=episode, key=key)
        updated["state"] = ACTIVE_STATE
        updated["active_episode"] = episode
        updated["recovered_episode"] = current.get("recovered_episode")
    elif normalized["status"] == "success":
        if state != ACTIVE_STATE:
            current["seen_observation_ids"] = seen_ids
            incidents[key] = current
            return result, []
        episode = episode if episode > 0 else 1
        updated, transition = _transition(current, normalized, kind="recovered", episode=episode, key=key)
        updated["state"] = RECOVERED_STATE
        updated["active_episode"] = None
        updated["recovered_episode"] = episode
    else:
        current["seen_observation_ids"] = seen_ids
        incidents[key] = current
        return result, []
    updated["seen_observation_ids"] = seen_ids

    incidents[key] = updated
    return result, [transition]
