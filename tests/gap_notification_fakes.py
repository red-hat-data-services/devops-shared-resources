"""Opt-in GAP test builders and synchronous in-memory service fakes.

Fresh instances/journals per caller. These model facade-level policy and faults,
not HTTP serialization, eventual consistency, atomicity or concurrency. Leader
receipt builders are pure data; production imports occur only when used.
"""
import copy

LEADER_URL = "https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/84"

def observation(status="failure", oid="run-1", **changes):
    value = dict(environment="stage-redhat.atlassian.net/RHAI/C0883L8FARE", repository="acme/repo",
                 source_branch="main", destination_branch="stable", category="build", component="cpu",
                 status=status, observation_id=oid, evidence={"test_only": True, "synthetic": True})
    value.update(changes)
    return value


class Jira:
    def __init__(self):
        self.issues, self.properties, self.calls = {}, {}, []
        self.fail_create = self.fail_readback = self.fail_receipt = False

    def find_by_label(self, label):
        return [copy.deepcopy(i) for i in self.issues.values() if label in i["fields"]["labels"]]

    def get_issue(self, key):
        return copy.deepcopy(self.issues[key])

    def get_notification_state(self, key):
        return copy.deepcopy(self.properties.get(key))

    def put_notification_state(self, key, value):
        if self.fail_receipt and value["delivery"]["root"]:
            raise TimeoutError("receipt persistence unavailable")
        self.properties[key] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def create_bug(self, summary, description, labels, *, notification_state, extra_fields=None):
        self.calls.append("create")
        if self.fail_create:
            raise TimeoutError("SECRET")
        key = "RHAI-" + str(len(self.issues) + 1)
        self.issues[key] = {"key": key, "fields": {"summary": summary, "project": {"key": "RHAI"},
            "issuetype": {"name": "Bug", "id": "10016"}, "description": description,
            "labels": labels, "status": {"statusCategory": {"key": "new"}}}}
        self.issues[key]['fields'].update(copy.deepcopy(extra_fields or {}))
        self.properties[key] = copy.deepcopy(notification_state)
        if self.fail_readback:
            error = TimeoutError("SECRET")
            error.key = key
            raise error
        return {"key": key, "url": "https://stage-redhat.atlassian.net/browse/" + key}

    def update_issue(self, key, **fields):
        self.calls.append("update")
        assert not self.closed(key)
        additions = fields.pop("add_labels", [])
        self.issues[key]["fields"]["labels"] = sorted(set(self.issues[key]["fields"]["labels"]) | set(additions))
        self.issues[key]["fields"].update(fields)
        return self.get_issue(key)

    def closed(self, key):
        return self.issues[key]["fields"]["status"]["statusCategory"]["key"] == "done"

    def close(self, key):
        self.issues[key]["fields"]["status"]["statusCategory"]["key"] = "done"

    def transition_to_done(self, key, transition_id, *, fields=None):
        self.calls.append(("close", transition_id, fields))
        self.close(key)
        return self.get_issue(key)


class Slack:
    def __init__(self):
        self.calls, self.fail = [], False

    def send(self, text, **kwargs):
        CHANNEL = "C0883L8FARE"
        self.calls.append((text, kwargs))
        if self.fail:
            raise TimeoutError("SECRET")
        return {"channel": CHANNEL, "ts": f"123.{len(self.calls):06}"}


def make_runner(tmp_path):
    from lib.gap_notifications.dispatch_incidents import Dispatcher, Ledger
    jira, slack = Jira(), Slack()
    def runner(name="ledger", trial="trial"):
        return Dispatcher(Ledger(tmp_path / (name + ".json")), jira, slack,
            trial_id=trial, transition_id="41", transition_fields={"resolution": {"id": "10000"}})
    return runner, jira, slack


def state(*, repo="acme/demo", pr_url="https://github.com/acme/demo/pull/42",
          source_branch="feature/x", target_branch="main", pr_status="build-failure",
          pr_state="OPEN", head_sha="a" * 40, observed_at="2026-11-06T15:12:46.242781+00:00",
          expected_components=None, runs=None, query_error=None, query_skipped=None,
          gate_reason="odm-demo failure"):
    return {
        "pull-requests": [{
            "repo": "demo",
            "pr-url": pr_url,
            "pr-status": pr_status,
            "builds": [],
            "build-observations": {
                "source": "konflux",
                "repository": repo,
                "pr": 42,
                "head-sha": head_sha,
                "observed-at": observed_at,
                "expected-components": list(expected_components or []),
                "runs": list(runs or []),
                **({"query-error": query_error} if query_error else {}),
                **({"query-skipped": query_skipped} if query_skipped else {}),
                "pr-state": pr_state,
                "gate-reason": gate_reason,
            },
        }],
        "creator-outcomes": [{
            "repo": repo,
            "source-branch": source_branch,
            "target-branch": target_branch,
            "outcome": "success",
            "reason": "creator snapshot",
            "pr-url": pr_url,
            "workflow-url": "https://github.com/acme/workflows/actions/runs/1",
            "observed-at": observed_at,
        }],
    }


def run(component, status, *, reason="Completed", head_sha="a" * 40, uid=None, created_at="2026-11-06T14:38:29Z"):
    return {
        "component": component,
        "status": status,
        "reason": reason,
        "head_sha": head_sha,
        "name": f"{component}-pipeline",
        "uid": uid or f"uid-{component}",
        "namespace": "rhoai-tenant",
        "created_at": created_at,
        "url": f"https://konflux.example/ns/rhoai-tenant/pipelinerun/{component}",
    }
