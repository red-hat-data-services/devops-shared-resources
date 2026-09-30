from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

from lib.tekton import (
    ABSENT,
    _SELF_REF_RE,
    _deep_merge,
    _lookup_path,
    _preserve_type,
    classify_pipeline,
    extract_component_prefix,
    get_param,
    render_and_merge,
    resolve_overlay,
)

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "tekton"
TEMPLATES = REPO_ROOT / "embargo-templates"


def _ruamel_load(name):
    from ruamel.yaml import YAML
    ruamel = YAML()
    ruamel.preserve_quotes = True
    with open(FIXTURES / name) as f:
        return ruamel.load(f)


def _ruamel_roundtrip(data):
    from io import StringIO
    from ruamel.yaml import YAML
    ruamel = YAML()
    ruamel.preserve_quotes = True
    ruamel.width = 4096
    buf = StringIO()
    ruamel.dump(data, buf)
    return buf.getvalue()


# --- classify_pipeline ---


def test_classify_pr_from_annotation():
    data = {"metadata": {"annotations": {"pipelinesascode.tekton.dev/on-event": "[pull_request]"}, "name": "x"}}
    assert classify_pipeline("x-pull-request.yaml", data) == "pull_request"


def test_classify_push_from_name():
    data = {"metadata": {"annotations": {}, "name": "odh-dashboard-v3-5-on-push"}}
    assert classify_pipeline("odh-dashboard-v3-5-push.yaml", data) == "push"


def test_classify_scheduled():
    data = {"metadata": {"annotations": {"pipelinesascode.tekton.dev/on-cel-expression": 'event == "push"'}, "name": "x-on-schedule"}}
    assert classify_pipeline("x-scheduled.yaml", data) == "scheduled"


def test_classify_pr_from_name():
    data = {"metadata": {"annotations": {}, "name": "odh-dashboard-on-pull-request-12345"}}
    assert classify_pipeline("odh-dashboard-pull-request.yaml", data) == "pull_request"


def test_classify_push_in_component_name_pr():
    """A component named 'push-notifications' with pull-request in metadata.name should be classified as pull_request."""
    data = {"metadata": {"annotations": {}, "name": "push-notifications-on-pull-request"}}
    assert classify_pipeline("push-notifications-pull-request.yaml", data) == "pull_request"


def test_classify_unknown():
    data = {"metadata": {"annotations": {}, "name": "x"}}
    assert classify_pipeline("component.yaml", data) == "unknown"


# --- extract_component_prefix ---


def test_component_prefix_versioned_push():
    assert extract_component_prefix("odh-dashboard-v3-5-push.yaml") == "odh-dashboard"


def test_component_prefix_pull_request():
    assert extract_component_prefix("odh-dashboard-pull-request.yaml") == "odh-dashboard"


def test_component_prefix_plain_push():
    assert extract_component_prefix("odh-dashboard-push.yaml") == "odh-dashboard"


def test_component_prefix_ea_versioned():
    assert extract_component_prefix("odh-component-v3-5-ea-2-on-push.yaml") == "odh-component"


def test_component_prefix_scheduled():
    assert extract_component_prefix("odh-operator-v3-5-on-schedule.yaml") == "odh-operator"


def test_component_prefix_on_push_no_version():
    assert extract_component_prefix("odh-dashboard-on-push.yaml") == "odh-dashboard"


def test_component_prefix_multi_dash_component():
    assert extract_component_prefix("odh-model-controller-v3-5-push.yaml") == "odh-model-controller"


def test_component_prefix_multi_dash_pr():
    assert extract_component_prefix("odh-model-controller-pull-request.yaml") == "odh-model-controller"


def test_component_prefix_v_in_component_name():
    """Component names with -vN-N are ambiguous; the regex treats the first -vN-N as the version."""
    assert extract_component_prefix("my-v3-5-push.yaml") == "my"


def test_component_prefix_versioned_pull_request():
    assert extract_component_prefix("odh-mod-arch-model-registry-v2-25-pull-request.yaml") == "odh-mod-arch-model-registry"


def test_component_prefix_versioned_pull_request_short():
    assert extract_component_prefix("odh-dashboard-v3-5-pull-request.yaml") == "odh-dashboard"


def test_component_prefix_on_pull_request():
    assert extract_component_prefix("odh-kserve-autogluon-on-pull-request.yaml") == "odh-kserve-autogluon"


def test_component_prefix_from_metadata_name_push():
    data = {"metadata": {"name": "odh-kserve-autogluon-server-v3-4-on-push"}}
    assert extract_component_prefix("odh-kserve-autogluon-server-v3-4-push.yaml", data) == "odh-kserve-autogluon-server"


def test_component_prefix_from_metadata_name_pr():
    data = {"metadata": {"name": "odh-kserve-autogluon-on-pull-request"}}
    assert extract_component_prefix("odh-kserve-autogluon-on-pull-request.yaml", data) == "odh-kserve-autogluon"


def test_component_prefix_from_metadata_name_schedule():
    data = {"metadata": {"name": "odh-operator-v2-25-on-schedule"}}
    assert extract_component_prefix("odh-operator-v2-25-scheduled.yaml", data) == "odh-operator"


def test_component_prefix_from_metadata_name_pr_with_suffix():
    data = {"metadata": {"name": "odh-training-operator-on-pull-request-21567"}}
    assert extract_component_prefix("whatever.yaml", data) == "odh-training-operator"


def test_component_prefix_no_match():
    assert extract_component_prefix("random-file.yaml") is None


# --- get_param ---


def test_get_param_found():
    data = {"spec": {"params": [{"name": "git-url", "value": "foo"}, {"name": "revision", "value": "bar"}]}}
    assert get_param(data, "git-url") == "foo"
    assert get_param(data, "revision") == "bar"


def test_get_param_missing():
    data = {"spec": {"params": [{"name": "git-url", "value": "foo"}]}}
    assert get_param(data, "nonexistent") is None


def test_get_param_no_spec():
    assert get_param({}, "git-url") is None


# --- _preserve_type ---


def test_preserve_type_plain_string():
    assert type(_preserve_type("foo", "bar")) is str


def test_preserve_type_single_quoted():
    from ruamel.yaml.scalarstring import SingleQuotedScalarString
    result = _preserve_type(SingleQuotedScalarString("foo"), "bar")
    assert isinstance(result, SingleQuotedScalarString)
    assert str(result) == "bar"


def test_preserve_type_double_quoted():
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString
    result = _preserve_type(DoubleQuotedScalarString("foo"), "bar")
    assert isinstance(result, DoubleQuotedScalarString)


def test_preserve_type_literal_block():
    from ruamel.yaml.scalarstring import LiteralScalarString
    result = _preserve_type(LiteralScalarString("foo\n"), "bar\n")
    assert isinstance(result, LiteralScalarString)


# --- _deep_merge ---


def test_deep_merge_dict_preserves_existing_order():
    existing = {"b": 1, "a": 2, "c": 3}
    template = {"a": 99, "d": 4}
    result = _deep_merge(template, existing)
    assert list(result.keys()) == ["b", "a", "c", "d"]
    assert result["a"] == 99
    assert result["d"] == 4


