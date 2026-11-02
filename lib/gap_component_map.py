"""Repository-scoped allowlist for GAP's Konflux component build gate."""

from __future__ import annotations

import base64
import json
import re
from typing import Mapping

from lib.github_check_runs import CheckQueryError, GhRunner, github_api_json

COMPONENT_MAP_ENDPOINT = (
    "repos/red-hat-data-services/konflux-central/contents/"
    "config/component_repo_map.json?ref=main"
)


def fetch_component_repo_map(runner: GhRunner) -> dict[str, frozenset[str]]:
    """Fetch and validate one mapping snapshot; errors must never mean allow all."""
    payload = github_api_json(runner, COMPONENT_MAP_ENDPOINT)
    try:
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise ValueError("expected base64 GitHub contents response")
        data = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        if not isinstance(data, dict) or not data:
            raise ValueError("expected a nonempty repository mapping")
        result: dict[str, frozenset[str]] = {}
        for repo, components in data.items():
            if not isinstance(repo, str) or not repo.strip() or not isinstance(components, dict):
                raise ValueError("invalid repository entry")
            if any(
                not isinstance(name, str) or not name.strip()
                or not isinstance(image, str) or not image.strip()
                for name, image in components.items()
            ):
                raise ValueError(f"invalid component entry for {repo}")
            result[repo] = frozenset(components)
        return result
    except (KeyError, ValueError, TypeError, UnicodeError) as exc:
        raise CheckQueryError(f"invalid component repository mapping: {exc}") from exc


def normalize_component_name(component: str) -> str:
    """Strip a trailing numeric Konflux release suffix, leaving other names intact."""
    return re.sub(r"-v\d+(?:-\d+)+$", "", component)


def component_is_allowed(component: str, allowed: frozenset[str]) -> bool:
    """Normalize the component before checking its repository allowlist."""
    return normalize_component_name(component) in allowed


def repository_components(
    mapping: Mapping[str, frozenset[str]], repo: str,
) -> frozenset[str]:
    """Never borrow components from another repository or guess repo aliases."""
    return mapping.get(repo, frozenset())
