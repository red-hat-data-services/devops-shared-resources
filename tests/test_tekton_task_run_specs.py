"""Regression coverage for identity-based TaskRun override reconciliation."""

from copy import deepcopy

import pytest

from lib.tekton import ABSENT, _deep_merge, _lookup_path, render_and_merge


def test_task_run_specs_merge_by_identity_not_position():
    """Reconcile only the matching task/step without mutation or repeated drift."""
    existing = [
        {"pipelineTaskName": "build", "timeout": "3h"},
        {"pipelineTaskName": "rpms-signature-scan", "timeout": "20m", "stepSpecs": [
            {"name": "output-results", "computeResources": {"requests": {"memory": "64Mi"}}},
            {"name": "rpms-signature-scan", "computeResources": {
                "requests": {"cpu": "2600m", "memory": "1Gi"},
                "limits": {"cpu": "2600m", "memory": "1Gi"},
            }},
        ]},
        {"pipelineTaskName": "test", "timeout": "1h"},
    ]
    original = deepcopy(existing)
    template = [{"pipelineTaskName": "rpms-signature-scan", "stepSpecs": [
        {"name": "rpms-signature-scan", "computeResources": {"limits": {"cpu": ABSENT}}},
    ]}]
    errors = []
    result = _deep_merge(template, existing, errors)
    assert [x["pipelineTaskName"] for x in result] == ["build", "rpms-signature-scan", "test"]
    assert result[0] == existing[0]
    assert result[2] == existing[2]
    assert result[1]["timeout"] == "20m"
    assert result[1]["stepSpecs"][0] == existing[1]["stepSpecs"][0]
    scan = result[1]["stepSpecs"][1]["computeResources"]
    assert scan == {"requests": {"cpu": "2600m", "memory": "1Gi"}, "limits": {"memory": "1Gi"}}
    assert existing == original
    assert errors == ["[rpms-signature-scan].stepSpecs[rpms-signature-scan].computeResources.limits.cpu: should not exist"]
    second_errors = []
    assert _deep_merge(template, result, second_errors) == result
    assert second_errors == []


def test_missing_task_override_appends_without_dropping_existing():
    """Append a new task override and preserve unrelated entries in place."""
    existing = [{"pipelineTaskName": "build", "timeout": "3h"}]
    template = [{"pipelineTaskName": "rpms-signature-scan", "timeout": "20m"}]
    errors = []
    assert _deep_merge(template, existing, errors) == existing + template
    assert errors == ["[rpms-signature-scan]: missing"]


@pytest.mark.parametrize("existing", [{}, {"spec": {}}, {"spec": {"taskRunSpecs": []}},
    {"spec": {"taskRunSpecs": [{"pipelineTaskName": "build", "timeout": "3h"}]}}])
def test_new_nested_overrides_do_not_emit_absent_sentinel(existing):
    """Strip absence markers at every newly created level, idempotently."""
    template = {"spec": {"taskRunSpecs": [{
        "pipelineTaskName": "rpms-signature-scan", "stepSpecs": [{
            "name": "rpms-signature-scan", "computeResources": {
                "requests": {"cpu": "250m", "memory": "512Mi"},
                "limits": {"cpu": ABSENT, "memory": "512Mi"},
            },
        }],
    }]}}
    result = _deep_merge(template, existing)
    scan = next(t for t in result["spec"]["taskRunSpecs"] if t["pipelineTaskName"] == "rpms-signature-scan")
    assert scan["stepSpecs"][0]["computeResources"]["limits"] == {"memory": "512Mi"}
    assert ABSENT not in repr(result)
    assert _deep_merge(template, result) == result


def test_lookup_nested_task_and_step_identity():
    """Resolve a nested resource by task and step identity rather than position."""
    data = {"spec": {"taskRunSpecs": [
        {"pipelineTaskName": "build"},
        {"pipelineTaskName": "rpms-signature-scan", "stepSpecs": [
            {"name": "output-results"},
            {"name": "rpms-signature-scan", "computeResources": {"requests": {"memory": "1Gi"}}},
        ]},
    ]}}
    path = ("spec", "taskRunSpecs", "rpms-signature-scan", "stepSpecs", "rpms-signature-scan", "computeResources", "requests", "memory")
    assert _lookup_path(data, path) == "1Gi"
    assert _lookup_path(data, ("spec", "taskRunSpecs", "missing")) is None


def test_template_self_reference_uses_matching_task_and_step(tmp_path):
    """Preserve the matching step's value when rendering a self-reference."""
    (tmp_path / "resources.yaml.j2").write_text('''spec:
  taskRunSpecs:
    - pipelineTaskName: rpms-signature-scan
      stepSpecs:
        - name: rpms-signature-scan
          computeResources:
            requests:
              memory: "{{ _ }}"
            limits:
              cpu: "{{ ABSENT }}"
''')
    existing = {"spec": {"taskRunSpecs": [
        {"pipelineTaskName": "build", "timeout": "3h"},
        {"pipelineTaskName": "rpms-signature-scan", "stepSpecs": [
            {"name": "output-results"},
            {"name": "rpms-signature-scan", "computeResources": {
                "requests": {"memory": "1Gi"}, "limits": {"cpu": "200m"},
            }},
        ]},
    ]}}
    errors = []
    result = render_and_merge(str(tmp_path), "resources.yaml.j2", existing, "scan.yaml", mode="fix", errors=errors)
    overrides = result["spec"]["taskRunSpecs"]
    assert overrides[0] == existing["spec"]["taskRunSpecs"][0]
    assert overrides[1]["stepSpecs"][0] == {"name": "output-results"}
    resources = overrides[1]["stepSpecs"][1]["computeResources"]
    assert resources["requests"]["memory"] == "1Gi"
    assert "cpu" not in resources["limits"]
