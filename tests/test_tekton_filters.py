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
