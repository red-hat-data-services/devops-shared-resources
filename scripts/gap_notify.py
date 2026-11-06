#!/usr/bin/env python3
"""Explicit normalized observations -> isolated staging notification ledger."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.gap_notification_dispatch import CHANNEL, STAGE, Dispatcher, Ledger, bind_trial, ingest


def main(argv=None, *, environ=None, jira_factory=None, slack_factory=None, stdout=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", metavar="RHAI-N", help="Read-only Jira status/registry diagnostics; no ledger or Slack access")
    parser.add_argument("--observations", help="JSON list; [] drains pending deliveries")
    parser.add_argument("--ledger", help="Schema2 local uncertainty journal/cache; Jira is incident authority")
    parser.add_argument("--trial-id", help="Stable safe test namespace; immutable per ledger")
    parser.add_argument("--write", action="store_true", help="Allow staging writes and ledger persistence")
    parser.add_argument("--jira-url", default=STAGE)
    parser.add_argument("--project", default="RHAI")
    parser.add_argument("--issue-type", default="Bug", choices=["Bug", "10016"])
    parser.add_argument("--channel", default=CHANNEL)
    parser.add_argument("--transition-id", help="Explicit verified staging transition to done")
    parser.add_argument("--transition-fields", default="{}", help="JSON fields required for closure")
    parser.add_argument("--resolve-receipt", help="JSON file containing task_id and operator-attested receipt/reason")
    args = parser.parse_args(argv)
    out = stdout or sys.stdout
    try:
        if args.jira_url != STAGE or args.project != "RHAI" or args.channel != CHANNEL:
            raise ValueError("only the agreed staging site/project/channel are supported")
        if args.inspect:
            if args.write or args.observations or args.ledger or args.resolve_receipt or args.transition_id or args.transition_fields != "{}":
                raise ValueError("inspect is exclusive and read-only")
            from lib.gap_notification_jira import JiraNotifier
            from lib.gap_notification_diagnostics import inspect_issue
            JiraNotifier._issue_key(args.inspect)
            env = environ if environ is not None else os.environ
            email = env.get("JIRA_USER_EMAIL") or env.get("JIRA_API_EMAIL")
            token = env.get("JIRA_API_TOKEN")
            if not all(isinstance(v, str) and v.strip() for v in (email, token)):
                raise ValueError("explicit Jira environment credentials required")
            jira = (jira_factory or JiraNotifier)(STAGE, "RHAI", args.issue_type, email, token)
            print(json.dumps(inspect_issue(jira, args.inspect, trial=args.trial_id), indent=2), file=out)
            return 0
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
        ledger = Ledger(target)
        bind_trial(ledger.read(), args.trial_id)
        if not args.write:
            if args.resolve_receipt:
                raise ValueError("receipt resolution requires --write")
            result = ingest(ledger.read(), observations, trial_id=args.trial_id)
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
                raise ValueError("explicit staging Jira/Slack environment credentials required")
            if jira_factory is None:
                from lib.gap_notification_jira import JiraNotifier
                jira_factory = JiraNotifier
            if slack_factory is None:
                from lib.gap_notification_slack import SlackNotifier
                slack_factory = SlackNotifier
            jira = jira_factory(STAGE, "RHAI", args.issue_type, email, token)
            slack = slack_factory(slack_token, CHANNEL)
            receipt = json.loads(Path(args.resolve_receipt).read_text()) if args.resolve_receipt else None
            result = Dispatcher(ledger, jira, slack, transition_id=args.transition_id,
                                transition_fields=fields, trial_id=args.trial_id).run(observations, receipt=receipt)
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
