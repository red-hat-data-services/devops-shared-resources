"""Read-only registry diagnostics and cosmetic discussion projection helpers."""
import copy
import json
import re
import uuid

from lib.gap_notification_incidents import incident_key
from lib.gap_notification_jira import JiraNotifier

SLACK_ORIGIN = "https://redhat-internal.slack.com"
HEADING = {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Slack discussion"}]}


def discussion_url(root):
    from lib.gap_notification_dispatch import _receipt
    receipt = _receipt(root)
    return f"{SLACK_ORIGIN}/archives/{receipt['channel']}/p{receipt['ts'].replace('.', '')}"


def validated_property(issue, state, *, trial=None, identity=None):
    """Validate full stored scope/identity before using routing data; never description."""
    from lib.gap_notification_dispatch import _validate_state, _verify_issue, bind_trial, incident_label
    if not isinstance(state, dict):
        raise ValueError("missing property")
    stored_trial = state.get("trial_id")
    bind_trial({}, stored_trial)
    if trial is not None and trial != stored_trial:
        raise ValueError("trial mismatch")
    stored_identity = state.get("incident_key")
    parsed = json.loads(stored_identity)
    obs = dict(parsed, status="failure", observation_id="diagnostic", evidence={})
    if incident_key(obs) != stored_identity or (identity is not None and identity != stored_identity):
        raise ValueError("incident mismatch")
    label = incident_label(stored_identity, stored_trial)
    checked = _validate_state(state, stored_identity, stored_trial, label)
    # Summary retains caller repository spelling; identity canonicalizes it.
    # Validate that spelling against the stored identity, never against body links.
    prefix = f"[GAP STAGING TEST] {obs['category']} failure: "
    summary = issue.get("fields", {}).get("summary", "")
    if not isinstance(summary, str) or not summary.startswith(prefix):
        raise ValueError("summary identity mismatch")
    summary_obs = dict(obs, repository=summary[len(prefix):])
    if incident_key(summary_obs) != stored_identity:
        raise ValueError("summary repository mismatch")
    _verify_issue(issue, {"label": label, "latest": summary_obs})
    if "gap-episode-" + uuid.UUID(checked["episode_id"]).hex not in issue["fields"]["labels"]:
        raise ValueError("episode mismatch")
    if checked["delivery"]["root"] is not None and checked["delivery"]["last_event"] is None:
        raise ValueError("root lacks confirmed delivery event")
    return checked


def metadata_url(key):
    from lib.gap_notification_dispatch import STAGE, _key
    return f"{STAGE}/rest/api/3/issue/{_key(key)}/properties/gap.notification.v1"


def section(url, *, metadata=False):
    heading = copy.deepcopy(HEADING)
    if metadata:
        heading["content"][0]["text"] = "Notification metadata"
    content = [{"type": "text", "text": "View notification metadata (JSON — source of truth)" if metadata else "Open Slack discussion",
                "marks": [{"type": "link", "attrs": {"href": url}}]}]
    if metadata:
        content.append({"type": "text", "text": " Jira permissions required."})
    return [heading, {"type": "paragraph", "content": content}]


def _managed_indices(body, *, metadata=False):
    if not isinstance(body, dict) or body.get("type") != "doc" or not isinstance(body.get("content"), list):
        raise ValueError("description unavailable")
    nodes = body["content"]
    indices = [i for i, n in enumerate(nodes) if n.get("type") == "heading" and
               any(c.get("text") == ("Notification metadata" if metadata else "Slack discussion") for c in n.get("content", []))]
    if len(indices) > 1:
        raise ValueError("ambiguous discussion sections")
    if indices:
        i = indices[0]
        if i + 1 >= len(nodes) or nodes[i] != section("", metadata=metadata)[0]:
            raise ValueError("edited discussion section")
        paragraph = nodes[i + 1]
        try:
            url = paragraph["content"][0]["marks"][0]["attrs"]["href"]
            pattern = (r"https://stage-redhat\.atlassian\.net/rest/api/3/issue/RHAI-[1-9][0-9]*/properties/gap\.notification\.v1"
                       if metadata else re.escape(SLACK_ORIGIN) + r"/archives/C0883L8FARE/p[0-9]+")
            if not re.fullmatch(pattern, url) or paragraph != section(url, metadata=metadata)[1]:
                raise ValueError("edited discussion section")
        except (KeyError, IndexError, TypeError):
            raise ValueError("edited discussion section") from None
    return indices


def project_body(body, url, *, metadata=None):
    updated = copy.deepcopy(body)
    # Plan both sections before returning any mutation; edits/duplicates fail closed.
    for value, is_metadata in ((url, False), (metadata, True)):
        if value is None:
            continue
        indices = _managed_indices(updated, metadata=is_metadata)
        nodes = section(value, metadata=is_metadata)
        if indices:
            updated["content"][indices[0]:indices[0] + 2] = nodes
        else:
            updated["content"].extend(nodes)
    JiraNotifier._validate_adf(updated)
    return updated


def visible_link_status(body, url, *, metadata=False):
    try:
        indices = _managed_indices(body, metadata=metadata)
        if not indices:
            return "missing"
        return "match" if body["content"][indices[0] + 1] == section(url, metadata=metadata)[1] else "mismatch"
    except ValueError:
        return "ambiguous_or_edited"


def inspect_issue(jira, key, *, trial=None):
    """GET only. Output allowlisted fields; malformed properties never leak raw values."""
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
        checked = validated_property(issue, state)
        if trial is not None and trial != checked["trial_id"]:
            result.update(property="trial_mismatch", trial_match=False)
            return result
        delivery = checked["delivery"]
        root = delivery["root"]
        url = discussion_url(root) if root else None
        def intent(value):
            if value is None:
                return None
            return {k: value[k] for k in ("state", "outcome", "event_id", "retry_at") if k in value}
        result.update(property="valid", scope=checked["scope"], trial_id=checked["trial_id"],
                      trial_match=True if trial is not None else None, incident_identity=json.loads(checked["incident_key"]),
                      identity_match=True, recorded_outcome=checked["last_outcome"], root=root, discussion_url=url,
                      metadata_url=metadata_url(issue["key"]), metadata_note="Jira permissions required.",
                      visible_metadata_link=visible_link_status(issue["fields"].get("description"), metadata_url(issue["key"]), metadata=True),
                      visible_link=visible_link_status(issue["fields"].get("description"), url) if url else "no_confirmed_root",
                      jira_intent=intent(checked["jira_intent"]), slack_intent=intent(delivery["intent"]))
    except Exception:
        result["property"] = "malformed_or_unavailable"
    return result
