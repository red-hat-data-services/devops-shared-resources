#!/usr/bin/env python3
"""Shared, directly testable policy for the reusable monitor workflow.

Receipt detection uses only the standard library, before dependency setup.
Infrastructure, trusted checkouts and credential exposure remain in workflow YAML.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def receipt_only(env, *, git=None):
    """Only first-attempt PR synchronize app receipts are skipped, never reruns."""
    if (env.get("MONITOR_RUN_ATTEMPT", "1") != "1"
            or env.get("MONITOR_EVENT") != "pull_request"
            or env.get("MONITOR_ACTION") != "synchronize"):
        return False
    if git is None:
        def git(*args):
            return subprocess.check_output(["git", *args])
    if git("log", "-1", "--format=%s").decode().rstrip("\n") != "Update GAP state from PR monitor (RHOAIENG-97052)":
        return False
    if git("log", "-1", "--format=%ae").decode().rstrip("\n") != "github-actions[bot]@users.noreply.github.com":
        return False
    paths = git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD").decode().rstrip("\0").split("\0")
    patterns = ("GAP Leaders/*/state.json", "GAP History/successful/*/state.json", "GAP History/unsuccessful/*/state.json")
    return bool(paths and paths[0]) and all(any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns) for p in paths)


def monitor_arguments(env):
    """Resolve policy before monitor import, with bounded fail-closed diagnostics."""
    if env.get("MONITOR_DRY_RUN") == "true":
        return ["--dry-run"], "monitor-dry-run"
    args = ["--publish-state"]
    mode = env.get("GAP_NOTIFICATION_MODE", "disabled")
    if mode == "disabled":
        return args, "disabled"
    if mode not in {"preview", "write"}:
        return args, "invalid-mode"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.gap_notifications.monitor_hook import NotificationConfig
    from lib.gap_notifications.notify_tools import configured_channel, jira_target_from_url, STAGING_JIRA_ORIGIN
    try:
        if "GAP_JIRA_ENVIRONMENT" in env:
            return args, "legacy-jira-selector"
        target = jira_target_from_url(env.get("GAP_JIRA_URL", STAGING_JIRA_ORIGIN))
        channel = configured_channel(env.get("GAP_SLACK_CHANNEL", ""))
        scope = "staging" if target.name == "stage" else "production"
        trial = f"gap-monitor-{scope}-v1"
        ledger = str(Path(env["RUNNER_TEMP"]) / "gap-notifications" / scope / f"{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}" / "journal.json") if mode == "write" else None
        transition = "41" if mode == "write" and target.name == "stage" else None
        fields = {"resolution": {"id": "10000"}} if transition else {}
        NotificationConfig(mode=mode, ledger=ledger, trial_id=trial, jira_environment=target.name,
                           channel=channel, transition_id=transition, transition_fields=fields).validate()
    except (ValueError, KeyError, TypeError):
        return args, "invalid-configuration"
    args += ["--notification-mode", mode, "--notification-trial-id", trial]
    if ledger:
        args += ["--notification-ledger", ledger]
    if transition:
        import json
        args += ["--notification-transition-id", transition, "--notification-transition-fields", json.dumps(fields)]
    return args, "configured-" + mode


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["receipt"]:
        skip = receipt_only(os.environ)
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"skip={str(skip).lower()}\n")
        print("GAP monitor: receipt-only update skipped" if skip else "GAP monitor: scan eligible")
        return 0
    if argv == ["run"]:
        args, reason = monitor_arguments(os.environ)
        print("GAP notifications configuration: " + reason, flush=True)
        return subprocess.call([sys.executable, str(ROOT / "scripts/gap_pr_monitor.py"),
                                "--repo-root", ".", "--allow-gap-dir-fallback", *args])
    raise ValueError("expected receipt or run")


if __name__ == "__main__":
    raise SystemExit(main())
