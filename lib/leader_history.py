"""Preserve terminal Leader records without changing component gate decisions."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Callable, Sequence

from lib.gap_promotion_gates import derive_legacy_overall_status
from lib.github_cli import run_gh
from lib.pr_status_updater import parse_pr_url

HISTORY_ROOT = Path("GAP History")
HISTORY_FOLDERS = {"success": "successful", "failure": "unsuccessful"}
# Keep result labels outside the gap-* trigger-ID namespace used by monitors.
RESULT_LABELS = {"success": "result-gap-success", "failure": "result-gap-failure"}


def write_history_snapshot(repo_root: Path, state_path: Path) -> Path | None:
    """Copy either terminal outcome; retain the canonical monitor state path."""
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    # Canonical gates take precedence, even when empty or incomplete. Derivation
    # normalizes gates, so use a copy to preserve the exact archived payload.
    overall = (
        derive_legacy_overall_status(copy.deepcopy(payload))
        if "promotion-gates" in payload
        else payload.get("overall-status")
    )
    folder = HISTORY_FOLDERS.get(overall)
    if folder is None:
        return None
    relative = state_path.resolve().relative_to(repo_root.resolve())
    if relative.parts[0] == "GAP Leaders":
        relative = Path(*relative.parts[1:])
    snapshot = repo_root / HISTORY_ROOT / folder / relative
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return snapshot


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

    label = RESULT_LABELS[overall_status]
    run(["label", "create", label, "--repo", ref.slug, "--force",
         "--color", "b60205" if overall_status == "failure" else "0e8a16",
         "--description", f"Recorded GAP run result: {overall_status}"])
    run(["api", "--method", "POST", f"repos/{ref.slug}/issues/{ref.number}/labels",
         "-f", f"labels[]={label}"])
    for other in RESULT_LABELS.values():
        if other != label and other in labels:
            run(["api", "--method", "DELETE",
                 f"repos/{ref.slug}/issues/{ref.number}/labels/{other}"])
    title = re.sub(r"^\[GAP (?:failed|passed)\]\s*", "", pull["title"])
    prefix = "[GAP failed]" if overall_status == "failure" else "[GAP passed]"
    run(["api", "--method", "PATCH", endpoint, "-f", f"title={prefix} {title}"])
    # No --admin, force merge, branch deletion, or synthetic component success.
    run(["pr", "merge", str(ref.number), "--repo", ref.slug, "--auto", "--merge",
         "--match-head-commit", published_sha])
