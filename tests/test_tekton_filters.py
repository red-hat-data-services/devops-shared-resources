"""Tests for Tekton template filters.

Each filter should have tests for:
- Pass case: valid input returns unchanged (or correctly transformed)
- Fail case: invalid input raises ValidationError with correct message
- Fix case: ValidationError.fixed_value contains the corrected value
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from lib.tekton_filters import (
    DELETE_FILE_PREFIX,
    DeleteFileError,
    ValidationError,
    default_comment,
    default_label,
    delete_if,
    delete_if_contains,
    make_filters,
    must_be_approved_pipeline,
    must_be_one_of,
    must_be_version,
    must_equal,
    must_match,
    repo_name,
    split_tag,
    strip_prefix,
    strip_version,
    to_private_gitlab,
    to_private_quay,
    to_pr_cel,
    release_push_to_main_pr_cel,
    to_stable_pr_cel,
    validate_cel,
    validate_cel_tekton_ignore,
)


# --- repo_name ---


def test_repo_name_https():
    assert repo_name("https://github.com/org/my-repo.git") == "my-repo"


def test_repo_name_with_query():
    assert repo_name("https://gitlab.com/org/repo?rev=abc") == "repo"


def test_repo_name_trailing_slash():
    assert repo_name("https://github.com/org/repo/") == "repo"


# --- strip_prefix ---


def test_strip_prefix_default():
    assert strip_prefix("odh-dashboard") == "dashboard"


def test_strip_prefix_custom():
    assert strip_prefix("rhoai-foo", "rhoai-") == "foo"


def test_strip_prefix_no_match():
    assert strip_prefix("dashboard") == "dashboard"


# --- strip_version ---


def test_strip_version_basic():
    assert strip_version("odh-dashboard-v3-5") == "odh-dashboard"


def test_strip_version_ea():
    assert strip_version("odh-component-v3-5-ea-2") == "odh-component"


def test_strip_version_no_version():
    assert strip_version("odh-dashboard") == "odh-dashboard"


# --- split_tag ---


def test_split_tag_basic():
    assert split_tag("quay.io/org/repo:v1.0") == "quay.io/org/repo"


def test_split_tag_no_tag():
    assert split_tag("quay.io/org/repo") == "quay.io/org/repo"


# --- default_comment ---


def test_default_comment_empty():
    assert default_comment("") == "^/build-konflux"


def test_default_comment_existing():
    assert default_comment("^/custom") == "^/custom"


# --- default_label ---


def test_default_label_empty():
    assert default_label("", "odh-dashboard-v3-5") == "[kfbuild-all, kfbuild-dashboard]"


def test_default_label_existing():
    assert default_label("[existing]", "odh-dashboard-v3-5") == "[existing]"


def test_default_label_no_component():
    assert default_label("", "") == "[kfbuild-all, kfbuild-]"


# --- to_private_quay ---


def test_to_private_quay_already_correct():
    assert to_private_quay("quay.io/rhoai-private/foo:bar") == "quay.io/rhoai-private/foo:bar"


def test_to_private_quay_needs_fix():
    with pytest.raises(ValidationError) as exc:
        to_private_quay("quay.io/rhoai/foo:bar")
    assert exc.value.fixed_value == "quay.io/rhoai-private/foo:bar"
    assert "rhoai-private" in str(exc.value)


def test_to_private_quay_repository_prefix():
    with pytest.raises(ValidationError) as exc:
        to_private_quay("quay.io/repository/rhoai/foo:bar")
    assert exc.value.fixed_value == "quay.io/rhoai-private/foo:bar"


def test_to_private_quay_unknown_org():
    with pytest.raises(ValidationError) as exc:
        to_private_quay("quay.io/evil/foo:bar")
    assert exc.value.fixed_value is None  # can't fix unknown org


# --- to_private_gitlab ---


def test_to_private_gitlab_already_correct():
    val = "https://gitlab.cee.redhat.com/rhoai/private/repo?rev={{revision}}"
    assert to_private_gitlab(val) == val


def test_to_private_gitlab_needs_fix():
    with pytest.raises(ValidationError) as exc:
        to_private_gitlab("https://github.com/red-hat-data-services/repo?rev={{revision}}")
    assert "gitlab.cee.redhat.com/rhoai/private/repo" in exc.value.fixed_value


# --- must_equal ---


def test_must_equal_pass():
    assert must_equal("foo", "foo") == "foo"


def test_must_equal_fail():
    with pytest.raises(ValidationError) as exc:
        must_equal("bar", "foo")
    assert exc.value.fixed_value == "foo"
    assert "expected 'foo'" in str(exc.value)


# --- must_match ---


def test_must_match_pass():
    assert must_match("quay.io/rhoai-private/foo", "rhoai-private") == "quay.io/rhoai-private/foo"


def test_must_match_fail():
    with pytest.raises(ValidationError) as exc:
        must_match("quay.io/rhoai/foo", "rhoai-private")
    assert exc.value.fixed_value is None  # no auto-fix for pattern match


# --- must_be_one_of ---


def test_must_be_one_of_pass():
    assert must_be_one_of("a", ["a", "b", "c"]) == "a"


def test_must_be_one_of_fail():
    with pytest.raises(ValidationError):
        must_be_one_of("d", ["a", "b", "c"])


def test_must_be_one_of_string_arg():
    assert must_be_one_of("a", "a") == "a"


# --- must_be_version ---


def test_must_be_version_basic():
    assert must_be_version("3.5.0") == "3.5.0"


def test_must_be_version_ea():
    assert must_be_version("3.5.0-ea.1") == "3.5.0-ea.1"


def test_must_be_version_fail():
    with pytest.raises(ValidationError) as exc:
        must_be_version("not-a-version")
    assert "not a valid version" in str(exc.value)


def test_must_be_version_partial():
    with pytest.raises(ValidationError):
        must_be_version("3.5")


def test_must_be_version_leading_dash_fixable():
    with pytest.raises(ValidationError) as exc:
        must_be_version("-2.25.10")
    assert exc.value.fixed_value == "2.25.10"


def test_must_be_version_prefix_junk_fixable():
    with pytest.raises(ValidationError) as exc:
        must_be_version("v2.25.10")
    assert exc.value.fixed_value == "2.25.10"


def test_must_be_version_leading_dash_with_expected():
    with pytest.raises(ValidationError) as exc:
        must_be_version("-2.25.10", "2.25.10")
    assert exc.value.fixed_value == "2.25.10"


def test_must_be_version_unfixable_has_no_fixed_value():
    with pytest.raises(ValidationError) as exc:
        must_be_version("not-a-version")
    assert exc.value.fixed_value is None


# --- validate_cel ---


def test_validate_cel_valid():
    cel = 'event == "push" && target_branch == "rhoai-3.5" && ".tekton/x.yaml".pathChanged()'
    assert validate_cel(cel, "rhoai-3.5", "x.yaml") == cel


def test_validate_cel_wrong_branch():
    cel = 'event == "push" && target_branch == "main"'
    with pytest.raises(ValidationError) as exc:
        validate_cel(cel, "rhoai-3.5", "")
    assert "rhoai-3.5" in str(exc.value)
    assert exc.value.fixed_value == 'event == "push" && target_branch == "rhoai-3.5"'


def test_validate_cel_no_target_branch_no_fix():
    cel = 'event == "push"'
    with pytest.raises(ValidationError) as exc:
        validate_cel(cel, "rhoai-3.5", "")
    assert exc.value.fixed_value is None


def test_validate_cel_missing_self_ref():
    cel = 'event == "push" && target_branch == "rhoai-3.5"'
    with pytest.raises(ValidationError) as exc:
        validate_cel(cel, "", "my-push.yaml")
    assert "my-push.yaml" in str(exc.value)


def test_validate_cel_multiline_branch_valid():
    cel = 'event == "push" && target_branch\n== "rhoai-2.25" && ( "catalog/**".pathChanged() )'
    assert validate_cel(cel, "rhoai-2.25", "") == cel


def test_validate_cel_multiline_branch_wrong():
    cel = 'event == "push" && target_branch\n== "rhoai-2.25" && ( "catalog/**".pathChanged() )'
    with pytest.raises(ValidationError) as exc:
        validate_cel(cel, "rhoai-3.5", "")
    assert "rhoai-3.5" in str(exc.value)
    assert '"rhoai-3.5"' in exc.value.fixed_value
    assert "\n" in exc.value.fixed_value


def test_validate_cel_multiline_newline_after_equals():
    cel = 'event == "push" && target_branch ==\n"rhoai-2.25" && ( "catalog/**".pathChanged() )'
    assert validate_cel(cel, "rhoai-2.25", "") == cel


def test_validate_cel_skipped_when_empty_args():
    assert validate_cel("anything", "", "") == "anything"


def test_validate_cel_wrong_filename_fix():
    """When .tekton/ pathChanged has wrong filename, validate_cel fixes it directly (transform, not validation)."""
    cel = 'event == "push" && target_branch == "rhoai-3.5" && ".tekton/old-push.yaml".pathChanged()'
    result = validate_cel(cel, "rhoai-3.5", "new-pr.yaml")
    assert result == 'event == "push" && target_branch == "rhoai-3.5" && ".tekton/new-pr.yaml".pathChanged()'


def test_validate_cel_wrong_filename_no_pathchanged():
    """No .tekton/ pathChanged in CEL — can't fix, raises error."""
    cel = 'event == "push" && target_branch == "rhoai-3.5"'
    with pytest.raises(ValidationError) as exc:
        validate_cel(cel, "rhoai-3.5", "my-pr.yaml")
    assert "my-pr.yaml" in str(exc.value)


