"""Preserve terminal Leader records without changing component gate decisions."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Callable, Sequence

from lib.gap_promotion_gates import derive_legacy_overall_status
from lib.github_cli import run_gh
from lib.leader_lifecycle import retired_outcome
from lib.pr_status_updater import parse_pr_url

HISTORY_ROOT = Path("GAP History")
HISTORY_FOLDERS = {"success": "successful", "failure": "unsuccessful", "superseded": "unsuccessful"}
# Keep result labels outside the gap-* trigger-ID namespace used by monitors.
RESULT_LABELS = {"success": "result-gap-success", "failure": "result-gap-failure",
                 "superseded": "result-gap-superseded"}


def state_outcome(payload: dict) -> str | None:
    """Derive an archive outcome without normalizing the saved payload."""
    return retired_outcome(payload) or (
        derive_legacy_overall_status(copy.deepcopy(payload))
        if "promotion-gates" in payload else payload.get("overall-status")
    )


def is_history_state(path: Path) -> bool:
    """Recognize state files beneath an outcome-specific history directory."""
    return (path.name == "state.json" and path.parent.parent.name in HISTORY_FOLDERS.values()
            and path.parent.parent.parent.name == str(HISTORY_ROOT))


def history_state_outcome(path: Path) -> str | None:
    """An archive is immutable and must agree with its outcome directory."""
    if not is_history_state(path):
        return None
    outcome = state_outcome(json.loads(path.read_text(encoding="utf-8")))
    if outcome not in HISTORY_FOLDERS or HISTORY_FOLDERS[outcome] != path.parent.parent.name:
        raise ValueError("Archived Leader outcome does not match its history folder")
    return outcome


def canonical_state_path(repo_root: Path, state_path: Path) -> Path:
    """Validate record layout and return its original active path within the repo."""
    relative = state_path.resolve().relative_to(repo_root.resolve())
    if is_history_state(state_path):
        if len(relative.parts) != 4 or relative.parts[0] != str(HISTORY_ROOT):
            raise ValueError("Expected a history path directly under the repository root")
        relative = Path("GAP Leaders") / relative.parts[-2] / "state.json"
    if len(relative.parts) != 3 or relative.parts[0] != "GAP Leaders" or relative.name != "state.json":
        raise ValueError("Expected a Leader state or outcome history path")
    return repo_root.resolve() / relative


def resolve_existing_state_path(repo_root: Path, state_path: Path) -> Path:
    """Follow a former active path after finalization; never guess between archives."""
    repo_root = repo_root.expanduser().resolve()
    path = state_path.expanduser().resolve()
    if is_history_state(path):
        history_state_outcome(path)
        return path
    if path.exists():
        return path
    try:
        canonical = canonical_state_path(repo_root, path)
    except ValueError:
        return path
    candidates = [repo_root / HISTORY_ROOT / folder / canonical.parent.name / "state.json"
                  for folder in sorted(set(HISTORY_FOLDERS.values()))]
    existing = [item for item in candidates if item.is_file()]
    if len(existing) > 1:
        raise ValueError("Multiple archived states for this Leader; refusing to choose")
    if existing:
        history_state_outcome(existing[0])
        return existing[0].resolve()
    return path


def write_history_snapshot(repo_root: Path, state_path: Path, *, move: bool = True) -> Path | None:
    """Archive a final record; move=False supports callers awaiting rollout."""
    state_path = resolve_existing_state_path(repo_root, state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    overall = state_outcome(payload)
    folder = HISTORY_FOLDERS.get(overall)
    if folder is None:
        return None
    if not move:
        relative = state_path.resolve().relative_to(repo_root.resolve())
        if relative.parts[0] == "GAP Leaders":
            relative = Path(*relative.parts[1:])
        snapshot = repo_root / HISTORY_ROOT / folder / relative
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return snapshot
    canonical = canonical_state_path(repo_root, state_path)
    snapshot = repo_root / HISTORY_ROOT / folder / canonical.parent.name / "state.json"
    if is_history_state(state_path):
        history_state_outcome(state_path)
        if canonical.exists():
            if json.loads(canonical.read_text()) != payload:
                raise ValueError("Active and archived Leader records disagree; refusing to remove either")
            canonical.unlink()
        return state_path
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if canonical.exists():
        canonical.unlink()
    return snapshot.resolve()


def prepare_leader_history_merge(
    leader_pr_url: str,
    published_sha: str,
    *,
    overall_status: str,
    dry_run: bool = False,
    gh_runner: Callable[[Sequence[str]], str] | None = None,
) -> None:
    """Label a terminal Leader and request normal, SHA-pinned GitHub auto-merge.

    Called before the existing Leader conclusion status. That status authorizes
    merging the run record, not approving any failed component PR.
    """
    if overall_status not in RESULT_LABELS:
        raise ValueError(f"Cannot merge a non-terminal Leader: {overall_status!r}")
    ref = parse_pr_url(leader_pr_url)
    if dry_run:
        print(f"[dry-run] would label {leader_pr_url} {RESULT_LABELS[overall_status]} "
              f"and enable auto-merge at {published_sha}")
        return
    run = gh_runner or run_gh
    endpoint = f"repos/{ref.slug}/pulls/{ref.number}"
    pull = json.loads(run(["api", endpoint]))
    head = pull.get("head") or {}
    if head.get("sha") != published_sha:
        raise RuntimeError("Leader head moved; rerun before enabling history merge")
    if pull.get("merged"):
        return
    if pull.get("state") != "open":
        raise RuntimeError("Leader closed; rerun before enabling history merge")
    labels = {item["name"] for item in pull.get("labels", [])}
    if ("gated-artifacts-promoter" not in labels
            or not str(head.get("ref", "")).startswith("gap-leader/")
            or (head.get("repo") or {}).get("full_name") != ref.slug):
        raise RuntimeError("Refusing history merge for a PR that is not a same-repository GAP Leader")

    if "result-gap-superseded" in labels and overall_status != "superseded":
        raise RuntimeError("Cannot replace a superseded Leader result with a build/test outcome")

    label = RESULT_LABELS[overall_status]
    run(["label", "create", label, "--repo", ref.slug, "--force",
         "--color", {"failure": "b60205", "success": "0e8a16", "superseded": "6e7781"}[overall_status],
         "--description", f"Recorded GAP run result: {overall_status}"])
    run(["api", "--method", "POST", f"repos/{ref.slug}/issues/{ref.number}/labels",
         "-f", f"labels[]={label}"])
    for other in RESULT_LABELS.values():
        if other != label and other in labels:
            run(["api", "--method", "DELETE",
                 f"repos/{ref.slug}/issues/{ref.number}/labels/{other}"])
    title = re.sub(r"^\[GAP (?:failed|passed|superseded)\]\s*", "", pull["title"])
    prefix = {"failure": "[GAP failed]", "success": "[GAP passed]",
              "superseded": "[GAP superseded]"}[overall_status]
    run(["api", "--method", "PATCH", endpoint, "-f", f"title={prefix} {title}"])
    # No --admin, force merge, branch deletion, or synthetic component success.
    run(["pr", "merge", str(ref.number), "--repo", ref.slug, "--auto", "--merge",
         "--match-head-commit", published_sha])