def test_deep_merge_named_list():
    existing = [{"name": "x", "value": "old"}, {"name": "y", "value": "keep"}]
    template = [{"name": "x", "value": "new"}, {"name": "z", "value": "added"}]
    result = _deep_merge(template, existing)
    assert len(result) == 3
    assert result[0]["name"] == "x" and result[0]["value"] == "new"
    assert result[1]["name"] == "y" and result[1]["value"] == "keep"
    assert result[2]["name"] == "z" and result[2]["value"] == "added"


def test_deep_merge_preserves_scalar_type():
    from ruamel.yaml.scalarstring import SingleQuotedScalarString
    existing = {"key": SingleQuotedScalarString("old")}
    template = {"key": "new"}
    result = _deep_merge(template, existing)
    assert isinstance(result["key"], SingleQuotedScalarString)
    assert str(result["key"]) == "new"


def test_deep_merge_preserves_unchanged_value():
    from ruamel.yaml.scalarstring import SingleQuotedScalarString
    existing = {"key": SingleQuotedScalarString("same")}
    template = {"key": "same"}
    result = _deep_merge(template, existing)
    assert result["key"] is existing["key"]


def test_deep_merge_recursive():
    existing = {"a": {"b": {"c": "old"}}}
    template = {"a": {"b": {"c": "new", "d": "added"}}}
    result = _deep_merge(template, existing)
    assert result["a"]["b"]["c"] == "new"
    assert result["a"]["b"]["d"] == "added"


def test_deep_merge_reports_mismatch():
    errors = []
    _deep_merge({"key": "expected"}, {"key": "actual"}, errors)
    assert any("expected" in e and "actual" in e for e in errors)


def test_deep_merge_reports_missing_key():
    errors = []
    _deep_merge({"new_key": "val"}, {}, errors)
    assert any("new_key" in e and "missing" in e for e in errors)


def test_deep_merge_reports_missing_named_item():
    errors = []
    template = [{"name": "new-param", "value": "val"}]
    _deep_merge(template, [], errors)
    assert any("new-param" in e and "missing" in e for e in errors)


def test_deep_merge_absent_removes_key():
    from lib.tekton import ABSENT
    result = _deep_merge({"keep": "yes", "remove": ABSENT}, {"keep": "yes", "remove": "old"})
    assert "keep" in result
    assert "remove" not in result


def test_deep_merge_absent_reports_error():
    errors = []
    _deep_merge({"remove": ABSENT}, {"remove": "old"}, errors)
    assert any("should not exist" in e for e in errors)


def test_deep_merge_absent_no_error_when_already_absent():
    errors = []
    result = _deep_merge({"remove": ABSENT}, {}, errors)
    assert "remove" not in result
    assert not any("should not exist" in e for e in errors)


def test_deep_merge_reports_mismatch_int_vs_string():
    errors = []
    _deep_merge({"key": "true"}, {"key": True}, errors)
    assert any("expected" in e for e in errors)


def test_deep_merge_no_error_when_int_matches_string():
    errors = []
    result = _deep_merge({"key": "3"}, {"key": 3}, errors)
    assert not errors
    assert result["key"] == 3


def test_deep_merge_no_error_when_string_matches_int():
    errors = []
    result = _deep_merge({"key": "5"}, {"key": 5}, errors)
    assert not errors
    assert result["key"] == 5


def test_deep_merge_plain_list_drops_extra_existing_items_with_error():
    existing = ["tag1", "tag2", "tag3"]
    template = ["new-tag"]
    errors = []
    result = _deep_merge(template, existing, errors)
    assert len(result) == 1
    assert result[0] == "new-tag"
    assert any("extra" in e for e in errors)


# --- _lookup_path ---


def test_lookup_path_simple_dict():
    data = {"metadata": {"name": "foo"}}
    assert _lookup_path(data, ("metadata", "name")) == "foo"


def test_lookup_path_nested():
    data = {"a": {"b": {"c": "deep"}}}
    assert _lookup_path(data, ("a", "b", "c")) == "deep"


def test_lookup_path_params_list():
    data = {"spec": {"params": [{"name": "git-url", "value": "bar"}]}}
    assert _lookup_path(data, ("spec", "params", "git-url", "value")) == "bar"


def test_lookup_path_missing_key():
    assert _lookup_path({"a": 1}, ("b",)) is None


def test_lookup_path_missing_param():
    data = {"spec": {"params": [{"name": "x", "value": "y"}]}}
    assert _lookup_path(data, ("spec", "params", "nonexistent", "value")) is None


def test_lookup_path_pipelineref_params():
    data = {"spec": {"pipelineRef": {"params": [{"name": "url", "value": "https://example.com"}]}}}
    assert _lookup_path(data, ("spec", "pipelineRef", "params", "url", "value")) == "https://example.com"


# --- _SELF_REF_RE ---


def test_self_ref_re_matches_bare_underscore():
    assert _SELF_REF_RE.search("{{ _ }}").group(0) == "_"


def test_self_ref_re_matches_parent():
    assert _SELF_REF_RE.search("{{ _parent }}").group(0) == "_parent"


def test_self_ref_re_no_match_inside_identifier():
    assert _SELF_REF_RE.search("{{ my_parent }}") is None


def test_self_ref_re_no_match_underscore_prefix():
    assert _SELF_REF_RE.search("{{ _parent_thing }}") is None


def test_self_ref_re_matches_in_filter_chain():
    m = _SELF_REF_RE.search("{{ _ | split_tag | to_private_quay }}")
    assert m.group(0) == "_"


def test_self_ref_re_matches_parent_in_filter_chain():
    m = _SELF_REF_RE.search("{{ _parent | split_tag }}")
    assert m.group(0) == "_parent"


def test_self_ref_re_matches_optional_parent():
    m = _SELF_REF_RE.search("{{ _parent? }}")
    assert m.group(0) == "_parent?"
    assert m.group(1) == "_parent"
    assert m.group(2) == "?"


def test_self_ref_re_matches_optional_self():
    m = _SELF_REF_RE.search("{{ _? }}")
    assert m.group(0) == "_?"
    assert m.group(1) == "_"
    assert m.group(2) == "?"


def test_self_ref_re_optional_parent_in_filter_chain():
    m = _SELF_REF_RE.search("{{ _parent? | must_be_version }}")
    assert m.group(1) == "_parent"
    assert m.group(2) == "?"


def test_self_ref_re_non_optional_groups():
    """Non-optional refs have empty group(2)."""
    m = _SELF_REF_RE.search("{{ _parent }}")
    assert m.group(1) == "_parent"
    assert m.group(2) == ""
    m2 = _SELF_REF_RE.search("{{ _ }}")
    assert m2.group(1) == "_"
    assert m2.group(2) == ""


# --- render_and_merge (template engine) ---