def test_validate_cel_monorepo_only_replaces_tekton_path():
    """Monorepo CEL with package pathChanged — only the .tekton/ one is replaced."""
    cel = (
        'event == "push" && target_branch == "rhoai-3.5"'
        ' && ( "packages/automl/**".pathChanged()'
        ' || ".tekton/old-push.yaml".pathChanged() )'
    )
    result = validate_cel(cel, "rhoai-3.5", "new-pr.yaml")
    assert '".tekton/new-pr.yaml".pathChanged()' in result
    assert '"packages/automl/**".pathChanged()' in result

# --- validate_cel_tekton_ignore ---


def test_validate_cel_tekton_ignore_bad():
    cel = ( 'event == "push"'
            ' && target_branch == "rhoai-3.3"'
            ' && ( !".tekton/**".pathChanged() || ".tekton/odh-llama-stack-core-v3-3-push.yaml".pathChanged() )')
                
    with pytest.raises(ValidationError) as exc:
        validate_cel_tekton_ignore(cel)
    assert "files.all.exists" in exc.value.fixed_value

def test_validate_cel_tekton_ignore_good():
    cel = ( 'event == "push"'
            ' && target_branch == "rhoai-3.3"' )
    assert validate_cel_tekton_ignore(cel) == cel

# --- to_pr_cel ---


