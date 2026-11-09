"""Opt-in environment-scoped notifications after accepted monitor state publication.

Filter the published Leader snapshot to this pass's accepted child decisions,
collect normalized observations, then invoke the existing dispatcher only in
explicit write mode. Disabled/preview/monitor-dry-run paths avoid notification
credentials, clients and ledger access. Write mode uses explicit allowlisted adapters,
an explicit stable trial and a journal outside the monitored checkout.

Return/log bounded delivery counts, never a historical task/evidence dump.
An optional separate tracking handoff performs validated read-only Jira lookup
for the same accepted observations, without initiating writes or Slack reads
for display. Errors remain isolated from promotion gates and summary reporting.

This hook neither chooses a Leader nor decides gates. Multiple active Leaders
may update the same incident's Jira issue and Slack thread; no newest-Leader
authority is enforced. State publication and notification writes are not one
transaction.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit


@dataclass(frozen=True)
class NotificationConfig:
    mode: str = "disabled"
    ledger: str | None = None
    trial_id: str = "default"
    transition_id: str | None = None
    transition_fields: dict = field(default_factory=dict)
    jira_environment: str = "stage"
    channel: str | None = None

    def validate(self):
        from lib.gap_notifications.notify_tools import routing_target
        routing_target(self.jira_environment, self.channel)
        if self.mode not in {"disabled", "preview", "write"}:
            raise ValueError("invalid notification mode")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.trial_id):
            raise ValueError("invalid trial ID")
        if self.mode == "write" and not self.ledger:
            raise ValueError("write mode requires an isolated notification ledger")
        if self.transition_id is not None and not re.fullmatch(r"[0-9]+", self.transition_id):
            raise ValueError("invalid transition ID")
        if not isinstance(self.transition_fields, dict):
            raise ValueError("transition fields must be an object")
        json.dumps(self.transition_fields, allow_nan=False)


def _state_url(leader_url, published_sha, relative):
    parsed = urlsplit(leader_url)
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
            or len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit()
            or not re.fullmatch(r"[a-f0-9]{40}", published_sha)):
        raise ValueError("published Leader identity is required")
    return f"https://github.com/{parts[0]}/{parts[1]}/blob/{published_sha}/{quote(relative, safe='/')}"


def notify_after_publish(payload, *, accepted_urls, config=None, leader_url=None,
                         published_sha=None, state_path=None, repo_root=None,
                         dry_run=False, collector=None, dispatcher_factory=None,
                         jira_factory=None, slack_factory=None, environ=None, tracking=None):
    """Non-blocking. No credential/client/ledger access unless explicitly write-enabled.

    Input is the published payload, restricted to this pass's accepted decisions.
    No reload of arbitrary historical children, aggregate-success inference, or live
    service queries occurs in collection. Exceptions never leak transport text.
    """
    if tracking is not None:
        tracking.clear()
        tracking.update(status="not_checked")
    if config is None or config.mode == "disabled":
        return {"status": "disabled"}
    if dry_run:
        return {"status": "monitor-dry-run"}
    try:
        config.validate()
        if payload.get("leader-lifecycle"):
            return {"status": "retired-or-lifecycle-record"}
        accepted = set(accepted_urls)
        if not accepted:
            return {"status": "no-accepted-decisions"}
        root, path = Path(repo_root).resolve(), Path(state_path).resolve()
        relative = path.relative_to(root).as_posix()
        if config.ledger:
            ledger_path = Path(config.ledger).resolve()
            if ledger_path == path or ledger_path.name == "state.json":
                raise ValueError("notification ledger must not be Leader state")
            # Keep the durable journal outside the monitored Git checkout so a
            # later state publish cannot accidentally include delivery metadata.
            if ledger_path.is_relative_to(root):
                raise ValueError("notification ledger must be outside monitored checkout")
        link = _state_url(leader_url or "", published_sha or "", relative)
        filtered = copy.deepcopy(payload)
        filtered["pull-requests"] = [c for c in payload.get("pull-requests", [])
                                      if c.get("pr-url") in accepted]
        if collector is None:
            from lib.gap_notifications.analyze_merge_state import collect_observations
            collector = collect_observations
        from lib.gap_notifications.dispatch_incidents import Ledger, Dispatcher, validate_observations, bind_trial
        from lib.gap_notifications.notify_tools import routing_target
        target = routing_target(config.jira_environment, config.channel)
        observations = collector(filtered, environment=target.scope, leader_url=leader_url, state_url=link)
        validate_observations(observations, **target.options)
        counts = {s: sum(o["status"] == s for o in observations) for s in ("failure", "success", "unknown")}
        if config.mode == "preview" or not observations:
            result = {"status": "preview" if config.mode == "preview" else "no-observations", "counts": counts}
        else:
            # Runtime opt-in only. Workers never execute this branch with real
            # credentials; tests supply fake environment and fake adapters.
            bind_trial(Ledger(config.ledger).read(), config.trial_id, **target.options)
            if environ is None:
                import os
                environ = os.environ
            email = environ.get("JIRA_USER_EMAIL") or environ.get("JIRA_API_EMAIL")
            token = environ.get("JIRA_API_TOKEN")
            slack_token = environ.get("SLACK_TOKEN") or environ.get("SLACK_BOT_TOKEN")
            if not all(isinstance(v, str) and v.strip() for v in (email, token, slack_token)):
                raise ValueError("explicit notification credentials for the selected Jira environment required")
            if jira_factory is None:
                from lib.gap_notifications.notify_tools import JiraNotifier
                jira_factory = JiraNotifier
            if slack_factory is None:
                from lib.gap_notifications.notify_tools import SlackNotifier
                slack_factory = SlackNotifier
            jira = jira_factory(target.url, "RHAI", "Bug", email, token, **target.jira.options)
            dispatcher = (dispatcher_factory or Dispatcher)(Ledger(config.ledger),
                jira, slack_factory(slack_token, target.channel),
                trial_id=config.trial_id, transition_id=config.transition_id,
                transition_fields=config.transition_fields, **target.options)
            data = dispatcher.run(observations)
            if tracking is not None:
                try:
                    from lib.gap_notifications.dispatch_incidents import incident_key
                    from lib.gap_notifications.lookup_tools import project_tracking
                    current = {(incident_key(o), o["observation_id"]) for o in observations}
                    hints = {}
                    for task in data.get("outbox", []):
                        obs = task.get("observation", {})
                        identity = task.get("incident_key")
                        if ((identity, obs.get("observation_id")) in current
                                and incident_key(obs) == identity and task.get("jira_key")):
                            hints.setdefault(identity, []).append(task["jira_key"])
                    tracking.update(project_tracking(observations, jira, trial_id=config.trial_id, known_keys=hints, **target.options))
                except Exception:
                    tracking.clear()
                    tracking.update(status="unavailable")
            # Fixed keys keep historical outbox growth out of logs; never emit
            # arbitrary state names, warning text, or task-level evidence.
            state_counts = dict.fromkeys(("confirmed", "superseded", "pending", "inflight",
                                          "reconciliation_required", "rejected", "other"), 0)
            warning_count = 0
            for task in data.get("outbox", []):
                state = task.get("state")
                state_counts[state if state in state_counts else "other"] += 1
                warning_count += bool(task.get("presentation_warning"))
            result = {"status": "held" if any(state_counts[s] for s in
                      ("pending", "inflight", "reconciliation_required", "rejected")) else "settled",
                      "counts": counts, "delivery_state_counts": state_counts,
                      "delivery_task_count": sum(state_counts.values()),
                      "presentation_warning_count": warning_count}
        print("GAP notifications: " + json.dumps(result, sort_keys=True), file=sys.stderr)
        return result
    except Exception as exc:
        if tracking is not None:
            tracking.clear()
            tracking.update(status="unavailable")
        result = {"status": "error", "error_type": type(exc).__name__}
        print("WARNING: GAP notifications isolated failure: " + type(exc).__name__, file=sys.stderr)
        return result