def test_render_push_preserves_key_fields():
    data = _ruamel_load("valid-push.yaml")
    result = render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="fix")
    assert result["metadata"]["namespace"] == "rhoai-private-tenant"
    assert result["metadata"]["name"] == data["metadata"]["name"]
    assert get_param(result, "dockerfile") == get_param(data, "dockerfile")


def test_render_push_fixes_output_image():
    data = _ruamel_load("valid-push.yaml")
    for p in data["spec"]["params"]:
        if p["name"] == "output-image":
            p["value"] = "quay.io/rhoai/odh-dashboard-rhel9:{{target_branch}}"
    result = render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="fix")
    assert "rhoai-private" in str(get_param(result, "output-image"))


def test_render_push_validates_output_image():
    data = _ruamel_load("valid-push.yaml")
    for p in data["spec"]["params"]:
        if p["name"] == "output-image":
            p["value"] = "quay.io/rhoai/odh-dashboard-rhel9:{{target_branch}}"
    errors = []
    render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="validate", errors=errors)
    assert any("rhoai-private" in e for e in errors)


def test_render_push_validates_namespace():
    data = _ruamel_load("valid-push.yaml")
    data["metadata"]["namespace"] = "rhoai-tenant"
    errors = []
    render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="validate", errors=errors)
    assert any("rhoai-private-tenant" in e for e in errors)


def test_render_push_validates_missing_param():
    data = _ruamel_load("valid-push.yaml")
    data["spec"]["params"] = [p for p in data["spec"]["params"] if p["name"] != "disable-slack-notifications"]
    errors = []
    render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="validate", errors=errors)
    assert any("disable-slack-notifications" in e and "missing" in e for e in errors)


def test_render_push_preserves_component_params():
    data = _ruamel_load("valid-push.yaml")
    result = render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="fix")
    assert get_param(result, "dockerfile") == get_param(data, "dockerfile")


def test_render_push_preserves_existing_order():
    data = _ruamel_load("valid-push.yaml")
    result = render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="fix")
    existing_names = [str(p["name"]) for p in data["spec"]["params"]]
    result_names = [str(p["name"]) for p in result["spec"]["params"]]
    for name in existing_names:
        assert name in result_names


def test_render_push_preserves_scalar_types():
    data = _ruamel_load("valid-push.yaml")
    result = render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="fix")
    output = _ruamel_roundtrip(result)
    for line in output.splitlines():
        if "build.appstudio.openshift.io/repo:" in line:
            assert line.strip().startswith("build.appstudio"), f"repo annotation should be unquoted: {line}"
            break


def test_render_pr_fixes_from_push():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    pr["metadata"]["labels"]["appstudio.openshift.io/application"] = "automation"
    pr["metadata"]["labels"]["appstudio.openshift.io/component"] = "pull-request-pipelines-odh-dashboard"
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    assert result["metadata"]["labels"]["appstudio.openshift.io/application"] == \
        push["metadata"]["labels"]["appstudio.openshift.io/application"]


def test_render_pr_output_image_from_push():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    output_image = str(get_param(result, "output-image"))
    assert "on-pr-{{pull_request_number}}" in output_image
    assert "rhoai-private" in output_image


def test_render_pr_preserves_component_params():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    assert get_param(result, "dockerfile") == get_param(pr, "dockerfile")


def test_render_pr_strips_on_label():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    assert "pipelinesascode.tekton.dev/on-label" not in result["metadata"]["annotations"]


def test_render_pr_strips_on_target_branch():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    assert "pipelinesascode.tekton.dev/on-target-branch" not in result["metadata"]["annotations"]


def test_render_pr_strips_on_event():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    assert "pipelinesascode.tekton.dev/on-event" not in result["metadata"]["annotations"]


def test_render_pr_adds_cel_from_push():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    cel = str(result["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"])
    assert 'event == "pull_request"' in cel
    assert 'event == "push"' not in cel
    assert 'target_branch == "rhoai-3.5"' in cel


def test_render_pr_cel_swaps_pathchanged_filename():
    """When push CEL has a .pathChanged() ref, it should be rewritten to the PR filename."""
    push = _ruamel_load("valid-push.yaml")
    push["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"] = (
        'event == "push" && target_branch == "rhoai-3.5"'
        ' && ( files.all.exists(p, !p.matches(\'^\\.tekton/\'))'
        ' || ".tekton/odh-dashboard-v3-5-push.yaml".pathChanged() )'
    )
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    cel = str(result["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"])
    assert '".tekton/odh-dashboard-pull-request.yaml".pathChanged()' in cel
    assert '".tekton/odh-dashboard-v3-5-push.yaml"' not in cel


def test_render_pr_strips_name_version():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    name = str(result["metadata"]["name"])
    assert name.endswith("-on-pull-request")
    assert "-v3-5" not in name


def test_render_pr_validates_wrong_namespace():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    pr["metadata"]["namespace"] = "wrong-tenant"
    errors = []
    render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="validate", errors=errors)
    assert any("rhoai-private-tenant" in e for e in errors)


def test_render_pr_validates_wrong_output_image():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    for p in pr["spec"]["params"]:
        if p["name"] == "output-image":
            p["value"] = "quay.io/rhoai/wrong:tag"
    errors = []
    render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="validate", errors=errors)
    assert any("rhoai-private" in e for e in errors)


def test_render_pr_validates_missing_disable_slack():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    pr["spec"]["params"] = [p for p in pr["spec"]["params"] if p["name"] != "disable-slack-notifications"]
    errors = []
    render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="validate", errors=errors)
    assert any("disable-slack-notifications" in e and "missing" in e for e in errors)


def test_render_pr_absent_removes_param():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    pr["spec"]["params"].append({"name": "enable-slack-failure-notification", "value": "false"})
    result = render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="fix")
    assert get_param(result, "enable-slack-failure-notification") is None


def test_render_pr_absent_reports_error():
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    pr["spec"]["params"].append({"name": "enable-slack-failure-notification", "value": "false"})
    errors = []
    render_and_merge(str(TEMPLATES), "pull-request.yaml.j2", pr, "odh-dashboard-pull-request.yaml", "rhoai-3.5", parent_data=push, mode="validate", errors=errors)
    assert any("enable-slack-failure-notification" in e and "should not exist" in e for e in errors)


def test_render_pr_without_push_data_does_not_crash():
    """PR template must not crash when no matching push pipeline exists."""
    pr = _ruamel_load("valid-pull-request.yaml")
    errors = []
    result = render_and_merge(
        str(TEMPLATES), "pull-request.yaml.j2", pr,
        "odh-dashboard-pull-request.yaml", "rhoai-3.5",
        parent_data=None, mode="validate", errors=errors,
    )
    assert result is not None


def test_render_pr_parent_ref_without_parent_data_resolves_empty():
    """_parent references must resolve to empty when parent_data is None, not fall back to the PR's own data."""
    pr = _ruamel_load("valid-pull-request.yaml")
    result = render_and_merge(
        str(TEMPLATES), "pull-request.yaml.j2", pr,
        "odh-dashboard-pull-request.yaml", "rhoai-3.5",
        parent_data=None, mode="fix",
    )
    app_label = result["metadata"]["labels"]["appstudio.openshift.io/application"]
    assert str(app_label) != str(pr["metadata"]["labels"]["appstudio.openshift.io/application"]), \
        "_parent should not fall back to the PR pipeline's own label value"


