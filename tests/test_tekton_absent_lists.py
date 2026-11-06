"""Absence markers must not change identity-based list merge semantics."""

from copy import deepcopy

import pytest

from lib.tekton import ABSENT, _deep_merge, render_and_merge


@pytest.mark.parametrize("identity", ["name", "pipelineTaskName"])
@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("target_exists", [False, True])
def test_absent_items_do_not_change_keyed_merge(identity, position, target_exists):
    """Ignore markers anywhere in keyed templates without losing other entries."""
    existing = [{identity: "build", "timeout": "3h"}]
    if target_exists:
        existing.append({identity: "scan", "timeout": "1h", "keep": "unchanged"})
    original = deepcopy(existing)
    template = [{identity: "scan", "timeout": "20m"}, {identity: "test", "timeout": "10m"}]
    template.insert(position, ABSENT)
    original_template = deepcopy(template)

    result = _deep_merge(template, existing)

    assert [item[identity] for item in result] == ["build", "scan", "test"]
    assert result[0] == original[0]
    assert result[1]["timeout"] == "20m"
    if target_exists:
        assert result[1]["keep"] == "unchanged"
    assert result[2] == {identity: "test", "timeout": "10m"}
    assert existing == original
    assert template == original_template
    errors = []
    assert _deep_merge(template, result, errors) == result
    assert errors == []


@pytest.mark.parametrize("identity,field", [("pipelineTaskName", "taskRunSpecs"), ("name", "params")])
def test_rendered_absent_items_preserve_keyed_entries(tmp_path, identity, field):
    """Rendered list-item sentinels must not turn keyed merges into positional ones."""
    template_path = tmp_path / "list.yaml.j2"
    template_path.write_text('''spec:
  $FIELD:
    - "{{ ABSENT }}"
    - $KEY: scan
      value: new
    - "{{ ABSENT }}"
'''.replace("$FIELD", field).replace("$KEY", identity))
    existing = {"spec": {field: [
        {identity: "build", "value": "preserve"},
        {identity: "scan", "value": "old", "keep": "unchanged"},
    ]}}
    original = deepcopy(existing)

    result = render_and_merge(str(tmp_path), "list.yaml.j2", existing, "scan.yaml", mode="fix")

    assert result["spec"][field] == [
        {identity: "build", "value": "preserve"},
        {identity: "scan", "value": "new", "keep": "unchanged"},
    ]
    assert existing == original
    errors = []
    assert render_and_merge(
        str(tmp_path), "list.yaml.j2", result, "scan.yaml", mode="fix", errors=errors,
    ) == result
    assert errors == []