def test_to_pr_cel_basic():
    cel = 'event == "push" && target_branch == "rhoai-3.5"'
    assert to_pr_cel(cel) == 'event == "pull_request" && target_branch == "rhoai-3.5"'


def test_to_pr_cel_full():
    cel = (
        'event == "push" && target_branch == "rhoai-3.5"'
        ' && ( files.all.exists(p, !p.matches(\'^\\.tekton/\'))'
        ' || ".tekton/odh-dashboard-pull-request.yaml".pathChanged() )'
    )
    result = to_pr_cel(cel)
    assert result.startswith('event == "pull_request"')
    assert 'event == "push"' not in result


def test_to_pr_cel_no_push_event():
    with pytest.raises(ValidationError) as exc:
        to_pr_cel('event == "pull_request" && target_branch == "main"')
    assert 'event == "push"' in str(exc.value)


def test_to_pr_cel_empty():
    with pytest.raises(ValidationError):
        to_pr_cel("")


# --- skip_for ---


def test_skip_for_matches():
    filters = make_filters("validate", [], component="odh-operator-v3-4")
    result = filters["skip_for"]("some-cel-expression", ["odh-operator"])
    from lib.tekton_filters import SkipValidation
    assert isinstance(result, SkipValidation)
    assert str(result) == "some-cel-expression"