def test_validate_pr_without_push_pipeline_reports_error():
    """validate_tekton_dir should report an error when a PR pipeline has no matching push pipeline."""
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as tmpdir:
        tekton_dir = Path(tmpdir)
        shutil.copy(FIXTURES / "valid-pull-request.yaml", tekton_dir / "odh-dashboard-pull-request.yaml")
        results = _vt.validate_tekton_dir(tekton_dir, "rhoai-3.5", "rhoai-private-tenant")
        pr_result = results.get("odh-dashboard-pull-request.yaml", {})
        assert any("push pipeline" in e.lower() for e in pr_result.get("errors", [])), \
            f"expected 'push pipeline' error, got: {pr_result.get('errors', [])}"


def test_render_pr_reports_absent_annotations():
    """Validate mode should report on-label, on-target-branch, on-event as 'should not exist'."""
    push = _ruamel_load("valid-push.yaml")
    pr = _ruamel_load("valid-pull-request.yaml")
    errors = []
    render_and_merge(
        str(TEMPLATES), "pull-request.yaml.j2", pr,
        "odh-dashboard-pull-request.yaml", "rhoai-3.5",
        parent_data=push, mode="validate", errors=errors,
    )
    absent_errors = [e for e in errors if "should not exist" in e]
    assert any("on-label" in e for e in absent_errors)
    assert any("on-target-branch" in e for e in absent_errors)
    assert any("on-event" in e for e in absent_errors)


def test_render_push_missing_filtered_param_no_spurious_filter_error():
    """When a param with filters is missing entirely, only report 'missing' — not a filter error on empty string."""
    data = _ruamel_load("valid-push.yaml")
    data["spec"]["params"] = [p for p in data["spec"]["params"] if p["name"] != "output-image"]
    errors = []
    render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5", mode="validate", errors=errors)
    output_image_errors = [e for e in errors if "output-image" in e or "rhoai-private" in e]
    assert len(output_image_errors) == 1
    assert "missing" in output_image_errors[0]


def test_render_push_validates_cel_wrong_branch():
    data = _ruamel_load("valid-push.yaml")
    errors = []
    render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.4", mode="validate", errors=errors)
    assert any("rhoai-3.4" in e for e in errors)


_SKIP_FOR_TEMPLATE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  annotations:
    pipelinesascode.tekton.dev/on-cel-expression: "{{ _parent | skip_for(['odh-operator']) | validate_cel(branch, filename) | to_pr_cel }}"
  labels:
    appstudio.openshift.io/application: "{{ _parent }}"
    appstudio.openshift.io/component: "{{ _parent }}"
  name: "{{ _parent | strip_version }}-on-pull-request"
spec:
  params:
  - name: git-url
    value: "{{ '{{source_url}}' }}"
