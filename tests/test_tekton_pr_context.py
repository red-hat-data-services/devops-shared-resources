"""Presentation-only PR context: repository roles, immutable links and safe text."""
from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

from lib import tekton_reconciliation as operation

POLICY = "a" * 40
WORKFLOW = "b" * 40
SHARED = "c" * 40
INVENTORY = "d" * 40


def example(module=None):
    return {
        "repository": "target-org/component",
        "changes": [{"status": "M", "path": ".tekton/update.yaml"},
                    {"status": "D", "path": ".tekton/remove.yml"}],
        "diagnostics": {"files": [{"status": "manual", "path": ".tekton/manual.yaml", "reason": "task budget"},
                                  {"status": "unresolved", "path": ".tekton/ref.yaml", "reason": "remote reference"}]},
        "provenance": {"policy_revision": POLICY, "policy_python": module,
                       "worker_revision": SHARED, "validator_revision": SHARED,
                       "publisher_revision": SHARED, "inventory_revision": INVENTORY,
                       "policy_digest": "f" * 64},
    }


def environment():
    return {"GITHUB_REPOSITORY": "red-hat-data-services/konflux-central",
            "GITHUB_WORKFLOW": "PipelineRun reconciliation",
            "GITHUB_WORKFLOW_REF": "red-hat-data-services/konflux-central/.github/workflows/reconcile.yml@refs/heads/main",
            "GITHUB_WORKFLOW_SHA": WORKFLOW, "GITHUB_SHA": "e" * 40,
            "GITHUB_RUN_ID": "123456", "GITHUB_SERVER_URL": "https://github.com"}


@pytest.mark.parametrize("profile,module", [("pipelineruns", "script/scan_resource_policy.py"), ("cleanup-odh-tenant", None)])
def test_consumer_profiles_link_actual_policy_and_workflow_roles(tmp_path, monkeypatch, profile, module):
    source = tmp_path / "policy"
    source.mkdir()
    calls = []
    def origin(root, *args):
        calls.append((root, args))
        return "git@github.com:red-hat-data-services/konflux-central.git"
    monkeypatch.setattr(operation, "git", origin)
    result = example(module)
    original = deepcopy(result)
    body = operation.summary(result, policy_source=source, config_dir=source / "tekton-templates" / profile, environ=environment())
    base = "https://github.com/red-hat-data-services/konflux-central"
    assert f"{base}/tree/{POLICY}/tekton-templates/{profile}" in body
    assert f"{base}/blob/{WORKFLOW}/.github/workflows/reconcile.yml" in body
    assert f"{base}/actions/runs/123456" in body
    assert "PipelineRun reconciliation" in body
    if module:
        assert f"{base}/blob/{POLICY}/{module}" in body
    else:
        assert "**Policy module:**" not in body
    assert "### Modified (1)" in body and "- .tekton/update.yaml" in body
    assert "### Deleted (1)" in body and "- .tekton/remove.yml" in body
    assert "Manual: 1; unresolved: 1." in body
    assert "task budget" in body and "remote reference" in body
    assert "non-fix validation and idempotence passed" in body
    assert f'"worker_revision": "{SHARED}"' in body
    assert f'"inventory_revision": "{INVENTORY}"' in body
    assert f"/tree/{SHARED}/" not in body and f"/tree/{INVENTORY}/" not in body
    assert "https://github.com/target-org/component" not in body
    assert calls == [(source, ("remote", "get-url", "origin"))]
    assert result == original


@pytest.mark.parametrize("origin", [None, "https://gitlab.example.com/group/policy.git", "https://user:secret@github.com/group/policy.git"])
def test_local_or_unknown_origin_falls_back_to_plain_path_and_sha(tmp_path, monkeypatch, origin):
    def read_origin(*args):
        if origin is None:
            raise subprocess.CalledProcessError(2, "git remote get-url origin")
        return origin
    monkeypatch.setattr(operation, "git", read_origin)
    body = operation.summary(example(), policy_source=tmp_path, config_dir=tmp_path / "templates", environ={})
    assert "Local run (GitHub Actions context unavailable)." in body
    assert "**Config directory:** templates" in body
    assert POLICY in body
    assert "https://" not in body
    assert "secret" not in body