def test_skip_for_no_match():
    filters = make_filters("validate", [], component="odh-dashboard-v3-4")
    result = filters["skip_for"]("some-cel-expression", ["odh-operator"])
    from lib.tekton_filters import SkipValidation
    assert not isinstance(result, SkipValidation)


def test_skip_for_skips_downstream_validation():
    errors = []
    filters = make_filters("validate", errors, component="odh-operator-v3-4")
    val = filters["skip_for"]("wrong-value", ["odh-operator"])
    val = filters["must_equal"](val, "expected-value")
    assert val == "wrong-value"
    assert errors == []


def test_skip_for_no_component():
    filters = make_filters("validate", [], component=None)
    result = filters["skip_for"]("value", ["odh-operator"])
    from lib.tekton_filters import SkipValidation
    assert not isinstance(result, SkipValidation)


# --- delete_if ---


def test_delete_if_no_match():
    assert delete_if("rhoai-tenant", "open-data-hub-tenant") == "rhoai-tenant"


def test_delete_if_match():
    with pytest.raises(DeleteFileError) as exc:
        delete_if("open-data-hub-tenant", "open-data-hub-tenant")
    assert "matched 'open-data-hub-tenant'" in str(exc.value)


def test_wrapper_delete_if_records_prefixed_error():
    errors = []
    filters = make_filters("validate", errors)
    result = filters["delete_if"]("open-data-hub-tenant", "open-data-hub-tenant")
    assert result == "open-data-hub-tenant"
    assert len(errors) == 1
    assert errors[0].startswith(DELETE_FILE_PREFIX)


def test_wrapper_delete_if_records_in_fix_mode():
    errors = []
    filters = make_filters("fix", errors)
    result = filters["delete_if"]("open-data-hub-tenant", "open-data-hub-tenant")
    assert result == "open-data-hub-tenant"
    assert len(errors) == 1
    assert errors[0].startswith(DELETE_FILE_PREFIX)


# --- delete_if_contains ---


def test_delete_if_contains_no_match():
    assert delete_if_contains("https://github.com/red-hat-data-services/foo", "opendatahub-io") == "https://github.com/red-hat-data-services/foo"


def test_delete_if_contains_match():
    with pytest.raises(DeleteFileError) as exc:
        delete_if_contains("https://github.com/opendatahub-io/opendatahub-operator?rev={{revision}}", "opendatahub-io")
    assert "contains 'opendatahub-io'" in str(exc.value)


# --- make_filters / wrapper behavior ---


def test_wrapper_validate_mode_returns_original():
    errors = []
    filters = make_filters("validate", errors)
    result = filters["to_private_quay"]("quay.io/rhoai/foo")
    assert result == "quay.io/rhoai/foo"  # original, not fixed
    assert len(errors) == 1


def test_wrapper_fix_mode_returns_fixed():
    errors = []
    filters = make_filters("fix", errors)
    result = filters["to_private_quay"]("quay.io/rhoai/foo")
    assert result == "quay.io/rhoai-private/foo"  # fixed
    assert len(errors) == 1


def test_wrapper_no_error_on_pass():
    errors = []
    filters = make_filters("validate", errors)
    filters["to_private_quay"]("quay.io/rhoai-private/foo")
    assert errors == []


