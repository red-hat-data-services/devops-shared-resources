"""Identity and validation helpers for GAP failure notifications."""

from __future__ import annotations

import copy
import json
from typing import Any

VALID_CATEGORIES = frozenset({"build", "conflict", "feasibility"})
VALID_STATUSES = frozenset({"failure", "success", "unknown"})


class IncidentError(ValueError):
    """Raised when an observation payload is invalid."""


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
