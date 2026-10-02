"""Build and validate Gated Artifacts Promoter state.json files (RHOAIENG-93564)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_LEADER_REPO = "red-hat-data-services/gated-artifacts-promoter"

# Stage 1 initial status written by the PR sync workflow (RHOAIENG-93524).
PR_STATUS_NEW = "new"

PR_STATUSES = frozenset(
    {
        "new",
        "merge-failure",
        "build-pending",
        "build-success",
        "build-failure",
        "test-pending",
        "test-failure",
        "success",
    }
)

# Leader-run conclusion (RHOAIENG-97052). Absent while the run is still in progress.
OVERALL_STATUS_SUCCESS = "success"
OVERALL_STATUS_FAILURE = "failure"
OVERALL_STATUSES = frozenset({OVERALL_STATUS_SUCCESS, OVERALL_STATUS_FAILURE})


class StateFileError(ValueError):
    """Raised when a state file payload is invalid."""


@dataclass(frozen=True)
class StateBuild:
    component: str
    image: str

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, path: str) -> "StateBuild":
        if not isinstance(data, dict):
            raise StateFileError(f"{path} must be a mapping")
        component = str(data.get("component") or "").strip()
        image = str(data.get("image") or "").strip()
        if not component:
            raise StateFileError(f"{path}.component is required")
        if not image:
            raise StateFileError(f"{path}.image is required")
        return cls(component=component, image=image)

    def to_dict(self) -> dict[str, str]:
        return {"component": self.component, "image": self.image}


@dataclass(frozen=True)
class StatePullRequest:
    """One child sync PR entry in state.json (RHOAIENG-93564)."""

    repo: str  # short slug only, e.g. "odh-dashboard"
    pr_url: str
    pr_status: str = PR_STATUS_NEW
    builds: tuple[StateBuild, ...] = ()

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, index: int) -> "StatePullRequest":
        path = f"pull-requests[{index}]"
        if not isinstance(data, dict):
            raise StateFileError(f"{path} must be a mapping")

        repo = str(data.get("repo") or "").strip()
        pr_url = str(data.get("pr-url") or data.get("pr_url") or "").strip()
        pr_status = str(data.get("pr-status") or data.get("pr_status") or "").strip()
        raw_builds = data.get("builds")

        if not repo:
            raise StateFileError(f"{path}.repo is required")
        if "/" in repo:
            raise StateFileError(
                f"{path}.repo must be a short slug (e.g. 'odh-dashboard'), not '{repo}'"
            )
        if not pr_url or not _looks_like_pr_url(pr_url):
            raise StateFileError(
                f"{path}.pr-url must be https://github.com/<owner>/<repo>/pull/<n>"
            )
        if pr_status not in PR_STATUSES:
            raise StateFileError(
                f"{path}.pr-status must be one of {sorted(PR_STATUSES)}, got {pr_status!r}"
            )
        if raw_builds is None:
            builds: tuple[StateBuild, ...] = ()
        elif not isinstance(raw_builds, list):
            raise StateFileError(f"{path}.builds must be a list")
        else:
            builds = tuple(
                StateBuild.from_mapping(item, path=f"{path}.builds[{i}]")
                if isinstance(item, dict)
                else (_raise_build_type(f"{path}.builds[{i}]"))
                for i, item in enumerate(raw_builds)
            )

        return cls(repo=repo, pr_url=pr_url, pr_status=pr_status, builds=builds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "pr-url": self.pr_url,
            "pr-status": self.pr_status,
            "builds": [build.to_dict() for build in self.builds],
        }


@dataclass
class PromoterState:
    """Leader PR state.json root object (RHOAIENG-93564 / RHOAIENG-97052)."""

    pull_requests: list[StatePullRequest] = field(default_factory=list)
    # Set when the GAP run has concluded; omitted from JSON while unset.
    overall_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pull-requests": [pr.to_dict() for pr in self.pull_requests]
        }
        if self.overall_status is not None:
            payload["overall-status"] = self.overall_status
        return payload

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False) + "\n"

    # Back-compat alias used by leader PR body formatting.
    @property
    def prs(self) -> list[StatePullRequest]:
        return self.pull_requests


# Leader-repo directory that holds per-run state.json files.
STATE_ROOT_DIR = "GAP Leaders"


def state_path_for_trigger(
    trigger_id: str,
    *,
    when: datetime | None = None,
) -> str:
    """Return Leader-repo relative path: ``GAP Leaders/<UTC-date>_<trigger-id>/state.json``."""
    moment = when or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y-%m-%d")
    return f"{STATE_ROOT_DIR}/{stamp}_{trigger_id}/state.json"


def find_existing_state_path(repo_root: str | Path, trigger_id: str) -> str | None:
    """Reuse an existing dated folder for this trigger ID if present."""
    root = Path(repo_root) / STATE_ROOT_DIR
    if not root.is_dir():
        return None
    suffix = f"_{trigger_id}"
    matches = sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir() and child.name.endswith(suffix)
    )
    if not matches:
        return None
    return f"{STATE_ROOT_DIR}/{matches[-1]}/state.json"


def _normalize_overall_status(value: Any) -> str | None:
    """Return a validated overall-status, or None when unset/blank."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text not in OVERALL_STATUSES:
        raise StateFileError(
            f"overall-status must be one of {sorted(OVERALL_STATUSES)}, got {value!r}"
        )
    return text