def test_wrapper_chains_continue_after_error():
    errors = []
    filters = make_filters("validate", errors)
    val = "quay.io/rhoai/foo:bar"
    val = filters["split_tag"](val)
    val = filters["to_private_quay"](val)
    assert val == "quay.io/rhoai/foo"  # split_tag worked, to_private_quay returned original
    assert len(errors) == 1


# --- must_be_approved_pipeline ---


def test_must_be_approved_pipeline_pass():
    assert must_be_approved_pipeline("https://github.com/red-hat-data-services/konflux-central.git") == \
        "https://github.com/red-hat-data-services/konflux-central.git"


def test_must_be_approved_pipeline_fail():
    with pytest.raises(ValidationError) as exc:
        must_be_approved_pipeline("https://github.com/evil/pipeline.git")
    assert "not in approved" in str(exc.value)


# --- release push to main PR CEL policy ---


def test_release_push_to_main_pr_cel_legacy_alias():
    assert to_stable_pr_cel is release_push_to_main_pr_cel


@pytest.mark.parametrize("name", ["release_push_to_main_pr_cel", "to_stable_pr_cel"])
@pytest.mark.parametrize("mode", ["validate", "fix"])
@pytest.mark.parametrize("labels", ["", "[kfbuild-all]"])
def test_release_push_to_main_pr_cel_jinja_names(name, mode, labels):
    from jinja2 import Environment

    errors = []
    env = Environment()
    env.filters.update(make_filters(mode, errors))
    template = env.from_string("{{ value | " + name + "(filename, labels) }}")
    value = 'event == "push" && ".tekton/push.yaml".pathChanged()'
    assert template.render(value=value, filename="pr.yaml", labels=labels) == (
        release_push_to_main_pr_cel(value, "pr.yaml", labels)
    )
    assert errors == []


@pytest.mark.parametrize("name", ["release_push_to_main_pr_cel", "to_stable_pr_cel"])
@pytest.mark.parametrize("mode", ["validate", "fix"])
def test_release_push_to_main_pr_cel_jinja_names_report_errors(name, mode):
    errors = []
    filters = make_filters(mode, errors)
    assert filters[name]("&&&") == "&&&"
    assert len(errors) == 1
    assert errors[0].startswith("cannot decompose push CEL: failed to parse")


@pytest.mark.parametrize("labels", ["", "[kfbuild-all]"])
def test_stable_pr_output_format(labels):
    result = release_push_to_main_pr_cel(
        'event == "push" && target_branch == "release"'
        ' && ".tekton/push.yaml".pathChanged()',
        "pull-request.yaml", labels,
    )
    path = '".tekton/pull-request.yaml".pathChanged()'
    if labels:
        expected = (
            'event == "pull_request"\n&& (\n  (\n    '
            'has(body.pull_request.labels)\n'
            '    && body.pull_request.labels.exists(l, l.name in ["kfbuild-all"])'
            '\n  )\n  ||\n  (\n    ' + path + '\n  )\n)'
        )
    else:
        expected = 'event == "pull_request"\n&& ' + path
    assert result == expected


@pytest.mark.parametrize("value,message", [
    ("   ", "push CEL expression is empty"),
    ("&&&", "cannot decompose push CEL: failed to parse"),
    ('target_branch == "release"', "cannot decompose push CEL: CEL expression has no event clause"),
    ('event == "push" || skip_build', "cannot decompose push CEL: top-level OR"),
])
def test_stable_pr_filter_reports_invalid_input(value, message):
    with pytest.raises(ValidationError, match=message) as exc:
        release_push_to_main_pr_cel(value)
    assert exc.value.fixed_value is None


# --- release_push_to_main_pr_cel filter (end-to-end) ---


