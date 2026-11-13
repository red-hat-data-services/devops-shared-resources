"""Regression coverage for localized reconciliation and round-trip merge metadata."""
from io import StringIO
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from lib.tekton import ABSENT, _deep_merge
from lib.tekton_yaml import localized_yaml

FIXTURES = Path(__file__).parent / "fixtures/tekton/comment-preservation"
BUDGET = {"requests": {"cpu": "500m", "memory": "1Gi"},
          "limits": {"cpu": "500m", "memory": "1Gi"}}
OVERLAY = {"spec": {"taskRunSpecs": [{"pipelineTaskName": "rpms-signature-scan",
            "computeResources": ABSENT, "stepSpecs": [{"name": "rpms-signature-scan", "computeResources": BUDGET}]}]}}


def merge(source, overlay=OVERLAY):
    parser = YAML()
    parser.preserve_quotes = True
    return _deep_merge(overlay, parser.load(source))


@pytest.mark.parametrize("fixture", ["mlserver", "autogluon"])
def test_actual_pipelineruns_add_only_scan_budget(fixture):
    source = (FIXTURES / f"{fixture}.yaml").read_bytes()
    candidate = merge(source)
    output = localized_yaml(source, candidate)
    original = source.decode()
    prefix, suffix = original.split("status: {}")
    added = output[len(prefix):len(output) - len("status: {}" + suffix)]
    assert output == prefix + added + "status: {}" + suffix
    assert added.startswith("  taskRunSpecs:\n")
    assert "rpms-signature-scan" in added and "500m" in added
    assert "# konflux build manual retrigger" in output and output.endswith("#restart\n")
    assert YAML().load(output) == candidate
    assert localized_yaml(output, merge(output)) == output


def test_generic_merge_keeps_container_comments_and_styles():
    source = "# header\nitems: # items\n- name: scan # item\n  value: {cpu: '100m'} # flow\n# footer\n"
    candidate = merge(source, {"items": [{"name": "scan", "value": {"cpu": "500m"}}]})
    stream = StringIO()
    parser = YAML()
    parser.preserve_quotes = True
    parser.dump(candidate, stream)
    output = stream.getvalue()
    for comment in ("# header", "# items", "# item", "# flow", "# footer"):
        assert comment in output
    assert "{cpu: '500m'}" in output


@pytest.mark.parametrize("indent", [2, 4])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing", [True, False])
def test_task_budget_deletion_and_scalar_updates_preserve_other_bytes(indent, newline, trailing):
    source = """# retrigger café
kind: PipelineRun
nulls: [null, Null, NULL, ~, ] # untouched
spec:
  taskRunSpecs:
  - pipelineTaskName: other
    computeResources: {requests: {cpu: 20m}} # unrelated task
  - pipelineTaskName: rpms-signature-scan
    computeResources:
      requests: {cpu: 4, memory: 4Gi}
    stepSpecs:
    - name: init
      computeResources: {requests: {cpu: 20m}} # unrelated step
    - name: rpms-signature-scan
      computeResources:
        requests:
          cpu: '100m' # CPU comment
          memory: 200Mi
        limits:
          cpu: '100m'
          memory: 200Mi
status: {} # last
"""
    if indent == 4:
        source = "\n".join("  " + line if line.startswith(" ") else line for line in source.split("\n"))
    if not trailing:
        source = source.rstrip("\n")
    source = source.replace("\n", newline)
    expected = source.replace(" " * (indent + 2) + "computeResources:" + newline + " " * (indent + 4) + "requests: {cpu: 4, memory: 4Gi}" + newline, "")
    expected = expected.replace("'100m'", "'500m'").replace("memory: 200Mi", "memory: 1Gi")
    output = localized_yaml(source, merge(source))
    assert output == expected
    assert localized_yaml(output, merge(output)) == output


@pytest.mark.parametrize("old,new", [([{"name": "a"}, {"name": "b"}], [{"name": "b"}, {"name": "c"}]),
                                    ([1, 2, 3], [4, 2]), ([1], [1, 2])])
def test_list_insert_modify_delete(old, new):
    yaml = YAML()
    stream = StringIO()
    yaml.dump({"items": old, "untouched": None}, stream)
    source = stream.getvalue() + "# end\n"
    candidate = {"items": new, "untouched": None}
    output = localized_yaml(source, candidate)
    assert yaml.load(output) == candidate
    assert "untouched:" in output and output.endswith("# end\n")


def test_flow_collection_fallback_is_limited_to_changed_collection():
    source = "untouched: [1,\n    2, 3] # keep wrapping\nchanged: {cpu: 100m, memory: 1Gi} # budget comment\nlast: Null # spelling\n"
    output = localized_yaml(source, merge(source, {"changed": {"cpu": "500m", "memory": "1Gi"}}))
    assert output == source.replace("cpu: 100m", "cpu: 500m")