"""


def test_skip_for_suppresses_deep_merge_errors():
    """skip_for should prevent _deep_merge from recording errors for skipped values.

    Uses a multi-line CEL in the push pipeline because _parent resolves to the push
    pipeline's value, and _to_block_scalar converts multi-line strings to a different
    type which can lose the SkipValidation sentinel.
    """
    import tempfile

    parent_push_data = _ruamel_load("valid-push.yaml")
    parent_push_data["metadata"]["labels"]["appstudio.openshift.io/component"] = "odh-operator-v3-5"
    parent_push_data["metadata"]["name"] = "odh-operator-v3-5-on-push"
    parent_push_data["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"] = (
        'event == "push"\n&& target_branch == "rhoai-3.5"\n'
        '&& ( files.all.exists(p, !p.matches(\'^\\\\.tekton/\'))\n'
        '|| ".tekton/odh-operator-v3-5-push.yaml".pathChanged() )\n'
    )

    pr_data = _ruamel_load("valid-pull-request.yaml")
    pr_data["metadata"]["labels"]["appstudio.openshift.io/component"] = "odh-operator-v3-5"
    pr_data["metadata"]["name"] = "odh-operator-on-pull-request"
    pr_data["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"] = (
        'event == "push"\n&& target_branch == "rhoai-3.5"\n&& "some/custom/trigger.txt".pathChanged()\n'
    )

    errors = []
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "pull-request.yaml.j2").write_text(_SKIP_FOR_TEMPLATE)
        render_and_merge(
            tmpdir, "pull-request.yaml.j2", pr_data,
            "odh-operator-pull-request.yaml", "rhoai-3.5",
            parent_data=parent_push_data, mode="validate", errors=errors,
        )
    cel_errors = [e for e in errors if "on-cel-expression" in e]
    assert not cel_errors, f"skip_for should suppress CEL errors but got: {cel_errors}"


def test_validate_delete_if_reports_delete():
    """validate_tekton_dir should report delete signal for files matching delete_if."""
    import tempfile, shutil
    from ruamel.yaml import YAML
    ruamel = YAML()
    ruamel.preserve_quotes = True
    with tempfile.TemporaryDirectory() as tmpdir:
        tekton_dir = Path(tmpdir)
        push = _ruamel_load("valid-push.yaml")
        push["metadata"]["namespace"] = "open-data-hub-tenant"
        with open(tekton_dir / "odh-dashboard-v3-5-push.yaml", "w") as f:
            ruamel.dump(push, f)
        results = _vt.validate_tekton_dir(tekton_dir, "rhoai-3.5", None)
        push_result = results.get("odh-dashboard-v3-5-push.yaml", {})
        assert push_result.get("delete") is True
        assert any("should be deleted" in e for e in push_result.get("errors", []))


def test_validate_no_delete_for_correct_namespace():
    """validate_tekton_dir should not report delete for correct namespace."""
    import tempfile, shutil
    from ruamel.yaml import YAML
    ruamel = YAML()
    ruamel.preserve_quotes = True
    with tempfile.TemporaryDirectory() as tmpdir:
        tekton_dir = Path(tmpdir)
        push = _ruamel_load("valid-push.yaml")
        with open(tekton_dir / "odh-dashboard-v3-5-push.yaml", "w") as f:
            ruamel.dump(push, f)
        results = _vt.validate_tekton_dir(tekton_dir, "rhoai-3.5", None)
        push_result = results.get("odh-dashboard-v3-5-push.yaml", {})
        assert push_result.get("delete") is not True


def test_fix_delete_if_removes_file():
    """fix_tekton_dir should delete files matching delete_if."""
    import tempfile
    from ruamel.yaml import YAML
    ruamel = YAML()
    ruamel.preserve_quotes = True
    with tempfile.TemporaryDirectory() as tmpdir:
        tekton_dir = Path(tmpdir)
        push = _ruamel_load("valid-push.yaml")
        push["metadata"]["namespace"] = "open-data-hub-tenant"
        filepath = tekton_dir / "odh-dashboard-v3-5-push.yaml"
        with open(filepath, "w") as f:
            ruamel.dump(push, f)
        assert filepath.exists()
        fixed, _ = _vt.fix_tekton_dir(tekton_dir, "rhoai-3.5", None, dry_run=False)
        assert "odh-dashboard-v3-5-push.yaml" in fixed
        assert not filepath.exists()


# --- prefix mismatch: PR without odh- prefix, push with odh- prefix ---


def test_validate_pr_prefix_mismatch_finds_push():
    """validate_tekton_dir should match a PR file named 'caikit-nlp-pull-request.yaml'
    to a push pipeline named 'odh-caikit-nlp-v3-5-push.yaml' via odh- prefix fallback."""
    import tempfile, shutil
    from ruamel.yaml import YAML
    ruamel = YAML()
    ruamel.preserve_quotes = True
    with tempfile.TemporaryDirectory() as tmpdir:
        tekton_dir = Path(tmpdir)
        # Push pipeline with odh- prefix
        push = _ruamel_load("valid-push.yaml")
        with open(tekton_dir / "odh-caikit-nlp-v3-5-push.yaml", "w") as f:
            ruamel.dump(push, f)
        # PR pipeline WITHOUT odh- prefix
        pr = _ruamel_load("valid-pull-request.yaml")
        with open(tekton_dir / "caikit-nlp-pull-request.yaml", "w") as f:
            ruamel.dump(pr, f)
        results = _vt.validate_tekton_dir(tekton_dir, "rhoai-3.5", "rhoai-private-tenant")
        pr_result = results.get("caikit-nlp-pull-request.yaml", {})
        # Should NOT report "no matching push pipeline" -- the fallback should find it
        assert not any("no matching push pipeline" in e for e in pr_result.get("errors", [])), \
            f"expected odh- fallback to find push pipeline, got errors: {pr_result.get('errors', [])}"


def test_fix_pr_prefix_mismatch_finds_push():
    """fix_tekton_dir should match a PR file named 'caikit-nlp-pull-request.yaml'
    to a push pipeline named 'odh-caikit-nlp-v3-5-push.yaml' via odh- prefix fallback."""
    import tempfile, shutil
    from ruamel.yaml import YAML
    ruamel = YAML()
    ruamel.preserve_quotes = True
    with tempfile.TemporaryDirectory() as tmpdir:
        tekton_dir = Path(tmpdir)
        # Push pipeline with odh- prefix
        push = _ruamel_load("valid-push.yaml")
        with open(tekton_dir / "odh-caikit-nlp-v3-5-push.yaml", "w") as f:
            ruamel.dump(push, f)
        # PR pipeline WITHOUT odh- prefix
        pr = _ruamel_load("valid-pull-request.yaml")
        with open(tekton_dir / "caikit-nlp-pull-request.yaml", "w") as f:
            ruamel.dump(pr, f)
        fixed, _ = _vt.fix_tekton_dir(tekton_dir, "rhoai-3.5", "rhoai-private-tenant", dry_run=True)
        # fix_tekton_dir should NOT skip the PR file with a warning -- it should process it
        # A more direct check: run without dry_run and see if the file was modified
        pr2 = _ruamel_load("valid-pull-request.yaml")
        with open(tekton_dir / "caikit-nlp-pull-request.yaml", "w") as f:
            ruamel.dump(pr2, f)
        fixed, _ = _vt.fix_tekton_dir(tekton_dir, "rhoai-3.5", "rhoai-private-tenant", dry_run=False)
        # The PR file should have been processed (not skipped due to missing push pipeline)
        assert "caikit-nlp-pull-request.yaml" in fixed, \
            f"expected PR file to be fixed via odh- fallback, but fixed files were: {fixed}"


def test_multi_document_yaml_skipped():
    """Multi-document YAML files should be silently skipped, not reported as errors."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tekton_dir = Path(tmpdir)
        # Write a multi-document YAML file (two documents separated by ---)
        (tekton_dir / "task.yaml").write_text(
            "apiVersion: tekton.dev/v1\n"
            "kind: Task\n"
            "metadata:\n"
            "  name: task-one\n"
            "---\n"
            "apiVersion: tekton.dev/v1\n"
            "kind: Task\n"
            "metadata:\n"
            "  name: task-two\n"
        )
        results = _vt.validate_tekton_dir(tekton_dir, "main", None)
        # The multi-document file should be skipped entirely, not appear as an error
        assert "task.yaml" not in results, \
            f"multi-document YAML should be skipped, but got: {results.get('task.yaml')}"


# --- optional references (_parent? / _?) ---


_OPTIONAL_REF_TEMPLATE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  labels:
    appstudio.openshift.io/application: "{{ _parent }}"
    appstudio.openshift.io/component: "{{ _parent }}"
  name: "{{ _parent }}"
spec:
  params:
  - name: git-url
    value: "{{ '{{source_url}}' }}"
  - name: optional-param
    value: "{{ _parent? }}"
"""


def _render_optional_template(existing_data, parent_data=None, mode="fix"):
    """Render the optional-ref test template and return (result, errors)."""
    import tempfile
    errors = []
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "optional.yaml.j2").write_text(_OPTIONAL_REF_TEMPLATE)
        result = render_and_merge(
            tmpdir, "optional.yaml.j2", existing_data,
            "test-on-pull-request.yaml", "main",
            parent_data=parent_data, mode=mode, errors=errors,
        )
    return result, errors


def test_optional_parent_present_passes_value():
    """_parent? passes the parent value through when present."""
    parent = {"spec": {"params": [{"name": "optional-param", "value": "hello"}]},
              "metadata": {"labels": {}, "name": "test-on-push", "annotations": {}}}
    existing = {"spec": {"params": [{"name": "git-url", "value": "x"}, {"name": "optional-param", "value": "hello"}]},
                "metadata": {"labels": {"appstudio.openshift.io/application": "a", "appstudio.openshift.io/component": "c"}, "name": "test-on-pull-request", "annotations": {}}}
    result, errors = _render_optional_template(existing, parent_data=parent)
    assert get_param(result, "optional-param") == "hello"


def test_optional_parent_missing_produces_absent():
    """_parent? removes the param when parent lacks the key."""
    parent = {"spec": {"params": []},
              "metadata": {"labels": {}, "name": "test-on-push", "annotations": {}}}
    existing = {"spec": {"params": [{"name": "git-url", "value": "x"}, {"name": "optional-param", "value": "stale"}]},
                "metadata": {"labels": {"appstudio.openshift.io/application": "a", "appstudio.openshift.io/component": "c"}, "name": "test-on-pull-request", "annotations": {}}}
    result, errors = _render_optional_template(existing, parent_data=parent)
    assert get_param(result, "optional-param") is None


def test_optional_parent_missing_reports_should_not_exist():
    """_parent? reports 'should not exist' in validate mode when parent lacks the key."""
    parent = {"spec": {"params": []},
              "metadata": {"labels": {}, "name": "test-on-push", "annotations": {}}}
    existing = {"spec": {"params": [{"name": "git-url", "value": "x"}, {"name": "optional-param", "value": "stale"}]},
                "metadata": {"labels": {"appstudio.openshift.io/application": "a", "appstudio.openshift.io/component": "c"}, "name": "test-on-pull-request", "annotations": {}}}
    _, errors = _render_optional_template(existing, parent_data=parent, mode="validate")
    assert any("optional-param" in e and "should not exist" in e for e in errors)


def test_optional_parent_no_parent_data_produces_absent():
    """_parent? removes the param when parent_data is None entirely."""
    existing = {"spec": {"params": [{"name": "git-url", "value": "x"}, {"name": "optional-param", "value": "stale"}]},
                "metadata": {"labels": {"appstudio.openshift.io/application": "a", "appstudio.openshift.io/component": "c"}, "name": "test-on-pull-request", "annotations": {}}}
    result, errors = _render_optional_template(existing, parent_data=None)
    assert get_param(result, "optional-param") is None


def test_optional_parent_missing_no_error_when_existing_also_missing():
    """No error when both parent and existing lack the optional param."""
    parent = {"spec": {"params": []},
              "metadata": {"labels": {}, "name": "test-on-push", "annotations": {}}}
    existing = {"spec": {"params": [{"name": "git-url", "value": "x"}]},
                "metadata": {"labels": {"appstudio.openshift.io/application": "a", "appstudio.openshift.io/component": "c"}, "name": "test-on-pull-request", "annotations": {}}}
    _, errors = _render_optional_template(existing, parent_data=parent, mode="validate")
    assert not any("optional-param" in e and "should not exist" in e for e in errors)


_OPTIONAL_SELF_TEMPLATE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  labels:
    appstudio.openshift.io/application: "{{ _ }}"
  name: "{{ _ }}"
spec:
  params:
  - name: git-url
    value: "{{ '{{source_url}}' }}"
  - name: maybe-present
    value: "{{ _? }}"
"""