def build_state(
    *,
    pull_requests: list[StatePullRequest] | list[dict[str, Any]] | None = None,
    prs: list[StatePullRequest] | list[dict[str, Any]] | None = None,
    overall_status: str | None = None,
) -> PromoterState:
    """Build a validated PromoterState from PR records.

    Stage 1 (RHOAIENG-93524): each entry should use pr-status ``new`` and empty builds.
    ``overall_status`` (RHOAIENG-97052) is optional until the run concludes.
    """
    raw = pull_requests if pull_requests is not None else (prs or [])
    normalized: list[StatePullRequest] = []
    for index, item in enumerate(raw):
        if isinstance(item, StatePullRequest):
            normalized.append(item)
        elif isinstance(item, dict):
            # Default Stage 1 fields when callers only supply repo + pr-url.
            payload = {
                "repo": item.get("repo"),
                "pr-url": item.get("pr-url") or item.get("pr_url") or item.get("url"),
                "pr-status": item.get("pr-status") or item.get("pr_status") or PR_STATUS_NEW,
                "builds": item.get("builds") if "builds" in item else [],
            }
            # Accept owner/name and reduce to short slug.
            repo = str(payload["repo"] or "").strip()
            if "/" in repo:
                payload["repo"] = repo.rsplit("/", 1)[-1]
            normalized.append(StatePullRequest.from_mapping(payload, index=index))
        else:
            raise StateFileError(f"pull-requests[{index}] must be a mapping")

    state = PromoterState(
        pull_requests=normalized,
        overall_status=_normalize_overall_status(overall_status),
    )
    validate_state(state.to_dict())
    return state


def validate_state(data: Any) -> dict[str, Any]:
    """Validate a state.json payload and return a normalized dict."""
    if not isinstance(data, dict):
        raise StateFileError("state must be a JSON object")

    raw_prs = data.get("pull-requests")
    if raw_prs is None and "prs" in data:
        raise StateFileError(
            "state uses obsolete key 'prs'; expected 'pull-requests' (RHOAIENG-93564)"
        )
    if not isinstance(raw_prs, list):
        raise StateFileError("pull-requests must be a list")

    prs = [
        StatePullRequest.from_mapping(item, index=index)
        if isinstance(item, dict)
        else (_raise_pr_type(index))
        for index, item in enumerate(raw_prs)
    ]
    normalized: dict[str, Any] = {
        "pull-requests": [pr.to_dict() for pr in prs]
    }
    # Accept either wire key or pythonic alias from callers.
    overall = _normalize_overall_status(
        data.get("overall-status", data.get("overall_status"))
    )
    if overall is not None:
        normalized["overall-status"] = overall
    return normalized


def write_state_file(path: str | Path, state: PromoterState | dict[str, Any]) -> Path:
    """Write pretty-printed state.json to path after validation."""
    payload = state.to_dict() if isinstance(state, PromoterState) else validate_state(state)
    validate_state(payload)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def load_state_file(path: str | Path) -> dict[str, Any]:
    """Load and validate a state.json file from disk."""
    content = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StateFileError(f"invalid JSON in {path}: {exc}") from exc
    return validate_state(data)


def parse_pr_number_from_url(url: str) -> int:
    """Extract the pull request number from a GitHub PR URL."""
    parsed = urlparse(url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "pull":
        raise StateFileError(f"not a GitHub pull request URL: {url}")
    try:
        return int(parts[3])
    except ValueError as exc:
        raise StateFileError(f"invalid pull request number in URL: {url}") from exc


def repo_slug_from_url(repo_url: str) -> str:
    """Return owner/name for a GitHub HTTPS/SSH URL or slug."""
    text = repo_url.strip().removesuffix(".git")
    if text.startswith("git@github.com:"):
        return text.split(":", 1)[1]
    if text.startswith("http://") or text.startswith("https://"):
        path = urlparse(text).path.lstrip("/")
        return path.removesuffix(".git")
    if "/" in text or text:
        return text
    raise StateFileError(f"cannot derive repo slug from '{repo_url}'")


def short_repo_name(repo_url_or_slug: str) -> str:
    """Return the short repo slug (last path segment), e.g. odh-dashboard."""
    slug = repo_slug_from_url(repo_url_or_slug)
    return slug.rsplit("/", 1)[-1] if slug else slug


def _looks_like_pr_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) >= 4 and parts[2] == "pull" and parts[3].isdigit()


def _raise_pr_type(index: int) -> StatePullRequest:
    raise StateFileError(f"pull-requests[{index}] must be a mapping")


def _raise_build_type(path: str) -> StateBuild:
    raise StateFileError(f"{path} must be a mapping")
