"""Validated, credential-free creator observations retained in Leader state."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import os
import re
from typing import Any

OUTCOMES = frozenset({"success", "creation-failure", "pr-operation-failure",
                      "manual-merge", "automerge-unavailable"})
ERROR_OUTCOMES = frozenset({"creation-failure", "pr-operation-failure"})
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/[0-9]+")
RUN = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+")


def safe_reason(value: Any, token: str | None = None) -> str:
    text = str(value)
    if token:
        text = text.replace(token, "[redacted]")
    text = re.sub(r"https?://[^/\s]*@", "https://[redacted]@", text)
    text = re.sub(r"(?im)authorization\s*[:=][^\r\n]*", "Authorization: [redacted]", text)
    return " ".join(text.split())[:600]


def workflow_url() -> str:
    url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
           f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
           f"{os.environ.get('GITHUB_RUN_ID', '')}")
    return url if RUN.fullmatch(url) else ""


def validate_outcomes(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("creator-outcomes must be a list")
    result = []
    keys = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("creator-outcomes entries must be objects")
        for field in ("repo", "source-branch", "target-branch", "outcome", "reason", "observed-at"):
            if not isinstance(item.get(field), str) or not item[field]:
                raise ValueError(f"creator-outcomes.{field} is required")
        if not REPO.fullmatch(item["repo"]) or item["outcome"] not in OUTCOMES:
            raise ValueError("Invalid creator repository/outcome")
        datetime.fromisoformat(item["observed-at"].replace("Z", "+00:00"))
        for optional in ("pr-url", "workflow-url"):
            if optional in item and not isinstance(item[optional], str):
                raise ValueError(f"creator-outcomes.{optional} must be a string")
        url = item.get("pr-url", "")
        match = PR.fullmatch(url)
        if url and (not match or match[1].lower() != item["repo"].lower()):
            raise ValueError("Creator PR URL must belong to its repository")
        if item.get("workflow-url") and not RUN.fullmatch(item["workflow-url"]):
            raise ValueError("Invalid creator workflow URL")
        key = (item["repo"].lower(), item["source-branch"], item["target-branch"])
        if key in keys:
            raise ValueError("Duplicate creator repository/branch outcome")
        keys.add(key)
        result.append(copy.deepcopy(item))
    return result


def observation(repo: str, source: str, target: str, outcome: str, reason: str,
                *, pr_url: str = "", token: str | None = None) -> dict[str, str]:
    item = {"repo": repo, "source-branch": source, "target-branch": target,
            "outcome": outcome, "reason": safe_reason(reason, token),
            "pr-url": pr_url, "workflow-url": workflow_url(),
            "observed-at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return validate_outcomes([item])[0]


def merge_outcomes(previous, attempted):
    """A successful retry clears its old warning; untouched repos retain theirs."""
    merged = {}
    for item in validate_outcomes(previous) + validate_outcomes(attempted):
        merged[(item["repo"].lower(), item["source-branch"], item["target-branch"])] = item
    return list(merged.values())
