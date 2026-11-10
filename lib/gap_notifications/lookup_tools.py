"""Read-only registry inspection, tracking lookup and dependency-light rendering.

Inspection and current-observation projection receive an explicit Jira adapter
and validate trial/incident/episode/environment metadata with shared dispatch helpers.
Only search/GET service calls occur; Slack URLs derive from confirmed stored
root receipts, never history queries. Hints are revalidated, ambiguity stays
unavailable, and closed fallbacks are explicitly marked.

tracking_cell only formats a supplied ephemeral projection. It distinguishes
not checked, unavailable, successful-empty and pending/held/closed results.
Empty matches for success/unknown observations need no failure record; other
empty matches retain their component/check labels for investigation. Lookup-only
imports are deferred until lookup or inspection, not rendering. No client construction, credential/ledger access,
mutation or resend is initiated here; display text is never routing authority.
"""
from __future__ import annotations

import html
import re
import json


def project_tracking(observations, jira, *, trial_id, known_keys=None, jira_environment="stage", channel=None):
    """Search/GET only; hints require the same fresh identity/property validation.

    Absence is not proof that no ticket exists (search may lag). Nothing here
    creates adapters, reads a ledger, touches Slack, or retries a mutation.
    """
    from lib.gap_notifications.dispatch_incidents import discussion_url, validated_property
    from lib.gap_notifications.dispatch_incidents import incident_key
    from lib.gap_notifications.dispatch_incidents import bind_trial, incident_label, validate_observations
    from lib.gap_notifications.notify_tools import routing_target
    target = routing_target(jira_environment, channel)
    bind_trial({}, trial_id, **target.options)
    validate_observations(observations, **target.options)
    if getattr(jira, "base_url", target.url) != target.url:
        return {"status": "unavailable"}
    rows = {}
    cache = {}
    for obs in observations:
        pr = obs.get("evidence", {}).get("pr_url")
        if not isinstance(pr, str):
            continue
        identity = incident_key(obs)
        entry = {"component": obs.get("component") or obs["category"],
                 "pipeline": obs.get("pipeline"), "status": "unavailable",
                 "observation_status": obs["status"]}
        if identity not in cache:
            try:
                found = jira.find_by_label(incident_label(identity, trial_id))
                if not isinstance(found, list):
                    raise ValueError("incomplete search")
                keys = [item["key"] for item in found]
                if len(keys) != len(set(keys)):
                    raise ValueError("duplicate search result")
                keys = sorted(set(keys) | set((known_keys or {}).get(identity, [])))
                records = []
                for key in keys:
                    issue = jira.get_issue(key)
                    if issue.get("key") != key:
                        raise ValueError("issue mismatch")
                    state = validated_property(issue, jira.get_notification_state(key),
                                               trial=trial_id, identity=identity, **target.options)
                    records.append((issue, state))
                if len({s["episode_id"] for _, s in records}) != len(records):
                    raise ValueError("duplicate episodes")
                opened = [(i, s) for i, s in records
                          if i["fields"]["status"]["statusCategory"]["key"] != "done"]
                if len(opened) > 1:
                    raise ValueError("ambiguous open incidents")
                if not records:
                    projected = {"status": "not_found"}
                else:
                    issue, state = (opened or sorted(records, key=lambda p: int(p[0]["key"].split("-")[1])))[-1]
                    root = state["delivery"]["root"]
                    closed = issue["fields"]["status"]["statusCategory"]["key"] == "done"
                    projected = {"jira_key": issue["key"], "closed": closed,
                                 "status": "closed" if closed else ("confirmed" if root else "slack_pending")}
                    if root:
                        projected["slack_url"] = discussion_url(root, channel=target.channel)
                    if state["jira_intent"] or state["delivery"]["intent"]:
                        projected["status"] = "held"
                cache[identity] = projected
            except Exception:
                cache[identity] = {"status": "unavailable"}
        entry.update(cache[identity])
        if entry not in rows.setdefault(pr, []):
            rows[pr].append(entry)
    return {"status": "checked", "rows": rows, "jira_origin": target.url, "channel": target.channel}


def _text(value):
    text = html.escape(" ".join(str(value).split())[:160], quote=True)
    return re.sub(r"([\\`*{}\[\]()#+.!_|>~-])", r"\\\1", text)