@pytest.mark.parametrize("source", ["a: &budget {cpu: 100m}\nb: *budget\n", "a: &loop [*loop]\n"])
def test_anchors_and_cycles_fail_closed_without_splicing_shared_spans(source):
    with pytest.raises(ValueError, match="anchors/aliases"):
        localized_yaml(source, {"a": {"cpu": "500m"}})


@pytest.mark.parametrize("value", [{}, []])
def test_removing_all_collection_members_keeps_empty_collection(value):
    source = "before: Null\nchanged:\n  old: value\nafter: ~\n" if isinstance(value, dict) else "before: Null\nchanged:\n- value\nafter: ~\n"
    output = localized_yaml(source, {"before": None, "changed": value, "after": None})
    assert YAML().load(output)["changed"] == value
    assert output.startswith("before: Null\n") and output.endswith("after: ~\n")


def test_end_of_file_insertion_preserves_missing_trailing_newline():
    assert localized_yaml("items:\n- a", {"items": ["a", "b"]}) == "items:\n- a\n- b"


@pytest.mark.parametrize("use_merge", [False, True])
def test_block_scalar_replacement_preserves_following_key_newline(use_merge):
    source = "before: Null\nchanged: |-\n  line1\n  line2\nafter: ~\n"
    candidate = merge(source, {"changed": "new\ntext"}) if use_merge else {"before": None, "changed": "new\ntext", "after": None}
    output = localized_yaml(source, candidate)
    assert YAML().load(output) == candidate
    assert output.startswith("before: Null\n") and output.endswith("after: ~\n")


@pytest.mark.parametrize("source,value,reason", [("changed: old\n", {"cpu": "500m"}, "collection shape"),
                                                ("changed: [1, 2]\n", [1, 3], "collection style")])
def test_unsupported_shape_or_unpreserved_flow_style_fails_closed(source, value, reason):
    with pytest.raises(ValueError, match=reason):
        localized_yaml(source, {"changed": value})


@pytest.mark.parametrize("style", ["literal", "folded", "quoted", "plain-wrapped"])
@pytest.mark.parametrize("indent", [2, 4])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiline_annotation_uses_key_indent_not_scalar_column(style, indent, newline):
    from ruamel.yaml.scalarstring import LiteralScalarString
    key = "pipelinesascode.tekton.dev/on-cel-expression"
    prefix = "# keep header\nmetadata:\n" + " " * indent + "annotations:\n"
    key_prefix = " " * (2 * indent) + key + ": "
    body_prefix = " " * (2 * indent + 2)
    old = {"literal": "|\n" + body_prefix + 'event == "push"\n',
           "folded": ">-\n" + body_prefix + 'event == "push"\n',
           "quoted": "'event == \"push\"'\n",
           "plain-wrapped": 'event == "push" &&\n' + body_prefix + 'target_branch == "release"\n'}[style]
    suffix = " " * indent + "labels: {keep: yes} # untouched\nfooter: Null\n"
    source = (prefix + key_prefix + old + suffix).replace("\n", newline)
    candidate = YAML().load(source)
    candidate["metadata"]["annotations"][key] = LiteralScalarString('event == "push"\n&& konflux_cluster == "stone-prod-p02"')
    output = localized_yaml(source, candidate)
    expected = (prefix + key_prefix + "|-\n" + body_prefix + 'event == "push"\n'
                + body_prefix + '&& konflux_cluster == "stone-prod-p02"\n' + suffix).replace("\n", newline)
    assert output == expected
    assert YAML().load(output) == candidate
    assert localized_yaml(output, candidate) == output


@pytest.mark.parametrize("indent", [0, 2])
def test_multiline_sequence_scalar_uses_dash_indent(indent):
    from ruamel.yaml.scalarstring import LiteralScalarString
    source = "values:\n" + " " * indent + "- old\nfooter: Null\n"
    candidate = {"values": [LiteralScalarString("new\ntext")], "footer": None}
    expected = ("values:\n" + " " * indent + "- |-\n"
                + " " * (indent + 2) + "new\n" + " " * (indent + 2) + "text\nfooter: Null\n")
    output = localized_yaml(source, candidate)
    assert output == expected
    assert YAML().load(output) == candidate
    assert localized_yaml(output, candidate) == output


@pytest.mark.parametrize("value", ["  leading spaces\ntext", "\nleading blank\ntext"])
def test_block_scalar_explicit_indent_preserves_content(value):
    from ruamel.yaml.scalarstring import LiteralScalarString
    source = "metadata:\n  annotations:\n    long-annotation-name: old\nfooter: Null\n"
    candidate = YAML().load(source)
    candidate["metadata"]["annotations"]["long-annotation-name"] = LiteralScalarString(value)
    output = localized_yaml(source, candidate)
    assert "long-annotation-name: |2-\n" in output
    assert YAML().load(output) == candidate
    assert localized_yaml(output, candidate) == output
    assert output.endswith("footer: Null\n")


def test_duplicate_keys_remain_rejected():
    with pytest.raises(Exception, match="duplicate key"):
        localized_yaml("a: 1\na: 2\n", {"a": 3})