def test_optional_self_present_passes_value():
    """_? passes the existing value through when present."""
    existing = {"spec": {"params": [{"name": "git-url", "value": "x"}, {"name": "maybe-present", "value": "kept"}]},
                "metadata": {"labels": {"appstudio.openshift.io/application": "a"}, "name": "test", "annotations": {}}}
    import tempfile
    errors = []
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "self-opt.yaml.j2").write_text(_OPTIONAL_SELF_TEMPLATE)
        result = render_and_merge(tmpdir, "self-opt.yaml.j2", existing, "test.yaml", "main", mode="fix", errors=errors)
    assert get_param(result, "maybe-present") == "kept"


def test_optional_self_missing_produces_absent():
    """_? removes the param when existing file lacks the key."""
    existing = {"spec": {"params": [{"name": "git-url", "value": "x"}]},
                "metadata": {"labels": {"appstudio.openshift.io/application": "a"}, "name": "test", "annotations": {}}}
    import tempfile
    errors = []
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "self-opt.yaml.j2").write_text(_OPTIONAL_SELF_TEMPLATE)
        result = render_and_merge(tmpdir, "self-opt.yaml.j2", existing, "test.yaml", "main", mode="fix", errors=errors)
    assert get_param(result, "maybe-present") is None


# --- inherit_parent_params ---


_INHERIT_TEMPLATE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  labels:
    appstudio.openshift.io/application: "{{ _parent }}"
    appstudio.openshift.io/component: "{{ _parent }}"
  name: "{{ _parent }}"
spec:
  params:
  - name: git-url
    value: "{{ '{{source_url}}' }}"
  - name: output-image
    value: "{{ _parent | split_tag | to_private_quay }}:on-pr-{{ '{{pull_request_number}}' }}"
