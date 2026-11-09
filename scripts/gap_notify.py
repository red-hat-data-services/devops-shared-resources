#!/usr/bin/env python3
"""CLI for explicit observations and environment-scoped notification trials.

Without --write, validate supplied observation JSON and ingest it against a
trial-bound journal snapshot in memory, printing a preview without authenticated
clients or journal writes. With --write, use explicitly supplied environment
credentials for the selected Jira environment/configured Slack channel and existing
dispatcher; an empty observation list can drain pending work. Receipt resolution
is an explicit write-mode operator action, not automatic proof of delivery.

The separate --inspect mode reads only Jira issue/property diagnostics, needs
explicit Jira credentials and avoids ledger/Slack access. Preview can read the
local journal; it is not the monitor's stricter no-ledger preview. None of these
modes collects live build truth. Error reports are sanitized; preview output
includes supplied outbox/evidence and is not a secret scrubber. Unresolved writes
stay visible, and the ledger cannot be Leader state or the observation input.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.gap_notifications.dispatch_incidents import Dispatcher, Ledger, bind_trial, ingest, validate_observations
from lib.gap_notifications.notify_tools import configured_channel, routing_target, jira_target_from_url, STAGING_JIRA_ORIGIN


def main(argv=None, *, environ=None, jira_factory=None, slack_factory=None, stdout=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", metavar="RHAI-N", help="Read-only Jira status/registry diagnostics; no ledger or Slack access")
    parser.add_argument("--observations", help="JSON list; [] drains pending deliveries")
    parser.add_argument("--ledger", help="Schema2 local uncertainty journal/cache; Jira is incident authority")
    parser.add_argument("--trial-id", help="Stable safe test namespace; immutable per ledger")
    parser.add_argument("--write", action="store_true", help="Allow selected-environment writes and ledger persistence")
    parser.add_argument("--jira-url", help="Deprecated matching assertion of GAP_JIRA_URL, never a routing override")
    parser.add_argument("--project", default="RHAI")
    parser.add_argument("--issue-type", default="Bug", choices=["Bug", "10016"])
    parser.add_argument("--channel", help="Matching assertion of GAP_SLACK_CHANNEL, never a routing override")
    parser.add_argument("--transition-id", help="Explicit verified transition to done for the selected Jira environment")
    parser.add_argument("--transition-fields", default="{}", help="JSON fields required for closure")
    parser.add_argument("--resolve-receipt", help="JSON file containing task_id and operator-attested receipt/reason")
    args = parser.parse_args(argv)
    out = stdout or sys.stdout
    try:
        env = environ if environ is not None else os.environ
        if "GAP_JIRA_ENVIRONMENT" in env:
            print(json.dumps({"error": "notification_prototype_failed", "type": "ValueError",
                              "guidance": "Remove GAP_JIRA_ENVIRONMENT; use GAP_JIRA_URL."}), file=out)
            return 1
        destination = jira_target_from_url(env.get("GAP_JIRA_URL", STAGING_JIRA_ORIGIN))
        channel = configured_channel(env.get("GAP_SLACK_CHANNEL", ""))
        route = routing_target(destination.name, channel)
        asserted = jira_target_from_url(args.jira_url) if args.jira_url is not None else destination
        if asserted != destination or args.project != "RHAI" or (args.channel is not None and args.channel != channel):
            raise ValueError("destination must match the selected Jira environment and fixed project and configured channel")
        if destination.name == "production" and args.issue_type != "Bug":
            raise ValueError("production requires the Bug type name, not a staging type ID")
        if args.inspect:
            if args.write or args.observations or args.ledger or args.resolve_receipt or args.transition_id or args.transition_fields != "{}":
                raise ValueError("inspect is exclusive and read-only")
            from lib.gap_notifications.notify_tools import JiraNotifier
            from lib.gap_notifications.lookup_tools import inspect_issue
            JiraNotifier._issue_key(args.inspect)
            env = environ if environ is not None else os.environ
            email = env.get("JIRA_USER_EMAIL") or env.get("JIRA_API_EMAIL")
            token = env.get("JIRA_API_TOKEN")
            if not all(isinstance(v, str) and v.strip() for v in (email, token)):
                raise ValueError("explicit Jira environment credentials required")
            jira = (jira_factory or JiraNotifier)(destination.url, "RHAI", args.issue_type, email, token, **destination.options)
            print(json.dumps(inspect_issue(jira, args.inspect, trial=args.trial_id, **route.options), indent=2), file=out)
            return 0
        from lib.gap_notifications.dispatch_incidents import ABSENT_RECEIPT, validate_resolution
        receipt = ABSENT_RECEIPT
        if args.resolve_receipt is not None:
            receipt = json.loads(Path(args.resolve_receipt).read_text())
            validate_resolution(receipt, channel=channel)
        args.trial_id = args.trial_id or "default"
        if not args.observations or not args.ledger:
            raise ValueError("observations and ledger required outside inspect mode")
        if args.transition_id and not args.transition_id.isdigit():
            raise ValueError("transition ID must be numeric")
        fields = json.loads(args.transition_fields)
        if not isinstance(fields, dict):
            raise ValueError("transition fields must be an object")
        source, target = Path(args.observations), Path(args.ledger)
        if source.resolve() == target.resolve() or target.name == "state.json":
            raise ValueError("ledger must be separate from observation/Leader state")
        if source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("observation file exceeds 4 MiB")
        observations = json.loads(source.read_text())
        validate_observations(observations, **route.options)
        ledger = Ledger(target)
        bind_trial(ledger.read(), args.trial_id, **route.options)
        if not args.write:
            if args.resolve_receipt:
                raise ValueError("receipt resolution requires --write")
            result = ingest(ledger.read(), observations, trial_id=args.trial_id, **route.options)
            summary = {"dry_run": True, "registry_version": 2,
                       "incidents": len({t["incident_key"] for t in result.get("outbox", [])}),
                       "outbox": result.get("outbox", [])}
        else:
            # Explicit write opt-in only. No dotenv loading or credential discovery.
            env = environ if environ is not None else os.environ
            email = env.get("JIRA_USER_EMAIL") or env.get("JIRA_API_EMAIL")
            token = env.get("JIRA_API_TOKEN")
            slack_token = env.get("SLACK_TOKEN") or env.get("SLACK_BOT_TOKEN")
            if not all(isinstance(v, str) and v.strip() for v in (email, token, slack_token)):
                raise ValueError("explicit credentials for the selected Jira environment and Slack required")
            if jira_factory is None:
                from lib.gap_notifications.notify_tools import JiraNotifier
                jira_factory = JiraNotifier
            if slack_factory is None:
                from lib.gap_notifications.notify_tools import SlackNotifier
                slack_factory = SlackNotifier
            jira = jira_factory(destination.url, "RHAI", args.issue_type, email, token, **destination.options)
            slack = slack_factory(slack_token, channel)
            result = Dispatcher(ledger, jira, slack, transition_id=args.transition_id,
                                transition_fields=fields, trial_id=args.trial_id, **route.options).run(observations, receipt=receipt)
            summary = {"dry_run": False, "registry_version": 2,
                       "incidents": len({t["incident_key"] for t in result.get("outbox", [])}),
                       "deliveries": [{"id": t["id"], "system": t["system"], "state": t["state"],
                                       "hold": t.get("hold"), "presentation_warning": t.get("presentation_warning")} for t in result.get("outbox", [])]}
        print(json.dumps(summary, indent=2), file=out)
        if args.write and any(t["state"] in {"pending", "inflight", "reconciliation_required", "rejected"} for t in result.get("outbox", [])):
            return 2
        return 0
    except Exception as exc:
        # Never print transport exceptions, evidence, credentials or response bodies.
        print(json.dumps({"error": "notification_prototype_failed", "type": type(exc).__name__}), file=out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