class TestReleasePushToMainPrCelFilter:
    """End-to-end tests for the release_push_to_main_pr_cel Jinja filter."""

    @pytest.mark.parametrize("labels", ["", "[kfbuild-all]"])
    @pytest.mark.parametrize("branch", ["", '\n&& target_branch == "rhoai-3.6"\n'])
    def test_pathless_push_becomes_unrestricted_pr_trigger(self, labels, branch):
        import celpy

        result = release_push_to_main_pr_cel(
            f'event == "push"{branch}', "odh-must-gather-pull-request.yaml", labels,
        )
        assert result == 'event == "pull_request"'
        env = celpy.Environment()
        program = env.program(env.compile(result))
        assert program.evaluate({"event": celpy.celtypes.StringType("pull_request")})
        assert not program.evaluate({"event": celpy.celtypes.StringType("push")})

    def test_pathless_push_preserves_negative_guard(self):
        import celpy

        result = release_push_to_main_pr_cel(
            'event == "push" && target_branch == "rhoai-3.6"'
            ' && !("manifests/rhoai/base/params-latest.env".pathChanged())',
            "odh-must-gather-pull-request.yaml",
        )
        assert 'event == "pull_request"' in result
        assert 'event == "push"' not in result
        assert 'target_branch == "rhoai-3.6"' not in result
        assert "params-latest.env" in result
        celpy.Environment().compile(result)

    def test_broad_filter_no_labels(self):

        push_cel = (
            'event == "push" && target_branch == "rhoai-3.6"'
            " && ( files.all.exists(p, !p.matches('^\\\\.tekton/'))"
            ' || ".tekton/odh-dashboard-v3-6-push.yaml".pathChanged() )'
        )
        result = release_push_to_main_pr_cel(
            push_cel,
            "odh-dashboard-pull-request.yaml",
            "",
        )
        assert 'event == "pull_request"' in result
        assert 'event == "push"' not in result
        assert "target_branch" not in result
        assert "odh-dashboard-pull-request.yaml" in result
        assert "v3-6-push" not in result
        assert "files.all.exists" in result
        assert "has(body.pull_request.labels)" not in result

    def test_broad_filter_with_labels(self):

        push_cel = (
            'event == "push" && target_branch == "rhoai-3.6"'
            " && ( files.all.exists(p, !p.matches('^\\\\.tekton/'))"
            ' || ".tekton/odh-dashboard-v3-6-push.yaml".pathChanged() )'
        )
        result = release_push_to_main_pr_cel(
            push_cel,
            "odh-dashboard-pull-request.yaml",
            "[kfbuild-all, kfbuild-dashboard]",
        )
        assert 'event == "pull_request"' in result
        assert '"kfbuild-all"' in result
        assert '"kfbuild-dashboard"' in result
        assert "||" in result

    def test_fine_grained_with_negative_guard(self):

        push_cel = (
            'event == "push" && target_branch == "rhoai-3.6"'
            ' && !("manifests/rhoai/base/params-latest.env".pathChanged())'
            ' && ( ".tekton/odh-workbench-v3-6-push.yaml".pathChanged()'
            ' || "jupyter/utils/**".pathChanged() )'
        )
        result = release_push_to_main_pr_cel(
            push_cel,
            "odh-workbench-pull-request.yaml",
            "",
        )
        assert 'event == "pull_request"' in result
        assert "manifests/rhoai/base/params-latest.env" in result
        assert "odh-workbench-pull-request.yaml" in result
        assert '"jupyter/utils/**"' in result
        assert "has(body.pull_request.labels)" not in result

    def test_empty_push_cel_raises(self):

        with pytest.raises(ValidationError, match="empty"):
            release_push_to_main_pr_cel("", "foo.yaml", "")