"""


def _make_parent(**extra_params):
    params = [{"name": "git-url", "value": "{{source_url}}"}, {"name": "output-image", "value": "quay.io/rhoai-private/img:tag"}]
    for k, v in extra_params.items():
        params.append({"name": k, "value": v})
    return {"spec": {"params": params}, "metadata": {"labels": {}, "name": "test-on-push", "annotations": {}}}


def _make_child(**extra_params):
    params = [{"name": "git-url", "value": "{{source_url}}"}, {"name": "output-image", "value": "quay.io/rhoai-private/img:on-pr-{{pull_request_number}}"}]
    for k, v in extra_params.items():
        params.append({"name": k, "value": v})
    return {"spec": {"params": params}, "metadata": {"labels": {"appstudio.openshift.io/application": "a", "appstudio.openshift.io/component": "c"}, "name": "test-on-pull-request", "annotations": {}}}


def _render_inherit(existing, parent, mode="fix"):
    import tempfile
    errors = []
    warnings = []
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "inherit.yaml.j2").write_text(_INHERIT_TEMPLATE)
        result = render_and_merge(
            tmpdir, "inherit.yaml.j2", existing, "test-pr.yaml", "main",
            parent_data=parent, mode=mode, errors=errors, warnings=warnings,
            inherit_params=True,
        )
    return result, errors, warnings


def test_inherit_matching_param_no_error():
    """No error when child param matches parent."""
    parent = _make_parent(dockerfile="Dockerfile.rhel")
    child = _make_child(dockerfile="Dockerfile.rhel")
    _, errors, _ = _render_inherit(child, parent, mode="validate")
    assert not any("dockerfile" in e for e in errors)


def test_inherit_mismatched_param_reports_error():
    """Validate mode reports mismatch for inherited param."""
    parent = _make_parent(dockerfile="Dockerfile.rhel")
    child = _make_child(dockerfile="Dockerfile.centos")
    _, errors, _ = _render_inherit(child, parent, mode="validate")
    assert any("dockerfile" in e and "from parent" in e for e in errors)


def test_inherit_mismatched_param_fixed():
    """Fix mode overwrites child param with parent value."""
    parent = _make_parent(dockerfile="Dockerfile.rhel")
    child = _make_child(dockerfile="Dockerfile.centos")
    result, _, _ = _render_inherit(child, parent, mode="fix")
    assert get_param(result, "dockerfile") == "Dockerfile.rhel"


def test_inherit_missing_param_reports_error():
    """Validate mode reports missing inherited param."""
    parent = _make_parent(dockerfile="Dockerfile.rhel")
    child = _make_child()
    _, errors, _ = _render_inherit(child, parent, mode="validate")
    assert any("dockerfile" in e and "missing" in e for e in errors)


def test_inherit_missing_param_added_in_fix():
    """Fix mode adds missing inherited param."""
    parent = _make_parent(dockerfile="Dockerfile.rhel")
    child = _make_child()
    result, _, _ = _render_inherit(child, parent, mode="fix")
    assert get_param(result, "dockerfile") == "Dockerfile.rhel"


def test_inherit_skips_template_params():
    """Params explicitly in the template are not inherited (template handles them)."""
    parent = _make_parent(**{"output-image": "quay.io/wrong/img:tag"})
    child = _make_child(**{"output-image": "quay.io/rhoai-private/img:on-pr-{{pull_request_number}}"})
    _, errors, _ = _render_inherit(child, parent, mode="validate")
    assert not any("from parent" in e and "output-image" in e for e in errors)


def test_inherit_child_only_param_preserved():
    """Child-only params (not in parent or template) are left alone."""
    parent = _make_parent()
    child = _make_child(**{"my-custom-param": "keep-me"})
    result, errors, _ = _render_inherit(child, parent, mode="fix")
    assert get_param(result, "my-custom-param") == "keep-me"
    assert not any("my-custom-param" in e for e in errors)


def test_inherit_list_value_matching():
    """List-valued params are compared correctly."""
    parent = _make_parent(**{"build-platforms": ["linux/x86_64", "linux/arm64"]})
    child = _make_child(**{"build-platforms": ["linux/x86_64", "linux/arm64"]})
    _, errors, _ = _render_inherit(child, parent, mode="validate")
    assert not any("build-platforms" in e for e in errors)


def test_inherit_list_value_mismatch():
    """Mismatched list-valued params are reported and fixed."""
    parent = _make_parent(**{"build-platforms": ["linux/x86_64", "linux/arm64"]})
    child = _make_child(**{"build-platforms": ["linux/x86_64"]})
    _, errors, _ = _render_inherit(child, parent, mode="validate")
    assert any("build-platforms" in e and "from parent" in e for e in errors)
    result, _, _ = _render_inherit(child, parent, mode="fix")
    assert get_param(result, "build-platforms") == ["linux/x86_64", "linux/arm64"]


def test_inherit_list_order_warns_not_errors():
    """Different list ordering is a warning, not an error."""
    parent = _make_parent(**{"build-platforms": ["linux/x86_64", "linux/arm64"]})
    child = _make_child(**{"build-platforms": ["linux/arm64", "linux/x86_64"]})
    _, errors, warnings = _render_inherit(child, parent, mode="validate")
    assert not any("build-platforms" in e for e in errors)
    assert any("build-platforms" in w and "semantically identical" in w for w in warnings)


def test_inherit_list_order_fixed():
    """Fix mode preserves child formatting when values are semantically equal."""
    parent = _make_parent(**{"build-platforms": ["linux/x86_64", "linux/arm64"]})
    child = _make_child(**{"build-platforms": ["linux/arm64", "linux/x86_64"]})
    result, _, _ = _render_inherit(child, parent, mode="fix")
    # Semantically equal — child's ordering is preserved (no cosmetic normalization)
    assert get_param(result, "build-platforms") == ["linux/arm64", "linux/x86_64"]


def test_inherit_json_formatting_warns_not_errors():
    """Different JSON formatting is a warning, not an error."""
    parent = _make_parent(**{"prefetch-input": '{\n  "type": "gomod",\n  "path": "."\n}\n'})
    child = _make_child(**{"prefetch-input": '{"type": "gomod", "path": "."}\n'})
    _, errors, warnings = _render_inherit(child, parent, mode="validate")
    assert not any("prefetch-input" in e for e in errors)
    assert any("prefetch-input" in w and "semantically identical" in w for w in warnings)


def test_inherit_json_formatting_fixed():
    """Fix mode preserves child formatting when JSON content is semantically equal."""
    parent = _make_parent(**{"prefetch-input": '{\n  "type": "gomod",\n  "path": "."\n}\n'})
    child = _make_child(**{"prefetch-input": '{"type": "gomod", "path": "."}\n'})
    result, _, _ = _render_inherit(child, parent, mode="fix")
    # Semantically equal — child's compact formatting is preserved
    assert get_param(result, "prefetch-input") == '{"type": "gomod", "path": "."}\n'


def test_inherit_disabled_by_default():
    """Without inherit_params=True, non-template params are not checked."""
    import tempfile
    parent = _make_parent(dockerfile="Dockerfile.rhel")
    child = _make_child(dockerfile="Dockerfile.centos")
    errors = []
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "inherit.yaml.j2").write_text(_INHERIT_TEMPLATE)
        render_and_merge(
            tmpdir, "inherit.yaml.j2", child, "test-pr.yaml", "main",
            parent_data=parent, mode="validate", errors=errors, inherit_params=False,
        )
    assert not any("dockerfile" in e and "from parent" in e for e in errors)


def _load_validate_tekton():
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader("validate_tekton", str(REPO_ROOT / "scripts" / "validate-tekton"))
    spec = importlib.util.spec_from_loader("validate_tekton", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_vt = _load_validate_tekton()


# --- resolve_overlay ---


def test_resolve_overlay_by_prefix(tmp_path):
    overlay_dir = tmp_path /"odh-dashboard"
    overlay_dir.mkdir(parents=True)
    overlay_file = overlay_dir / "push.yaml.j2"
    overlay_file.write_text("dummy")

    result = resolve_overlay(str(tmp_path), "push.yaml.j2", "odh-dashboard", "odh-dashboard-v3-5")
    assert result == str(overlay_file)


def test_resolve_overlay_by_label_fallback(tmp_path):
    overlay_dir = tmp_path /"odh-dashboard-v3-5"
    overlay_dir.mkdir(parents=True)
    overlay_file = overlay_dir / "push.yaml.j2"
    overlay_file.write_text("dummy")

    result = resolve_overlay(str(tmp_path), "push.yaml.j2", "no-match-prefix", "odh-dashboard-v3-5")
    assert result == str(overlay_file)


def test_resolve_overlay_no_match(tmp_path):
    result = resolve_overlay(str(tmp_path), "push.yaml.j2", "odh-dashboard", "odh-dashboard-v3-5")
    assert result is None


def test_resolve_overlay_label_preferred_over_prefix(tmp_path):
    prefix_dir = tmp_path / "odh-dashboard"
    prefix_dir.mkdir(parents=True)
    prefix_file = prefix_dir / "push.yaml.j2"
    prefix_file.write_text("prefix overlay")

    label_dir = tmp_path / "odh-dashboard-v3-5"
    label_dir.mkdir(parents=True)
    label_file = label_dir / "push.yaml.j2"
    label_file.write_text("label overlay")

    result = resolve_overlay(str(tmp_path), "push.yaml.j2", "odh-dashboard", "odh-dashboard-v3-5")
    assert result == str(label_file)


def test_resolve_overlay_skips_label_when_same_as_prefix(tmp_path):
    """When prefix and label are the same, don't check twice."""
    result = resolve_overlay(str(tmp_path), "push.yaml.j2", "odh-dashboard", "odh-dashboard")
    assert result is None


def test_resolve_overlay_none_prefix_and_label(tmp_path):
    result = resolve_overlay(str(tmp_path), "push.yaml.j2", None, None)
    assert result is None


# --- overlay merging in render_and_merge ---


def _make_overlay_setup(tmp_path, base_template, overlay_content, component="odh-dashboard-v3-5"):
    """Create a base template and overlay, return (template_dir, template_name, overlay_path)."""
    base_file = tmp_path / "push.yaml.j2"
    base_file.write_text(base_template)

    overlay_dir = tmp_path /"odh-dashboard"
    overlay_dir.mkdir(parents=True)
    overlay_file = overlay_dir / "push.yaml.j2"
    overlay_file.write_text(overlay_content)

    return str(tmp_path), "push.yaml.j2", str(overlay_file)