def tracking_cell(tracking, pr_url):
    """Render only an explicitly supplied projection, never prior display text."""
    if not tracking or tracking.get("status") == "not_checked":
        return "Not checked"
    if tracking.get("status") != "checked":
        return "Unavailable"
    entries = tracking.get("rows", {}).get(pr_url)
    if not entries:
        return "Not checked"
    parts = []
    try:
        # Display-only allowlist: keep this renderer free of service-module imports.
        origin = tracking.get("jira_origin", "https://stage-redhat.atlassian.net")
        if origin not in {"https://stage-redhat.atlassian.net", "https://redhat.atlassian.net"}:
            raise ValueError("invalid Jira display origin")
        for entry in entries:
            label = _text(entry["component"])
            if entry.get("pipeline"):
                label += " / " + _text(entry["pipeline"])
            status = entry["status"]
            key = entry.get("jira_key")
            if key is None:
                if status == "not_found":
                    # Only failures create incidents. Still look up all observations
                    # so recovery/history links and unavailable lookups remain visible.
                    # Legacy entries without observation_status stay fail-visible.
                    if entry.get("observation_status") in {"success", "unknown"}:
                        continue
                    value = "No confirmed registry match"
                else:
                    value = "Unavailable"
            else:
                if not re.fullmatch(r"RHAI-[1-9][0-9]*", key):
                    raise ValueError("invalid issue")
                value = f"[{key}]({origin}/browse/{key})"
                closed = entry.get("closed", False)
                if not isinstance(closed, bool):
                    raise ValueError("invalid issue status")
                if closed:
                    value += " (Closed)"
                url = entry.get("slack_url")
                if url:
                    if not re.fullmatch(r"[CG][A-Z0-9]+", tracking["channel"]):
                        raise ValueError("invalid configured channel")
                    if not re.fullmatch(r"https://redhat-internal\.slack\.com/archives/" + re.escape(tracking["channel"]) + r"/p[0-9]+", url):
                        raise ValueError("invalid discussion")
                    value += f" · [Slack thread]({url})"
                else:
                    value += " · No confirmed Slack thread" if closed else " · Slack pending / unconfirmed"
                if status == "held":
                    value += " (delivery held)"
            parts.append(label + ": " + value)
    except (KeyError, TypeError, ValueError):
        return "Unavailable"
    return "<br>".join(parts) if parts else "No failure records to link"


def inspect_issue(jira, key, *, trial=None, jira_environment="stage", channel=None):
    """GET only. Output allowlisted fields; malformed properties never leak raw values."""
    from lib.gap_notifications.dispatch_incidents import validated_property, discussion_url, metadata_url
    from lib.gap_notifications.notify_tools import visible_link_status, routing_target
    target = routing_target(jira_environment, channel)
    if getattr(jira, "base_url", target.url) != target.url:
        raise ValueError("Jira adapter environment mismatch")
    issue = jira.get_issue(key)
    category = issue.get("fields", {}).get("status", {}).get("statusCategory", {}).get("key")
    result = {"issue": key, "status_category": category if category in {"new", "indeterminate", "done"} else "unavailable",
              "open": (category != "done") if category in {"new", "indeterminate", "done"} else None,
              "authority": {"status": "Jira issue status", "routing": "Jira gap.notification.v1", "visible_link": "projection only", "ledger": "cache/audit only"}}
    try:
        state = jira.get_notification_state(key)
        if state is None:
            result["property"] = "missing"
            return result
        # Validate stored trial first so omitted --trial-id does not compare default.
        checked = validated_property(issue, state, **target.options)
        if trial is not None and trial != checked["trial_id"]:
            result.update(property="trial_mismatch", trial_match=False)
            return result
        delivery = checked["delivery"]
        root = delivery["root"]
        url = discussion_url(root, channel=target.channel) if root else None
        def intent(value):
            if value is None:
                return None
            return {k: value[k] for k in ("state", "outcome", "event_id", "retry_at") if k in value}
        result.update(property="valid", scope=checked["scope"], trial_id=checked["trial_id"],
                      trial_match=True if trial is not None else None, incident_identity=json.loads(checked["incident_key"]),
                      identity_match=True, recorded_outcome=checked["last_outcome"], root=root, discussion_url=url,
                      metadata_url=metadata_url(issue["key"], **target.options), metadata_note="Jira permissions required.",
                      visible_metadata_link=visible_link_status(issue["fields"].get("description"), metadata_url(issue["key"], **target.options), metadata=True, **target.options),
                      visible_link=visible_link_status(issue["fields"].get("description"), url) if url else "no_confirmed_root",
                      jira_intent=intent(checked["jira_intent"]), slack_intent=intent(delivery["intent"]))
    except Exception:
        result["property"] = "malformed_or_unavailable"
    return result
