"""Conservative, read-only evidence for automatic retries (never gate authority)."""
from datetime import timedelta
from urllib.parse import quote

from lib.konflux_builds import (
    COMPONENT, NAME, PAC, SHA, KonfluxQueryError, _facts, _observation,
    _repository, _timestamp,
)


def component_push_branch(client, *, repo, component):
    """PRs often target stable; the exact Component owns its release push branch."""
    if not NAME.fullmatch(component):
        raise KonfluxQueryError("Invalid component identity")
    document = client._request_json(client.api_url +
        f"/apis/appstudio.redhat.com/v1alpha1/namespaces/{quote(client.namespace)}/components/{quote(component)}")
    meta = document.get("metadata") or {}
    source = ((document.get("spec") or {}).get("source") or {}).get("git") or {}
    branch = source.get("revision")
    if (document.get("kind") != "Component" or meta.get("name") != component
            or meta.get("namespace") != client.namespace or _repository(source.get("url")) != repo.lower()
            or not isinstance(branch, str) or not branch or SHA.fullmatch(branch)):
        raise KonfluxQueryError("Cannot verify the Component's release push branch")
    return branch.removeprefix("refs/heads/")

def recent_push_health(client, *, repo, component, branch, now, count=3):
    """Require the last three same-release push attempts to succeed within 7 days."""
    if not NAME.fullmatch(component) or not branch:
        raise KonfluxQueryError("Missing exact push component/branch identity")
    runs = client._list_resources(f"{COMPONENT}={component},{PAC}event-type=push")
    candidates = []
    for run in runs:
        meta, labels, annotations = _facts(run)
        if labels.get(COMPONENT) != component or labels.get(PAC + "event-type") != "push":
            continue
        repositories = {_repository(annotations.get(PAC + "repo-url"))}
        if labels.get(PAC + "url-org") and labels.get(PAC + "url-repository"):
            repositories.add((labels[PAC + "url-org"] + "/" + labels[PAC + "url-repository"]).lower())
        repositories.discard(None)
        branches = {str(v).removeprefix("refs/heads/") for v in (
            annotations.get(PAC + "branch"), annotations.get(PAC + "target-branch"),
            annotations.get(PAC + "source-branch"), labels.get(PAC + "target-branch")) if v}
        if repo.lower() not in repositories or branch not in branches:
            continue
        if repositories != {repo.lower()} or branches != {branch}:
            raise KonfluxQueryError("Ambiguous push build repository/branch")
        if (run.get("kind") != "PipelineRun" or meta.get("namespace") != client.namespace
                or labels.get("pipelines.appstudio.openshift.io/type") != "build"
                or labels.get(PAC + "pull-request") or annotations.get(PAC + "pull-request")):
            raise KonfluxQueryError("Ambiguous push build identity")
        if not NAME.fullmatch(str(meta.get("name", ""))) or not meta.get("uid"):
            raise KonfluxQueryError("Push build has no verified name/UID")
        shas = {str(v) for v in (labels.get(PAC + "sha"), annotations.get(PAC + "sha")) if v}
        for param in (run.get("spec") or {}).get("params", []):
            if param.get("name") == "revision" and SHA.fullmatch(str(param.get("value", ""))):
                shas.add(param["value"])
        if len(shas) != 1 or not SHA.fullmatch(next(iter(shas))):
            raise KonfluxQueryError("Unverified push revision")
        created = _timestamp(meta.get("creationTimestamp"))
        if created > now:
            raise KonfluxQueryError("Push build timestamp is in the future")
        if created < now - timedelta(days=7):
            continue
        candidates.append((created, _observation(run, next(iter(shas)), client.namespace, client.ui_url)))
    candidates.sort(key=lambda item: item[0], reverse=True)
    recent = candidates[:count]
    if len({item[0] for item in recent}) != len(recent):
        raise KonfluxQueryError("Ambiguous push attempt ordering")
    observations = [item[1].to_dict() for item in recent]
    healthy = len(recent) == count and all(item[1].status == "success" for item in recent)
    return {"healthy": healthy, "branch": branch, "runs": observations,
            "reason": "Last three recent push builds succeeded" if healthy else
                      "Push health unknown or recent same-release attempts did not all succeed"}


def failure_details(client, observation):
    """Use failed TaskRun conditions/steps; absence is unknown, never infra proof."""
    if not NAME.fullmatch(observation["name"]):
        raise KonfluxQueryError("Invalid failed PipelineRun name")
    tasks = client._list_resources("tekton.dev/pipelineRun=" + observation["name"], "taskruns")
    details = [observation.get("reason", ""), observation.get("message", "")]
    failed = 0
    for task in tasks:
        meta, _, _ = _facts(task)
        owners = meta.get("ownerReferences") or []
        if not any(o.get("kind") == "PipelineRun" and o.get("uid") == observation["uid"] for o in owners):
            continue
        if task.get("kind") != "TaskRun" or meta.get("namespace") != client.namespace:
            raise KonfluxQueryError("Invalid failed TaskRun identity")
        status = task.get("status") or {}
        conditions = [c for c in status.get("conditions", []) if c.get("type") == "Succeeded"]
        if len(conditions) != 1 or conditions[0].get("status") != "False":
            continue
        failed += 1
        details.extend([meta.get("name", ""), conditions[0].get("reason", ""), conditions[0].get("message", "")])
        for step in status.get("steps", []):
            terminated = step.get("terminated") or {}
            if terminated.get("exitCode", 0) != 0:
                details.extend([step.get("name", ""), terminated.get("reason", ""), terminated.get("message", "")])
    return "\n".join(str(value) for value in details)[:24000], failed > 0