def test_workflow_sha_fallback_only_for_same_runtime_workflow_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(operation, "git", lambda *args: "https://github.com/policy-owner/policy.git")
    env = environment()
    del env["GITHUB_WORKFLOW_SHA"]
    body = operation.summary(example(), policy_source=tmp_path, config_dir=tmp_path / "templates", environ=env)
    assert f"/blob/{env['GITHUB_SHA']}/.github/workflows/reconcile.yml" in body
    env["GITHUB_WORKFLOW_REF"] = "reusable-owner/workflows/.github/workflows/shared.yml@refs/heads/main"
    body = operation.summary(example(), policy_source=tmp_path, config_dir=tmp_path / "templates", environ=env)
    assert "exact commit unavailable" in body
    assert "/blob/" not in body
    assert "https://github.com/red-hat-data-services/konflux-central/actions/runs/123456" in body
    env["GITHUB_WORKFLOW_SHA"] = WORKFLOW
    body = operation.summary(example(), policy_source=tmp_path, config_dir=tmp_path / "templates", environ=env)
    assert f"https://github.com/reusable-owner/workflows/blob/{WORKFLOW}/.github/workflows/shared.yml" in body


@pytest.mark.parametrize("server", ["javascript:alert(1)", "https://github.com)/evil", "https://[", "https://user:pass@github.com", "https://github.com\n.evil"])
def test_unsafe_display_and_url_inputs_do_not_create_links(tmp_path, monkeypatch, server):
    monkeypatch.setattr(operation, "git", lambda *args: "https://github.com/good/policy.git")
    env = environment()
    env.update(GITHUB_WORKFLOW="Build\n[click](javascript:alert(1))<script>", GITHUB_SERVER_URL=server)
    data = example("../outside.py")
    data["changes"][0]["path"] = "bad](https://evil.test)/<img>.yaml"
    data["diagnostics"]["files"][0]["reason"] = "\n# heading [click](javascript:bad)"
    body = operation.summary(data, policy_source=tmp_path, config_dir=tmp_path / "templates", environ=env)
    assert "<script>" not in body and "<img>" not in body
    assert "](javascript:" not in body and "](https://evil.test)" not in body
    assert "\n# heading" not in body
    assert "/actions/runs/" not in body and f"/blob/{WORKFLOW}/" not in body
    assert f"/blob/{POLICY}/../outside.py" not in body
    assert "&lt;script&gt;" in body and "&lt;img&gt;" in body


def test_workflow_branch_with_at_sign_does_not_change_file_path(tmp_path, monkeypatch):
    monkeypatch.setattr(operation, "git", lambda *args: "https://github.com/good/policy.git")
    env = environment()
    env["GITHUB_WORKFLOW_REF"] = "red-hat-data-services/konflux-central/.github/workflows/reconcile.yml@refs/heads/feature@topic"
    body = operation.summary(example(), policy_source=tmp_path, config_dir=tmp_path / "templates", environ=env)
    assert f"/blob/{WORKFLOW}/.github/workflows/reconcile.yml)" in body
    assert "reconcile.yml%40refs" not in body


def test_link_paths_are_url_encoded(tmp_path, monkeypatch):
    monkeypatch.setattr(operation, "git", lambda *args: "https://github.com/good/policy.git")
    body = operation.summary(example("script/a b#c.py"), policy_source=tmp_path,
                             config_dir=tmp_path / "templates (reviewed)", environ={})
    assert f"/tree/{POLICY}/templates%20%28reviewed%29" in body
    assert f"/blob/{POLICY}/script/a%20b%23c.py" in body