@pytest.mark.parametrize("source,target,event,params,image,expected", [
    ("main", "stable", "pull_request", True, False, True),
    ("main", "stable", "pull_request", True, True, True),
    ("main", "stable", "push", True, True, False),
    ("feature", "stable", "pull_request", True, True, False),
    ("feature", "stable", "pull_request", False, True, True),
    ("feature", "stable", "pull_request", False, False, False),
    ("main", "other", "pull_request", True, False, False),
])
@pytest.mark.parametrize("labels", ["", "[kfbuild-all]"])
def test_notebooks_sync_trigger_semantics(source, target, event, params, image, expected, labels):
    import celpy

    expression = release_push_to_main_pr_cel(
        'event == "push" && target_branch == "rhoai-3.7-ea.1"'
        ' && !("manifests/rhoai/base/params-latest.env".pathChanged())'
        ' && ("jupyter/utils/**".pathChanged() || ".tekton/push.yaml".pathChanged())',
        "pull-request.yaml", labels,
    )
    env = celpy.Environment()
    program = env.program(env.compile(expression), functions={
        "pathChanged": lambda path: celpy.celtypes.BoolType(
            params if str(path) == "manifests/rhoai/base/params-latest.env" else image
        ),
    })
    assert bool(program.evaluate({
        "source_branch": celpy.celtypes.StringType(source),
        "target_branch": celpy.celtypes.StringType(target),
        "event": celpy.celtypes.StringType(event),
        "body": celpy.json_to_cel({"pull_request": {"labels": []}}),
    })) == expected


@pytest.mark.parametrize("guard", [
    '!skip_build',
    '!("other/params-latest.env".pathChanged())',
    '!("manifests/rhoai/base/params-latest.env".pathChanged() || skip_build)',
])
def test_unrelated_negative_guards_do_not_add_sync_escape(guard):

    result = release_push_to_main_pr_cel(
        f'event == "push" && target_branch == "release" && {guard}'
        ' && "src/**".pathChanged()', "pull-request.yaml",
    )
    assert "source_branch" not in result


def test_notebooks_label_arm_still_bypasses_path_guard():
    import celpy

    result = release_push_to_main_pr_cel(
        'event == "push" && target_branch == "release"'
        ' && !("manifests/rhoai/base/params-latest.env".pathChanged())'
        ' && "src/**".pathChanged()', "pull-request.yaml", "[kfbuild-all]",
    )
    env = celpy.Environment()
    program = env.program(env.compile(result), functions={
        "pathChanged": lambda path: celpy.celtypes.BoolType(True),
    })
    assert program.evaluate({
        "source_branch": celpy.celtypes.StringType("feature"),
        "target_branch": celpy.celtypes.StringType("stable"),
        "event": celpy.celtypes.StringType("pull_request"),
        "body": celpy.json_to_cel({"pull_request": {"labels": [{"name": "kfbuild-all"}]}}),
    })

@pytest.mark.parametrize("labels_str,labels", [
    ("", []),
    (None, []),
    ("[]", []),
    (" [ kfbuild-all, , kfbuild-image ] ", ["kfbuild-all", "kfbuild-image"]),
    ("[kfbuild-all, kfbuild-workbench, kfbuild-cpu, kfbuild-datascience]",
     ["kfbuild-all", "kfbuild-workbench", "kfbuild-cpu", "kfbuild-datascience"]),
])
def test_stable_pr_label_annotation(labels_str, labels):
    result = release_push_to_main_pr_cel(
        'event == "push" && "src/**".pathChanged()', labels_str=labels_str,
    )
    if labels:
        quoted = ", ".join(f'"{label}"' for label in labels)
        assert f"l.name in [{quoted}]" in result
    else:
        assert "body.pull_request.labels" not in result


def test_stable_pr_custom_path_glob():
    result = release_push_to_main_pr_cel(
        'event == "push" && target_branch == "rhoai-3.5-ea.2"'
        ' && ("helm/rhai-on-openshift-chart/**".pathChanged()'
        ' || ".tekton/chart-push.yaml".pathChanged())',
        "chart-pull-request.yaml",
    )
    assert '"helm/rhai-on-openshift-chart/**".pathChanged()' in result
    assert '".tekton/chart-pull-request.yaml".pathChanged()' in result
    assert "target_branch" not in result


def test_stable_pr_pathless_guard_is_preserved():
    assert release_push_to_main_pr_cel('event == "push" && !skip_build') == (
        'event == "pull_request"\n&& ! skip_build'
    )