def test_overlay_merges_annotation():
    import textwrap
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        base = textwrap.dedent("""\
            metadata:
              annotations:
                pipelinesascode.tekton.dev/max-keep-runs: "3"
                pipelinesascode.tekton.dev/on-cel-expression: "{{ _ }}"
              name: "{{ _ }}"
        """)
        overlay = textwrap.dedent("""\
            metadata:
              annotations:
                pipelinesascode.tekton.dev/on-cel-expression: "custom-cel-value"
        """)
        template_dir, template_name, overlay_path = _make_overlay_setup(tmp_path, base, overlay)

        data = {
            "metadata": {
                "annotations": {
                    "pipelinesascode.tekton.dev/max-keep-runs": "3",
                    "pipelinesascode.tekton.dev/on-cel-expression": "custom-cel-value",
                },
                "name": "test-on-push",
            },
        }

        errors = []
        result = render_and_merge(template_dir, template_name, data, "test-on-push.yaml", "main",
                                  mode="validate", errors=errors, overlay_path=overlay_path)
        assert not errors
        assert result["metadata"]["annotations"]["pipelinesascode.tekton.dev/max-keep-runs"] == "3"


def test_overlay_merges_param_by_name():
    import textwrap
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        base = textwrap.dedent("""\
            spec:
              params:
              - name: git-url
                value: "{{ _ }}"
              - name: output-image
                value: "{{ _ }}"
              - name: rhoai-version
                value: "{{ _ }}"
        """)
        overlay = textwrap.dedent("""\
            spec:
              params:
              - name: output-image
                value: "custom-image:tag"
        """)
        template_dir, template_name, overlay_path = _make_overlay_setup(tmp_path, base, overlay)

        data = {
            "spec": {
                "params": [
                    {"name": "git-url", "value": "https://example.com"},
                    {"name": "output-image", "value": "custom-image:tag"},
                    {"name": "rhoai-version", "value": "3.5.0"},
                ],
            },
            "metadata": {"labels": {}},
        }

        errors = []
        result = render_and_merge(template_dir, template_name, data, "test-on-push.yaml", "main",
                                  mode="validate", errors=errors, overlay_path=overlay_path)
        assert not errors
        assert get_param(result, "git-url") == "https://example.com"
        assert get_param(result, "output-image") == "custom-image:tag"
        assert get_param(result, "rhoai-version") == "3.5.0"


def test_overlay_adds_new_param():
    import textwrap
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        base = textwrap.dedent("""\
            spec:
              params:
              - name: git-url
                value: "{{ _ }}"
        """)
        overlay = textwrap.dedent("""\
            spec:
              params:
              - name: build-type
                value: "ci"
        """)
        template_dir, template_name, overlay_path = _make_overlay_setup(tmp_path, base, overlay)

        data = {
            "spec": {
                "params": [
                    {"name": "git-url", "value": "https://example.com"},
                    {"name": "build-type", "value": "ci"},
                ],
            },
            "metadata": {"labels": {}},
        }

        errors = []
        result = render_and_merge(template_dir, template_name, data, "test-on-push.yaml", "main",
                                  mode="validate", errors=errors, overlay_path=overlay_path)
        assert not errors
        assert get_param(result, "git-url") == "https://example.com"
        assert get_param(result, "build-type") == "ci"


def test_overlay_absent_removes_param():
    import textwrap
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        base = textwrap.dedent("""\
            spec:
              params:
              - name: git-url
                value: "{{ _ }}"
              - name: disable-slack-notifications
                value: "true"
        """)
        overlay = textwrap.dedent("""\
            spec:
              params:
              - name: disable-slack-notifications
                value: "{{ ABSENT }}"
        """)
        template_dir, template_name, overlay_path = _make_overlay_setup(tmp_path, base, overlay)

        data = {
            "spec": {
                "params": [
                    {"name": "git-url", "value": "https://example.com"},
                    {"name": "disable-slack-notifications", "value": "true"},
                ],
            },
            "metadata": {"labels": {}},
        }

        errors = []
        result = render_and_merge(template_dir, template_name, data, "test-on-push.yaml", "main",
                                  mode="validate", errors=errors, overlay_path=overlay_path)
        assert any("disable-slack-notifications" in e and "should not exist" in e for e in errors)


def test_overlay_preserves_jinja_expressions():
    import textwrap
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        base = textwrap.dedent("""\
            metadata:
              annotations:
                pipelinesascode.tekton.dev/on-cel-expression: "{{ _ }}"
              name: "{{ _ }}"
              namespace: "{{ _ }}"
        """)
        overlay = textwrap.dedent("""\
            metadata:
              namespace: "custom-namespace"
        """)
        template_dir, template_name, overlay_path = _make_overlay_setup(tmp_path, base, overlay)

        data = {
            "metadata": {
                "annotations": {
                    "pipelinesascode.tekton.dev/on-cel-expression": "some-cel",
                },
                "name": "test-on-push",
                "namespace": "custom-namespace",
            },
        }

        errors = []
        result = render_and_merge(template_dir, template_name, data, "test-on-push.yaml", "main",
                                  mode="validate", errors=errors, overlay_path=overlay_path)
        assert not errors
        assert result["metadata"]["namespace"] == "custom-namespace"
        assert result["metadata"]["name"] == "test-on-push"
        assert result["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"] == "some-cel"


def test_validate_with_overlay():
    """End-to-end: overlay changes expected CEL, pipeline with matching value passes."""
    data = _ruamel_load("valid-push.yaml")
    custom_cel = 'event == "push" && target_branch == "rhoai-3.5" && "bundle/**".pathChanged()'
    data["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"] = custom_cel

    import textwrap
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        overlay_dir = tmp_path /"odh-dashboard"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / "push.yaml.j2").write_text(textwrap.dedent("""\
            metadata:
              annotations:
                pipelinesascode.tekton.dev/on-cel-expression: "{{ _ }}"
        """))
        overlay_path = str(overlay_dir / "push.yaml.j2")

        errors = []
        render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5",
                         mode="validate", errors=errors, overlay_path=overlay_path)
        cel_errors = [e for e in errors if "on-cel-expression" in e]
        assert not cel_errors


def test_fix_with_overlay():
    """End-to-end: overlay enforces a literal value, fix mode applies it."""
    data = _ruamel_load("valid-push.yaml")
    data["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"] = "wrong-value"

    import textwrap
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        overlay_dir = tmp_path /"odh-dashboard"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / "push.yaml.j2").write_text(textwrap.dedent("""\
            metadata:
              annotations:
                pipelinesascode.tekton.dev/on-cel-expression: "custom-fixed-cel"
        """))
        overlay_path = str(overlay_dir / "push.yaml.j2")

        errors = []
        result = render_and_merge(str(TEMPLATES), "push.yaml.j2", data, "odh-dashboard-v3-5-push.yaml", "rhoai-3.5",
                                  mode="fix", errors=errors, overlay_path=overlay_path)
        assert result["metadata"]["annotations"]["pipelinesascode.tekton.dev/on-cel-expression"] == "custom-fixed-cel"
