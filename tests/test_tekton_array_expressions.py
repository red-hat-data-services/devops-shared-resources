"""Opt-in native arrays leave legacy scalar template rendering unchanged."""
from copy import deepcopy

import pytest

from lib.tekton import _deep_merge, render_and_merge
from lib.tekton_filters import DELETE_FILE_PREFIX, append_unique, exclude_values, replace_prefix


def render(tmp_path, expression, value=None, present=True, mode="fix", errors=None):
    (tmp_path / "array.yaml.j2").write_text(
        'spec:\n  params:\n    - name: items\n      value: "' + expression + '"\n'
    )
    data = {"spec": {"params": [{"name": "items", "value": value}] if present else []}}
    original = deepcopy(data)
    result = render_and_merge(str(tmp_path), "array.yaml.j2", data, "sample.yaml", mode=mode, errors=errors)
    assert data == original
    return result["spec"]["params"]


def test_native_array_references_preserve_strings_and_other_entries(tmp_path):
    values = ["true", "001", "3.5", "version=old", "io.openshift.tags=keep"]
    result = render(tmp_path, "{{ _ | replace_prefix('version=', 'version=new') | as_array }}", values)
    assert result[0]["value"] == ["true", "001", "3.5", "version=new", "io.openshift.tags=keep"]
    assert all(isinstance(item, str) for item in result[0]["value"])


def test_native_array_optional_absence(tmp_path):
    assert render(tmp_path, "{{ _? | append_unique('tag') | as_array }}", present=False) == []


@pytest.mark.parametrize("values", [[], ["existing"], ["tag", "existing"]])
def test_append_unique_preserves_order_and_is_idempotent(tmp_path, values):
    expression = "{{ params.get('items', []) | append_unique('tag') | as_array }}"
    expected = values if "tag" in values else values + ["tag"]
    first = render(tmp_path, expression, values)[0]["value"]
    assert first == expected
    errors = []
    assert render(tmp_path, expression, first, errors=errors)[0]["value"] == expected
    assert errors == []


def test_missing_array_creation(tmp_path):
    result = render(tmp_path, "{{ params.get('items', []) | append_unique('tag') | as_array }}", present=False)
    assert result == [{"name": "items", "value": ["tag"]}]


def test_array_growth_is_detected_in_validate_mode(tmp_path):
    errors = []
    render(tmp_path, "{{ _ | append_unique('tag') | as_array }}", ["existing"], mode="validate", errors=errors)
    assert errors  # --fix must run even when appending is the only required change.


def test_array_exclusion_and_file_deletion(tmp_path):
    expression = "{{ _ | exclude_values(['power', 'z'], delete_if_empty=true) | as_array }}"
    assert render(tmp_path, expression, ["x86", "power", "z"])[0]["value"] == ["x86"]
    errors = []
    render(tmp_path, expression, ["power", "z"], errors=errors)
    assert any(error.startswith(DELETE_FILE_PREFIX) for error in errors)


@pytest.mark.parametrize("value", ["not-an-array", None, [42]])
def test_array_filters_reject_malformed_values(value):
    for function, args in [(append_unique, ("tag",)), (exclude_values, (["x"],)), (replace_prefix, ("v=", "v=new"))]:
        with pytest.raises(ValueError, match="array of strings"):
            function(value, *args)


def test_scalar_rendering_is_not_native(tmp_path):
    assert render(tmp_path, "{{ '001' }}", "old")[0]["value"] == "001"
    assert render(tmp_path, "{{ 'true' }}", "old")[0]["value"] == "true"


def test_native_parent_array(tmp_path):
    (tmp_path / 'array.yaml.j2').write_text('''spec:
  params:
    - name: items
      value: "{{ _parent | append_unique('new') | as_array }}"
''')
    data = {'spec': {'params': [{'name': 'items', 'value': ['child']}]}}
    parent = {'spec': {'params': [{'name': 'items', 'value': ['001', 'true']}]}}
    result = render_and_merge(str(tmp_path), 'array.yaml.j2', data, 'sample.yaml',
                              parent_data=parent, mode='fix')
    assert result['spec']['params'][0]['value'] == ['001', 'true', 'new']


def test_skip_validation_preserves_native_array(tmp_path):
    (tmp_path / 'array.yaml.j2').write_text('''spec:
  params:
    - name: items
      value: "{{ _ | skip_for(['sample']) | append_unique('new') | as_array }}"
''')
    data = {'metadata': {'labels': {'appstudio.openshift.io/component': 'sample'}},
            'spec': {'params': [{'name': 'items', 'value': ['keep']}]}}
    errors = []
    result = render_and_merge(str(tmp_path), 'array.yaml.j2', data, 'sample.yaml', errors=errors)
    assert result['spec']['params'][0]['value'] == ['keep']
    assert not errors


def test_plain_list_growth_records_validation_error():
    errors = []
    assert _deep_merge(["one", "two"], ["one"], errors) == ["one", "two"]
    assert errors == ["[1]: missing"]
